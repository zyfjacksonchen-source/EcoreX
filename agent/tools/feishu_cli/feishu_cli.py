"""Feishu/Lark CLI tool wrapper for EcoreX runtimes.

This tool keeps Feishu access out of ad-hoc shell commands. It resolves an
already available `lark-cli`, reports auth state, starts split-flow user auth,
and runs CLI commands with bounded timeouts. It does not install the CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


DEFAULT_LARK_CLI_PACKAGE = os.environ.get("ECOREX_LARK_CLI_PACKAGE", "@larksuite/cli@1.0.40")
FEISHU_LARK_SOURCE_URL = "https://github.com/larksuite/oapi-sdk-python"
FEISHU_LARK_MIRROR_URLS = ["https://gitcode.com/gh_mirrors/oa/oapi-sdk-python.git"]
FEISHU_LARK_PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_TIMEOUT_SECONDS = 45


_SECRET_PATTERNS = [
    re.compile(r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?secret|tenant[_-]?token|authorization)(\"?\s*[:=]\s*\")([^\"\s,]+)"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)(=|:)\s*[^\s,&}\"]+"),
]


class _ProcessCancelled(Exception):
    def __init__(self, stdout: str = "", stderr: str = ""):
        super().__init__("process cancelled by user")
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _sanitize(text: str) -> str:
    value = text or ""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
        else:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    value = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", value)
    value = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", value)
    return value


def _prepend_path(env: Dict[str, str], path: Optional[Path]) -> None:
    if not path or not path.exists():
        return
    current = env.get("PATH", "")
    raw = str(path)
    parts = current.split(os.pathsep) if current else []
    if raw not in parts:
        env["PATH"] = raw + (os.pathsep + current if current else "")


def _candidate_bin_dirs() -> List[Path]:
    home = Path.home()
    dirs: List[Path] = []
    if os.name == "nt":
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if base:
                dirs.append(Path(base) / "npm")
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                dirs.append(Path(base) / "nodejs")
        dirs.append(Path("C:/cli-main/bin"))
        dirs.append(Path("C:/EcoreX Artifact Desk/cli-main/bin"))
        dirs.append(Path("C:/EcoreX Artifact Desk/cli-main"))
    else:
        dirs.extend([
            home / ".npm-global" / "bin",
            home / ".npm" / "bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
        ])

    runtime_root = Path(__file__).resolve().parents[3]
    dirs.extend([
        runtime_root / "bin",
        runtime_root / "tools" / "bin",
        runtime_root / "node" / "bin",
        runtime_root / "tools" / "lark-cli" / "bin",
    ])
    return dirs


def _tool_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for candidate in _candidate_bin_dirs():
        _prepend_path(env, candidate)
    return env


def _which(name: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    search_path = (env or os.environ).get("PATH")
    found = shutil.which(name, path=search_path)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith(".cmd"):
        return shutil.which(f"{name}.cmd", path=search_path)
    return None


def _find_local_lark_runner(env: Dict[str, str]) -> Optional[List[str]]:
    node = _which("node", env)
    if not node:
        return None
    candidates = [
        Path("C:/EcoreX Artifact Desk/cli-main/scripts/run.js"),
        Path(__file__).resolve().parents[3] / "tools" / "lark-cli" / "scripts" / "run.js",
        Path(__file__).resolve().parents[3] / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
    ]
    for script in candidates:
        if script.exists():
            return [node, str(script)]
    return None


def _find_direct_lark_binary() -> Optional[str]:
    names = ["lark-cli.exe", "lark-cli.cmd", "lark-cli"] if os.name == "nt" else ["lark-cli"]
    for directory in _candidate_bin_dirs():
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _resolve_lark_command(env: Dict[str, str]) -> Optional[List[str]]:
    cli = _which("lark-cli", env)
    if cli:
        return [cli]
    direct = _find_direct_lark_binary()
    if direct:
        return [direct]
    return _find_local_lark_runner(env)


def _kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as exc:
            logger.debug(f"[FeishuCli] taskkill failed for pid {process.pid}: {exc}")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except Exception as exc:
            logger.debug(f"[FeishuCli] killpg failed for pid {process.pid}: {exc}")
    try:
        process.kill()
    except Exception:
        pass


def _run_process(
    command: List[str],
    timeout: int,
    env: Dict[str, str],
    cwd: Optional[str] = None,
    cancel_event=None,
) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "cwd": cwd or os.getcwd(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    deadline = time.time() + max(1, timeout)
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _kill_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    stdout, stderr = process.communicate()
                raise _ProcessCancelled(stdout, stderr)
            if time.time() >= deadline:
                _kill_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _parse_json_output(output: str) -> Any:
    text = (output or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _as_args(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _check_expected_paths(paths: Iterable[str]) -> Dict[str, Any]:
    checked = []
    missing = []
    empty_dirs = []
    for raw in paths or []:
        path = Path(str(raw))
        exists = path.exists()
        count = None
        if exists and path.is_dir():
            count = len([item for item in path.iterdir()])
            if count == 0:
                empty_dirs.append(str(path))
        if not exists:
            missing.append(str(path))
        checked.append({"path": str(path), "exists": exists, "entries": count})
    return {"checked": checked, "missing": missing, "emptyDirs": empty_dirs}


class FeishuCli(BaseTool):
    name: str = "feishu_cli"
    description: str = (
        "Use this instead of bash for Feishu/Lark operations when lark-cli is already "
        "available. It checks user auth, starts split-flow auth with --no-wait, and runs "
        "lark-cli commands with bounded timeouts. It never installs lark-cli; if missing, "
        "use the built-in find skill first (gated as find-skill), then the discovery-only connector guidance returned by this tool."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, ensure, auth_login, run",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments after lark-cli for action=run, e.g. ['base', '+record-list', '--as', 'user', ...].",
            },
            "scope": {
                "type": "string",
                "description": "Scope for auth_login, e.g. search:docs:read. Use domain when unsure.",
            },
            "domain": {
                "type": "string",
                "description": "Domain for auth_login, e.g. base, docs, drive. Defaults to base.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout seconds for install/run actions.",
            },
            "install_if_missing": {
                "type": "boolean",
                "description": "Deprecated. Ignored because Feishu/Lark connector installation is discovery-only.",
            },
            "expected_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional files/directories expected after a command. Empty directories are reported as errors.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: dict = None):
        self.apply_config(config or {})

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())
        self.package = str(self.config.get("package") or DEFAULT_LARK_CLI_PACKAGE)
        self.auto_install = bool(self.config.get("auto_install", False))

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        timeout = int(args.get("timeout") or self.config.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
        install_if_missing = False
        env = _tool_env()

        if action == "status":
            return ToolResult.success(self._status(env))
        if action == "ensure":
            return self._ensure(env, timeout, install_if_missing=install_if_missing)
        if action == "auth_login":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._auth_login(args, env, timeout)
        if action == "run":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._run_cli(args, env, timeout)
        return ToolResult.fail({"status": "error", "message": "action must be one of: status, ensure, auth_login, run"})

    def _status(self, env: Dict[str, str]) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        payload: Dict[str, Any] = {
            "status": "success",
            "available": bool(command),
            "command": command,
            "npm": _which("npm", env),
            "npx": _which("npx", env),
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }
        if command:
            result = self._safe_run(command + ["auth", "status"], env, 15)
            payload["authStatus"] = result
            parsed = result.get("json")
            payload["authenticated"] = bool(isinstance(parsed, dict) and parsed.get("ok") is not False and (parsed.get("defaultAs") or parsed.get("identity") or parsed.get("data")))
        return payload

    def _ensure(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> ToolResult:
        return ToolResult.success(self._ensure_payload(env, timeout, install_if_missing))

    def _ensure_payload(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        if command:
            return {"status": "success", "available": True, "command": command, "installedNow": False}
        return self._missing_payload(env)

    def _missing_payload(self, env: Dict[str, str], message: str = "") -> Dict[str, Any]:
        return {
            "status": "error",
            "available": False,
            "discoveryOnly": True,
            "message": message or "lark-cli is not available. EcoreX no longer auto-installs the old CLI path; use the built-in find skill/find-skill gate first.",
            "npm": _which("npm", env),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "installHint": (
                "Use the built-in find skill first (gated as find-skill) to discover and install the Feishu/Lark skill or connector. "
                f"If the find skill falls back to the official GitHub source, run: python -m pip install --upgrade \"git+{FEISHU_LARK_SOURCE_URL}.git\". "
                f"If GitHub times out, use the domestic Git mirror: python -m pip install --upgrade \"git+{FEISHU_LARK_MIRROR_URLS[0]}\". "
                f"If that still fails, use the PyPI mirror: python -m pip install -i {FEISHU_LARK_PYPI_MIRROR} --upgrade lark-oapi."
            ),
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }

    def _auth_login(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))

        cli_args = ["auth", "login"]
        scope = str(args.get("scope") or "").strip()
        domain = str(args.get("domain") or "base").strip()
        if scope:
            cli_args.extend(["--scope", scope])
        elif domain:
            cli_args.extend(["--domain", domain])
        else:
            return ToolResult.fail({"status": "error", "message": "auth_login requires scope or domain"})
        cli_args.extend(["--no-wait", "--json"])

        result = self._safe_run(command + cli_args, env, timeout)
        parsed = result.get("json")
        if isinstance(parsed, dict):
            url = parsed.get("verification_url") or parsed.get("verification_uri") or parsed.get("verification_uri_complete")
            if url:
                result["authRequired"] = True
                result["message"] = "Open the verification URL, finish Feishu authorization, then ask EcoreX to continue."
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _run_cli(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))
        cli_args = _as_args(args.get("args"))
        if cli_args and Path(cli_args[0]).name.lower() in {"lark-cli", "lark-cli.cmd"}:
            cli_args = cli_args[1:]
        if not cli_args:
            return ToolResult.fail({"status": "error", "message": "args is required for action=run"})

        result = self._safe_run(command + cli_args, env, timeout)
        expected = _check_expected_paths(args.get("expected_paths") or [])
        if expected["checked"]:
            result["expectedPaths"] = expected
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        parsed = result.get("json")
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return ToolResult.fail(result)
        if expected["missing"] or expected["emptyDirs"]:
            result["status"] = "error"
            result["message"] = "Command exited 0 but expected output files were missing or empty."
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _safe_run(self, command: List[str], env: Dict[str, str], timeout: int) -> Dict[str, Any]:
        try:
            result = _run_process(
                command,
                timeout=timeout,
                env=env,
                cwd=self.cwd,
                cancel_event=getattr(self, "cancel_event", None),
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "timeout",
                "exitCode": None,
                "command": self._display_command(command),
                "output": _sanitize((exc.output or "") + ("\n" + exc.stderr if exc.stderr else ""))[-4000:],
                "message": f"lark-cli command timed out after {timeout} seconds",
            }
        except _ProcessCancelled as exc:
            return {
                "status": "cancelled",
                "exitCode": None,
                "command": self._display_command(command),
                "output": _sanitize((exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else ""))[-4000:],
                "message": "lark-cli command cancelled by user",
            }
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        output = _sanitize(output)
        return {
            "status": "success" if result.returncode == 0 else "error",
            "exitCode": result.returncode,
            "command": self._display_command(command),
            "output": output[-12000:] if output else "(no output)",
            "json": _parse_json_output(result.stdout),
        }

    @staticmethod
    def _display_command(command: List[str]) -> List[str]:
        display = []
        for part in command:
            text = str(part)
            if re.search(r"(?i)(token|secret|password|api[_-]?key)", text):
                display.append("***")
            else:
                display.append(text)
        return display

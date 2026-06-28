"""Feishu/Lark CLI tool wrapper for EcoreX runtimes.

This tool keeps Feishu access out of ad-hoc shell commands. It resolves an
already available `lark-cli`, reports auth state, starts split-flow user auth,
and runs CLI commands with bounded timeouts. It only installs the official CLI
after a structured find-skill discovery gate requests that on-demand setup.
"""

from __future__ import annotations

import json
import hashlib
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


DEFAULT_LARK_CLI_PACKAGE = os.environ.get("ECOREX_LARK_CLI_PACKAGE", "@larksuite/cli@1.0.56")
FEISHU_LARK_SOURCE_URL = "https://github.com/larksuite/cli"
FEISHU_LARK_MIRROR_URLS = ["https://registry.npmmirror.com/@larksuite/cli"]
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DOMESTIC_NPM_REGISTRY = "https://registry.npmmirror.com"
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


def _configured_install_root_from_conf() -> Optional[Path]:
    try:
        from config import conf

        tools = conf().get("tools", {})
        config = tools.get("feishu_cli") if isinstance(tools, dict) else None
        if isinstance(config, dict):
            value = config.get("install_root") or config.get("installRoot")
            if value:
                return Path(str(value)).expanduser()
    except Exception:
        return None
    return None


def _candidate_bin_dirs() -> List[Path]:
    home = Path.home()
    dirs: List[Path] = []
    install_root_override = os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
    if install_root_override:
        install_root = Path(install_root_override).expanduser()
        dirs.extend([
            install_root / "bin",
            install_root / "node_modules" / ".bin",
        ])
    configured_install_root = _configured_install_root_from_conf()
    if configured_install_root:
        dirs.extend([
            configured_install_root / "bin",
            configured_install_root / "node_modules" / ".bin",
        ])
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
        runtime_root / "tools" / "lark-cli" / "node_modules" / ".bin",
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
    install_root_override = env.get("ECOREX_LARK_CLI_INSTALL_ROOT") or os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
    override_root = Path(install_root_override).expanduser() if install_root_override else None
    candidates = [
        *(([
            override_root / "scripts" / "run.js",
            override_root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
        ]) if override_root else []),
        Path("C:/EcoreX Artifact Desk/cli-main/scripts/run.js"),
        Path(__file__).resolve().parents[3] / "tools" / "lark-cli" / "scripts" / "run.js",
        Path(__file__).resolve().parents[3] / "tools" / "lark-cli" / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
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
    input_text: Optional[str] = None,
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
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    deadline = time.time() + max(1, timeout)
    pending_input = input_text
    while True:
        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
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


def _clean_cli_value(value: Any) -> str:
    return str(value or "").strip()


def _json_find_first(payload: Any, keys: Iterable[str]) -> str:
    wanted = {key.lower() for key in keys}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in wanted:
                text = _clean_cli_value(value)
                if text:
                    return text
        for value in payload.values():
            found = _json_find_first(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _json_find_first(item, keys)
            if found:
                return found
    return ""


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", text or "")
    return match.group(0) if match else ""


def _auth_url_from_result(result: Dict[str, Any]) -> str:
    parsed = result.get("json")
    url = _json_find_first(parsed, (
        "verification_url",
        "verification_uri",
        "verification_uri_complete",
        "console_url",
        "url",
    ))
    return url or _extract_first_url(str(result.get("output") or ""))


def _device_code_from_result(result: Dict[str, Any]) -> str:
    return _json_find_first(result.get("json"), ("device_code", "deviceCode", "device-code"))


def _auth_json_bool(payload: Any) -> Optional[bool]:
    if not isinstance(payload, dict):
        return None
    for key in (
        "authenticated",
        "isAuthenticated",
        "is_authenticated",
        "authorized",
        "configured",
        "loggedIn",
        "logged_in",
        "login",
    ):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        nested = _auth_json_bool(data)
        if nested is not None:
            return nested
        if data.get("defaultAs") or data.get("identity") or data.get("user") or data.get("tenant"):
            return True
    if payload.get("defaultAs") or payload.get("identity") or payload.get("user") or payload.get("tenant"):
        return True
    if payload.get("ok") is False:
        return False
    return None


def _auth_state_from_status_result(result: Dict[str, Any]) -> str:
    if result.get("status") == "timeout":
        return "unknown"
    parsed_state = _auth_json_bool(result.get("json"))
    if parsed_state is True:
        return "ready"
    if parsed_state is False:
        return "needs_login"
    output = str(result.get("output") or "").lower()
    if any(marker in output for marker in (
        "not configured",
        "not login",
        "not logged",
        "unauthorized",
        "no credential",
        "未配置",
        "未登录",
        "未授权",
    )):
        return "needs_login"
    if any(marker in output for marker in (
        "logged in",
        "authenticated",
        "authorized",
        "已登录",
        "已授权",
    )):
        return "ready"
    if result.get("exitCode") not in (0, None):
        return "needs_login"
    return "unknown"


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


def _has_find_skill_gate(args: Dict[str, Any]) -> bool:
    for key in ("discovery_source", "source", "via", "gate", "resolved_by"):
        value = str(args.get(key) or "").strip().lower().replace("_", "-")
        if value in {"find", "find-skill", "find skill"} or "find-skill" in value:
            return True
    result = args.get("find_skill_result")
    if result is None:
        result = args.get("findSkillResult")
    if result is None:
        return False
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or result.get("state") or result.get("result") or "").strip().lower()
    positive = (
        status in {"success", "ok", "found", "pass", "passed", "ready", "available"}
        or result.get("success") is True
        or result.get("ok") is True
        or result.get("found") is True
        or result.get("available") is True
    )
    if not positive:
        return False
    try:
        text = json.dumps(result, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        text = str(result).lower()
    return any(hint in text for hint in ("feishu", "lark", "飞书", "@larksuite", "lark-cli"))


def _find_skill_gate_error(package: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "available": False,
        "discoveryOnly": True,
        "package": package,
        "sourceUrl": FEISHU_LARK_SOURCE_URL,
        "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
        "message": (
            "Feishu/Lark CLI installation must be discovered through the built-in "
            "find skill / find-skill gate first. Retry feishu_cli install with "
            "discovery_source='find-skill' or a find_skill_result payload."
        ),
        "nextAction": {
            "skill": "find",
            "ability": "find-skill",
            "query": "official Feishu Lark CLI @larksuite/cli install source",
        },
    }


class FeishuCli(BaseTool):
    name: str = "feishu_cli"
    description: str = (
        "Use this instead of bash for Feishu/Lark operations when lark-cli is already "
        "available. It checks user auth, starts split-flow auth with --no-wait, runs "
        "bounded lark-cli commands, and can install the official @larksuite/cli only after "
        "a find-skill/on-demand flow requests it."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, ensure, diagnose, install, config_init, auth_login, run",
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
            "device_code": {
                "type": "string",
                "description": "Device code returned by a previous auth_login --no-wait flow; completes authorization.",
            },
            "app_id": {
                "type": "string",
                "description": "Feishu/Lark app ID for config_init. If omitted, EcoreX uses saved external connection credentials.",
            },
            "app_secret": {
                "type": "string",
                "description": "Feishu/Lark app secret for config_init. Passed to lark-cli over stdin, never as a command argument.",
            },
            "brand": {
                "type": "string",
                "description": "Brand for config_init, usually feishu or lark. Defaults to feishu.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout seconds for install/run actions.",
            },
            "install_if_missing": {
                "type": "boolean",
                "description": "Deprecated for ensure. Use action=install after find-skill discovery and user permission.",
            },
            "registry": {
                "type": "string",
                "description": "Optional npm registry for action=install. Defaults to npmjs, then npmmirror fallback.",
            },
            "discovery_source": {
                "type": "string",
                "description": "Required for action=install. Must be find-skill after built-in skill discovery.",
            },
            "find_skill_result": {
                "type": "object",
                "description": "Structured result returned by the built-in find skill/find-skill gate.",
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

    def _env(self) -> Dict[str, str]:
        env = _tool_env()
        install_root = self._install_root()
        env["ECOREX_LARK_CLI_INSTALL_ROOT"] = str(install_root)
        _prepend_path(env, install_root / "bin")
        _prepend_path(env, install_root / "node_modules" / ".bin")
        return env

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        timeout = int(args.get("timeout") or self.config.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
        install_if_missing = False
        env = self._env()

        if action == "status":
            return ToolResult.success(self._status(env, auth_timeout=max(1, min(timeout, 15))))
        if action == "diagnose":
            return ToolResult.success(self._diagnose(env))
        if action == "ensure":
            return self._ensure(env, timeout, install_if_missing=install_if_missing)
        if action == "install":
            if not _has_find_skill_gate(args):
                return ToolResult.fail(_find_skill_gate_error(self.package))
            return self._install(env, timeout, registry=str(args.get("registry") or "").strip())
        if action == "config_init":
            ensure = self._ensure_payload(env, timeout, install_if_missing)
            if not ensure.get("available"):
                return ToolResult.fail(ensure)
            return self._config_init(args, env, timeout)
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
        return ToolResult.fail({"status": "error", "message": "action must be one of: status, ensure, diagnose, install, config_init, auth_login, run"})

    def _status(self, env: Dict[str, str], auth_timeout: int = 15) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        payload: Dict[str, Any] = {
            "status": "success",
            "available": bool(command),
            "command": command,
            "npm": _which("npm", env),
            "npx": _which("npx", env),
            "package": self.package,
            "installRoot": str(self._install_root()),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "authState": "unknown" if command else "cli_missing",
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }
        if command:
            result = self._safe_run(command + ["auth", "status", "--json"], env, auth_timeout)
            output = str(result.get("output") or "").lower()
            if result.get("exitCode") != 0 and any(marker in output for marker in (
                "unknown option",
                "unknown flag",
                "flag provided but not defined",
                "unrecognized option",
                "unrecognized flag",
                "no such option",
            )):
                result = self._safe_run(command + ["auth", "status"], env, auth_timeout)
            payload["authStatus"] = result
            payload["authState"] = _auth_state_from_status_result(result)
            payload["authenticated"] = payload["authState"] == "ready"
            if payload["authState"] == "needs_login":
                payload["nextAction"] = {"tool": "feishu_cli", "action": "auth_login", "domain": "base"}
        return payload

    def _ensure(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> ToolResult:
        return ToolResult.success(self._ensure_payload(env, timeout, install_if_missing))

    def _ensure_payload(self, env: Dict[str, str], timeout: int, install_if_missing: bool) -> Dict[str, Any]:
        command = _resolve_lark_command(env)
        if command:
            return {"status": "success", "available": True, "command": command, "installedNow": False}
        return self._missing_payload(env)

    def _install_root(self) -> Path:
        override = self.config.get("install_root") or os.environ.get("ECOREX_LARK_CLI_INSTALL_ROOT")
        if override:
            return Path(str(override)).expanduser()
        configured = _configured_install_root_from_conf()
        if configured:
            return configured
        return Path(__file__).resolve().parents[3] / "tools" / "lark-cli"

    def _diagnose(self, env: Dict[str, str]) -> Dict[str, Any]:
        status = self._status(env)
        status.update({
            "action": "diagnose",
            "installRoot": str(self._install_root()),
            "officialRegistry": DEFAULT_NPM_REGISTRY,
            "domesticRegistry": DOMESTIC_NPM_REGISTRY,
        })
        node = _which("node", env)
        if node:
            status["nodeVersion"] = self._safe_run([node, "--version"], env, 10)
        return status

    def _install(self, env: Dict[str, str], timeout: int, registry: str = "") -> ToolResult:
        existing = _resolve_lark_command(env)
        if existing:
            return ToolResult.success({
                "status": "success",
                "available": True,
                "installedNow": False,
                "command": existing,
                "message": "lark-cli is already available.",
            })

        npm = _which("npm", env)
        if not npm:
            return ToolResult.fail({
                **self._missing_payload(env, "npm is not available; cannot install @larksuite/cli on demand."),
                "nextAction": "Install Node.js/npm or ask an administrator to preinstall @larksuite/cli.",
            })

        install_root = self._install_root()
        install_root.mkdir(parents=True, exist_ok=True)
        registries = [registry] if registry else [DEFAULT_NPM_REGISTRY, DOMESTIC_NPM_REGISTRY]
        attempts: List[Dict[str, Any]] = []
        for npm_registry in [item for item in registries if item]:
            command = [
                npm,
                "install",
                "--prefix",
                str(install_root),
                self.package,
                "--registry",
                npm_registry,
            ]
            result = self._safe_run(command, env, timeout)
            attempts.append({
                "registry": npm_registry,
                "exitCode": result.get("exitCode"),
                "status": result.get("status"),
                "output": result.get("output", "")[-1200:],
            })
            if result.get("exitCode") == 0:
                next_env = self._env()
                resolved = _resolve_lark_command(next_env)
                return ToolResult.success({
                    "status": "success",
                    "available": bool(resolved),
                    "installedNow": True,
                    "command": resolved,
                    "package": self.package,
                    "installRoot": str(install_root),
                    "sourceUrl": FEISHU_LARK_SOURCE_URL,
                    "registry": npm_registry,
                    "fallbackUsed": npm_registry != DEFAULT_NPM_REGISTRY,
                    "attempts": attempts,
                    "message": "Installed official @larksuite/cli on demand.",
                })

        return ToolResult.fail({
            "status": "error",
            "available": False,
            "package": self.package,
            "installRoot": str(install_root),
            "sourceUrl": FEISHU_LARK_SOURCE_URL,
            "mirrorUrls": FEISHU_LARK_MIRROR_URLS,
            "attempts": attempts,
            "message": "Failed to install official @larksuite/cli from npmjs.org and domestic npm mirror.",
        })

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
                f"For real CLI work, run feishu_cli action=install with discovery_source='find-skill' to install official {self.package}. "
                f"If npmjs.org times out, EcoreX retries with domestic npm mirror {DOMESTIC_NPM_REGISTRY}."
            ),
            "pathHints": [str(path) for path in _candidate_bin_dirs() if path.exists()],
        }

    def _config_init(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))
        app_id, app_secret, credential_source = self._feishu_credentials(args)
        brand = _clean_cli_value(args.get("brand") or "feishu") or "feishu"
        if app_id and app_secret:
            cli_args = ["config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", brand]
            name = _clean_cli_value(args.get("name"))
            if name:
                cli_args.extend(["--name", name])
            extra = _as_args(args.get("args"))
            if extra:
                cli_args.extend(extra)
            result = self._safe_run(command + cli_args, env, timeout, input_text=app_secret + "\n")
            result["credentialSource"] = credential_source
            result["message"] = (
                "lark-cli config initialized from EcoreX Feishu external connection credentials."
                if result.get("exitCode") == 0
                else "lark-cli config initialization failed."
            )
            if result.get("exitCode") != 0:
                return ToolResult.fail(result)
            return ToolResult.success(result)

        cli_args = ["config", "init", "--new"]
        extra = _as_args(args.get("args"))
        if extra:
            cli_args.extend(extra)
        result = self._safe_run(command + cli_args, env, timeout)
        url = _auth_url_from_result(result)
        if url:
            result["verificationUrl"] = url
            result["qrCode"] = self._generate_auth_qrcode(command, env, url, timeout)
            result["authRequired"] = True
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _auth_login(self, args: Dict[str, Any], env: Dict[str, str], timeout: int) -> ToolResult:
        command = _resolve_lark_command(env)
        if not command:
            return ToolResult.fail(self._missing_payload(env))

        device_code = _clean_cli_value(args.get("device_code") or args.get("deviceCode"))
        if device_code:
            result = self._safe_run(command + ["auth", "login", "--device-code", device_code], env, timeout)
            result["authFlow"] = "complete"
            result["authCompleted"] = result.get("exitCode") == 0
            if result.get("exitCode") != 0:
                return ToolResult.fail(result)
            result["authStatus"] = self._status(env, auth_timeout=max(1, min(timeout, 15)))
            return ToolResult.success(result)

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
        url = _auth_url_from_result(result)
        device_code = _device_code_from_result(result)
        result["authFlow"] = "start"
        if device_code:
            result["deviceCode"] = device_code
        if url:
            result["verificationUrl"] = url
            result["qrCode"] = self._generate_auth_qrcode(command, env, url, timeout)
            result["authRequired"] = True
            result["message"] = (
                "Open the verification URL or scan the QR code, finish Feishu authorization, "
                "then run feishu_cli auth_login again with device_code to complete."
            )
            result["nextAction"] = {
                "tool": "feishu_cli",
                "action": "auth_login",
                "device_code": device_code,
            }
        if result.get("exitCode") != 0:
            return ToolResult.fail(result)
        return ToolResult.success(result)

    def _feishu_credentials(self, args: Dict[str, Any]) -> tuple[str, str, str]:
        app_id = _clean_cli_value(
            args.get("app_id")
            or args.get("appId")
            or args.get("feishu_app_id")
            or args.get("client_id")
            or args.get("clientId")
        )
        app_secret = _clean_cli_value(
            args.get("app_secret")
            or args.get("appSecret")
            or args.get("feishu_app_secret")
            or args.get("client_secret")
            or args.get("clientSecret")
        )
        if app_id and app_secret:
            return app_id, app_secret, "tool_args"

        env_app_id = _clean_cli_value(os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID"))
        env_app_secret = _clean_cli_value(os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET"))
        if env_app_id and env_app_secret:
            return env_app_id, env_app_secret, "environment"

        try:
            from config import conf, load_config

            cfg = conf()
            if not cfg:
                load_config()
                cfg = conf()
            config_app_id = _clean_cli_value(cfg.get("feishu_app_id") or cfg.get("lark_app_id"))
            config_app_secret = _clean_cli_value(cfg.get("feishu_app_secret") or cfg.get("lark_app_secret"))
            if config_app_id and config_app_secret:
                return config_app_id, config_app_secret, "ecorex_external_connection"
        except Exception as exc:
            logger.debug(f"[FeishuCli] failed reading Feishu credentials from config: {exc}")
        return "", "", "missing"

    def _generate_auth_qrcode(self, command: List[str], env: Dict[str, str], url: str, timeout: int) -> Dict[str, Any]:
        if not url:
            return {"status": "skipped", "message": "verification URL missing"}
        cwd = Path(self.cwd or os.getcwd()).expanduser()
        cwd.mkdir(parents=True, exist_ok=True)
        qr_dir = cwd / ".ecorex" / "lark-auth"
        qr_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        relative_output = f".ecorex/lark-auth/feishu-auth-{digest}.png"
        result = self._safe_run(
            command + ["auth", "qrcode", url, "--output", relative_output, "--size", "256"],
            env,
            max(5, min(timeout, 30)),
        )
        path = cwd / relative_output
        return {
            "status": result.get("status"),
            "exitCode": result.get("exitCode"),
            "path": str(path),
            "relativePath": relative_output,
            "output": result.get("output", ""),
        }

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

    def _safe_run(self, command: List[str], env: Dict[str, str], timeout: int, input_text: Optional[str] = None) -> Dict[str, Any]:
        try:
            result = _run_process(
                command,
                timeout=timeout,
                env=env,
                cwd=self.cwd,
                cancel_event=getattr(self, "cancel_event", None),
                input_text=input_text,
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
        redact_next = False
        sensitive_flags = {
            "--app-id",
            "--app_secret",
            "--app-secret",
            "--device-code",
            "--access-token",
            "--refresh-token",
        }
        for part in command:
            text = str(part)
            if redact_next:
                display.append("***")
                redact_next = False
            elif text in sensitive_flags:
                display.append(text)
                redact_next = True
            elif re.search(r"(?i)(token|secret|password|api[_-]?key)", text):
                display.append("***")
            else:
                display.append(text)
        return display

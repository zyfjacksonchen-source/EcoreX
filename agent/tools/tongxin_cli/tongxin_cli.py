"""Tongxin Assistant CLI read-only wrapper for EcoreX runtimes."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


DEFAULT_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 12000
DEFAULT_SCRIPT_NAME = "xin_agent_cli.py"
DEFAULT_TONGXIN_SCOPE = "all-users-read-only"

READ_ONLY_ALLOWED_COMMANDS = (
    "schema",
    "account list --source --platform --xhs-channel --project-id --search --limit --offset",
    "project list --source --platform --xhs-channel --search --limit --offset",
    "report summary --source --platform --xhs-channel --account-id --project-id --start-date --end-date --limit --offset",
    "note detail --source --platform --xhs-channel --account-id --project-id --start-date --end-date --limit --offset",
    "realtime summary --xhs-channel all|spotlight|chengfeng --project-id --account-id --search --limit --offset",
)

_MUTATING_WORDS = {
    "write",
    "sync",
    "update",
    "delete",
    "remove",
    "create",
    "submit",
    "approve",
    "reject",
    "import",
    "export",
    "upload",
    "download",
    "cache-write",
    "cache_write",
    "refresh-token",
    "token-refresh",
    "auth",
    "login",
    "logout",
    "config",
    "set",
    "edit",
    "patch",
    "grant",
    "permission",
}
_MUTATING_FLAGS = {
    "--write",
    "--write-mode",
    "--write_mode",
    "--cache-write",
    "--cache_write",
    "--sync",
    "--refresh",
    "--token-refresh",
    "--token_refresh",
    "--submit",
    "--approve",
    "--delete",
    "--force",
    "--config",
    "--set",
}
_FLAGS_WITH_VALUES = {
    "--source",
    "--platform",
    "--xhs-channel",
    "--account-id",
    "--project-id",
    "--search",
    "--start-date",
    "--end-date",
    "--limit",
    "--offset",
    "--task-id",
    "--operator",
    "--date",
    "--format",
}
_BOOLEAN_FLAGS = {"--json", "--help", "-h", "--no-cache-write", "--read-only"}
_ALL_FLAGS = _FLAGS_WITH_VALUES | _BOOLEAN_FLAGS
_COMMAND_ALLOWED_FLAGS = {
    ("account", "list"): {"--source", "--platform", "--xhs-channel", "--project-id", "--search", "--limit", "--offset"},
    ("project", "list"): {"--source", "--platform", "--xhs-channel", "--search", "--limit", "--offset"},
    ("report", "summary"): {
        "--source",
        "--platform",
        "--xhs-channel",
        "--account-id",
        "--project-id",
        "--start-date",
        "--end-date",
        "--limit",
        "--offset",
    },
    ("note", "detail"): {
        "--source",
        "--platform",
        "--xhs-channel",
        "--account-id",
        "--project-id",
        "--start-date",
        "--end-date",
        "--limit",
        "--offset",
    },
    ("realtime", "summary"): {"--xhs-channel", "--project-id", "--account-id", "--search", "--limit", "--offset"},
}
_SOURCE_VALUES = {"mpi", "cache"}
_PLATFORM_VALUES = {"xhs", "bili", "alipay"}
_XHS_REALTIME_CHANNELS = {"all", "spotlight", "chengfeng"}
_XHS_OFFLINE_CHANNELS = {"spotlight", "chengfeng"}
_SENSITIVE_JSON_KEYS = {
    "access_token",
    "access-token",
    "refresh_token",
    "refresh-token",
    "app_secret",
    "app-secret",
    "authorization",
    "token",
    "secret",
    "password",
    "api_key",
    "api-key",
    "apikey",
}
_SENSITIVE_JSON_COMPACT_KEYS = {
    "accesstoken",
    "refreshtoken",
    "appsecret",
    "apikey",
    "authheader",
    "authorizationheader",
}
_SENSITIVE_JSON_KEY_PARTS = {
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?secret|authorization)(\"?\s*[:=]\s*\"?)([^\",\s&}]+)"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)(=|:)\s*[^\s,&}\"]+"),
]


class _ProcessCancelled(Exception):
    def __init__(self, stdout: str = "", stderr: str = ""):
        super().__init__("process cancelled by user")
        self.stdout = stdout or ""
        self.stderr = stderr or ""


def _sanitize(text: str) -> str:
    value = text or ""
    value = re.sub(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|app[_-]?secret|auth[_-]?header|authorization|credential[_-]?id|api[_-]?key|token|secret|password|credential)\b(\s*[:=]\s*)(?:bearer\s+)?[^\r\n,;}&]+",
        lambda m: f"{m.group(1)}{m.group(2)}***",
        value,
    )
    value = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}",
        r"\1***",
        value,
    )
    value = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|app[_-]?secret|auth[_-]?header|authorization|credential[_-]?id|api[_-]?key|token|secret|password|credential)[\"']?\s*[:=]\s*[\"'])([^\"'\r\n]*)([\"'])",
        r"\1***\3",
        value,
    )
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
        else:
            value = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    value = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", value)
    value = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", value)
    return value


def _sanitize_json(value: Any, *, parent_key: str = "") -> Any:
    if _is_sensitive_json_key(parent_key):
        return "***"
    if isinstance(value, dict):
        return {item_key: _sanitize_json(item_value, parent_key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return _sanitize(value)
    return value


def _is_sensitive_json_key(key: Any) -> bool:
    raw = str(key or "").strip().lower()
    if not raw:
        return False
    dashed = raw.replace("_", "-")
    compact = "".join(char for char in raw if char.isalnum())
    if dashed in _SENSITIVE_JSON_KEYS or compact in _SENSITIVE_JSON_COMPACT_KEYS:
        return True
    if any(part in compact for part in _SENSITIVE_JSON_KEY_PARTS):
        return True
    return compact.startswith("auth") and compact not in {"author", "authors", "authority"}


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text or "") <= limit:
        return text or ""
    return (text or "")[:limit] + f"\n\n[truncated at {limit} chars]"


def _as_args(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, tuple):
        return [str(item) for item in raw if str(item).strip()]
    return [str(raw)]


def _lower_tokens(args: Iterable[str]) -> List[str]:
    return [str(item).strip().strip("\"'").lower() for item in args if str(item).strip()]


def _flag_values(tokens: List[str], flag: str) -> List[str]:
    values: List[str] = []
    for idx, token in enumerate(tokens):
        if token == flag and idx + 1 < len(tokens):
            values.append(tokens[idx + 1])
    return values


def _validate_known_flags(tokens: List[str], allowed_flags: Iterable[str], *, command_words: int = 2) -> Tuple[bool, str]:
    allowed = set(allowed_flags)
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if idx < command_words:
            idx += 1
            continue
        if token.startswith("--") or token == "-h":
            if token not in _ALL_FLAGS or token not in allowed:
                return False, f"flag '{token}' is not allowed for this Tongxin read-only command"
            if token in _FLAGS_WITH_VALUES:
                if idx + 1 >= len(tokens) or tokens[idx + 1].startswith("-"):
                    return False, f"flag '{token}' requires a value"
                idx += 2
                continue
            if token in _BOOLEAN_FLAGS:
                idx += 1
                continue
        return False, f"positional argument '{token}' is not allowed for this Tongxin read-only command"
    return True, ""


def _has_mutating_intent(tokens: List[str]) -> Tuple[bool, str]:
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in _FLAGS_WITH_VALUES:
            skip_next = True
            continue
        normalized = token.replace("_", "-")
        if token in _MUTATING_FLAGS or normalized in _MUTATING_WORDS:
            return True, token
    return False, ""


def _source_platform_channel_ok(tokens: List[str], *, realtime: bool = False) -> Tuple[bool, str]:
    source_values = _flag_values(tokens, "--source")
    if source_values and any(value not in _SOURCE_VALUES for value in source_values):
        return False, "source must be mpi or cache"
    platform_values = _flag_values(tokens, "--platform")
    if platform_values and any(value not in _PLATFORM_VALUES for value in platform_values):
        return False, "platform must be xhs, bili, or alipay"
    channel_values = _flag_values(tokens, "--xhs-channel")
    allowed_channels = _XHS_REALTIME_CHANNELS if realtime else _XHS_OFFLINE_CHANNELS
    if channel_values and any(value not in allowed_channels for value in channel_values):
        return False, f"xhs-channel must be one of: {', '.join(sorted(allowed_channels))}"
    return True, ""


def validate_read_only_tongxin_args(raw_args: Any) -> Tuple[bool, str]:
    """Return whether a Tongxin CLI argv tail is within the read-only contract."""
    tokens = _lower_tokens(_as_args(raw_args))
    if not tokens:
        return False, "args is required"
    if tokens == ["schema"] or tokens == ["--help"] or tokens == ["-h"]:
        return True, "read-only schema/help"

    mutating, token = _has_mutating_intent(tokens)
    if mutating:
        return False, f"mutating token '{token}' is blocked"

    if tokens[:2] in (["account", "list"], ["project", "list"]):
        flags_ok, flags_reason = _validate_known_flags(tokens, _COMMAND_ALLOWED_FLAGS[(tokens[0], tokens[1])])
        if not flags_ok:
            return False, flags_reason
        return _source_platform_channel_ok(tokens)
    if tokens[:2] == ["report", "summary"]:
        flags_ok, flags_reason = _validate_known_flags(tokens, _COMMAND_ALLOWED_FLAGS[("report", "summary")])
        if not flags_ok:
            return False, flags_reason
        return _source_platform_channel_ok(tokens)
    if tokens[:2] == ["note", "detail"]:
        flags_ok, flags_reason = _validate_known_flags(tokens, _COMMAND_ALLOWED_FLAGS[("note", "detail")])
        if not flags_ok:
            return False, flags_reason
        ok, reason = _source_platform_channel_ok(tokens)
        if not ok:
            return ok, reason
        source_values = _flag_values(tokens, "--source")
        platform_values = _flag_values(tokens, "--platform")
        if "mpi" in source_values and platform_values and any(value != "xhs" for value in platform_values):
            return False, "note detail --source mpi is read-only only for xhs"
        return True, "read-only note detail"
    if tokens[:2] == ["realtime", "summary"]:
        flags_ok, flags_reason = _validate_known_flags(tokens, _COMMAND_ALLOWED_FLAGS[("realtime", "summary")])
        if not flags_ok:
            return False, flags_reason
        ok, reason = _source_platform_channel_ok(tokens, realtime=True)
        return (ok, reason) if not ok else (True, "read-only realtime summary")
    return False, "command is not in the Tongxin read-only allowlist"


def is_read_only_tongxin_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    if action in {"status", "schema", "diagnose"}:
        return True
    if action == "run":
        ok, _reason = validate_read_only_tongxin_args((args or {}).get("args"))
        return ok
    return False


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
            logger.debug(f"[TongxinCli] taskkill failed for pid {process.pid}: {exc}")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except Exception as exc:
            logger.debug(f"[TongxinCli] killpg failed for pid {process.pid}: {exc}")
    try:
        process.kill()
    except Exception:
        pass


def _run_process(command: List[str], timeout: int, cwd: str, env: Dict[str, str], cancel_event=None) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
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
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _kill_process_tree(process)
                stdout, stderr = process.communicate()
                raise _ProcessCancelled(stdout, stderr)
            if time.time() >= deadline:
                _kill_process_tree(process)
                stdout, stderr = process.communicate()
                exc = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
                raise exc


class TongxinCli(BaseTool):
    name: str = "tongxin_cli"
    description: str = (
        "Default all-user read-only access to Tongxin Assistant / Xin Agent account data. "
        "Use this instead of bash for xin_agent_cli.py. It can run schema and approved "
        "read-only account/project/report/note/realtime queries only; write, sync, auth, "
        "submit, approve, delete, and permission-changing commands are blocked."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, schema, diagnose, run.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments after xin_agent_cli.py for action=run, e.g. ['realtime', 'summary', '--xhs-channel', 'all'].",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout seconds. Default: {DEFAULT_TIMEOUT_SECONDS}; maximum: 300.",
            },
            "include_paths": {
                "type": "boolean",
                "description": "Include resolved local script path in diagnostics. Default false.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: Optional[dict] = None):
        self.apply_config(config or {})

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = str(self.config.get("cwd") or os.getcwd())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        timeout = self._timeout(args.get("timeout"))
        include_paths = bool(args.get("include_paths"))
        if action in {"status", "diagnose"}:
            return ToolResult.success(self._status(include_paths=include_paths, diagnose=action == "diagnose"))
        if action == "schema":
            return self._run_cli(["schema"], timeout, include_paths=include_paths)
        if action == "run":
            cli_args = _as_args(args.get("args"))
            ok, reason = validate_read_only_tongxin_args(cli_args)
            if not ok:
                return ToolResult.fail({
                    "status": "error",
                    "errorType": "tongxin_cli_read_only_block",
                    "message": reason,
                    "readOnly": True,
                    "allowedCommands": list(READ_ONLY_ALLOWED_COMMANDS),
                })
            return self._run_cli(cli_args, timeout, include_paths=include_paths)
        return ToolResult.fail({"status": "error", "message": "action must be one of: status, schema, diagnose, run"})

    def _status(self, *, include_paths: bool = False, diagnose: bool = False) -> Dict[str, Any]:
        script = self._script_path()
        payload: Dict[str, Any] = {
            "status": "success",
            "available": bool(script),
            "tool": self.name,
            "scriptName": DEFAULT_SCRIPT_NAME,
            "readOnly": True,
            "defaultAudience": DEFAULT_TONGXIN_SCOPE,
            "allowedCommands": list(READ_ONLY_ALLOWED_COMMANDS),
            "configured": bool(self._configured_script_path()),
        }
        if include_paths:
            payload["scriptPath"] = str(script) if script else ""
            payload["candidatePaths"] = [str(path) for path in self._candidate_script_paths()]
        if diagnose:
            payload["python"] = sys.executable
            payload["cwd"] = self.cwd
        if not script:
            payload["message"] = (
                "Tongxin CLI script was not found. Configure tools.tongxin_cli.script_path "
                "or ECOREX_TONGXIN_CLI_PATH to the read-only xin_agent_cli.py path."
            )
        return payload

    def _run_cli(self, cli_args: List[str], timeout: int, *, include_paths: bool = False) -> ToolResult:
        script = self._script_path()
        if not script:
            return ToolResult.fail(self._status(include_paths=include_paths))
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        command = [sys.executable, str(script), *cli_args]
        try:
            result = _run_process(
                command,
                timeout=timeout,
                cwd=str(script.parent),
                env=env,
                cancel_event=getattr(self, "cancel_event", None),
            )
        except subprocess.TimeoutExpired as exc:
            output = _sanitize((exc.output or "") + ("\n" + exc.stderr if exc.stderr else ""))
            return ToolResult.fail({
                "status": "timeout",
                "exitCode": None,
                "command": self._display_command(cli_args),
                "output": _truncate(output),
                "message": f"tongxin_cli command timed out after {timeout} seconds",
            })
        except _ProcessCancelled as exc:
            output = _sanitize((exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else ""))
            return ToolResult.fail({
                "status": "cancelled",
                "exitCode": None,
                "command": self._display_command(cli_args),
                "output": _truncate(output),
                "message": "tongxin_cli command cancelled by user",
            })
        except Exception as exc:
            return ToolResult.fail({
                "status": "error",
                "exitCode": None,
                "command": self._display_command(cli_args),
                "message": f"tongxin_cli execution failed: {exc}",
            })

        output = _sanitize((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
        payload: Dict[str, Any] = {
            "status": "success" if result.returncode == 0 else "error",
            "exitCode": result.returncode,
            "command": self._display_command(cli_args),
            "output": _truncate(output) if output else "(no output)",
            "json": _sanitize_json(self._parse_json(result.stdout)),
            "readOnly": True,
            "defaultAudience": DEFAULT_TONGXIN_SCOPE,
        }
        if include_paths:
            payload["scriptPath"] = str(script)
        if result.returncode != 0:
            return ToolResult.fail(payload)
        return ToolResult.success(payload)

    def _candidate_script_paths(self) -> List[Path]:
        configured = self._configured_script_path()
        runtime_root = Path(__file__).resolve().parents[3]
        raw: List[Any] = [
            configured,
            os.environ.get("ECOREX_TONGXIN_CLI_PATH"),
            os.environ.get("XIN_AGENT_CLI_PATH"),
            os.environ.get("TONGXIN_CLI_PATH"),
            Path(self.cwd) / DEFAULT_SCRIPT_NAME,
            runtime_root / "tools" / "tongxin" / DEFAULT_SCRIPT_NAME,
            runtime_root / DEFAULT_SCRIPT_NAME,
            Path("C:/自动报表工具") / DEFAULT_SCRIPT_NAME,
            Path("C:/EcoreX Artifact Desk") / DEFAULT_SCRIPT_NAME,
        ]
        paths: List[Path] = []
        seen = set()
        for item in raw:
            if not item:
                continue
            path = Path(str(item)).expanduser()
            key = str(path).lower()
            if key not in seen:
                seen.add(key)
                paths.append(path)
        return paths

    def _configured_script_path(self) -> str:
        for key in ("script_path", "scriptPath", "path"):
            value = self.config.get(key)
            if value:
                return str(value)
        try:
            from config import conf

            tools = conf().get("tools", {})
            cfg = tools.get("tongxin_cli") if isinstance(tools, dict) else None
            if isinstance(cfg, dict):
                for key in ("script_path", "scriptPath", "path"):
                    value = cfg.get(key)
                    if value:
                        return str(value)
        except Exception:
            return ""
        return ""

    def _script_path(self) -> Optional[Path]:
        for path in self._candidate_script_paths():
            try:
                if path.is_file():
                    return path
            except Exception:
                continue
        return None

    @staticmethod
    def _display_command(cli_args: List[str]) -> List[str]:
        return ["python", DEFAULT_SCRIPT_NAME, *[str(item) for item in cli_args]]

    @staticmethod
    def _parse_json(text: str) -> Any:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            pass
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line.startswith(("{", "[")):
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
        return None

    @staticmethod
    def _timeout(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = DEFAULT_TIMEOUT_SECONDS
        return max(1, min(parsed, 300))

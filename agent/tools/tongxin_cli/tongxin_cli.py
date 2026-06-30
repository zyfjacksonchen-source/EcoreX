"""Tongxin Assistant CLI read-only wrapper for EcoreX runtimes."""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import ast
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from common.tool_execution_environment import ToolExecutionCancelled, ToolExecutionEnvironment


DEFAULT_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 12000
MAX_BOOTSTRAP_BYTES = 10 * 1024 * 1024
DEFAULT_SCRIPT_NAME = "xin_agent_cli.py"
DEFAULT_CLIENT_EVENT_KEY = "ecorex-web-v0.2.5-web.1"
PYTHON_CONFIG_KEYS = ("python_path", "pythonPath", "runtime_python_path", "runtimePythonPath")
SUPPORTED_SCRIPT_NAMES = (
    DEFAULT_SCRIPT_NAME,
    "xin agent cli.py",
    "xin-agent-cli.py",
    "tongxin_cli.py",
)
DEFAULT_TONGXIN_SCOPE = "all-users-read-only"
TONGXIN_DATA_HEALTH_PROBE_ARGS = ("project", "list", "--source", "cache", "--limit", "1")
BOOTSTRAP_CONFIG_KEYS = {
    "url": ("bootstrap_url", "bootstrapUrl", "download_url", "downloadUrl", "remote_url", "remoteUrl"),
    "manifest_url": ("bootstrap_manifest_url", "bootstrapManifestUrl", "manifest_url", "manifestUrl"),
    "sha256": ("bootstrap_sha256", "bootstrapSha256", "expected_sha256", "expectedSha256", "sha256"),
    "token": ("bootstrap_token", "bootstrapToken", "auth_token", "authToken"),
    "target_dir": ("bootstrap_dir", "bootstrapDir", "install_dir", "installDir"),
}
REMOTE_AUTH_CONFIG_KEYS = {
    "url": ("auth_url", "authUrl", "login_url", "loginUrl", "remote_auth_url", "remoteAuthUrl"),
    "client_key": ("client_event_key", "clientEventKey", "client_key", "clientKey"),
}
TONGXIN_RUNPY_SHIM = (
    "import os, runpy, sys; "
    "script=os.path.abspath(sys.argv[1]); "
    "script_dir=os.path.dirname(script); "
    "sys.path=[script_dir]+[p for p in sys.path if os.path.abspath(p or os.getcwd())!=script_dir]; "
    "sys.argv=[script]+sys.argv[2:]; "
    "runpy.run_path(script, run_name='__main__')"
)

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


def _path_ref(value: Any) -> Dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {"present": False}
    return {
        "present": True,
        "name": Path(raw).name,
        "pathHash": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16],
    }


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


def is_config_driven_tongxin_bootstrap_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    if action not in {"bootstrap", "download"}:
        return False
    explicit_keys = {
        "url",
        "download_url",
        "downloadUrl",
        "remote_url",
        "remoteUrl",
        "manifest_url",
        "manifestUrl",
        "bootstrap_url",
        "bootstrapUrl",
        "bootstrap_manifest_url",
        "bootstrapManifestUrl",
        "token",
        "auth_token",
        "authToken",
        "bootstrap_token",
        "bootstrapToken",
        "target_dir",
        "targetDir",
        "bootstrap_dir",
        "bootstrapDir",
    }
    return not any((args or {}).get(key) for key in explicit_keys)


def is_config_driven_tongxin_auth_request(args: Dict[str, Any]) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    if action not in {"auth", "login", "auto_configure", "auto-configure", "auto_config"}:
        return False
    explicit_remote_keys = {
        "auth_url",
        "authUrl",
        "login_url",
        "loginUrl",
        "remote_auth_url",
        "remoteAuthUrl",
        "url",
        "download_url",
        "downloadUrl",
        "remote_url",
        "remoteUrl",
        "manifest_url",
        "manifestUrl",
        "bootstrap_url",
        "bootstrapUrl",
        "bootstrap_manifest_url",
        "bootstrapManifestUrl",
        "auth_token",
        "authToken",
        "bootstrap_token",
        "bootstrapToken",
        "token",
        "target_dir",
        "targetDir",
        "bootstrap_dir",
        "bootstrapDir",
    }
    return not any((args or {}).get(key) for key in explicit_remote_keys)


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
    try:
        return ToolExecutionEnvironment(tool_name="tongxin_cli", base_env=env, cwd=cwd).run_completed(
            command,
            timeout=timeout,
            cwd=cwd,
            env=env,
            cancel_event=cancel_event,
        )
    except ToolExecutionCancelled as exc:
        raise _ProcessCancelled(exc.stdout, exc.stderr)


class TongxinCli(BaseTool):
    name: str = "tongxin_cli"
    description: str = (
        "Default all-user read-only access to Tongxin Assistant / Xin Agent account data. "
        "Use this instead of bash for xin_agent_cli.py. It can configure an existing "
        "local CLI path, authenticate to the Tongxin server with per-call login input, bootstrap the CLI from an authenticated server with SHA256 verification, run schema, and run approved "
        "read-only account/project/report/note/realtime queries only; data writes, sync, "
        "submit, approve, delete, and permission-changing commands are blocked."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, configure, auto_configure, auth, login, bootstrap, download, schema, diagnose, run.",
            },
            "script_path": {
                "type": "string",
                "description": "Optional local xin_agent_cli.py path for action=configure. If omitted, EcoreX persists the auto-discovered local path.",
            },
            "python_path": {
                "type": "string",
                "description": "Optional EcoreX-owned Python executable path for the Tongxin CLI. Prefer tools.tongxin_cli.python_path or ECOREX_TONGXIN_CLI_PYTHON_PATH.",
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
            "url": {
                "type": "string",
                "description": "Optional authenticated HTTPS download URL for action=bootstrap/download. Prefer configuring tools.tongxin_cli.bootstrap_url.",
            },
            "manifest_url": {
                "type": "string",
                "description": "Optional authenticated HTTPS manifest URL returning downloadUrl/url and sha256 for action=bootstrap/download.",
            },
            "expected_sha256": {
                "type": "string",
                "description": "Required SHA256 for direct bootstrap downloads unless a manifest provides sha256.",
            },
            "auth_token": {
                "type": "string",
                "description": "Optional bearer token for authenticated bootstrap downloads. Redacted from all outputs.",
            },
            "auth_url": {
                "type": "string",
                "description": "Optional HTTPS Tongxin login endpoint. Prefer tools.tongxin_cli.auth_url or ECOREX_TONGXIN_AUTH_URL.",
            },
            "username": {
                "type": "string",
                "description": "Per-call Tongxin username for remote auth. Not read from or written to persisted config.",
            },
            "password": {
                "type": "string",
                "description": "Per-call Tongxin password for remote auth. Never returned or persisted.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional per-call EcoreX/Codex thread id used as a remote auth context hint.",
            },
            "include_paths": {
                "type": "boolean",
                "description": "Include resolved local script path in diagnostics. Default false.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: Optional[dict] = None):
        self._script_health_cache: Dict[str, Dict[str, Any]] = {}
        self._last_script_health_failure: Dict[str, Any] = {}
        self.apply_config(config or {})

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = str(self.config.get("cwd") or os.getcwd())
        if not hasattr(self, "_script_health_cache"):
            self._script_health_cache = {}
        if not hasattr(self, "_last_script_health_failure"):
            self._last_script_health_failure = {}

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        with self._transient_call_config(args):
            action = str(args.get("action") or "").strip().lower()
            timeout = self._timeout(args.get("timeout"))
            include_paths = bool(args.get("include_paths"))
            if action in {"status", "diagnose"}:
                return ToolResult.success(self._status(include_paths=include_paths, diagnose=action == "diagnose"))
            if action == "configure":
                return self._configure(args, timeout, include_paths=include_paths)
            if action in {"auto_configure", "auto-configure", "auto_config"}:
                return self._auto_configure(args, timeout, include_paths=include_paths)
            if action in {"auth", "login"}:
                return self._remote_auth(args, timeout)
            if action in {"bootstrap", "download"}:
                return self._bootstrap(args, timeout, include_paths=include_paths)
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
            return ToolResult.fail({"status": "error", "message": "action must be one of: status, configure, auto_configure, auth, login, bootstrap, download, schema, diagnose, run"})

    def _status(self, *, include_paths: bool = False, diagnose: bool = False) -> Dict[str, Any]:
        script = self._script_path()
        auto_configurable = self._auto_configurable_script_path()
        configured_path = self._configured_script_path()
        persisted = bool(configured_path)
        configured_script = self._resolve_configurable_script(configured_path) if configured_path else None
        configured_health = self._script_health(configured_script) if configured_script else None
        configured = bool(
            configured_script
            and configured_health
            and configured_health.get("ok")
            and script
            and self._same_path(configured_script, script)
        )
        health_failure = configured_health if configured_health and not configured_health.get("ok") else None
        remote_auth_url = self._remote_auth_setting("url")
        remote_bootstrap_configured = bool(
            remote_auth_url
            or self._bootstrap_setting("manifest_url")
            or self._bootstrap_setting("url")
        )
        configuration_state = (
            str(health_failure.get("configurationState") or "dependency_failed")
            if health_failure
            else "configured"
            if configured
            else "detected_unconfigured"
            if auto_configurable
            else "detected_untrusted"
            if script
            else "remote_auth_required"
            if remote_auth_url
            else "remote_bootstrap_configured"
            if remote_bootstrap_configured
            else "missing"
        )
        payload: Dict[str, Any] = {
            "status": "success",
            "available": bool(script),
            "tool": self.name,
            "scriptName": script.name if script else DEFAULT_SCRIPT_NAME,
            "readOnly": True,
            "defaultAudience": DEFAULT_TONGXIN_SCOPE,
            "allowedCommands": list(READ_ONLY_ALLOWED_COMMANDS),
            "remoteAuthConfigured": bool(remote_auth_url),
            "remoteBootstrapAvailable": remote_bootstrap_configured,
            "remotePreferred": remote_bootstrap_configured,
            "configured": configured,
            "persistedConfig": persisted,
            "autoConfigurable": bool(auto_configurable),
            "configurationState": configuration_state,
        }
        if health_failure:
            payload["scriptHealth"] = health_failure
        if include_paths:
            payload["pathsRedacted"] = True
            payload["scriptPathRef"] = _path_ref(script)
            payload["candidatePathRefs"] = [_path_ref(path) for path in self._candidate_script_paths()]
            payload["configuredScriptPathRef"] = _path_ref(configured_path)
        if diagnose:
            python_dependency = self._python_dependency()
            payload["pythonRef"] = _path_ref(python_dependency.path)
            payload["pythonSource"] = python_dependency.source
            payload["pythonConfigured"] = bool(self._configured_python_path())
            payload["cwdRef"] = _path_ref(self.cwd)
            payload["remoteAuthSourceRef"] = self._safe_url_ref(remote_auth_url) if remote_auth_url else {"present": False}
        if health_failure:
            payload["message"] = (
                "Configured Tongxin CLI script failed the read-only data-layer health probe. "
                "Run tongxin_cli action=auto_configure to switch to a healthy local script or bootstrap a verified script."
            )
        elif not script and remote_auth_url:
            payload["message"] = (
                "Tongxin CLI is configured for remote auth/bootstrap. Run tongxin_cli action=auto_configure "
                "with per-call username/password to download and verify the read-only CLI. Local script_path is only an explicit fallback."
            )
            payload["nextAction"] = {
                "tool": "tongxin_cli",
                "action": "auto_configure",
                "requires": ["username", "password"],
                "configKey": "tools.tongxin_cli.auth_url",
            }
        elif not script:
            payload["message"] = (
                "Tongxin CLI script was not found. Configure tools.tongxin_cli.script_path "
                "or ECOREX_TONGXIN_CLI_PATH to the read-only xin_agent_cli.py path, configure "
                "tools.tongxin_cli.bootstrap_url/bootstrap_sha256, or use tools.tongxin_cli.auth_url "
                "with per-call Tongxin login input for authenticated server bootstrap."
            )
        elif not configured:
            if auto_configurable:
                payload["message"] = (
                    "Tongxin CLI script was auto-discovered but not persisted. "
                    "Run tongxin_cli action=configure or use EcoreX capability configuration to save it."
                )
            else:
                payload["message"] = (
                    "Tongxin CLI script was detected outside trusted auto-configuration roots. "
                    "Pass script_path explicitly and approve the local configuration action to use it."
                )
        return payload

    def _configure(self, args: Dict[str, Any], timeout: int = DEFAULT_TIMEOUT_SECONDS, *, include_paths: bool = False) -> ToolResult:
        explicit_path = any(args.get(key) for key in ("script_path", "scriptPath", "path"))
        candidate = (
            args.get("script_path")
            or args.get("scriptPath")
            or args.get("path")
            or self._auto_configurable_script_path(timeout)
        )
        script = self._resolve_configurable_script(candidate)
        if not script:
            payload = self._status(include_paths=include_paths)
            payload.update({
                "status": "error",
                "configured": False,
                "configurationState": payload.get("configurationState") or "missing",
                "message": (
                    "No trusted Tongxin xin_agent_cli.py script was found for automatic configuration. "
                    "Pass script_path explicitly and approve the local configuration action."
                    if not explicit_path else
                    "No local Tongxin xin_agent_cli.py script was found to configure."
                ),
            })
            return ToolResult.fail(payload)
        health = self._script_health(script, timeout)
        if not health.get("ok"):
            payload: Dict[str, Any] = {
                "status": "error",
                "available": True,
                "configured": False,
                "persistedConfig": False,
                "configurationState": health.get("configurationState") or "data_probe_failed",
                "scriptName": script.name,
                "readOnly": True,
                "defaultAudience": DEFAULT_TONGXIN_SCOPE,
                "scriptHealth": health,
                "message": (
                    "Tongxin CLI script was found, but it failed the read-only data-layer health probe. "
                    "EcoreX did not persist this script path."
                ),
            }
            if include_paths:
                payload["pathsRedacted"] = True
                payload["scriptPathRef"] = _path_ref(script)
            return ToolResult.fail(payload)
        try:
            config_path = self._persist_script_path(script)
        except Exception as exc:
            return ToolResult.fail({
                "status": "error",
                "available": True,
                "configured": False,
                "configurationState": "persist_failed",
                "message": f"Failed to persist Tongxin CLI configuration: {exc}",
            })

        payload: Dict[str, Any] = {
            "status": "success",
            "available": True,
            "configured": True,
            "persistedConfig": True,
            "configurationState": "configured",
            "tool": self.name,
            "scriptName": script.name,
            "readOnly": True,
            "defaultAudience": DEFAULT_TONGXIN_SCOPE,
            "configKey": "tools.tongxin_cli.script_path",
            "message": "Tongxin CLI read-only path configured in EcoreX.",
        }
        if include_paths:
            payload["pathsRedacted"] = True
            payload["scriptPathRef"] = _path_ref(script)
            payload["configPathRef"] = _path_ref(config_path)
        return ToolResult.success(payload)

    def _auto_configure(self, args: Dict[str, Any], timeout: int, *, include_paths: bool = False) -> ToolResult:
        status = self._status(include_paths=include_paths)
        if status.get("configured"):
            status.update({
                "status": "success",
                "autoConfigured": False,
                "message": "Tongxin CLI is already configured for read-only EcoreX access.",
            })
            return ToolResult.success(status)

        prefer_remote = self._remote_auto_config_preferred(args)
        if prefer_remote:
            bootstrapped = self._bootstrap(args, timeout, include_paths=include_paths)
            payload = bootstrapped.result if isinstance(bootstrapped.result, dict) else {"result": bootstrapped.result}
            payload["autoConfigureStep"] = "remote_authenticated_bootstrap"
            payload["previousLocalStatus"] = status
            if bootstrapped.status == "success":
                payload["autoConfigured"] = True
                return ToolResult.success(payload)
            payload["autoConfigured"] = False
            if not self._truthy_arg(args, "allow_local_fallback", "allowLocalFallback"):
                return ToolResult.fail(payload)

        configured = self._configure({}, timeout, include_paths=include_paths)
        if configured.status == "success":
            payload = configured.result if isinstance(configured.result, dict) else {"result": configured.result}
            payload["autoConfigured"] = True
            payload["autoConfigureStep"] = "local_trusted_script"
            return ToolResult.success(payload)

        bootstrapped = self._bootstrap(args, timeout, include_paths=include_paths)
        payload = bootstrapped.result if isinstance(bootstrapped.result, dict) else {"result": bootstrapped.result}
        payload["autoConfigureStep"] = "remote_authenticated_bootstrap"
        payload["previousLocalStatus"] = status
        if bootstrapped.status == "success":
            payload["autoConfigured"] = True
            return ToolResult.success(payload)
        payload["autoConfigured"] = False
        return ToolResult.fail(payload)

    @staticmethod
    def _truthy_arg(args: Dict[str, Any], *keys: str) -> bool:
        for key in keys:
            value = (args or {}).get(key)
            if isinstance(value, bool):
                return value
            if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
                return True
        return False

    def _remote_auto_config_preferred(self, args: Dict[str, Any]) -> bool:
        if self._truthy_arg(args, "prefer_local", "preferLocal"):
            return False
        if self._truthy_arg(args, "prefer_remote", "preferRemote", "remote_first", "remoteFirst"):
            return True
        explicit_remote_keys = (
            "auth_url",
            "authUrl",
            "login_url",
            "loginUrl",
            "remote_auth_url",
            "remoteAuthUrl",
            "username",
            "user",
            "account",
            "login",
            "password",
            "passwd",
            "passcode",
            "url",
            "download_url",
            "downloadUrl",
            "manifest_url",
            "manifestUrl",
            "bootstrap_url",
            "bootstrapUrl",
            "bootstrap_manifest_url",
            "bootstrapManifestUrl",
        )
        if any((args or {}).get(key) for key in explicit_remote_keys):
            return True
        return bool(
            self._remote_auth_setting("url")
            or self._bootstrap_setting("manifest_url")
            or self._bootstrap_setting("url")
        )

    def _remote_auth(self, args: Dict[str, Any], timeout: int) -> ToolResult:
        try:
            auth_payload = self._remote_auth_payload(args, timeout, require_credentials=True)
            safe_payload = self._safe_remote_auth_public_payload(auth_payload, args)
            self._persist_remote_auth_state(safe_payload)
            return ToolResult.success(safe_payload)
        except urllib.error.HTTPError as exc:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "auth_http_error",
                "httpStatus": int(getattr(exc, "code", 0) or 0),
                "message": "Tongxin remote authentication failed with an HTTP error.",
            })
        except urllib.error.URLError:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "auth_network_error",
                "message": "Tongxin remote authentication failed with a network error.",
            })
        except Exception as exc:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "auth_failed",
                "message": f"Tongxin remote authentication failed: {_sanitize(str(exc))}",
            })

    def _bootstrap(self, args: Dict[str, Any], timeout: int, *, include_paths: bool = False) -> ToolResult:
        try:
            auth_payload = self._remote_auth_payload(args, timeout, require_credentials=False)
            auth_token = self._token_from_auth_payload(auth_payload)
            embedded_manifest = self._manifest_from_auth_payload(auth_payload)
            auth_manifest_url = self._manifest_url_from_auth_payload(auth_payload)
            manifest = embedded_manifest or self._load_bootstrap_manifest(
                args,
                timeout,
                token_override=auth_token,
                manifest_url_override=auth_manifest_url,
            )
            url = (
                str(args.get("url") or args.get("download_url") or args.get("downloadUrl") or args.get("remote_url") or args.get("remoteUrl") or "").strip()
                or str(manifest.get("downloadUrl") or manifest.get("download_url") or manifest.get("url") or "").strip()
                or str(auth_payload.get("downloadUrl") or auth_payload.get("download_url") or auth_payload.get("url") or "").strip()
                or self._bootstrap_setting("url")
            )
            expected_sha = (
                str(args.get("expected_sha256") or args.get("expectedSha256") or args.get("sha256") or "").strip()
                or str(manifest.get("sha256") or manifest.get("expectedSha256") or manifest.get("expected_sha256") or "").strip()
                or str(auth_payload.get("sha256") or auth_payload.get("expectedSha256") or auth_payload.get("expected_sha256") or "").strip()
                or self._bootstrap_setting("sha256")
            )
            token = (
                str(args.get("auth_token") or args.get("authToken") or args.get("token") or "").strip()
                or auth_token
                or self._bootstrap_setting("token")
            )
            file_name = str(args.get("file_name") or args.get("fileName") or manifest.get("fileName") or manifest.get("file_name") or DEFAULT_SCRIPT_NAME).strip()

            if not url:
                if self._remote_auth_setting("url") and not auth_payload:
                    return ToolResult.fail({
                        "status": "error",
                        "configurationState": "remote_auth_required",
                        "remoteAuthConfigured": True,
                        "remoteBootstrapAvailable": True,
                        "readOnly": True,
                        "nextAction": {
                            "tool": "tongxin_cli",
                            "action": "auto_configure",
                            "requires": ["username", "password"],
                            "configKey": "tools.tongxin_cli.auth_url",
                        },
                        "message": (
                            "Tongxin remote auth is configured. Provide per-call username/password so the server can issue a bootstrap manifest/token; "
                            "EcoreX will then download and verify xin_agent_cli.py automatically."
                        ),
                    })
                return ToolResult.fail({
                    "status": "error",
                    "configurationState": "bootstrap_not_configured",
                    "message": (
                        "Tongxin CLI bootstrap URL is not configured. Set tools.tongxin_cli.bootstrap_url "
                        "or ECOREX_TONGXIN_CLI_BOOTSTRAP_URL, plus bootstrap_sha256."
                    ),
                    "readOnly": True,
                })
            if not self._is_allowed_bootstrap_url(url, args=args):
                return ToolResult.fail({
                    "status": "error",
                    "configurationState": "bootstrap_url_rejected",
                    "sourceRef": self._safe_url_ref(url),
                    "message": "Tongxin CLI bootstrap requires HTTPS, except explicit localhost test URLs.",
                })
            expected_sha = expected_sha.upper()
            if not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_sha or ""):
                return ToolResult.fail({
                    "status": "error",
                    "configurationState": "bootstrap_sha256_required",
                    "sourceRef": self._safe_url_ref(url),
                    "message": "Tongxin CLI bootstrap requires a 64-character expected_sha256 before writing a downloaded CLI.",
                })

            script_name = Path(file_name).name
            if script_name.lower() not in {name.lower() for name in SUPPORTED_SCRIPT_NAMES}:
                return ToolResult.fail({
                    "status": "error",
                    "configurationState": "bootstrap_name_rejected",
                    "sourceRef": self._safe_url_ref(url),
                    "message": "Downloaded Tongxin CLI file name must be xin_agent_cli.py or a supported variant.",
                })

            file_entries = self._bootstrap_file_entries(
                manifest,
                auth_payload,
                args=args,
                script_name=script_name,
                url=url,
                expected_sha=expected_sha,
            )
            downloaded_files: List[Dict[str, Any]] = []
            for entry in file_entries:
                entry_url = str(entry["url"])
                entry_sha = str(entry["expectedSha256"]).upper()
                entry_path = str(entry["relativePath"])
                data = self._download_bootstrap_bytes(entry_url, token=token, timeout=timeout)
                actual_sha = hashlib.sha256(data).hexdigest().upper()
                if actual_sha != entry_sha:
                    return ToolResult.fail({
                        "status": "error",
                        "configurationState": "bootstrap_sha256_mismatch",
                        "sourceRef": self._safe_url_ref(entry_url),
                        "fileRef": self._safe_text_ref(entry_path),
                        "expectedSha256": entry_sha,
                        "actualSha256": actual_sha,
                        "message": "Downloaded Tongxin CLI SHA256 did not match the expected value; file was not installed.",
                    })
                self._validate_bootstrap_python(data, file_name=entry_path)
                downloaded_files.append({
                    "relativePath": entry_path,
                    "data": data,
                    "sha256": actual_sha,
                    "size": len(data),
                    "sourceRef": self._safe_url_ref(entry_url),
                    "isMain": bool(entry.get("isMain")),
                })
            target_dir = self._bootstrap_target_dir()
            script = target_dir / script_name
            target_dir.mkdir(parents=True, exist_ok=True)
            package_sha = hashlib.sha256(("".join(item["sha256"] for item in downloaded_files) + str(time.time())).encode()).hexdigest()
            install_nonce = package_sha[:12]
            install_plan: List[Dict[str, Any]] = []
            for item in downloaded_files:
                relative_path = self._safe_bootstrap_relative_path(str(item["relativePath"]))
                target = (target_dir / relative_path).resolve()
                if not (self._same_path(target, target_dir.resolve()) or self._path_within(target, target_dir.resolve())):
                    raise ValueError("Tongxin bootstrap file path escapes the target directory.")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.{install_nonce}.tmp")
                backup = target.with_name(f".{target.name}.{install_nonce}.bak")
                tmp.write_bytes(item["data"])
                try:
                    tmp.chmod(0o600)
                except Exception:
                    pass
                install_plan.append({
                    "target": target,
                    "tmp": tmp,
                    "backup": backup,
                    "hadExisting": target.exists(),
                    "relativePath": relative_path.as_posix(),
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "isMain": item["isMain"],
                })
            for item in install_plan:
                target = item["target"]
                backup = item["backup"]
                if item["hadExisting"]:
                    os.replace(target, backup)
                os.replace(item["tmp"], target)
            models_health = self._bootstrap_models_database_health(target_dir, script)
            if not models_health.get("ok"):
                health = models_health
            else:
                health = self._script_health(script, timeout)
            if not health.get("ok"):
                self._restore_bootstrap_install_plan(install_plan)
                main_file = next((item for item in downloaded_files if item.get("isMain")), downloaded_files[0])
                actual_sha = str(main_file["sha256"])
                total_size = sum(int(item.get("size") or 0) for item in downloaded_files)
                payload: Dict[str, Any] = {
                    "status": "error",
                    "available": True,
                    "configured": False,
                    "persistedConfig": False,
                    "downloaded": True,
                    "configurationState": health.get("configurationState") or "data_probe_failed",
                    "tool": self.name,
                    "scriptName": script.name,
                    "readOnly": True,
                    "defaultAudience": DEFAULT_TONGXIN_SCOPE,
                    "sourceRef": main_file.get("sourceRef") or self._safe_url_ref(url),
                    "sha256": actual_sha,
                    "size": total_size,
                    "installedFileCount": len(downloaded_files),
                    "scriptHealth": health,
                    "message": (
                        "Downloaded Tongxin CLI verified by SHA256, but failed the read-only data-layer health probe. "
                        "EcoreX did not persist this script path."
                    ),
                }
                if include_paths:
                    payload["pathsRedacted"] = True
                    payload["scriptPathRef"] = _path_ref(script)
                return ToolResult.fail(payload)
            for item in install_plan:
                backup = item["backup"]
                try:
                    backup.unlink()
                except Exception:
                    pass
            main_file = next((item for item in downloaded_files if item.get("isMain")), downloaded_files[0])
            actual_sha = str(main_file["sha256"])
            total_size = sum(int(item.get("size") or 0) for item in downloaded_files)
            installed_files = [
                {
                    "name": Path(str(item["relativePath"])).name,
                    "pathHash": hashlib.sha256(str(item["relativePath"]).encode("utf-8", errors="replace")).hexdigest()[:16],
                    "sha256": str(item["sha256"]),
                    "size": int(item["size"]),
                    "main": bool(item["isMain"]),
                }
                for item in install_plan
            ]
            config_path = self._persist_script_path(script)
            payload: Dict[str, Any] = {
                "status": "success",
                "available": True,
                "configured": True,
                "persistedConfig": True,
                "downloaded": True,
                "configurationState": "configured",
                "tool": self.name,
                "scriptName": script.name,
                "readOnly": True,
                "defaultAudience": DEFAULT_TONGXIN_SCOPE,
                "configKey": "tools.tongxin_cli.script_path",
                "sourceRef": main_file.get("sourceRef") or self._safe_url_ref(url),
                "sha256": actual_sha,
                "size": total_size,
                "installedFileCount": len(downloaded_files),
                "installedFiles": installed_files,
                "remoteAuthenticated": bool(auth_payload),
                "permission": self._permission_snapshot(auth_payload),
                "message": "Tongxin CLI downloaded from authenticated source, verified by SHA256, and configured for read-only EcoreX access.",
            }
            if auth_payload:
                self._persist_remote_auth_state(self._safe_remote_auth_public_payload(auth_payload, args))
            if include_paths:
                payload["pathsRedacted"] = True
                payload["scriptPathRef"] = _path_ref(script)
                payload["configPathRef"] = _path_ref(config_path)
            return ToolResult.success(payload)
        except urllib.error.HTTPError as exc:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "bootstrap_http_error",
                "httpStatus": int(getattr(exc, "code", 0) or 0),
                "message": "Tongxin CLI bootstrap download failed with an HTTP error.",
            })
        except urllib.error.URLError:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "bootstrap_network_error",
                "message": "Tongxin CLI bootstrap download failed with a network error.",
            })
        except Exception as exc:
            return ToolResult.fail({
                "status": "error",
                "configurationState": "bootstrap_failed",
                "message": f"Tongxin CLI bootstrap failed: {_sanitize(str(exc))}",
            })

    def _run_cli(self, cli_args: List[str], timeout: int, *, include_paths: bool = False) -> ToolResult:
        script = self._execution_script_path()
        if not script:
            payload = self._status(include_paths=include_paths)
            payload["status"] = "error"
            if payload.get("available"):
                payload["message"] = (
                    "Tongxin CLI script is detected but is not configured or trusted for automatic execution. "
                    "Use tongxin_cli action=configure for trusted local installs, or pass script_path explicitly and approve configuration."
                )
            return ToolResult.fail(payload)
        if self._requires_data_health(cli_args):
            health = self._script_health(script, timeout)
            if not health.get("ok"):
                payload: Dict[str, Any] = {
                    "status": "error",
                    "available": True,
                    "configured": False,
                    "configurationState": health.get("configurationState") or "data_probe_failed",
                    "scriptName": script.name,
                    "command": self._display_command(cli_args),
                    "readOnly": True,
                    "defaultAudience": DEFAULT_TONGXIN_SCOPE,
                    "scriptHealth": health,
                    "message": (
                        "Configured Tongxin CLI script failed the read-only data-layer health probe; "
                        "run tongxin_cli action=auto_configure to switch to a healthy script or bootstrap a verified script."
                    ),
                }
                if include_paths:
                    payload["pathsRedacted"] = True
                    payload["scriptPathRef"] = _path_ref(script)
                return ToolResult.fail(payload)
        env = self._cli_env()
        executor = ToolExecutionEnvironment(tool_name=self.name, base_env=env, cwd=str(script.parent))
        python_dependency = executor.resolve_python()
        if not python_dependency.available:
            payload = executor.provider.missing_dependency(python_dependency, required_by=self.name)
            payload.update({
                "command": self._display_command(cli_args),
                "readOnly": True,
                "defaultAudience": DEFAULT_TONGXIN_SCOPE,
            })
            return ToolResult.fail(payload)
        command = self._script_command(python_dependency.path, script, cli_args)
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
            payload["pathsRedacted"] = True
            payload["scriptPathRef"] = _path_ref(script)
        if result.returncode != 0:
            return ToolResult.fail(payload)
        return ToolResult.success(payload)

    @staticmethod
    def _requires_data_health(cli_args: List[str]) -> bool:
        tokens = _lower_tokens(cli_args)
        if not tokens:
            return False
        return tokens[0] not in {"schema", "help", "--help", "-h"}

    def _cli_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        configured_python = self._configured_python_path()
        if configured_python:
            env["ECOREX_PYTHON_PATH"] = configured_python
        # The external Tongxin script owns its adjacent modules such as
        # config.py/models.py. EcoreX PYTHONPATH entries can shadow them.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _python_dependency(self):
        env = self._cli_env()
        return ToolExecutionEnvironment(tool_name=self.name, base_env=env, cwd=self.cwd).resolve_python()

    @staticmethod
    def _script_command(python_path: str, script: Path, cli_args: List[str]) -> List[str]:
        return [str(python_path), "-c", TONGXIN_RUNPY_SHIM, str(script), *[str(item) for item in cli_args]]

    def _script_health(self, script: Optional[Path], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
        if not script:
            return {
                "ok": False,
                "configurationState": "missing",
                "message": "Tongxin CLI script was not found.",
            }
        cache_key = self._script_health_cache_key(script)
        cached = self._script_health_cache.get(cache_key)
        if cached:
            return dict(cached)

        probe_timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_SECONDS), 8))
        env = self._cli_env()
        executor = ToolExecutionEnvironment(tool_name=self.name, base_env=env, cwd=str(script.parent))
        python_dependency = executor.resolve_python()
        if not python_dependency.available:
            payload = self._script_health_failure(script, "python", None, "EcoreX-owned Python runtime is missing.")
            payload["missingDependency"] = executor.provider.missing_dependency(python_dependency, required_by=self.name)
            payload["configurationState"] = "dependency_missing"
            self._script_health_cache[cache_key] = payload
            self._last_script_health_failure = dict(payload)
            return dict(payload)
        probes: List[Tuple[str, Tuple[str, ...]]] = [
            ("schema", ("schema",)),
            ("data", TONGXIN_DATA_HEALTH_PROBE_ARGS),
        ]
        for phase, probe_args in probes:
            command = self._script_command(python_dependency.path, script, list(probe_args))
            try:
                result = _run_process(
                    command,
                    timeout=probe_timeout,
                    cwd=str(script.parent),
                    env=env,
                    cancel_event=getattr(self, "cancel_event", None),
                )
            except subprocess.TimeoutExpired as exc:
                output = _sanitize((exc.output or "") + ("\n" + exc.stderr if exc.stderr else ""))
                payload = self._script_health_failure(script, phase, None, output, timed_out=True)
                self._script_health_cache[cache_key] = payload
                self._last_script_health_failure = dict(payload)
                return dict(payload)
            except _ProcessCancelled as exc:
                output = _sanitize((exc.stdout or "") + ("\n" + exc.stderr if exc.stderr else ""))
                payload = self._script_health_failure(script, phase, None, output, cancelled=True)
                self._script_health_cache[cache_key] = payload
                self._last_script_health_failure = dict(payload)
                return dict(payload)
            except Exception as exc:
                payload = self._script_health_failure(script, phase, None, _sanitize(str(exc)))
                self._script_health_cache[cache_key] = payload
                self._last_script_health_failure = dict(payload)
                return dict(payload)

            output = _sanitize((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
            if result.returncode != 0:
                payload = self._script_health_failure(script, phase, result.returncode, output)
                self._script_health_cache[cache_key] = payload
                self._last_script_health_failure = dict(payload)
                return dict(payload)

        payload = {
            "ok": True,
            "configurationState": "healthy",
            "scriptName": script.name,
            "schemaProbe": "passed",
            "dataProbe": "passed",
            "dataProbeCommand": self._display_command(list(TONGXIN_DATA_HEALTH_PROBE_ARGS)),
        }
        self._script_health_cache[cache_key] = payload
        return dict(payload)

    @staticmethod
    def _script_health_cache_key(script: Path) -> str:
        try:
            stat = script.stat()
            raw = f"{script.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
        except Exception:
            raw = str(script)
        return raw

    def _script_health_failure(
        self,
        script: Path,
        phase: str,
        exit_code: Optional[int],
        output: str,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> Dict[str, Any]:
        lowered = (output or "").lower()
        if "models" in lowered and "database" in lowered:
            state = "dependency_failed"
            message = (
                "Tongxin CLI script imports an incompatible models module; "
                "the read-only data-layer health probe could not enter the data layer."
            )
        elif cancelled:
            state = "health_probe_cancelled"
            message = "Tongxin CLI health probe was cancelled before the script could be validated."
        elif timed_out:
            state = f"{phase}_probe_timeout"
            message = "Tongxin CLI health probe timed out before the script could be validated."
        elif phase == "schema":
            state = "schema_probe_failed"
            message = "Tongxin CLI script failed the schema health probe."
        else:
            state = "data_probe_failed"
            message = "Tongxin CLI script failed the read-only data-layer health probe."
        return {
            "ok": False,
            "configurationState": state,
            "scriptName": script.name,
            "probe": phase,
            "exitCode": exit_code,
            "dataProbeCommand": self._display_command(list(TONGXIN_DATA_HEALTH_PROBE_ARGS)),
            "output": _truncate(output or "", 1200),
            "message": message,
        }

    def _candidate_script_paths(self) -> List[Path]:
        configured = self._configured_script_path()
        raw: List[Any] = [
            configured,
            *self._env_script_path_values(),
            *self._trusted_auto_config_roots(),
            Path(self.cwd),
        ]
        paths: List[Path] = []
        seen = set()
        for item in raw:
            if not item:
                continue
            for path in self._expand_script_candidates(item):
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
        file_cfg = self._read_runtime_config()
        file_tools = file_cfg.get("tools") if isinstance(file_cfg.get("tools"), dict) else {}
        file_tongxin = file_tools.get("tongxin_cli") if isinstance(file_tools, dict) else None
        if isinstance(file_tongxin, dict):
            for key in ("script_path", "scriptPath", "path"):
                value = file_tongxin.get(key)
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

    def _configured_python_path(self) -> str:
        for key in PYTHON_CONFIG_KEYS:
            value = self.config.get(key)
            if value:
                return str(value)
        file_cfg = self._read_runtime_config()
        file_tools = file_cfg.get("tools") if isinstance(file_cfg.get("tools"), dict) else {}
        file_tongxin = file_tools.get("tongxin_cli") if isinstance(file_tools, dict) else None
        if isinstance(file_tongxin, dict):
            for key in PYTHON_CONFIG_KEYS:
                value = file_tongxin.get(key)
                if value:
                    return str(value)
        try:
            from config import conf

            tools = conf().get("tools", {})
            cfg = tools.get("tongxin_cli") if isinstance(tools, dict) else None
            if isinstance(cfg, dict):
                for key in PYTHON_CONFIG_KEYS:
                    value = cfg.get(key)
                    if value:
                        return str(value)
        except Exception:
            pass
        for key in ("ECOREX_TONGXIN_CLI_PYTHON_PATH", "ECOREX_TONGXIN_PYTHON_PATH"):
            value = os.environ.get(key)
            if value:
                return str(value)
        return ""

    def _script_path(self) -> Optional[Path]:
        for path in self._candidate_script_paths():
            script = self._resolve_configurable_script(path)
            if script:
                return script
        return None

    def _execution_script_path(self) -> Optional[Path]:
        raw: List[Any] = [
            self._configured_script_path(),
            *self._env_script_path_values(),
            *self._trusted_auto_config_roots(),
        ]
        for item in raw:
            if not item:
                continue
            script = self._resolve_configurable_script(item)
            if script:
                return script
        return None

    def _auto_configurable_script_path(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Optional[Path]:
        raw: List[Any] = [
            *self._env_script_path_values(),
            *self._trusted_auto_config_roots(),
        ]
        for item in raw:
            if not item:
                continue
            script = self._resolve_configurable_script(item)
            if script and (self._matches_env_script_path(script) or self._is_trusted_auto_config_path(script)):
                health = self._script_health(script, timeout)
                if health.get("ok"):
                    return script
                self._last_script_health_failure = dict(health)
        return None

    def _resolve_configurable_script(self, candidate: Any) -> Optional[Path]:
        if not candidate:
            return None
        for path in self._expand_script_candidates(candidate):
            try:
                resolved = path.expanduser().resolve()
            except Exception:
                resolved = path.expanduser()
            try:
                if (
                    resolved.is_file()
                    and resolved.suffix.lower() == ".py"
                    and resolved.name.lower() in {name.lower() for name in SUPPORTED_SCRIPT_NAMES}
                ):
                    return resolved
            except Exception:
                continue
        return None

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except Exception:
            return str(left).lower() == str(right).lower()

    def _env_script_path_values(self) -> List[str]:
        values = []
        provider = ToolExecutionEnvironment(tool_name=self.name).provider
        for key in ("ECOREX_TONGXIN_CLI_PATH",):
            value = os.environ.get(key)
            if value and provider.classify_path(value) in {"ecorex-bundled", "ecorex-state"}:
                values.append(value)
        return values

    def _trusted_auto_config_roots(self) -> List[Path]:
        runtime_root = Path(__file__).resolve().parents[3]
        roots: List[Path] = [
            runtime_root / "tools" / "tongxin",
            runtime_root,
        ]
        try:
            state_root = ToolExecutionEnvironment(tool_name=self.name).provider.state_root
            roots.extend([
                state_root / "tools" / "tongxin",
                state_root,
            ])
        except Exception:
            pass

        deduped: List[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = root.expanduser().resolve()
            except Exception:
                resolved = root.expanduser()
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(resolved)
        return deduped

    def _bootstrap_setting(self, group: str) -> str:
        keys = BOOTSTRAP_CONFIG_KEYS.get(group, ())
        for key in keys:
            value = self.config.get(key)
            if value:
                return str(value).strip()
        file_cfg = self._read_runtime_config()
        file_tools = file_cfg.get("tools") if isinstance(file_cfg.get("tools"), dict) else {}
        file_tongxin = file_tools.get("tongxin_cli") if isinstance(file_tools, dict) else None
        if isinstance(file_tongxin, dict):
            for key in keys:
                value = file_tongxin.get(key)
                if value:
                    return str(value).strip()
        env_map = {
            "url": ("ECOREX_TONGXIN_CLI_BOOTSTRAP_URL", "ECOREX_TONGXIN_CLI_DOWNLOAD_URL"),
            "manifest_url": ("ECOREX_TONGXIN_CLI_BOOTSTRAP_MANIFEST_URL",),
            "sha256": ("ECOREX_TONGXIN_CLI_BOOTSTRAP_SHA256", "ECOREX_TONGXIN_CLI_SHA256"),
            "token": ("ECOREX_TONGXIN_CLI_BOOTSTRAP_TOKEN", "ECOREX_TONGXIN_CLI_TOKEN"),
            "target_dir": ("ECOREX_TONGXIN_CLI_BOOTSTRAP_DIR",),
        }
        for key in env_map.get(group, ()):
            value = os.environ.get(key)
            if value:
                return str(value).strip()
        return ""

    def _remote_auth_setting(self, group: str) -> str:
        keys = REMOTE_AUTH_CONFIG_KEYS.get(group, ())
        for key in keys:
            value = self.config.get(key)
            if value:
                return str(value).strip()
        file_cfg = self._read_runtime_config()
        file_tools = file_cfg.get("tools") if isinstance(file_cfg.get("tools"), dict) else {}
        file_tongxin = file_tools.get("tongxin_cli") if isinstance(file_tools, dict) else None
        if isinstance(file_tongxin, dict):
            for key in keys:
                value = file_tongxin.get(key)
                if value:
                    return str(value).strip()
        env_map = {
            "url": ("ECOREX_TONGXIN_AUTH_URL", "ECOREX_TONGXIN_LOGIN_URL"),
            "client_key": ("ECOREX_TONGXIN_CLIENT_EVENT_KEY", "ECOREX_CLIENT_EVENT_KEY"),
        }
        for key in env_map.get(group, ()):
            value = os.environ.get(key)
            if value:
                return str(value).strip()
        return ""

    def _remote_auth_payload(self, args: Dict[str, Any], timeout: int, *, require_credentials: bool) -> Dict[str, Any]:
        auth_url = (
            str(args.get("auth_url") or args.get("authUrl") or args.get("login_url") or args.get("loginUrl") or args.get("remote_auth_url") or args.get("remoteAuthUrl") or "").strip()
            or self._remote_auth_setting("url")
        )
        username = (
            str(args.get("username") or args.get("user") or args.get("account") or args.get("login") or "").strip()
        )
        password = (
            str(args.get("password") or args.get("passwd") or args.get("passcode") or "").strip()
        )
        if not auth_url:
            if require_credentials:
                raise ValueError("Tongxin remote auth URL is not configured.")
            return {}
        if not self._is_allowed_bootstrap_url(auth_url, args=args):
            raise ValueError("Tongxin remote auth URL must use HTTPS, except explicit localhost test URLs.")
        if not username or not password:
            if require_credentials:
                raise ValueError("Tongxin username and password are required for remote auth.")
            return {}

        thread_id = (
            str(args.get("thread_id") or args.get("threadId") or args.get("auth_thread_id") or args.get("authThreadId") or "").strip()
        )
        body = {
            "username": username,
            "password": password,
            "threadId": thread_id,
            "scope": DEFAULT_TONGXIN_SCOPE,
            "readOnly": True,
            "visibility": "permission-visible-data-only",
        }
        response = self._post_json(auth_url, body, timeout=timeout, client_key=self._remote_auth_client_key(args))
        if not isinstance(response, dict):
            raise ValueError("Tongxin remote auth response must be a JSON object.")
        if response.get("ok") is False or str(response.get("status") or "").lower() in {"error", "failed", "fail"}:
            message = str(response.get("message") or response.get("error") or "Tongxin remote auth rejected login input.")
            raise ValueError(_sanitize(message))
        response["_ecorexAuthUrlRef"] = self._safe_url_ref(auth_url)
        response["_ecorexUsernameRef"] = self._safe_text_ref(username)
        response["_ecorexThreadRef"] = self._safe_text_ref(thread_id)
        response["_ecorexReadOnly"] = True
        return response

    def _remote_auth_client_key(self, args: Dict[str, Any]) -> str:
        explicit = str(
            (args or {}).get("client_event_key")
            or (args or {}).get("clientEventKey")
            or (args or {}).get("client_key")
            or (args or {}).get("clientKey")
            or ""
        ).strip()
        return explicit or self._remote_auth_setting("client_key") or DEFAULT_CLIENT_EVENT_KEY

    def _post_json(self, url: str, payload: Dict[str, Any], *, timeout: int, client_key: str = "") -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "EcoreX-Tongxin-Auth/0.2.5",
        }
        if client_key:
            headers["X-EcoreX-Client-Key"] = client_key
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(1, min(int(timeout), 300))) as response:
            raw = response.read(512 * 1024)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8-sig"))

    @staticmethod
    def _token_from_auth_payload(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("bootstrapToken", "bootstrap_token", "accessToken", "access_token", "token", "authToken", "auth_token"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        data = payload.get("data")
        if isinstance(data, dict):
            return TongxinCli._token_from_auth_payload(data)
        return ""

    @staticmethod
    def _manifest_from_auth_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        manifest = payload.get("manifest") or payload.get("bootstrapManifest") or payload.get("bootstrap_manifest")
        if isinstance(manifest, dict):
            return manifest
        data = payload.get("data")
        if isinstance(data, dict):
            return TongxinCli._manifest_from_auth_payload(data)
        return {}

    @staticmethod
    def _manifest_url_from_auth_payload(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("manifestUrl", "manifest_url", "bootstrapManifestUrl", "bootstrap_manifest_url"):
            value = payload.get(key)
            if value:
                return str(value).strip()
        data = payload.get("data")
        if isinstance(data, dict):
            return TongxinCli._manifest_url_from_auth_payload(data)
        return ""

    def _safe_remote_auth_public_payload(self, payload: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        token_present = bool(self._token_from_auth_payload(payload))
        manifest = self._manifest_from_auth_payload(payload)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        manifest_url = (
            str(payload.get("manifestUrl") or payload.get("manifest_url") or "").strip()
            or str(data.get("manifestUrl") or data.get("manifest_url") or "").strip()
        )
        safe: Dict[str, Any] = {
            "status": "success",
            "authenticated": True,
            "configurationState": "remote_authenticated",
            "tool": self.name,
            "readOnly": True,
            "defaultAudience": DEFAULT_TONGXIN_SCOPE,
            "remoteAuthenticated": True,
            "tokenReceived": token_present,
            "bootstrapManifestAvailable": bool(manifest or manifest_url),
            "permission": self._permission_snapshot(payload),
            "sourceRef": payload.get("_ecorexAuthUrlRef") or self._safe_url_ref(
                str(args.get("auth_url") or args.get("authUrl") or self._remote_auth_setting("url") or "")
            ),
            "usernameRef": payload.get("_ecorexUsernameRef") or self._safe_text_ref(
                str(args.get("username") or args.get("user") or "")
            ),
            "threadRef": payload.get("_ecorexThreadRef") or self._safe_text_ref(
                str(args.get("thread_id") or args.get("threadId") or "")
            ),
            "message": "Tongxin remote auth succeeded; only permission-visible read-only data may be disclosed.",
        }
        return safe

    def _permission_snapshot(self, payload: Any) -> Dict[str, Any]:
        return {
            "readOnly": bool(payload.get("_ecorexReadOnly", True)) if isinstance(payload, dict) else True,
            "scope": DEFAULT_TONGXIN_SCOPE,
            "visibility": "permission-visible-data-only",
        }

    @staticmethod
    def _scrub_persisted_login_fields(tongxin: Any) -> None:
        if not isinstance(tongxin, dict):
            return
        for key in (
            "username",
            "user",
            "account",
            "login",
            "login_name",
            "loginName",
            "password",
            "passwd",
            "passcode",
            "thread_id",
            "threadId",
            "auth_thread_id",
            "authThreadId",
            "token",
            "auth_token",
            "authToken",
            "bootstrap_token",
            "bootstrapToken",
            "ticket",
            "bootstrapTicket",
            "bootstrap_ticket",
        ):
            tongxin.pop(key, None)

    def _persist_remote_auth_state(self, safe_payload: Dict[str, Any]) -> None:
        try:
            config_path = self._runtime_config_path()
            data = self._read_runtime_config()
            tools = data.get("tools")
            if not isinstance(tools, dict):
                tools = {}
                data["tools"] = tools
            tongxin = tools.get("tongxin_cli")
            if not isinstance(tongxin, dict):
                tongxin = {}
                tools["tongxin_cli"] = tongxin
            self._scrub_persisted_login_fields(tongxin)
            self._scrub_persisted_login_fields(self.config)
            tongxin["read_only"] = True
            tongxin["last_auth"] = {
                "authenticated": bool(safe_payload.get("authenticated")),
                "read_only": True,
                "default_audience": DEFAULT_TONGXIN_SCOPE,
                "source_ref": safe_payload.get("sourceRef"),
                "username_ref": safe_payload.get("usernameRef"),
                "thread_ref": safe_payload.get("threadRef"),
                "permission": safe_payload.get("permission"),
                "updated_at": int(time.time()),
            }
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.debug(f"[TongxinCli] remote auth state persist skipped: {exc}")

    def _load_bootstrap_manifest(
        self,
        args: Dict[str, Any],
        timeout: int,
        *,
        token_override: str = "",
        manifest_url_override: str = "",
    ) -> Dict[str, Any]:
        manifest_url = (
            str(args.get("manifest_url") or args.get("manifestUrl") or args.get("bootstrap_manifest_url") or args.get("bootstrapManifestUrl") or "").strip()
            or manifest_url_override
            or self._bootstrap_setting("manifest_url")
        )
        if not manifest_url:
            return {}
        if not self._is_allowed_bootstrap_url(manifest_url, args=args):
            raise ValueError("Tongxin bootstrap manifest URL must use HTTPS, except explicit localhost test URLs.")
        token = (
            str(args.get("auth_token") or args.get("authToken") or args.get("token") or "").strip()
            or token_override
            or self._bootstrap_setting("token")
        )
        data = self._download_bootstrap_bytes(manifest_url, token=token, timeout=timeout, max_bytes=256 * 1024)
        payload = json.loads(data.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Tongxin bootstrap manifest must be a JSON object.")
        return payload

    def _download_bootstrap_bytes(self, url: str, *, token: str = "", timeout: int = DEFAULT_TIMEOUT_SECONDS, max_bytes: int = MAX_BOOTSTRAP_BYTES) -> bytes:
        headers = {
            "Accept": "application/json, text/x-python, text/plain, */*",
            "User-Agent": "EcoreX-Tongxin-Bootstrap/0.2.5",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        chunks: List[bytes] = []
        total = 0
        with urllib.request.urlopen(request, timeout=max(1, min(int(timeout), 300))) as response:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Tongxin CLI bootstrap payload is larger than the allowed limit.")
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise ValueError("Tongxin CLI bootstrap payload is empty.")
        return data

    def _bootstrap_file_entries(
        self,
        manifest: Dict[str, Any],
        auth_payload: Dict[str, Any],
        *,
        args: Dict[str, Any],
        script_name: str,
        url: str,
        expected_sha: str,
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = [
            {
                "relativePath": script_name,
                "url": url,
                "expectedSha256": expected_sha,
                "isMain": True,
            }
        ]
        raw_files: List[Any] = []
        for payload in (manifest, auth_payload):
            for key in ("files", "bootstrapFiles", "bootstrap_files", "packageFiles", "package_files"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, list):
                    raw_files.extend(value)
        seen = {script_name.replace("\\", "/").lower()}
        for raw in raw_files:
            if not isinstance(raw, dict):
                continue
            relative = str(
                raw.get("path")
                or raw.get("relativePath")
                or raw.get("relative_path")
                or raw.get("fileName")
                or raw.get("file_name")
                or ""
            ).strip()
            if not relative:
                continue
            safe_relative = self._safe_bootstrap_relative_path(relative).as_posix()
            key = safe_relative.lower()
            if key in seen:
                continue
            file_url = str(raw.get("downloadUrl") or raw.get("download_url") or raw.get("url") or "").strip()
            file_sha = str(raw.get("sha256") or raw.get("expectedSha256") or raw.get("expected_sha256") or "").strip().upper()
            if not file_url:
                raise ValueError("Tongxin bootstrap package file is missing downloadUrl.")
            if not self._is_allowed_bootstrap_url(file_url, args=args):
                raise ValueError("Tongxin bootstrap package file URL must use HTTPS, except explicit localhost test URLs.")
            if not re.fullmatch(r"[A-Fa-f0-9]{64}", file_sha or ""):
                raise ValueError("Tongxin bootstrap package file is missing a 64-character sha256.")
            entries.append({
                "relativePath": safe_relative,
                "url": file_url,
                "expectedSha256": file_sha,
                "isMain": False,
            })
            seen.add(key)
        return entries

    @staticmethod
    def _safe_bootstrap_relative_path(value: str) -> Path:
        raw = str(value or "").replace("\\", "/").strip().lstrip("/")
        path = Path(raw)
        parts = path.parts
        if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Tongxin bootstrap package contains an unsafe file path.")
        if len(parts) > 4:
            raise ValueError("Tongxin bootstrap package file path is too deep.")
        for part in parts:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", part):
                raise ValueError("Tongxin bootstrap package file path contains unsupported characters.")
        if path.suffix.lower() != ".py":
            raise ValueError("Tongxin bootstrap package may only install Python source files.")
        return path

    @staticmethod
    def _restore_bootstrap_install_plan(install_plan: List[Dict[str, Any]]) -> None:
        for item in reversed(install_plan):
            target = item["target"]
            backup = item["backup"]
            tmp = item["tmp"]
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            try:
                if item["hadExisting"] and backup.exists():
                    os.replace(backup, target)
                elif backup.exists():
                    backup.unlink()
            except Exception:
                pass
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _bootstrap_models_database_health(self, target_dir: Path, script: Path) -> Dict[str, Any]:
        try:
            source = script.read_text(encoding="utf-8-sig")
        except Exception as exc:
            return self._script_health_failure(script, "package", None, f"Unable to read bootstrap script: {_sanitize(str(exc))}")
        required = self._required_models_exports(source)
        if not required:
            return {"ok": True}
        provided = self._provided_models_exports(target_dir)
        missing = sorted(required - provided)
        if not missing:
            return {"ok": True}
        detail = (
            "Tongxin remote bootstrap package is missing models exports required by xin_agent_cli.py: "
            + ", ".join(f"models.{name}" for name in missing)
            + ". Ask the CLI provider to update the remote bootstrap package so models.py or models/__init__.py exposes database/DATABASE."
        )
        return self._script_health_failure(script, "package", None, detail)

    @staticmethod
    def _required_models_exports(source: str) -> set[str]:
        required: set[str] = set()
        if re.search(r"\bmodels\.DATABASE\b", source):
            required.add("DATABASE")
        if re.search(r"\bmodels\.database\b", source):
            required.add("database")
        for match in re.finditer(r"(?m)^\s*from\s+models\s+import\s+([^\n#]+)", source):
            names = [part.strip().split(" as ", 1)[0].strip() for part in match.group(1).split(",")]
            for name in names:
                if name in {"database", "DATABASE"}:
                    required.add(name)
        return required

    @classmethod
    def _provided_models_exports(cls, target_dir: Path) -> set[str]:
        provided: set[str] = set()
        for path in (target_dir / "models.py", target_dir / "models" / "__init__.py"):
            if path.is_file():
                provided.update(cls._python_top_level_exports(path))
        if (target_dir / "models" / "database.py").is_file():
            provided.add("database")
        return provided

    @staticmethod
    def _python_top_level_exports(path: Path) -> set[str]:
        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except Exception:
            return set()
        names: set[str] = set()
        for node in tree.body:
            targets: List[ast.AST] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.Assign):
                targets.extend(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets.append(node.target)
            elif isinstance(node, ast.AugAssign):
                targets.append(node.target)
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        return names

    def _validate_bootstrap_python(self, data: bytes, *, file_name: str = DEFAULT_SCRIPT_NAME) -> None:
        if b"\x00" in data[:4096]:
            raise ValueError("Tongxin CLI bootstrap payload is not a text Python file.")
        try:
            source = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Tongxin CLI bootstrap payload must be UTF-8 Python source.") from exc
        compile(source, file_name, "exec")

    def _bootstrap_target_dir(self) -> Path:
        configured = self._bootstrap_setting("target_dir")
        target = Path(configured).expanduser() if configured else self._default_bootstrap_target_dir()
        trusted = any(self._path_within(target, root) or self._same_path(target, root) for root in self._trusted_auto_config_roots())
        if not trusted:
            raise ValueError("Tongxin CLI bootstrap target directory is outside trusted Tongxin roots.")
        return target

    def _default_bootstrap_target_dir(self) -> Path:
        try:
            state_root = ToolExecutionEnvironment(tool_name=self.name).provider.state_root
            return state_root / "tools" / "tongxin"
        except Exception:
            return Path(__file__).resolve().parents[3] / "tools" / "tongxin"

    @staticmethod
    def _is_allowed_bootstrap_url(url: str, *, args: Dict[str, Any]) -> bool:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        if parsed.scheme == "https" and parsed.netloc:
            return True
        if parsed.scheme == "http" and str(parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}:
            return bool(args.get("allow_insecure_localhost") or os.environ.get("ECOREX_TONGXIN_ALLOW_INSECURE_LOCALHOST_BOOTSTRAP") == "1")
        return False

    @staticmethod
    def _safe_url_ref(url: str) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        host = str(parsed.hostname or "")
        path = str(parsed.path or "")
        return {
            "scheme": parsed.scheme or "",
            "hostHash": hashlib.sha256(host.encode("utf-8", errors="replace")).hexdigest()[:16] if host else "",
            "pathHash": hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()[:16] if path else "",
        }

    @staticmethod
    def _safe_text_ref(value: str) -> Dict[str, Any]:
        raw = str(value or "").strip()
        return {
            "present": bool(raw),
            "hash": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16] if raw else "",
        }

    def _matches_env_script_path(self, script: Path) -> bool:
        for value in self._env_script_path_values():
            for candidate in self._expand_script_candidates(value):
                resolved = self._resolve_configurable_script(candidate)
                if resolved and self._same_path(resolved, script):
                    return True
        return False

    def _is_trusted_auto_config_path(self, script: Path) -> bool:
        for root in self._trusted_auto_config_roots():
            if self._path_within(script, root):
                return True
        return False

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            resolved_path = path.expanduser().resolve()
            resolved_root = root.expanduser().resolve()
            resolved_path.relative_to(resolved_root)
            return True
        except Exception:
            try:
                path_text = os.path.normcase(os.path.abspath(str(path)))
                root_text = os.path.normcase(os.path.abspath(str(root)))
                return os.path.commonpath([path_text, root_text]) == root_text
            except Exception:
                return False

    @staticmethod
    def _expand_script_candidates(value: Any) -> List[Path]:
        path = Path(str(value)).expanduser()
        if path.name.lower() in {name.lower() for name in SUPPORTED_SCRIPT_NAMES}:
            return [path]
        candidates = [path / name for name in SUPPORTED_SCRIPT_NAMES]
        if path.suffix.lower() == ".py":
            candidates.insert(0, path)
        return candidates

    def _runtime_config_path(self) -> Path:
        configured = self.config.get("config_path") or self.config.get("configPath")
        if configured:
            return Path(str(configured)).expanduser()
        return Path(__file__).resolve().parents[3] / "config.json"

    def _read_runtime_config(self) -> Dict[str, Any]:
        path = self._runtime_config_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _transient_call_config(self, args: Dict[str, Any]):
        class _ConfigScope:
            def __init__(scope_self, tool: "TongxinCli", params: Dict[str, Any]):
                scope_self.tool = tool
                scope_self.original = tool.config
                scope_self.params = params or {}

            def __enter__(scope_self):
                transient = dict(scope_self.original or {})
                changed = False
                for key in PYTHON_CONFIG_KEYS:
                    value = scope_self.params.get(key)
                    if value:
                        transient["python_path"] = str(value)
                        changed = True
                        break
                if changed:
                    scope_self.tool.config = transient
                return scope_self.tool.config

            def __exit__(scope_self, *_exc):
                scope_self.tool.config = scope_self.original
                return False

        return _ConfigScope(self, args)

    def _persist_script_path(self, script: Path) -> Path:
        config_path = self._runtime_config_path()
        data = self._read_runtime_config()
        tools = data.get("tools")
        if not isinstance(tools, dict):
            tools = {}
            data["tools"] = tools
        tongxin = tools.get("tongxin_cli")
        if not isinstance(tongxin, dict):
            tongxin = {}
            tools["tongxin_cli"] = tongxin
        self._scrub_persisted_login_fields(tongxin)
        tongxin["script_path"] = str(script)
        tongxin["read_only"] = True
        python_path = self._configured_python_path()
        if python_path:
            tongxin["python_path"] = python_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._scrub_persisted_login_fields(self.config)
        self.config["script_path"] = str(script)
        if python_path:
            self.config["python_path"] = python_path
        try:
            from config import conf

            live = conf()
            live_tools = live.get("tools", {})
            if not isinstance(live_tools, dict):
                live_tools = {}
                live["tools"] = live_tools
            live_tongxin = live_tools.get("tongxin_cli")
            if not isinstance(live_tongxin, dict):
                live_tongxin = {}
                live_tools["tongxin_cli"] = live_tongxin
            self._scrub_persisted_login_fields(live_tongxin)
            live_tongxin["script_path"] = str(script)
            live_tongxin["read_only"] = True
            if python_path:
                live_tongxin["python_path"] = python_path
        except Exception as exc:
            logger.debug(f"[TongxinCli] live config update skipped: {exc}")
        return config_path

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

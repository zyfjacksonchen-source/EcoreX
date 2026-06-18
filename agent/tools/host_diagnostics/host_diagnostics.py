"""Read-only host capability diagnostics for EcoreX runtimes."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)(\"?\s*[:=]\s*\"?)[^\",\s&}]+"),
]


def _mask(text: str) -> str:
    value = text or ""
    value = _SENSITIVE_PATTERNS[0].sub("sk-***", value)
    value = _SENSITIVE_PATTERNS[1].sub("ghp_***", value)
    value = _SENSITIVE_PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    return value


def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for suffix in (".cmd", ".exe"):
            found = shutil.which(name + suffix)
            if found:
                return found
    return None


def _safe_exists(path: str) -> bool:
    try:
        return bool(path and Path(path).exists())
    except Exception:
        return False


def _tail_text(
    path: Path,
    max_lines: int = 120,
    max_bytes: int = 64 * 1024,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        if not path.exists() or not path.is_file():
            return {"path": str(path), "exists": False, "lines": []}
        try:
            from common.ecorex_tool_permissions import get_tool_permission_broker

            decision = get_tool_permission_broker().authorize_file_access(
                "read",
                str(path),
                cwd=cwd,
            )
            if not decision.get("allowed"):
                return {
                    "path": str(path),
                    "exists": True,
                    "blocked": True,
                    "reason": decision.get("reason") or "Log read blocked by permissions.",
                    "lines": [],
                }
        except Exception as exc:
            return {
                "path": str(path),
                "exists": True,
                "blocked": True,
                "reason": f"Permission broker unavailable; log read blocked. {exc}",
                "lines": [],
            }
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
            raw = handle.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        return {
            "path": str(path),
            "exists": True,
            "sizeBytes": size,
            "truncated": size > max_bytes,
            "lines": [_mask(line) for line in lines],
        }
    except Exception as exc:
        return {"path": str(path), "exists": False, "error": str(exc)}


def _candidate_log_paths(runtime_root: Path) -> List[Path]:
    paths = _active_log_paths() + [
        runtime_root / "run.log",
        runtime_root / "logs" / "run.log",
        Path.cwd() / "run.log",
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        paths.extend([
            Path(local_app) / "EcoreX WebUI" / "runtime" / "run.log",
            Path(local_app) / "EcoreX WebUI" / "state" / "ecorex-webui.log",
            Path(local_app) / "EcoreX WebUI" / "state" / "ecorex-webui.err.log",
        ])
    seen = set()
    unique: List[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _active_log_paths() -> List[Path]:
    paths: List[Path] = []
    for log_name in ("log", ""):
        try:
            active_logger = logging.getLogger(log_name)
            for handler in active_logger.handlers:
                filename = getattr(handler, "baseFilename", None)
                if filename:
                    paths.append(Path(str(filename)))
        except Exception:
            continue
    return paths


def _check_cdp(endpoint: str) -> Dict[str, Any]:
    if not endpoint:
        return {"configured": False, "ready": False}
    url = endpoint.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        return {
            "configured": True,
            "ready": True,
            "endpoint": endpoint,
            "browser": payload.get("Browser", ""),
            "webSocketDebuggerUrl": bool(payload.get("webSocketDebuggerUrl")),
        }
    except Exception as exc:
        return {
            "configured": True,
            "ready": False,
            "endpoint": endpoint,
            "error": str(exc),
        }


def _mcp_status() -> Dict[str, Any]:
    try:
        from agent.tools import ToolManager

        manager = ToolManager()
        status = manager.list_mcp_status()
        return {"status": "success", "servers": status}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _permission_state() -> Dict[str, Any]:
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        return get_tool_permission_broker().get_state()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _feishu_status(cwd: str) -> Dict[str, Any]:
    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        decision = get_tool_permission_broker().authorize_noninteractive(
            "feishu_cli",
            {"action": "status", "scope": "host_diagnostics"},
        )
        if not decision.get("allowed"):
            return {
                "status": "blocked",
                "available": False,
                "message": decision.get("reason") or "Feishu CLI status check blocked by permission boundary.",
            }

        from agent.tools.feishu_cli.feishu_cli import FeishuCli

        result = FeishuCli({"cwd": cwd}).execute({"action": "status"})
        payload = result.result if isinstance(result.result, dict) else {"raw": str(result.result)}
        command = payload.get("command")
        if isinstance(command, list) and command:
            payload["command"] = [Path(str(command[0])).name] + [str(item) for item in command[1:]]
        for key in ("authStatus", "output"):
            if key in payload:
                payload[key] = _mask(json.dumps(payload[key], ensure_ascii=False, default=str))
        payload["status"] = result.status
        return payload
    except Exception as exc:
        return {"status": "error", "available": False, "message": str(exc)}


def _skill_status(cwd: str) -> Dict[str, Any]:
    try:
        from agent.skills.loader import SkillLoader

        runtime_root = Path(__file__).resolve().parents[3]
        builtin_dir = runtime_root / "skills"
        custom_dir = Path(cwd or os.getcwd()) / "skills"
        loader = SkillLoader()
        skills = loader.load_all_skills(
            builtin_dir=str(builtin_dir),
            custom_dir=str(custom_dir),
        )
        diagnostics = [_mask(item) for item in loader.get_last_diagnostics(limit=20)]
        return {
            "status": "success",
            "loadedCount": len(skills),
            "loadedNames": sorted(skills.keys())[:50],
            "builtinDir": str(builtin_dir),
            "customDir": str(custom_dir),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


class HostDiagnostics(BaseTool):
    name: str = "host_diagnostics"
    description: str = (
        "Read-only EcoreX host capability diagnostics. Use before raw bash when a task "
        "appears stuck, when Feishu/Lark/Chrome/CDP/MCP availability is unclear, or when "
        "you need to decide whether to ask the user for authorization. Returns sanitized "
        "runtime status, capability boundaries, MCP state, permission mode, Feishu CLI, "
        "EcoreX CLI, subagent/goal boundary status, skill load diagnostics, and recent log tails."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: status, logs, capabilities, all. Default: status.",
            },
            "log_lines": {
                "type": "integer",
                "description": "Number of recent lines per log for action=logs/all. Default 80, max 300.",
            },
        },
    }

    def __init__(self, config: dict = None):
        self.apply_config(config or {})

    def apply_config(self, config: dict) -> None:
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "status").strip().lower()
        if action not in {"status", "logs", "capabilities", "all"}:
            return ToolResult.fail({"status": "error", "message": "action must be one of: status, logs, capabilities, all"})

        try:
            payload: Dict[str, Any] = {"status": "success", "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if action in {"status", "capabilities", "all"}:
                payload.update(self._status())
            if action in {"logs", "all"}:
                lines = max(1, min(300, int(args.get("log_lines") or 80)))
                payload["logs"] = self._logs(lines)
            return ToolResult.success(payload)
        except Exception as exc:
            logger.warning(f"[HostDiagnostics] failed: {exc}")
            return ToolResult.fail({"status": "error", "message": str(exc)})

    def _status(self) -> Dict[str, Any]:
        from config import conf

        runtime_root = Path(__file__).resolve().parents[3]
        tools = conf().get("tools", {}) if isinstance(conf().get("tools", {}), dict) else {}
        browser_cfg = tools.get("browser", {}) if isinstance(tools.get("browser"), dict) else {}
        endpoint = str(browser_cfg.get("cdp_endpoint") or "")

        return {
            "runtime": {
                "root": str(runtime_root),
                "cwd": self.cwd,
                "workspace": str(conf().get("agent_workspace", "")),
                "channelType": str(conf().get("channel_type", "")),
                "python": sys.executable,
                "pythonVersion": platform.python_version(),
                "platform": platform.platform(),
            },
            "hostBoundary": {
                "canReadWriteWorkspace": True,
                "canRunShellViaPermissionBroker": True,
                "canInspectSanitizedLogs": True,
                "canPatchRuntimeFiles": _safe_exists(str(runtime_root / "agent")),
                "selfEvolutionEnabled": bool(conf().get("self_evolution_enabled", False)),
                "schedulerEnabled": bool(conf().get("scheduler_enabled", False)),
                "mcpAutoStart": bool(conf().get("mcp_auto_start", False)),
                "hasBuiltInSubagents": False,
                "subagentPlanRequired": True,
                "subagentNote": "EcoreX runtime is single-agent today; parallel sub-agents require a product-level coordinator, durable child sessions, and UI/API orchestration.",
                "hasGoalTool": False,
                "goalPlanRequired": True,
                "goalNote": "Goal Ledger documentation exists, but no runtime goal tool/API is currently exposed to agents.",
                "availableStructuredCliTools": ["feishu_cli", "ecorex_cli", "optional_abilities"],
            },
            "executables": {
                "node": _which("node"),
                "npm": _which("npm"),
                "npx": _which("npx"),
                "larkCli": _which("lark-cli"),
                "legacyCli": _which("cow"),
                "chrome": _which("chrome") or _which("google-chrome") or _which("msedge"),
            },
            "browser": {
                "cdp": _check_cdp(endpoint),
                "cdpAutoLaunch": browser_cfg.get("cdp_auto_launch", False),
                "cdpFallback": browser_cfg.get("cdp_fallback", True),
                "persistent": browser_cfg.get("persistent", True),
            },
            "mcp": _mcp_status(),
            "permissions": _permission_state(),
            "feishu": _feishu_status(self.cwd),
            "skills": _skill_status(self.cwd),
        }

    def _logs(self, lines: int) -> List[Dict[str, Any]]:
        runtime_root = Path(__file__).resolve().parents[3]
        return [_tail_text(path, max_lines=lines, cwd=self.cwd) for path in _candidate_log_paths(runtime_root)]

"""Desktop tool-execution permission broker for EcoreX.

This module is intentionally optional. Non-desktop channels keep the original
tool behavior, while the desktop sidecar can pause high-risk tools until the
renderer returns a user decision through the local Web API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from common.log import logger


Decision = Dict[str, Any]
Emitter = Callable[[str, Dict[str, Any]], None]

_DANGEROUS_TOOLS = {"bash", "shell", "terminal", "browser"}
_DEFAULT_TIMEOUT_SECONDS = 300


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else fallback
    except FileNotFoundError:
        return fallback
    except Exception as exc:
        logger.warning(f"[EcoreXToolPermission] failed reading {path}: {exc}")
        return fallback


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _mask_sensitive(value: str) -> str:
    text = value or ""
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(=|:)\s*[^\s,&]+", r"\1\2 ***", text)
    return text


def _summarize_args(tool_name: str, arguments: Dict[str, Any]) -> str:
    if tool_name in {"bash", "shell", "terminal"}:
        command = str(arguments.get("command") or arguments.get("cmd") or "")
        return _mask_sensitive(command).strip()[:500] or "shell command"
    if tool_name == "browser":
        action = str(arguments.get("action") or "browser action")
        target = str(arguments.get("url") or arguments.get("selector") or arguments.get("text") or "")
        summary = f"{action} {target}".strip()
        return _mask_sensitive(summary)[:500] or "browser action"
    return _mask_sensitive(json.dumps(arguments, ensure_ascii=False, default=str))[:500]


class ToolPermissionBroker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._decisions: Dict[str, Decision] = {}

    def authorize(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Optional[Dict[str, Any]],
        emit_event: Optional[Emitter] = None,
        cancel_event: Any = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> Decision:
        args = arguments if isinstance(arguments, dict) else {}
        if not self._requires_permission(tool_name):
            return {"allowed": True, "reason": "not-required"}

        settings = self._load_settings()
        mode = str(settings.get("mode") or "smart-ask")
        grant_key = self._grant_key(tool_name)
        if mode == "read-only":
            self._audit("tool-execution", "deny", {"tool": tool_name, "reason": "read-only"})
            return {
                "allowed": False,
                "reason": "当前为只读权限模式，已阻止本机工具执行。",
            }
        if settings.get("alwaysAllow", {}).get(grant_key):
            self._audit("tool-execution", "allow", {"tool": tool_name, "reason": "remembered-grant"})
            return {"allowed": True, "reason": "remembered-grant"}

        request_id = uuid.uuid4().hex
        summary = _summarize_args(tool_name, args)
        request = {
            "id": request_id,
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "summary": summary,
            "title": self._title_for_tool(tool_name),
            "message": self._message_for_tool(tool_name, summary),
            "created_at": _now(),
            "mode": mode,
        }
        with self._condition:
            self._pending[request_id] = request

        if emit_event:
            emit_event("tool_permission_request", request)

        deadline = time.time() + max(1, timeout_seconds)
        decision: Optional[Decision] = None
        while time.time() < deadline:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                decision = {"allowed": False, "reason": "用户已停止当前任务。"}
                break
            with self._condition:
                if request_id in self._decisions:
                    decision = self._decisions.pop(request_id)
                    break
                self._condition.wait(timeout=0.25)

        if decision is None:
            decision = {"allowed": False, "reason": "等待权限确认超时，工具未执行。"}

        with self._condition:
            self._pending.pop(request_id, None)

        allowed = bool(decision.get("allowed"))
        if allowed and decision.get("remember"):
            settings.setdefault("alwaysAllow", {})[grant_key] = True
            settings["updatedAt"] = _now()
            self._save_settings(settings)

        self._audit(
            "tool-execution",
            "allow" if allowed else "deny",
            {
                "tool": tool_name,
                "requestId": request_id,
                "reason": decision.get("reason", ""),
                "remember": bool(decision.get("remember")),
            },
        )
        return decision

    def list_pending(self) -> Dict[str, Any]:
        with self._condition:
            return {"status": "success", "pending": list(self._pending.values())}

    def decide(self, request_id: str, decision: str, remember: bool = False) -> Dict[str, Any]:
        normalized = (decision or "").strip().lower()
        if normalized in {"allow", "allow_once", "always_allow"}:
            payload: Decision = {
                "allowed": True,
                "reason": "user-allowed" if normalized != "always_allow" else "user-allowed-always",
                "remember": remember or normalized == "always_allow",
            }
        elif normalized in {"deny", "reject"}:
            payload = {"allowed": False, "reason": "用户未授权此本机工具执行。"}
        else:
            return {"status": "error", "message": "invalid permission decision"}

        with self._condition:
            if request_id not in self._pending:
                return {"status": "error", "message": "permission request is no longer pending"}
            self._decisions[request_id] = payload
            self._condition.notify_all()
        return {"status": "success", "request_id": request_id, "allowed": payload["allowed"]}

    def _requires_permission(self, tool_name: str) -> bool:
        if os.environ.get("ECOREX_DESKTOP") != "1":
            return False
        return (tool_name or "").strip().lower() in _DANGEROUS_TOOLS

    def _user_data_dir(self) -> Path:
        configured = os.environ.get("ECOREX_DESKTOP_USER_DATA")
        if configured:
            return Path(configured)
        if os.name == "nt":
            return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "ecorex-desktop"
        return Path.home() / ".config" / "ecorex-desktop"

    def _settings_path(self) -> Path:
        return self._user_data_dir() / "permissions.json"

    def _audit_path(self) -> Path:
        return self._user_data_dir() / "permission-audit.jsonl"

    def _load_settings(self) -> Dict[str, Any]:
        data = _read_json(self._settings_path(), {})
        data.setdefault("mode", "smart-ask")
        data.setdefault("alwaysAllow", {})
        return data

    def _save_settings(self, settings: Dict[str, Any]) -> None:
        _write_json(self._settings_path(), settings)

    def _audit(self, action: str, decision: str, detail: Dict[str, Any]) -> None:
        try:
            path = self._audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "createdAt": _now(),
                "action": action,
                "decision": decision,
                "detail": detail,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _grant_key(tool_name: str) -> str:
        return f"tool-execution:{(tool_name or '').strip().lower()}"

    @staticmethod
    def _title_for_tool(tool_name: str) -> str:
        if tool_name == "browser":
            return "浏览器自动化前确认"
        return "本机命令执行前确认"

    @staticmethod
    def _message_for_tool(tool_name: str, summary: str) -> str:
        if tool_name == "browser":
            return f"EcoreX 将使用浏览器自动化执行：{summary}"
        return f"EcoreX 将读取或操作本机 shell：{summary}"


_BROKER = ToolPermissionBroker()


def get_tool_permission_broker() -> ToolPermissionBroker:
    return _BROKER

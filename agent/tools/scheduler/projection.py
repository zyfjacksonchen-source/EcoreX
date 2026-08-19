"""Backend-owned scheduler status and task projection."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from common.ecorex_public_payload import redact_public_tool_value
from common.utils import expand_path
from config import conf
from agent.tools.scheduler.delivery_target import project_scheduler_delivery_target


SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|bearer)", re.IGNORECASE)


def scheduler_store_path(workspace_root: Optional[str] = None) -> str:
    root = workspace_root or expand_path(conf().get("agent_workspace", "~/cow"))
    return os.path.join(str(root), "scheduler", "tasks.json")


def _hash_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _mask_sensitive(value: Any, key_hint: str = "") -> Any:
    if SENSITIVE_KEY_RE.search(str(key_hint or "")):
        return "***"
    if isinstance(value, dict):
        return {str(key): _mask_sensitive(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_sensitive(item, key_hint) for item in value]
    if isinstance(value, str) and re.search(r"\b(Bearer|sk-|xox[baprs]-|xapp-)", value, re.IGNORECASE):
        return "***"
    return value


def _project_last_error(value: Any) -> Dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {"lastError": "", "lastErrorHash": ""}
    return {
        "lastError": f"scheduler task failed; details redacted ({_hash_text(text)})",
        "lastErrorHash": _hash_text(text),
    }


def _project_public_error(prefix: str, value: Any, field_prefix: str) -> Dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {
            field_prefix: "",
            f"{field_prefix}Hash": "",
            f"{field_prefix}Type": "",
            f"{field_prefix}Length": 0,
            f"{field_prefix}Bytes": 0,
        }
    return {
        field_prefix: (
            f"{prefix}; details redacted "
            f"({_hash_text(text)})"
        ),
        f"{field_prefix}Hash": _hash_text(text),
        f"{field_prefix}Type": type(value).__name__,
        f"{field_prefix}Length": len(text),
        f"{field_prefix}Bytes": len(text.encode("utf-8", errors="replace")),
    }


def _project_body(prefix: str, value: Any) -> Dict[str, Any]:
    text = str(value or "")
    if not text:
        return {}
    return {
        f"{prefix}Preview": "[redacted-content]",
        f"{prefix}Hash": _hash_text(text),
        f"{prefix}Length": len(text),
        f"{prefix}Bytes": len(text.encode("utf-8", errors="replace")),
    }


def _safe_iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    try:
        datetime.fromisoformat(text)
        return text
    except Exception:
        return text


def schedule_description(schedule: Dict[str, Any]) -> str:
    if not isinstance(schedule, dict):
        return "unknown"
    schedule_type = str(schedule.get("type") or "")
    if schedule_type == "cron":
        expr = str(schedule.get("expression") or "")
        friendly = {
            "0 9 * * *": "daily at 09:00",
            "30 9 * * *": "daily at 09:30",
            "0 */1 * * *": "hourly",
            "*/30 * * * *": "every 30 minutes",
        }
        return friendly.get(expr, f"cron {expr}" if expr else "cron")
    if schedule_type == "interval":
        try:
            seconds = int(schedule.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds >= 86400 and seconds % 86400 == 0:
            return f"every {seconds // 86400} day(s)"
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"every {seconds // 3600} hour(s)"
        if seconds >= 60 and seconds % 60 == 0:
            return f"every {seconds // 60} minute(s)"
        return f"every {seconds} second(s)" if seconds > 0 else "interval"
    if schedule_type == "once":
        return f"once at {schedule.get('run_at') or 'unknown'}"
    return schedule_type or "unknown"


def _project_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    action_type = str(action.get("type") or "")
    projected = {
        "type": action_type,
        "channelType": str(action.get("channel_type") or ""),
        "receiverNameHash": _hash_text(action.get("receiver_name") or ""),
        "receiverHash": _hash_text(action.get("receiver") or action.get("notify_session_id") or ""),
        "isGroup": bool(action.get("is_group", False)),
    }
    if action_type == "send_message":
        projected.update(_project_body("content", action.get("content")))
    if action_type == "agent_task":
        projected.update(_project_body("taskDescription", action.get("task_description")))
    if action_type == "tool_call":
        projected["toolName"] = str(action.get("tool_name") or "")
        projected["toolParams"] = redact_public_tool_value(action.get("tool_params") or {})
        projected.update(_project_body("resultPrefix", action.get("result_prefix")))
    if action_type == "skill_call":
        projected["skillName"] = str(action.get("skill_name") or "")
        projected["skillArgs"] = redact_public_tool_value(action.get("skill_args") or {})
        projected.update(_project_body("resultPrefix", action.get("result_prefix")))
    projected["deliveryTarget"] = project_scheduler_delivery_target(action)
    return {key: value for key, value in projected.items() if value not in ("", None, {}, [])}


def project_task(task: Dict[str, Any]) -> Dict[str, Any]:
    task = task if isinstance(task, dict) else {}
    schedule = task.get("schedule") if isinstance(task.get("schedule"), dict) else {}
    action = task.get("action") if isinstance(task.get("action"), dict) else {}
    enabled = bool(task.get("enabled", True))
    has_error = bool(task.get("last_error"))
    error_projection = _project_last_error(task.get("last_error"))
    return {
        "id": str(task.get("id") or ""),
        "name": str(task.get("name") or ""),
        "enabled": enabled,
        "state": "error" if has_error else "enabled" if enabled else "disabled",
        "schedule": _mask_sensitive(schedule),
        "scheduleDescription": schedule_description(schedule),
        "action": _project_action(action),
        "createdAt": _safe_iso(task.get("created_at")),
        "updatedAt": _safe_iso(task.get("updated_at")),
        "nextRunAt": _safe_iso(task.get("next_run_at")),
        "lastRunAt": _safe_iso(task.get("last_run_at")),
        "lastError": error_projection["lastError"],
        "lastErrorHash": error_projection["lastErrorHash"],
        "lastErrorAt": _safe_iso(task.get("last_error_at")),
    }


def _sort_tasks(tasks: Iterable[Dict[str, Any]]) -> list:
    def sort_key(item: Dict[str, Any]):
        enabled_rank = 0 if item.get("enabled") else 1
        next_run = item.get("nextRunAt") or "9999"
        name = str(item.get("name") or "")
        return (enabled_rank, next_run, name)

    return sorted(tasks, key=sort_key)


def scheduler_runtime_state() -> Dict[str, Any]:
    enabled = bool(conf().get("scheduler_enabled", False))
    initialized = False
    running = False
    thread_alive = False
    try:
        from agent.tools.scheduler.integration import get_scheduler_service, get_task_store

        initialized = get_task_store() is not None
        service = get_scheduler_service()
        if service is not None:
            running = bool(getattr(service, "running", False))
            thread = getattr(service, "thread", None)
            thread_alive = bool(thread is not None and getattr(thread, "is_alive", lambda: False)())
            running = running and thread_alive
    except Exception:
        initialized = False

    if running:
        service_status = "running"
        blocking_reason = ""
    elif enabled and initialized:
        service_status = "initialized_stopped"
        blocking_reason = "scheduler service is initialized but not running"
    elif enabled:
        service_status = "enabled_not_initialized"
        blocking_reason = "scheduler_enabled is true but the runtime service has not started"
    else:
        service_status = "disabled"
        blocking_reason = "scheduler_enabled is false"

    return {
        "enabled": enabled,
        "initialized": initialized,
        "running": running,
        "threadAlive": thread_alive,
        "serviceStatus": service_status,
        "blockingReason": blocking_reason,
    }


def scheduler_projection(workspace_root: Optional[str] = None) -> Dict[str, Any]:
    store_path = scheduler_store_path(workspace_root)
    tasks = []
    load_error = ""
    can_modify = True
    modify_blocking_reason = ""
    try:
        from agent.tools.scheduler.task_store import TaskStore

        store = TaskStore(store_path)
        tasks = [project_task(task) for task in store.list_tasks()]
    except Exception as exc:
        load_error = _project_public_error("Scheduler task store unavailable", exc, "loadError")
    if isinstance(load_error, str):
        load_error = {
            "loadError": load_error,
            "loadErrorHash": "",
            "loadErrorType": "",
            "loadErrorLength": 0,
            "loadErrorBytes": 0,
        }
    if isinstance(modify_blocking_reason, str):
        modify_blocking_reason = {
            "modifyBlockingReason": modify_blocking_reason,
            "modifyBlockingReasonHash": "",
            "modifyBlockingReasonType": "",
            "modifyBlockingReasonLength": 0,
            "modifyBlockingReasonBytes": 0,
        }

    projected_tasks = _sort_tasks(tasks)
    runtime = scheduler_runtime_state()
    counts = {
        "total": len(projected_tasks),
        "enabled": sum(1 for item in projected_tasks if item.get("enabled")),
        "disabled": sum(1 for item in projected_tasks if not item.get("enabled")),
        "error": sum(1 for item in projected_tasks if item.get("lastError")),
    }
    return {
        **runtime,
        "taskStore": {
            "path": store_path,
            "exists": os.path.exists(store_path),
        },
        "tasks": projected_tasks,
        "taskCount": len(projected_tasks),
        "counts": counts,
        **load_error,
        "canStart": bool(runtime.get("enabled") or not runtime.get("running")),
        "canModify": can_modify,
        **modify_blocking_reason,
        "pollIntervalSeconds": 30,
    }

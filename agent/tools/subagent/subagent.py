from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from agent.protocol.cancel import get_cancel_registry
from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from config import conf


MAX_CONCURRENT_SUBAGENTS = 6
MAX_SUBAGENT_DEPTH = 1
DEFAULT_ROLE = "explorer"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ROLES = {
    "default": "General child agent for scoped execution and concise result reporting.",
    "worker": "Implementation-oriented child agent for concrete tasks.",
    "explorer": "Research-oriented child agent for investigation and options.",
}
_LOCK = threading.RLock()


def _workspace_for(tool: BaseTool) -> Path:
    agent = getattr(tool, "context", None)
    workspace = getattr(agent, "workspace_dir", None) or conf().get("agent_workspace", "~/cow")
    return Path(os.path.expanduser(str(workspace))).resolve()


def _state_path(workspace: Path) -> Path:
    target = workspace / ".ecorex" / "subagents.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _read_state(workspace: Path) -> Dict[str, Any]:
    path = _state_path(workspace)
    if not path.exists():
        return {"schemaVersion": 1, "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {"schemaVersion": 1, "tasks": {}}
    except Exception:
        return {"schemaVersion": 1, "tasks": {}}


def _write_state(workspace: Path, state: Dict[str, Any]) -> None:
    path = _state_path(workspace)
    tmp = path.with_suffix(".tmp")
    state["updatedAt"] = int(time.time())
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _update_task(workspace: Path, task_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        state = _read_state(workspace)
        tasks = state.setdefault("tasks", {})
        task = tasks.setdefault(task_id, {})
        task.update(updates)
        _write_state(workspace, state)
        return dict(task)


def _task_snapshot(workspace: Path, task_id: str = "") -> Dict[str, Any]:
    with _LOCK:
        state = _read_state(workspace)
        tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
        if task_id:
            return dict(tasks.get(task_id, {}))
        return {"tasks": [dict(value) for value in tasks.values()]}


def _running_count(workspace: Path) -> int:
    snapshot = _task_snapshot(workspace)
    return sum(1 for task in snapshot["tasks"] if task.get("status") in {"queued", "running", "cancelling"})


def cancel_children_for_parent(workspace: Path | str, parent_session_id: str) -> Dict[str, Any]:
    """Cancel every non-terminal subagent started by a parent session."""
    parent_session_id = str(parent_session_id or "").strip()
    if not parent_session_id:
        return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": []}
    workspace_path = Path(os.path.expanduser(str(workspace))).resolve()
    cancelled_tasks = []
    cancelled_requests = 0
    now = int(time.time())
    registry = get_cancel_registry()

    with _LOCK:
        state = _read_state(workspace_path)
        tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
        for task_id, task in tasks.items():
            if not isinstance(task, dict):
                continue
            if str(task.get("parentSessionId") or "") != parent_session_id:
                continue
            if str(task.get("status") or "") in TERMINAL_STATUSES:
                continue
            child_session_id = str(task.get("childSessionId") or "")
            request_count = registry.cancel_session(child_session_id) if child_session_id else 0
            cancelled_requests += request_count
            task.update({
                "status": "cancelling" if request_count else "cancelled",
                "cancelledAt": now,
                "parentCancelledAt": now,
            })
            cancelled_tasks.append(dict(task))
        if cancelled_tasks:
            _write_state(workspace_path, state)

    return {
        "cancelledTasks": len(cancelled_tasks),
        "cancelledRequests": cancelled_requests,
        "tasks": cancelled_tasks,
    }


def _run_child(workspace: Path, task: Dict[str, Any]) -> None:
    task_id = task["id"]
    child_session_id = task["childSessionId"]
    try:
        current = _task_snapshot(workspace, task_id)
        if str(current.get("status") or "") in {"cancelled", "cancelling"}:
            _update_task(workspace, task_id, {"status": "cancelled", "completedAt": int(time.time())})
            return
        _update_task(workspace, task_id, {"status": "running", "startedAt": int(time.time())})
        from bridge.bridge import Bridge
        from bridge.context import Context, ContextType

        context = Context(ContextType.TEXT, task["prompt"], {
            "session_id": child_session_id,
            "request_id": child_session_id,
            "subagent_parent_id": task.get("parentSessionId", ""),
        })
        result = Bridge().get_agent_bridge().agent_reply(task["prompt"], context=context, clear_history=True)
        current = _task_snapshot(workspace, task_id)
        if str(current.get("status") or "") in {"cancelled", "cancelling"}:
            _update_task(workspace, task_id, {
                "status": "cancelled",
                "result": str(result or ""),
                "completedAt": int(time.time()),
            })
        else:
            _update_task(workspace, task_id, {
                "status": "completed",
                "result": str(result or ""),
                "completedAt": int(time.time()),
            })
    except Exception as exc:
        logger.exception(f"[Subagent] child task failed: {task_id}")
        current = _task_snapshot(workspace, task_id)
        if str(current.get("status") or "") in {"cancelled", "cancelling"} or "cancel" in str(exc).lower():
            _update_task(workspace, task_id, {
                "status": "cancelled",
                "error": str(exc),
                "completedAt": int(time.time()),
            })
            return
        _update_task(workspace, task_id, {
            "status": "failed",
            "error": str(exc),
            "completedAt": int(time.time()),
        })


class SubagentTool(BaseTool):
    name = "subagent"
    description = (
        "Start, inspect, collect, or cancel scoped child agents. Use for explicit parallel subtasks. "
        "v1 max concurrency is 6 and recursion depth is 1; CSV batch and recursive subagents are not enabled yet."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "One of: start, status, list, collect, cancel."},
            "task": {"type": "string", "description": "Child task prompt for action=start."},
            "role": {"type": "string", "description": "default, worker, or explorer. Default explorer."},
            "id": {"type": "string", "description": "Subagent task id for status/collect/cancel."},
            "depth": {"type": "integer", "description": "Current depth. v1 allows only 0 -> child depth 1."},
        },
        "required": ["action"],
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        action = str(params.get("action") or "").strip().lower()
        workspace = _workspace_for(self)
        if action == "start":
            return self._start(workspace, params)
        if action == "status":
            return ToolResult.success({"status": "success", "task": _task_snapshot(workspace, str(params.get("id") or ""))})
        if action == "list":
            return ToolResult.success({"status": "success", **_task_snapshot(workspace)})
        if action == "collect":
            task = _task_snapshot(workspace, str(params.get("id") or ""))
            if not task:
                return ToolResult.fail({"status": "error", "message": "subagent not found"})
            return ToolResult.success({"status": "success", "task": task, "result": task.get("result", "")})
        if action == "cancel":
            return self._cancel(workspace, str(params.get("id") or ""))
        return ToolResult.fail({"status": "error", "message": "action must be one of: start, status, list, collect, cancel"})

    def _start(self, workspace: Path, params: Dict[str, Any]) -> ToolResult:
        parent_session_id = str(getattr(getattr(self, "context", None), "_current_session_id", "") or "")
        if parent_session_id.startswith("subagent-"):
            return ToolResult.fail({"status": "error", "message": "recursive subagents are disabled in v0.1.14"})
        depth = int(params.get("depth") or 0)
        if depth >= MAX_SUBAGENT_DEPTH:
            return ToolResult.fail({"status": "error", "message": "subagent max depth reached", "maxDepth": MAX_SUBAGENT_DEPTH})
        if _running_count(workspace) >= MAX_CONCURRENT_SUBAGENTS:
            return ToolResult.fail({"status": "error", "message": "subagent concurrency limit reached", "maxConcurrency": MAX_CONCURRENT_SUBAGENTS})
        prompt = str(params.get("task") or "").strip()
        if not prompt:
            return ToolResult.fail({"status": "error", "message": "task is required"})
        role = str(params.get("role") or DEFAULT_ROLE).strip().lower()
        if role not in ROLES:
            role = DEFAULT_ROLE
        task_id = uuid.uuid4().hex[:12]
        child_session_id = f"subagent-{task_id}"
        task = {
            "id": task_id,
            "status": "queued",
            "role": role,
            "roleDescription": ROLES[role],
            "prompt": prompt,
            "parentSessionId": parent_session_id,
            "childSessionId": child_session_id,
            "depth": depth + 1,
            "createdAt": int(time.time()),
        }
        _update_task(workspace, task_id, task)
        threading.Thread(target=_run_child, args=(workspace, task), daemon=True, name=f"subagent-{task_id}").start()
        return ToolResult.success({"status": "success", "task": task})

    def _cancel(self, workspace: Path, task_id: str) -> ToolResult:
        task = _task_snapshot(workspace, task_id)
        if not task:
            return ToolResult.fail({"status": "error", "message": "subagent not found"})
        if str(task.get("status") or "") in TERMINAL_STATUSES:
            return ToolResult.success({"status": "success", "cancelled": 0, "task": task})
        cancelled = get_cancel_registry().cancel_session(task.get("childSessionId", ""))
        task = _update_task(workspace, task_id, {"status": "cancelling" if cancelled else "cancelled", "cancelledAt": int(time.time())})
        return ToolResult.success({"status": "success", "cancelled": cancelled, "task": task})

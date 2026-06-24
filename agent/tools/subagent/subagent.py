from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from agent.protocol.cancel import AgentCancelledError, get_cancel_registry
from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
from config import conf


MAX_CONCURRENT_SUBAGENTS = 6
MAX_SUBAGENT_DEPTH = 1
DEFAULT_TIMEOUT_SECONDS = 12 * 60
MAX_TIMEOUT_SECONDS = 60 * 60
DEFAULT_ROLE = "explorer"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted", "timeout"}
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


def _active_count(tasks: Dict[str, Any]) -> int:
    return sum(
        1
        for task in tasks.values()
        if isinstance(task, dict) and task.get("status") in {"queued", "starting", "running", "waiting_model", "waiting_tool", "cancelling"}
    )


def _clamp_timeout_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except Exception:
        seconds = DEFAULT_TIMEOUT_SECONDS
    if seconds <= 0:
        seconds = DEFAULT_TIMEOUT_SECONDS
    return max(30, min(seconds, MAX_TIMEOUT_SECONDS))


def _derive_task_name(role: str, prompt: str, task_id: str, explicit: str = "") -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit[:32]
    first_line = " ".join(str(prompt or "").strip().split())
    if first_line:
        return first_line[:24]
    role_label = {"worker": "Worker", "explorer": "Explorer", "default": "Agent"}.get(role, "Agent")
    return f"{role_label} #{task_id[-4:]}"


def _reserve_task_slot(workspace: Path, task: Dict[str, Any]) -> tuple[bool, int]:
    with _LOCK:
        state = _read_state(workspace)
        tasks = state.setdefault("tasks", {})
        if not isinstance(tasks, dict):
            tasks = {}
            state["tasks"] = tasks
        active_count = _active_count(tasks)
        if active_count >= MAX_CONCURRENT_SUBAGENTS:
            return False, active_count
        tasks[task["id"]] = dict(task)
        _write_state(workspace, state)
        return True, active_count + 1


def _mark_subagent_run_created(task: Dict[str, Any]) -> None:
    try:
        from agent.protocol import get_run_ledger

        child_session_id = str(task.get("childSessionId") or "")
        get_run_ledger().create_run(
            child_session_id,
            child_session_id,
            run_type="subagent",
            parent_id=str(task.get("parentSessionId") or ""),
            phase=str(task.get("status") or "queued"),
            status=str(task.get("status") or "queued"),
            metadata={
                "task_id": task.get("id", ""),
                "name": task.get("name", ""),
                "summary": task.get("summary", ""),
                "expected_output": task.get("expectedOutput", ""),
                "role": task.get("role", ""),
                "parent_session_id": task.get("parentSessionId", ""),
                "parent_request_id": task.get("parentRequestId", ""),
                "depth": task.get("depth", 0),
                "timeout_seconds": task.get("timeoutSeconds", 0),
                "deadline_at": task.get("deadlineAt"),
            },
        )
    except Exception as exc:
        logger.debug(f"[Subagent] run ledger create skipped: {exc}")


def _mark_subagent_run_phase(
    task: Dict[str, Any],
    phase: str,
    *,
    status: str | None = None,
    preserve_cancelling: bool = False,
) -> None:
    try:
        from agent.protocol import get_run_ledger

        get_run_ledger().mark_phase(
            str(task.get("childSessionId") or ""),
            phase,
            status=status,
            preserve_cancelling=preserve_cancelling,
        )
    except Exception as exc:
        logger.debug(f"[Subagent] run ledger phase skipped: {exc}")


def _mark_subagent_run_terminal(
    task: Dict[str, Any],
    status: str,
    *,
    reason: str = "",
    error_code: str = "",
    error_message: str = "",
) -> None:
    try:
        from agent.protocol import get_run_ledger

        get_run_ledger().mark_terminal(
            str(task.get("childSessionId") or ""),
            status,
            reason=reason or status,
            error_code=error_code,
            error_message=error_message,
        )
    except Exception as exc:
        logger.debug(f"[Subagent] run ledger terminal skipped: {exc}")


def cancel_children_for_parent(workspace: Path | str, parent_session_id: str) -> Dict[str, Any]:
    """Cancel every non-terminal subagent started by a parent session."""
    parent_session_id = str(parent_session_id or "").strip()
    if not parent_session_id:
        return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": []}
    workspace_path = Path(os.path.expanduser(str(workspace))).resolve()
    cancelled_tasks = []
    cancelled_requests = 0
    ledger_updates = []
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
            next_status = "cancelling" if request_count else "cancelled"
            task.update({
                "status": next_status,
                "cancelledAt": now,
                "parentCancelledAt": now,
            })
            task_snapshot = dict(task)
            if next_status == "cancelling":
                ledger_updates.append(("phase", task_snapshot))
            else:
                ledger_updates.append(("terminal_cancelled", task_snapshot))
            cancelled_tasks.append(task_snapshot)
        if cancelled_tasks:
            _write_state(workspace_path, state)

    for update_type, task in ledger_updates:
        if update_type == "phase":
            _mark_subagent_run_phase(task, "cancelling", status="cancelling")
        else:
            _mark_subagent_run_terminal(task, "cancelled", reason="parent_cancelled_before_start")

    return {
        "cancelledTasks": len(cancelled_tasks),
        "cancelledRequests": cancelled_requests,
        "tasks": cancelled_tasks,
    }


def cancel_children_for_default_workspace(
    parent_session_id: str,
    workspace: Path | str | None = None,
) -> Dict[str, Any]:
    """Cancel non-terminal child agents for a parent session in the runtime workspace."""
    parent_session_id = str(parent_session_id or "").strip()
    if not parent_session_id or parent_session_id.startswith("subagent-"):
        return {"cancelledTasks": 0, "cancelledRequests": 0, "tasks": []}
    target_workspace = workspace or conf().get("agent_workspace", "~/cow")
    return cancel_children_for_parent(target_workspace, parent_session_id)


def interrupt_orphan_task(
    workspace: Path | str,
    *,
    task_id: str = "",
    child_session_id: str = "",
    reason: str = "subagent_sidecar_interrupted",
    error_code: str = "SUBAGENT_SIDECAR_INTERRUPTED",
    error_message: str = "",
) -> Dict[str, Any]:
    """Mark a pre-boot subagent state row terminal so it releases its slot."""
    task_id = str(task_id or "").strip()
    child_session_id = str(child_session_id or "").strip()
    if not task_id and not child_session_id:
        return {"updated": False, "task": {}}
    workspace_path = Path(os.path.expanduser(str(workspace))).resolve()
    now = int(time.time())
    with _LOCK:
        state = _read_state(workspace_path)
        tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
        matched_task_id = task_id
        task = tasks.get(matched_task_id) if matched_task_id else None
        if not isinstance(task, dict) and child_session_id:
            for candidate_id, candidate in tasks.items():
                if not isinstance(candidate, dict):
                    continue
                if child_session_id in {
                    str(candidate.get("childSessionId") or ""),
                    str(candidate.get("requestId") or ""),
                }:
                    matched_task_id = str(candidate_id)
                    task = candidate
                    break
        if not isinstance(task, dict):
            return {"updated": False, "task": {}}
        if str(task.get("status") or "") in TERMINAL_STATUSES:
            return {"updated": False, "task": dict(task)}
        task.update({
            "status": "interrupted",
            "interruptedAt": now,
            "completedAt": now,
            "terminalReason": reason,
            "errorCode": error_code,
            "error": error_message or "Subagent sidecar interrupted before this run reached a terminal state.",
        })
        _write_state(workspace_path, state)
        return {"updated": True, "task": dict(task), "taskId": matched_task_id}


def _run_child(workspace: Path, task: Dict[str, Any]) -> None:
    task_id = task["id"]
    child_session_id = task["childSessionId"]
    registry = get_cancel_registry()
    cancel_event = None
    token_registered = False
    timeout_timer = None

    def _cancel_requested() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def _pre_reply_cancel_reason(current_task: Dict[str, Any]) -> str:
        if current_task.get("parentCancelledAt"):
            return "parent_cancelled_before_start"
        return "cancelled_before_start"

    def _mark_timeout() -> None:
        current = _task_snapshot(workspace, task_id)
        if str(current.get("status") or "") in TERMINAL_STATUSES:
            return
        if cancel_event:
            cancel_event.set()
        now = int(time.time())
        final_task = _update_task(workspace, task_id, {
            "status": "timeout",
            "completedAt": now,
            "terminalReason": "subagent_timeout",
            "errorCode": "SUBAGENT_TIMEOUT",
            "error": "Subagent exceeded its timeout and was stopped.",
            "lastHeartbeatAt": now,
        })
        _mark_subagent_run_terminal(
            final_task or task,
            "timeout",
            reason="subagent_timeout",
            error_code="SUBAGENT_TIMEOUT",
            error_message="Subagent exceeded its timeout and was stopped.",
        )

    try:
        cancel_event = registry.register(child_session_id, session_id=child_session_id)
        token_registered = bool(child_session_id)
        deadline_at = float(task.get("deadlineAt") or 0)
        if deadline_at > time.time():
            timeout_timer = threading.Timer(max(1, deadline_at - time.time()), _mark_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()
        current = _task_snapshot(workspace, task_id)
        current_status = str(current.get("status") or "")
        if current_status in {"cancelled", "cancelling"} or _cancel_requested():
            final_task = _update_task(workspace, task_id, {"status": "cancelled", "completedAt": int(time.time())})
            _mark_subagent_run_terminal(final_task or task, "cancelled", reason=_pre_reply_cancel_reason(current))
            return
        if current_status in TERMINAL_STATUSES:
            return
        running_task = _update_task(workspace, task_id, {
            "status": "running",
            "startedAt": int(time.time()),
            "lastHeartbeatAt": int(time.time()),
        })
        current = _task_snapshot(workspace, task_id)
        current_status = str(current.get("status") or "")
        if current_status in {"cancelled", "cancelling"} or _cancel_requested():
            final_task = _update_task(workspace, task_id, {"status": "cancelled", "completedAt": int(time.time())})
            _mark_subagent_run_terminal(final_task or task, "cancelled", reason=_pre_reply_cancel_reason(current))
            return
        if current_status in TERMINAL_STATUSES:
            return
        _mark_subagent_run_phase(
            running_task or task,
            "running",
            status="running",
            preserve_cancelling=True,
        )
        current = _task_snapshot(workspace, task_id)
        current_status = str(current.get("status") or "")
        if current_status in {"cancelled", "cancelling"} or _cancel_requested():
            final_task = _update_task(workspace, task_id, {"status": "cancelled", "completedAt": int(time.time())})
            _mark_subagent_run_terminal(final_task or task, "cancelled", reason=_pre_reply_cancel_reason(current))
            return
        if current_status in TERMINAL_STATUSES:
            return
        from bridge.bridge import Bridge
        from bridge.context import Context, ContextType

        _update_task(workspace, task_id, {"status": "waiting_model", "lastHeartbeatAt": int(time.time())})
        _mark_subagent_run_phase(task, "waiting_model", status="running", preserve_cancelling=True)
        context = Context(ContextType.TEXT, task["prompt"], {
            "session_id": child_session_id,
            "request_id": child_session_id,
            "cancel_token_owner": "subagent",
            "subagent_parent_id": task.get("parentSessionId", ""),
            "subagent_name": task.get("name", ""),
            "subagent_expected_output": task.get("expectedOutput", ""),
        })
        result = Bridge().get_agent_bridge().agent_reply(task["prompt"], context=context, clear_history=True)
        current = _task_snapshot(workspace, task_id)
        current_status = str(current.get("status") or "")
        if current_status in {"cancelled", "cancelling"} or _cancel_requested():
            final_task = _update_task(workspace, task_id, {
                "status": "cancelled",
                "result": str(result or ""),
                "completedAt": int(time.time()),
            })
            _mark_subagent_run_terminal(final_task or task, "cancelled", reason="cancelled_after_reply")
        elif current_status in TERMINAL_STATUSES:
            return
        else:
            final_task = _update_task(workspace, task_id, {
                "status": "completed",
                "result": str(result or ""),
                "completedAt": int(time.time()),
                "lastHeartbeatAt": int(time.time()),
            })
            _mark_subagent_run_terminal(final_task or task, "completed", reason="subagent_completed")
    except Exception as exc:
        logger.exception(f"[Subagent] child task failed: {task_id}")
        current = _task_snapshot(workspace, task_id)
        current_status = str(current.get("status") or "")
        if (
            current_status in {"cancelled", "cancelling"}
            or _cancel_requested()
            or isinstance(exc, AgentCancelledError)
            or "cancel" in str(exc).lower()
        ):
            final_task = _update_task(workspace, task_id, {
                "status": "cancelled",
                "error": str(exc),
                "completedAt": int(time.time()),
            })
            _mark_subagent_run_terminal(final_task or task, "cancelled", reason="subagent_cancelled", error_message=str(exc))
            return
        if current_status in TERMINAL_STATUSES:
            return
        final_task = _update_task(workspace, task_id, {
            "status": "failed",
            "error": str(exc),
            "completedAt": int(time.time()),
            "lastHeartbeatAt": int(time.time()),
        })
        _mark_subagent_run_terminal(
            final_task or task,
            "failed",
            reason="subagent_failed",
            error_code="SUBAGENT_FAILED",
            error_message=str(exc),
        )
    finally:
        if timeout_timer:
            try:
                timeout_timer.cancel()
            except Exception:
                pass
        if token_registered:
            registry.unregister(child_session_id)


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
            "name": {"type": "string", "description": "Short display name for the child agent."},
            "summary": {"type": "string", "description": "Short task summary for UI display."},
            "expected_output": {"type": "string", "description": "Expected result shape or collection criteria."},
            "timeout_seconds": {"type": "integer", "description": "Timeout for this child task; default 720 seconds, max 3600."},
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
        prompt = str(params.get("task") or "").strip()
        if not prompt:
            return ToolResult.fail({"status": "error", "message": "task is required"})
        role = str(params.get("role") or DEFAULT_ROLE).strip().lower()
        if role not in ROLES:
            role = DEFAULT_ROLE
        task_id = uuid.uuid4().hex[:12]
        child_session_id = f"subagent-{task_id}"
        timeout_seconds = _clamp_timeout_seconds(params.get("timeout_seconds") or params.get("timeoutSeconds"))
        name = _derive_task_name(role, prompt, task_id, params.get("name") or params.get("title") or "")
        summary = str(params.get("summary") or "").strip() or prompt[:160]
        expected_output = str(params.get("expected_output") or params.get("expectedOutput") or "").strip()
        parent_request_id = str(getattr(getattr(self, "context", None), "_current_request_id", "") or "")
        now = int(time.time())
        task = {
            "id": task_id,
            "status": "queued",
            "name": name,
            "summary": summary,
            "expectedOutput": expected_output,
            "role": role,
            "roleDescription": ROLES[role],
            "prompt": prompt,
            "parentSessionId": parent_session_id,
            "parentRequestId": parent_request_id,
            "childSessionId": child_session_id,
            "requestId": child_session_id,
            "depth": depth + 1,
            "createdAt": now,
            "timeoutSeconds": timeout_seconds,
            "deadlineAt": now + timeout_seconds,
            "lastHeartbeatAt": now,
        }
        reserved, active_count = _reserve_task_slot(workspace, task)
        if not reserved:
            return ToolResult.fail({
                "status": "error",
                "message": "subagent concurrency limit reached",
                "code": "SUBAGENT_CONCURRENCY_LIMIT",
                "error_type": "concurrency_limit",
                "retryable": True,
                "maxConcurrency": MAX_CONCURRENT_SUBAGENTS,
                "activeCount": active_count,
            })
        _mark_subagent_run_created(task)
        try:
            threading.Thread(target=_run_child, args=(workspace, task), daemon=True, name=f"subagent-{task_id}").start()
        except Exception as exc:
            task = _update_task(workspace, task_id, {
                "status": "failed",
                "error": str(exc),
                "completedAt": int(time.time()),
            })
            _mark_subagent_run_terminal(
                task,
                "failed",
                reason="subagent_thread_start_failed",
                error_code="SUBAGENT_THREAD_START_FAILED",
                error_message=str(exc),
            )
            return ToolResult.fail({
                "status": "error",
                "message": str(exc),
                "code": "SUBAGENT_THREAD_START_FAILED",
                "task": task,
            })
        return ToolResult.success({"status": "success", "task": task})

    def _cancel(self, workspace: Path, task_id: str) -> ToolResult:
        task = _task_snapshot(workspace, task_id)
        if not task:
            return ToolResult.fail({"status": "error", "message": "subagent not found"})
        if str(task.get("status") or "") in TERMINAL_STATUSES:
            return ToolResult.success({"status": "success", "cancelled": 0, "task": task})
        cancelled = get_cancel_registry().cancel_session(task.get("childSessionId", ""))
        task = _update_task(workspace, task_id, {"status": "cancelling" if cancelled else "cancelled", "cancelledAt": int(time.time())})
        if cancelled:
            _mark_subagent_run_phase(task, "cancelling", status="cancelling")
        else:
            _mark_subagent_run_terminal(task, "cancelled", reason="cancelled_before_start")
        return ToolResult.success({"status": "success", "cancelled": cancelled, "task": task})

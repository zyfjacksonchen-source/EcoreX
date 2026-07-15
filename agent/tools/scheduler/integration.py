"""
Integration module for scheduler with AgentBridge
"""

import os
import hashlib
import threading
import uuid
from typing import Any, Optional
from config import conf
from channel.channel_catalog import normalize_channel_name
from common.log import logger
from common.utils import expand_path
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from agent.tools.scheduler.delivery_target import (
    apply_scheduler_delivery_target,
    resolve_scheduler_delivery_target,
)

# Global scheduler service instance
_scheduler_service = None
_task_store = None
# Module-level lock to guard idempotent initialization across threads
_init_lock = threading.Lock()
_SCHEDULER_RUN_REQUEST_ID_KEY = "_scheduler_run_request_id"


def _summary_hash(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _body_summary(value) -> dict:
    text = "" if value is None else str(value)
    return {
        "redacted": bool(text),
        "hash": _summary_hash(text),
        "chars": len(text),
        "bytes": len(text.encode("utf-8", errors="replace")),
    }


def _params_summary(value) -> dict:
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value.keys())
        return {
            "keys": keys,
            "hash": _summary_hash(repr(sorted((str(k), type(v).__name__) for k, v in value.items()))),
        }
    return _body_summary(value)


def _public_error_message(prefix: str, value) -> str:
    summary = _body_summary(value)
    if not summary["hash"]:
        return prefix
    return (
        f"{prefix} Details redacted "
        f"(type={type(value).__name__}, hash={summary['hash']}, "
        f"chars={summary['chars']}, bytes={summary['bytes']})."
    )


def _readiness_reason_summary(reason) -> dict:
    summary = _body_summary(reason)
    return summary if summary.get("hash") else {}


def init_scheduler(agent_bridge) -> bool:
    """
    Initialize scheduler service (idempotent).

    Safe to call multiple times and from multiple threads: only the first
    successful call creates the singleton ``SchedulerService`` + background
    scanning thread. Subsequent calls return immediately.

    Args:
        agent_bridge: AgentBridge instance

    Returns:
        True if scheduler is initialized (newly created or already running)
    """
    global _scheduler_service, _task_store

    # Fast path: already initialized and running
    if _scheduler_service is not None and getattr(_scheduler_service, "running", False):
        return True

    with _init_lock:
        # Re-check under the lock to avoid races where multiple threads
        # passed the fast-path check before any of them acquired the lock.
        if _scheduler_service is not None and getattr(_scheduler_service, "running", False):
            return True

        try:
            from agent.tools.scheduler.task_store import TaskStore
            from agent.tools.scheduler.scheduler_service import SchedulerService

            # Get workspace from config
            workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")

            # Create task store (reuse if already created)
            if _task_store is None:
                _task_store = TaskStore(store_path)
                logger.debug(f"[Scheduler] Task store initialized: {store_path}")

            # Create execute callback. Returns True on success, False to ask
            # the scheduler to retry on the next tick (e.g. channel not yet
            # ready right after process start).
            def execute_task_callback(task: dict):
                return _execute_scheduled_task(task, agent_bridge)

            # Create scheduler service
            _scheduler_service = SchedulerService(_task_store, execute_task_callback)
            _scheduler_service.start()

            logger.info("[Scheduler] Service initialized and started")
            return True

        except Exception as e:
            logger.error(f"[Scheduler] Failed to initialize scheduler: {_body_summary(e)}")
            return False


def _is_channel_ready(channel_type: str, receiver: str) -> bool:
    """Best-effort readiness probe for outbound channels.

    The probe is read-only: it must not create channel instances, import
    vendor SDKs, or inspect private web/session queues. Unknown channels now
    fail closed; explicit Web delivery keeps the legacy live-send fallback.
    """
    channel_name = str(channel_type or "").strip()
    if not channel_name or channel_name == "unknown" or "," in channel_name:
        logger.warning(f"[Scheduler] Refusing ambiguous delivery channel: {_body_summary(channel_name)}")
        return False
    try:
        from channel.messaging_adapter_contract import probe_messaging_adapter

        state = probe_messaging_adapter(channel_name, receiver=receiver)
        if channel_name == "web" and state.get("reason") in {
            "channel is not configured",
            "channel is not enabled",
            "no live channel manager state is available",
        }:
            # Preserve legacy scheduler tests and desktop handoff flows where
            # Web delivery is validated by the live/singleton send path rather
            # than by channel_type config.
            return True
        if state.get("readiness") == "unknown" and state.get("safeToSend") is True:
            return True
        if not state.get("safeToSend"):
            logger.warning(
                "[Scheduler] Adapter readiness blocked delivery for "
                f"{channel_name}: {_body_summary(state.get('reason'))}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"[Scheduler] Channel readiness check failed for {channel_name}: {_body_summary(e)}")
        return channel_name == "web"


def _delivery_target_summary(target: dict, *, reason: str = "", reason_summary: Optional[dict] = None) -> dict:
    platform = str(target.get("channel_type") or "unknown")
    summary = {
        "channel_type": platform,
        "source": str(target.get("source") or ""),
        "reason": reason or str(target.get("reason") or ""),
        "homeChannelRequired": bool(target.get("home_channel_required")),
        "homeChannelConfigured": bool(target.get("home_channel_configured")),
        "receiverHash": str(target.get("receiver_hash") or _summary_hash(target.get("receiver"))),
        "redacted": True,
    }
    if reason_summary:
        summary["reasonSummary"] = reason_summary
    return summary


def _record_scheduler_delivery_blocked(target: dict, *, reason: str, reason_summary: Optional[dict] = None) -> None:
    platform = str(target.get("channel_type") or "unknown")
    if not platform or platform == "web":
        return
    try:
        from channel.messaging_adapter_contract import record_external_connection_runtime_event
        payload = {
            "status": "blocked",
            "reason": reason,
            "scheduler": _delivery_target_summary(target, reason=reason, reason_summary=reason_summary),
        }
        if reason_summary:
            payload["reasonSummary"] = reason_summary

        record_external_connection_runtime_event(
            platform,
            "external_connection.delivery.blocked",
            payload,
        )
    except Exception as e:
        logger.debug(f"[Scheduler] external delivery blocked event skipped: {_body_summary(e)}")


def _channel_readiness_block_reason(channel_type: str, receiver: str) -> tuple[str, dict]:
    try:
        from channel.messaging_adapter_contract import probe_messaging_adapter

        state = probe_messaging_adapter(channel_type, receiver=receiver)
        return "adapter_not_ready", _readiness_reason_summary(state.get("reason"))
    except Exception as e:
        logger.debug(f"[Scheduler] adapter block reason unavailable: {_body_summary(e)}")
        return "adapter_not_ready", _readiness_reason_summary(e)


def _send_channel_reply(channel, reply: Reply, context: Context, channel_type: str) -> bool:
    platform = str(channel_type or "").strip()
    if platform and platform != "web":
        try:
            from channel.messaging_adapter_contract import deliver_reply

            result = deliver_reply(channel, reply, context, platform=platform)
            if result.get("status") == "sent":
                return True
            logger.error(f"[Scheduler] External delivery failed: {_body_summary(result.get('error') or result.get('status'))}")
            return False
        except Exception as e:
            logger.error(f"[Scheduler] External delivery raised: {_body_summary(e)}")
            return False
    channel.send(reply, context)
    return True


def get_task_store():
    """Get the global task store instance"""
    return _task_store


def get_scheduler_service():
    """Get the global scheduler service instance"""
    return _scheduler_service


def ensure_scheduler_runtime(agent_bridge=None) -> bool:
    """Start the scheduler runtime when it has been explicitly enabled."""
    if not conf().get("scheduler_enabled", False):
        return False
    if agent_bridge is None:
        try:
            from bridge.bridge import Bridge

            agent_bridge = Bridge().get_agent_bridge()
        except Exception as exc:
            logger.warning(f"[Scheduler] Failed to resolve AgentBridge for scheduler init: {_body_summary(exc)}")
            return False
    return init_scheduler(agent_bridge)


def _scheduler_action(task: dict) -> dict:
    action = task.get("action", {}) if isinstance(task, dict) else {}
    return action if isinstance(action, dict) else {}


def _scheduler_run_request_id(task: dict) -> str:
    if not isinstance(task, dict):
        return f"scheduler_unknown_{uuid.uuid4().hex[:8]}"
    existing = task.get(_SCHEDULER_RUN_REQUEST_ID_KEY)
    if existing:
        return str(existing)
    task_id = str(task.get("id") or "unknown")
    request_id = f"scheduler_{task_id}_{uuid.uuid4().hex[:8]}"
    task[_SCHEDULER_RUN_REQUEST_ID_KEY] = request_id
    return request_id


def _scheduler_session_id(task: dict) -> str:
    action = _scheduler_action(task)
    task_id = str(task.get("id") or "unknown") if isinstance(task, dict) else "unknown"
    receiver = str(action.get("receiver") or action.get("notify_session_id") or "")
    action_type = str(action.get("type") or "")
    channel_name = normalize_channel_name(action.get("channel_type") or "")
    if channel_name and channel_name != "web":
        return f"scheduler_{channel_name}_{_summary_hash(receiver)}_{task_id}"
    if action_type in {"agent_task", "skill_call"}:
        return f"scheduler_{receiver or 'unknown'}_{task_id}"
    return str(action.get("notify_session_id") or receiver or f"scheduler_{task_id}")


def _scheduler_parent_id(task: dict) -> str:
    action = _scheduler_action(task)
    channel_name = normalize_channel_name(action.get("channel_type") or "")
    receiver = str(action.get("receiver") or action.get("notify_session_id") or "")
    if channel_name and channel_name != "web":
        return f"scheduler_{channel_name}_{_summary_hash(receiver)}"
    return str(action.get("notify_session_id") or action.get("receiver") or "")


def _scheduler_run_metadata(task: dict) -> dict:
    action = _scheduler_action(task)
    schedule = task.get("schedule", {}) if isinstance(task, dict) else {}
    if not isinstance(schedule, dict):
        schedule = {}
    call_name = (
        action.get("call_name")
        or action.get("tool_name")
        or action.get("skill_name")
    )
    metadata = {
        "task_id": str(task.get("id") or "") if isinstance(task, dict) else "",
        "task_name": str(task.get("name") or "") if isinstance(task, dict) else "",
        "action_type": str(action.get("type") or ""),
        "channel_type": str(action.get("channel_type") or "unknown"),
        "receiverHash": _summary_hash(action.get("receiver") or ""),
        "schedule_type": str(schedule.get("type") or ""),
        "call_name": str(call_name or ""),
    }
    if normalize_channel_name(action.get("channel_type") or "") == "web":
        metadata["receiver"] = str(action.get("receiver") or "")
    else:
        metadata["receiverRedacted"] = True
    return {key: value for key, value in metadata.items() if value}


def _mark_scheduler_run_created(task: dict, request_id: str) -> None:
    try:
        from agent.protocol import get_run_ledger

        ledger = get_run_ledger()
        ledger.create_run(
            request_id,
            _scheduler_session_id(task),
            run_type="scheduler",
            parent_id=_scheduler_parent_id(task),
            phase="queued",
            status="queued",
            metadata=_scheduler_run_metadata(task),
        )
        ledger.mark_phase(request_id, "running", status="running")
    except Exception as e:
        logger.debug(f"[Scheduler] Run ledger create skipped for {request_id}: {_body_summary(e)}")


def _mark_scheduler_run_phase(request_id: str, phase: str, status: Optional[str] = None) -> None:
    if not request_id:
        return
    try:
        from agent.protocol import get_run_ledger

        get_run_ledger().mark_phase(request_id, phase, status=status)
    except Exception as e:
        logger.debug(f"[Scheduler] Run ledger phase update skipped for {request_id}: {_body_summary(e)}")


def _mark_scheduler_run_terminal(
    request_id: str,
    status: str,
    *,
    reason: str = "",
    error_code: str = "",
    error_message: str = "",
) -> None:
    if not request_id:
        return
    try:
        from agent.protocol import get_run_ledger

        get_run_ledger().mark_terminal(
            request_id,
            status,
            reason=reason,
            error_code=error_code,
            error_message=error_message,
        )
    except Exception as e:
        logger.debug(f"[Scheduler] Run ledger terminal update skipped for {request_id}: {_body_summary(e)}")


def _mark_scheduler_task_failed(
    task: dict,
    reason: str,
    error_code: str,
    error_message: str = "",
) -> None:
    _mark_scheduler_run_terminal(
        _scheduler_run_request_id(task),
        "failed",
        reason=reason,
        error_code=error_code,
        error_message=error_message,
    )


def _scheduler_run_was_cancelled(cancel_event) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except Exception:
        return False


def _mark_scheduler_run_cancelled(request_id: str) -> None:
    _mark_scheduler_run_terminal(
        request_id,
        "cancelled",
        reason="scheduler_cancelled",
        error_code="SCHEDULER_CANCELLED",
        error_message="Scheduled task execution was cancelled.",
    )


def _execute_scheduled_task(task: dict, agent_bridge) -> bool:
    """Execute one due scheduled task attempt and persist its lifecycle."""
    target = resolve_scheduler_delivery_target(task, prefer_home_channel=False)
    if not target.get("ok"):
        _record_scheduler_delivery_blocked(target, reason=str(target.get("reason") or "scheduler_delivery_target_unavailable"))
        logger.warning(
            f"[Scheduler] Task {task.get('id') if isinstance(task, dict) else '<unknown>'}: "
            f"delivery target unavailable ({target.get('reason')}); deferring"
        )
        return False
    apply_scheduler_delivery_target(task, target)
    action = _scheduler_action(task)
    action_type = action.get("type")
    channel_type = action.get("channel_type", "unknown")
    receiver = action.get("receiver", "")
    receiver_hash = _summary_hash(receiver)

    if not _is_channel_ready(channel_type, receiver):
        block_reason, block_reason_summary = _channel_readiness_block_reason(channel_type, receiver)
        _record_scheduler_delivery_blocked(
            {
                **target,
                "channel_type": channel_type,
                "receiver": receiver,
                "receiver_hash": receiver_hash,
            },
            reason=block_reason,
            reason_summary=block_reason_summary,
        )
        logger.warning(
            f"[Scheduler] Task {task.get('id') if isinstance(task, dict) else '<unknown>'}: "
            f"channel '{channel_type}' not ready for receiverHash={receiver_hash} "
            f"(no inbound msg cached since restart?); deferring"
        )
        return False

    if isinstance(task, dict):
        task.pop(_SCHEDULER_RUN_REQUEST_ID_KEY, None)
    request_id = _scheduler_run_request_id(task)
    cancel_event = None
    try:
        from agent.protocol import get_cancel_registry

        cancel_event = get_cancel_registry().register(
            request_id,
            session_id=_scheduler_session_id(task),
        )
    except Exception as e:
        logger.debug(f"[Scheduler] Cancel token registration skipped for {request_id}: {_body_summary(e)}")
    _mark_scheduler_run_created(task, request_id)

    try:
        _mark_scheduler_run_phase(request_id, "authorizing", status="running")
        if not _authorize_scheduled_execution(task):
            if _scheduler_run_was_cancelled(cancel_event):
                _mark_scheduler_run_cancelled(request_id)
                return True
            _mark_scheduler_run_terminal(
                request_id,
                "failed",
                reason="scheduler_permission_denied",
                error_code="SCHEDULER_PERMISSION_DENIED",
                error_message="Background scheduler execution was blocked by the permission boundary.",
            )
            return True

        if _scheduler_run_was_cancelled(cancel_event):
            _mark_scheduler_run_cancelled(request_id)
            return True

        if action_type == "agent_task":
            _mark_scheduler_run_phase(request_id, "agent_task_running", status="running")
            ok = _execute_agent_task(task, agent_bridge)
        elif action_type == "send_message":
            _mark_scheduler_run_phase(request_id, "send_message_running", status="running")
            ok = _execute_send_message(task, agent_bridge)
        elif action_type == "tool_call":
            _mark_scheduler_run_phase(request_id, "tool_call_running", status="running")
            ok = _execute_tool_call(task, agent_bridge)
        elif action_type == "skill_call":
            _mark_scheduler_run_phase(request_id, "skill_call_running", status="running")
            ok = _execute_skill_call(task, agent_bridge)
        else:
            logger.warning(f"[Scheduler] Unknown action type: {action_type}")
            _mark_scheduler_run_terminal(
                request_id,
                "failed",
                reason="scheduler_unknown_action",
                error_code="SCHEDULER_UNKNOWN_ACTION",
                error_message=f"Unknown scheduled action type: {action_type}",
            )
            return True

        cancelled = _scheduler_run_was_cancelled(cancel_event)
        if ok:
            if cancelled:
                _mark_scheduler_run_cancelled(request_id)
            else:
                _mark_scheduler_run_terminal(request_id, "completed", reason="scheduler_completed")
        else:
            if cancelled:
                _mark_scheduler_run_cancelled(request_id)
                return True
            else:
                _mark_scheduler_run_terminal(
                    request_id,
                    "failed",
                    reason="scheduler_execution_failed",
                    error_code="SCHEDULER_EXECUTION_FAILED",
                    error_message="Scheduled task execution or delivery failed; scheduler will retry if the task remains due.",
                )
        return ok
    except Exception as e:
        logger.error(
            f"[Scheduler] Error executing task "
            f"{task.get('id') if isinstance(task, dict) else '<unknown>'}: {_body_summary(e)}"
        )
        if _scheduler_run_was_cancelled(cancel_event):
            _mark_scheduler_run_cancelled(request_id)
            return True
        _mark_scheduler_run_terminal(
            request_id,
            "failed",
            reason="scheduler_execution_exception",
            error_code="SCHEDULER_EXECUTION_EXCEPTION",
            error_message=_public_error_message("Scheduled task execution raised an exception.", e),
        )
        return False
    finally:
        try:
            from agent.protocol import get_cancel_registry

            get_cancel_registry().unregister(request_id)
        except Exception:
            pass


def _authorize_scheduled_execution(task: dict) -> bool:
    """Fail closed for background scheduler work that cannot ask the UI."""
    def normalize_decision(decision: Any) -> dict:
        if isinstance(decision, dict) and decision.get("allowed") in {True, False}:
            return decision
        return {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}

    try:
        from common.ecorex_tool_permissions import get_tool_permission_broker

        broker = get_tool_permission_broker()
        action = task.get("action", {}) if isinstance(task, dict) else {}
        auth_args = {
            "action": "execute",
            "task_id": task.get("id") if isinstance(task, dict) else "",
            "name": task.get("name") if isinstance(task, dict) else "",
            "action_type": action.get("type") if isinstance(action, dict) else "",
        }
        capability_authorize = getattr(broker, "authorize_capability", None)
        decision = None
        if callable(capability_authorize):
            candidate = capability_authorize(
                "scheduler",
                "execute",
                arguments=auth_args,
                metadata={"source": "scheduler_background"},
            )
            decision = normalize_decision(candidate)
        if decision is None:
            noninteractive = getattr(broker, "authorize_noninteractive", None)
            if callable(noninteractive):
                candidate = noninteractive("scheduler", auth_args)
                if isinstance(candidate, dict):
                    decision = normalize_decision(candidate)
        if decision is None:
            decision = {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}
        if decision.get("allowed") is True:
            return True
        logger.warning(
            f"[Scheduler] Task {task.get('id') if isinstance(task, dict) else '<unknown>'} "
            f"blocked by permission boundary: {_body_summary(decision.get('reason'))}"
        )
        return False
    except Exception as e:
        logger.warning(f"[Scheduler] Permission broker unavailable; scheduled execution blocked: {_body_summary(e)}")
        return False


def _authorize_scheduled_tool_call(tool, tool_name: str, tool_params: dict, task: dict) -> bool:
    """Authorize a concrete tool invoked by a scheduled task."""
    def normalize_decision(decision: Any) -> dict:
        if isinstance(decision, dict) and decision.get("allowed") in {True, False}:
            return decision
        return {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}

    try:
        from agent.protocol.agent_stream import AgentStreamExecutor
        from common.ecorex_tool_permissions import get_tool_permission_broker

        proxy_name, proxy_args = AgentStreamExecutor._permission_proxy_for_tool(
            tool,
            tool_name,
            tool_params if isinstance(tool_params, dict) else {},
        )
        broker = get_tool_permission_broker()
        capability_authorize = getattr(broker, "authorize_capability", None)
        decision = None
        if callable(capability_authorize):
            candidate = capability_authorize(
                proxy_name,
                str((proxy_args or {}).get("action") or ""),
                arguments=proxy_args,
                metadata={"source": "scheduler_background_tool_call"},
            )
            decision = normalize_decision(candidate)
        if decision is None:
            noninteractive = getattr(broker, "authorize_noninteractive", None)
            if callable(noninteractive):
                candidate = noninteractive(proxy_name, proxy_args)
                if isinstance(candidate, dict):
                    decision = normalize_decision(candidate)
        if decision is None:
            decision = {"allowed": False, "reason": "Permission broker returned an invalid authorization decision."}
        if decision.get("allowed") is True:
            return True
        logger.warning(
            f"[Scheduler] Task {task.get('id') if isinstance(task, dict) else '<unknown>'} "
            f"tool_call {tool_name!r} blocked by permission boundary: {_body_summary(decision.get('reason'))}"
        )
        return False
    except Exception as e:
        logger.warning(f"[Scheduler] Permission broker unavailable; scheduled tool_call blocked: {_body_summary(e)}")
        return False


def _remember_delivered_output(
    agent_bridge,
    task: dict,
    channel_type: str,
    content: str,
) -> None:
    """Best-effort persistence of the message the scheduler sent to a user.

    Uses notify_session_id (the real chat session_id stored at task creation time)
    so that group chats correctly associate the output with the user's conversation.
    Falls back to receiver for backward compatibility with old tasks.

    Per-action-type behaviour:
        - agent_task / tool_call / skill_call: gated by ``scheduler_inject_to_session``
          (default True). These produce AI-generated content worth remembering.
        - send_message: additionally gated by ``scheduler_inject_send_message``
          (default False). Fixed reminder text rarely benefits follow-up Q&A and
          would just consume context tokens.
    """
    if not content:
        return
    action = task.get("action", {})
    action_type = action.get("type", "")

    # send_message defaults to NOT being injected; explicit opt-in via config.
    if action_type == "send_message":
        if not conf().get("scheduler_inject_send_message", False):
            return

    session_id = action.get("notify_session_id") or action.get("receiver")
    if not session_id:
        return
    try:
        remember = getattr(agent_bridge, "remember_scheduled_output", None)
        if remember:
            task_desc = action.get("task_description") or action.get("content", "")
            remember(session_id, str(content), channel_type=channel_type, task_description=task_desc)
    except Exception as e:
        logger.warning(
            f"[Scheduler] Failed to remember delivered output for "
            f"sessionHash={_summary_hash(session_id)}: {_body_summary(e)}"
        )


def _execute_agent_task(task: dict, agent_bridge) -> bool:
    """
    Execute an agent_task action - let Agent handle the task.
    Returns True on successful delivery, False to retry next tick.
    """
    try:
        action = task.get("action", {})
        task_description = action.get("task_description")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        
        if not task_description:
            logger.error(f"[Scheduler] Task {task['id']}: No task_description specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "agent_task action is missing task_description.",
            )
            return True  # malformed task, don't loop forever
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "agent_task action is missing receiver.",
            )
            return True
        
        # Check for unsupported channels
        if channel_type == "dingtalk":
            logger.warning(f"[Scheduler] Task {task['id']}: DingTalk channel does not support scheduled messages (Stream mode limitation). Task will execute but message cannot be sent.")
        
        logger.info(f"[Scheduler] Task {task['id']}: Executing agent task {_body_summary(task_description)}")
        
        # Create a unique session_id for this scheduled task to avoid polluting user's conversation.
        # Non-Web external targets use hashed receiver ids via the shared run-ledger helper.
        scheduler_session_id = _scheduler_session_id(task)
        
        # Create context for Agent
        context = Context(ContextType.TEXT, task_description)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id
        
        # Channel-specific setup
        if channel_type == "web":
            request_id = _scheduler_run_request_id(task)
            context["request_id"] = request_id
            context["cancel_token_owner"] = "scheduler"
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "dingtalk":
            # DingTalk requires msg object, set to None for scheduled tasks
            context["msg"] = None
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
        elif channel_type == "wecom_bot":
            context["msg"] = None

        # Use Agent to execute the task
        # Mark this as a scheduled task execution to prevent recursive task creation
        context["is_scheduled_task"] = True
        
        try:
            # Don't clear history - scheduler tasks use isolated session_id so they won't pollute user conversations
            reply = agent_bridge.agent_reply(task_description, context=context, on_event=None, clear_history=False)

            if not (reply and reply.content):
                logger.error(f"[Scheduler] Task {task['id']}: No result from agent execution")
                _mark_scheduler_task_failed(
                    task,
                    "scheduler_empty_result",
                    "SCHEDULER_EMPTY_RESULT",
                    "Agent execution completed without reply content.",
                )
                return True  # agent ran but produced nothing; don't loop

            from channel.channel_factory import create_channel
            channel = create_channel(channel_type)
            if not channel:
                logger.error(f"[Scheduler] Failed to create channel: {channel_type}")
                return False

            if channel_type == "web" and hasattr(channel, 'request_to_session'):
                request_id = context.get("request_id")
                if request_id:
                    channel.request_to_session[request_id] = receiver

            try:
                delivered = _send_channel_reply(channel, reply, context, channel_type)
            except Exception as e:
                logger.error(f"[Scheduler] Failed to send result: {_body_summary(e)}")
                return False
            if not delivered:
                return False

            _remember_delivered_output(agent_bridge, task, channel_type, reply.content)
            logger.info(
                f"[Scheduler] Task {task['id']} executed successfully, "
                f"result sent to receiverHash={_summary_hash(receiver)}"
            )
            return True

        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute task via Agent: {_body_summary(e)}")
            return False

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_agent_task: {_body_summary(e)}")
        return False


def _execute_send_message(task: dict, agent_bridge) -> bool:
    """Execute a send_message action. Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        content = action.get("content", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "send_message action is missing receiver.",
            )
            return True
        
        # Create context for sending message
        context = Context(ContextType.TEXT, content)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = receiver
        
        # Channel-specific context setup
        if channel_type == "web":
            # Web channel needs request_id
            request_id = _scheduler_run_request_id(task)
            context["request_id"] = request_id
            logger.debug(f"[Scheduler] Generated request_id for web channel: {request_id}")
        elif channel_type == "feishu":
            # Feishu channel: for scheduled tasks, send as new message (no msg_id to reply to)
            # Use chat_id for groups, open_id for private chats
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            # Keep isgroup as is, but set msg to None (no original message to reply to)
            # Feishu channel will detect this and send as new message instead of reply
            context["msg"] = None
            logger.debug(f"[Scheduler] Feishu: receive_id_type={context['receive_id_type']}, is_group={is_group}, receiver_hash={_summary_hash(receiver)}")
        elif channel_type == "dingtalk":
            # DingTalk channel setup
            context["msg"] = None
            # 如果是单聊，需要传递 sender_staff_id
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
                    logger.debug(f"[Scheduler] DingTalk single chat: sender_staff_hash={_summary_hash(sender_staff_id)}")
                else:
                    logger.warning(f"[Scheduler] Task {task['id']}: DingTalk single chat message missing sender_staff_id")
        elif channel_type == "wecom_bot":
            context["msg"] = None
        elif channel_type == "qq":
            context["msg"] = None

        # Create reply
        reply = Reply(ReplyType.TEXT, content)
        
        # Get channel and send
        from channel.channel_factory import create_channel
        
        channel = create_channel(channel_type)
        if not channel:
            logger.error(f"[Scheduler] Failed to create channel: {channel_type}")
            return False

        if channel_type == "web" and hasattr(channel, 'request_to_session'):
            channel.request_to_session[request_id] = receiver

        try:
            delivered = _send_channel_reply(channel, reply, context, channel_type)
        except Exception as e:
            logger.error(f"[Scheduler] Failed to send message: {_body_summary(e)}")
            return False
        if not delivered:
            return False

        _remember_delivered_output(agent_bridge, task, channel_type, content)
        logger.info(f"[Scheduler] Task {task['id']} executed: sent message to receiver_hash={_summary_hash(receiver)}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_send_message: {_body_summary(e)}")
        return False


def _execute_tool_call(task: dict, agent_bridge) -> bool:
    """Execute a tool_call action. Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        tool_name = action.get("call_name") or action.get("tool_name")
        tool_params = action.get("call_params") or action.get("tool_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")

        if not tool_name:
            logger.error(f"[Scheduler] Task {task['id']}: No tool_name specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "tool_call action is missing tool_name.",
            )
            return True
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "tool_call action is missing receiver.",
            )
            return True

        from agent.tools.tool_manager import ToolManager
        tool = ToolManager().create_tool(tool_name)
        if not tool:
            logger.error(f"[Scheduler] Task {task['id']}: Tool '{tool_name}' not found")
            _mark_scheduler_task_failed(
                task,
                "scheduler_tool_not_found",
                "SCHEDULER_TOOL_NOT_FOUND",
                f"Tool '{tool_name}' was not found.",
            )
            return True

        if not _authorize_scheduled_tool_call(tool, tool_name, tool_params, task):
            _mark_scheduler_task_failed(
                task,
                "scheduler_tool_permission_denied",
                "SCHEDULER_TOOL_PERMISSION_DENIED",
                f"Tool '{tool_name}' was blocked by the permission boundary.",
            )
            return True

        logger.info(f"[Scheduler] Task {task['id']}: Executing tool '{tool_name}' with params {_params_summary(tool_params)}")
        had_cancel_event = hasattr(tool, "cancel_event")
        previous_cancel_event = getattr(tool, "cancel_event", None)
        injected_cancel_event = None
        try:
            from agent.protocol import get_cancel_registry

            injected_cancel_event = get_cancel_registry().get_event(_scheduler_run_request_id(task))
        except Exception:
            injected_cancel_event = None
        if injected_cancel_event is not None:
            tool.cancel_event = injected_cancel_event
        try:
            result = tool.execute(tool_params)
        finally:
            if injected_cancel_event is not None and not had_cancel_event:
                try:
                    delattr(tool, "cancel_event")
                except Exception:
                    pass
            elif injected_cancel_event is not None:
                tool.cancel_event = previous_cancel_event
        content = result.result if hasattr(result, 'result') else str(result)
        if result_prefix:
            content = f"{result_prefix}\n\n{content}"

        context = Context(ContextType.TEXT, content)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = receiver

        request_id = None
        if channel_type == "web":
            request_id = _scheduler_run_request_id(task)
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "wecom_bot":
            context["msg"] = None

        reply = Reply(ReplyType.TEXT, content)

        from channel.channel_factory import create_channel
        channel = create_channel(channel_type)
        if not channel:
            logger.error(f"[Scheduler] Failed to create channel: {channel_type}")
            return False

        if channel_type == "web" and request_id and hasattr(channel, 'request_to_session'):
            channel.request_to_session[request_id] = receiver

        try:
            delivered = _send_channel_reply(channel, reply, context, channel_type)
        except Exception as e:
            logger.error(f"[Scheduler] Failed to send tool result: {_body_summary(e)}")
            return False
        if not delivered:
            return False

        _remember_delivered_output(agent_bridge, task, channel_type, content)
        logger.info(f"[Scheduler] Task {task['id']} executed: sent tool result to receiver_hash={_summary_hash(receiver)}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_tool_call: {_body_summary(e)}")
        return False


def _execute_skill_call(task: dict, agent_bridge) -> bool:
    """Execute a skill_call action by asking Agent to run the skill.
    Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        skill_name = action.get("call_name") or action.get("skill_name")
        skill_params = action.get("call_params") or action.get("skill_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("isgroup", False)
        channel_type = action.get("channel_type", "unknown")

        if not skill_name:
            logger.error(f"[Scheduler] Task {task['id']}: No skill_name specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "skill_call action is missing skill_name.",
            )
            return True
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _mark_scheduler_task_failed(
                task,
                "scheduler_malformed_task",
                "SCHEDULER_MALFORMED_TASK",
                "skill_call action is missing receiver.",
            )
            return True

        logger.info(f"[Scheduler] Task {task['id']}: Executing skill '{skill_name}' with params {_params_summary(skill_params)}")

        scheduler_session_id = _scheduler_session_id(task)
        param_str = ", ".join([f"{k}={v}" for k, v in skill_params.items()])
        query = f"Use {skill_name} skill"
        if param_str:
            query += f" with {param_str}"

        context = Context(ContextType.TEXT, query)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id

        if channel_type == "web":
            request_id = _scheduler_run_request_id(task)
            context["request_id"] = request_id
            context["cancel_token_owner"] = "scheduler"
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "wecom_bot":
            context["msg"] = None

        try:
            reply = agent_bridge.agent_reply(query, context=context, on_event=None, clear_history=False)
        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute skill via Agent: {_body_summary(e)}")
            return False

        if not (reply and reply.content):
            logger.error(f"[Scheduler] Task {task['id']}: No result from skill execution")
            _mark_scheduler_task_failed(
                task,
                "scheduler_empty_result",
                "SCHEDULER_EMPTY_RESULT",
                "Skill execution completed without reply content.",
            )
            return True

        content = reply.content
        if result_prefix:
            content = f"{result_prefix}\n\n{content}"

        from channel.channel_factory import create_channel
        channel = create_channel(channel_type)
        if not channel:
            logger.error(f"[Scheduler] Failed to create channel: {channel_type}")
            return False

        if channel_type == "web" and hasattr(channel, 'request_to_session'):
            req_id = context.get("request_id")
            if req_id:
                channel.request_to_session[req_id] = receiver

        try:
            delivered = _send_channel_reply(channel, Reply(ReplyType.TEXT, content), context, channel_type)
        except Exception as e:
            logger.error(f"[Scheduler] Failed to send skill result: {_body_summary(e)}")
            return False
        if not delivered:
            return False

        _remember_delivered_output(agent_bridge, task, channel_type, content)
        logger.info(f"[Scheduler] Task {task['id']} executed: skill result sent to receiver_hash={_summary_hash(receiver)}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_skill_call: {_body_summary(e)}")
        return False


def attach_scheduler_to_tool(tool, context: Context = None):
    """
    Attach scheduler components to a SchedulerTool instance
    
    Args:
        tool: SchedulerTool instance
        context: Current context (optional)
    """
    if _task_store is None and conf().get("scheduler_enabled", False):
        ensure_scheduler_runtime()

    if _task_store:
        tool.task_store = _task_store
    
    if context:
        tool.current_context = context
        
        channel_type = context.get("channel_type") or conf().get("channel_type", "unknown")
        if isinstance(channel_type, str) and "," in channel_type:
            channel_type = channel_type.split(",", 1)[0].strip() or "unknown"
        elif isinstance(channel_type, (list, tuple)):
            channel_type = str(channel_type[0]).strip() if channel_type else "unknown"
        if not tool.config:
            tool.config = {}
        tool.config["channel_type"] = channel_type

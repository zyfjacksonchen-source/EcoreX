"""EcoreX-native messaging adapter contract helpers.

This module deliberately stays above the platform SDKs and below the web UI:
it describes and validates how platform messages enter EcoreX's existing
ChatChannel pipeline without introducing a second queue or Hermes runtime.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from typing import Any, Dict, Mapping, Optional

from bridge.context import Context
from bridge.reply import Reply
from channel.channel_catalog import (
    active_channel_set,
    channel_config_status,
    channel_requires_complete_config,
    channel_requires_runtime_authorization,
    normalize_channel_name,
)
from common.ecorex_public_payload import mask_sensitive_text
from common.expired_dict import ExpiredDict
from common.log import logger
from config import conf


CONTRACT_VERSION = "ecorex.messaging_adapter.v1"
INGRESS_QUEUE = "ChatChannel.produce"
EGRESS_ENTRYPOINT = "Channel.send"
DEDUP_TTL_SECONDS = 60 * 60 * 8
EXTERNAL_CONNECTION_EVENT_SESSION_ID = "external_connections"


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", None) or value or "")


def _safe_string(value: Any, *, max_chars: int = 500) -> str:
    if value is None:
        return ""
    return mask_sensitive_text(value, max_chars=max_chars)


def _body_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    if not text:
        return {
            "contentPreview": "",
            "contentHash": "",
            "contentLength": 0,
            "contentBytes": 0,
        }
    return {
        "contentPreview": "[redacted-content]",
        "contentHash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
        "contentLength": len(text),
        "contentBytes": len(text.encode("utf-8", errors="replace")),
    }


def _identity_summary(value: Any) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    encoded = text.encode("utf-8", errors="replace")
    return {
        "redacted": bool(text),
        "hash": hashlib.sha256(encoded).hexdigest()[:16] if text else "",
        "chars": len(text),
        "bytes": len(encoded),
    }


def _safe_error_type(value: Any) -> str:
    raw = str(getattr(value, "__name__", None) or value or "Error").strip()
    if raw and len(raw) <= 80 and raw[0].isalpha() and all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for char in raw):
        return raw
    return "Error"


def _public_error_summary(exc: BaseException, *, message: str = "operation_failed") -> Dict[str, Any]:
    text = str(exc or "")
    encoded = text.encode("utf-8", errors="replace")
    return {
        "message": message,
        "errorType": _safe_error_type(exc.__class__),
        "errorHash": hashlib.sha256(encoded).hexdigest()[:16] if text else "",
        "errorLength": len(text),
        "errorBytes": len(encoded),
        "redacted": True,
    }


def _safe_event_token(value: Any, *, fallback: str = "event") -> str:
    raw = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if raw and len(raw) <= 96 and all(char in allowed for char in raw) and mask_sensitive_text(raw, max_chars=2048) == raw:
        return raw
    if not raw:
        raw = f"{fallback}:{time.time()}:{threading.get_ident()}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_event_identifier(value: Any, *, fallback: str = "event") -> str:
    raw = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if raw and len(raw) <= 160 and all(char in allowed for char in raw) and mask_sensitive_text(raw, max_chars=2048) == raw:
        return raw
    return _safe_event_token(raw, fallback=fallback)


def _external_connection_request_id(platform: str, operation_id: str = "", event_type: str = "") -> str:
    platform_token = _safe_event_token(normalize_channel_name(platform) or platform or "unknown", fallback="platform")
    operation_token = _safe_event_token(operation_id, fallback=event_type or "operation")
    return f"external-connection:{platform_token}:{operation_token}"


def record_external_connection_runtime_event(
    platform: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    request_id: str = "",
    operation_id: str = "",
    idempotency_key: str = "",
) -> Dict[str, Any]:
    """Best-effort RunEventLedger writer for external connection lifecycle events."""
    normalized_platform = normalize_channel_name(platform) or str(platform or "unknown").strip() or "unknown"
    normalized_event_type = str(event_type or "").strip()
    if not normalized_event_type.startswith("external_connection."):
        normalized_event_type = f"external_connection.{normalized_event_type or 'event'}"
    safe_payload = dict(payload or {})
    safe_payload.update({
        "platform": normalized_platform,
        "contractVersion": CONTRACT_VERSION,
    })
    operation_token = str(operation_id or safe_payload.get("operation_id") or safe_payload.get("operationId") or "").strip()
    safe_operation_token = _safe_event_token(operation_token, fallback=normalized_event_type or "operation")
    for key in ("operation_id", "operationId"):
        if key in safe_payload:
            safe_payload[key] = safe_operation_token
    raw_request_id = str(request_id or "").strip()
    safe_request_id = (
        _safe_event_identifier(raw_request_id, fallback=normalized_event_type)
        if raw_request_id
        else _external_connection_request_id(
            normalized_platform,
            safe_operation_token,
            normalized_event_type,
        )
    )
    safe_key = str(idempotency_key or "").strip()
    try:
        from agent.protocol import get_run_event_ledger

        return get_run_event_ledger().append_event(
            request_id=safe_request_id,
            session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID,
            event_type=normalized_event_type,
            payload=safe_payload,
            idempotency_key=safe_key,
            source="external_connections",
        )
    except Exception as exc:
        logger.debug(
            "[MessagingAdapter] external connection runtime event skipped: "
            f"platform={normalized_platform} event={normalized_event_type} error={_body_summary(exc)}"
        )
        return {
            "recorded": False,
            "event_type": normalized_event_type,
            "request_id": safe_request_id,
        }


def _context_platform(context: Optional[Context], platform: str = "") -> str:
    if platform:
        return platform
    if context is None:
        return ""
    return str(context.get("channel_type", "") or "").strip()


def normalize_chat_message(message: Any, *, platform: str = "") -> Dict[str, Any]:
    """Return a secret-free summary of a platform ChatMessage-like object."""
    if message is None:
        return {}
    is_group = bool(getattr(message, "is_group", False))
    return {
        "platform": platform,
        "messageId": str(getattr(message, "msg_id", "") or ""),
        "createdAt": getattr(message, "create_time", None),
        "type": _enum_name(getattr(message, "ctype", "")),
        **_body_summary(getattr(message, "content", "")),
        "fromUserId": str(getattr(message, "from_user_id", "") or ""),
        "toUserId": str(getattr(message, "to_user_id", "") or ""),
        "otherUserId": str(getattr(message, "other_user_id", "") or ""),
        "isGroup": is_group,
        "isAt": bool(getattr(message, "is_at", False)),
        "actualUserId": str(getattr(message, "actual_user_id", "") or "") if is_group else "",
    }


def normalize_inbound_context(context: Context, *, platform: str = "") -> Dict[str, Any]:
    """Return the common receive DTO used by adapter tests and projections."""
    resolved_platform = _context_platform(context, platform)
    message = context.get("msg")
    session_id = str(context.get("session_id", "") or "")
    receiver = str(context.get("receiver", "") or "")
    return {
        "contractVersion": CONTRACT_VERSION,
        "direction": "inbound",
        "platform": resolved_platform,
        "contextType": _enum_name(context.type),
        **_body_summary(context.content),
        "sessionId": session_id,
        "receiver": receiver,
        "isGroup": bool(context.get("isgroup", False) or getattr(message, "is_group", False)),
        "message": normalize_chat_message(message, platform=resolved_platform),
        "queue": {
            "entrypoint": INGRESS_QUEUE,
            "owner": "ecorex_chat_channel",
            "usesHermesActiveSessionQueue": False,
        },
    }


def normalize_reply_delivery(reply: Reply, context: Optional[Context] = None, *, platform: str = "") -> Dict[str, Any]:
    resolved_platform = _context_platform(context, platform)
    session_id = str(context.get("session_id", "") or "") if context else ""
    receiver = str(context.get("receiver", "") or "") if context else ""
    session_summary = _identity_summary(session_id)
    receiver_summary = _identity_summary(receiver)
    return {
        "contractVersion": CONTRACT_VERSION,
        "direction": "outbound",
        "platform": resolved_platform,
        "replyType": _enum_name(getattr(reply, "type", "")),
        **_body_summary(getattr(reply, "content", "")),
        "sessionHash": session_summary["hash"],
        "receiverHash": receiver_summary["hash"],
        "sessionSummary": session_summary,
        "receiverSummary": receiver_summary,
        "entrypoint": EGRESS_ENTRYPOINT,
    }


def ingress_dedupe_key(context: Optional[Context], *, platform: str = "") -> str:
    """Build a scoped key for idempotent ingress.

    Dedupe is intentionally enabled only when a real external message id is
    present. Internal flows without a ChatMessage id keep their existing
    behaviour and are never suppressed by this helper.
    """
    if context is None:
        return ""
    message = context.get("msg")
    message_id = str(
        getattr(message, "msg_id", "")
        or context.get("external_message_id", "")
        or context.get("message_id", "")
        or ""
    ).strip()
    if not message_id:
        return ""
    resolved_platform = _context_platform(context, platform) or "unknown"
    session_id = str(context.get("session_id", "") or getattr(message, "other_user_id", "") or "").strip()
    context_type = _enum_name(context.type)
    return f"{resolved_platform}:{session_id}:{context_type}:{message_id}"


class MessageIngressGate:
    """Thread-safe TTL dedupe gate for platform redelivery/reconnect cases."""

    def __init__(self, ttl_seconds: int = DEDUP_TTL_SECONDS):
        self._seen = ExpiredDict(ttl_seconds)
        self._lock = threading.RLock()

    def check_and_mark(self, key: str) -> Dict[str, Any]:
        if not key:
            return {"accepted": True, "deduped": False, "key": ""}
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            if self._seen.get(key_hash):
                return {"accepted": False, "deduped": True, "key": key_hash}
            self._seen[key_hash] = int(time.time())
        return {"accepted": True, "deduped": False, "key": key_hash}

    def forget(self, key: str) -> None:
        if not key:
            return
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            try:
                del self._seen[key_hash]
            except KeyError:
                pass


DEFAULT_INGRESS_GATE = MessageIngressGate()


PROACTIVE_REQUIRED_CONTEXT = {
    "slack": ["slack_channel"],
    "telegram": ["telegram_chat_id"],
    "discord": ["discord_channel_id"],
    "weixin": ["context_token_or_msg"],
    "dingtalk": ["dingtalk_sender_staff_id"],
}

DELIVERY_MODES = {
    "web": "polling_ui",
    "wechatmp": "passive_response_cache",
    "wechatmp_service": "proactive",
    "wechatcom_app": "inbound_reply",
    "weixin": "inbound_reply",
    "feishu": "proactive",
    "dingtalk": "inbound_reply",
    "wecom_bot": "proactive",
    "qq": "proactive",
    "telegram": "inbound_reply",
    "slack": "inbound_reply",
    "discord": "inbound_reply",
}


def _default_manager() -> Any:
    try:
        app_module = sys.modules.get("__main__") or sys.modules.get("app")
        return getattr(app_module, "_channel_mgr", None) if app_module else None
    except Exception:
        return None


def _live_channel(manager: Any, channel_name: str) -> Any:
    if manager is None or not hasattr(manager, "get_channel"):
        return None
    try:
        return manager.get_channel(channel_name)
    except Exception:
        return None


def _manager_thread_alive(manager: Any, channel_name: str) -> bool:
    try:
        thread = getattr(manager, "_threads", {}).get(channel_name)
        return bool(thread is not None and getattr(thread, "is_alive", lambda: False)())
    except Exception:
        return False


def _startup_status(channel: Any, manager: Any, channel_name: str) -> Dict[str, Any]:
    if channel is None:
        return {"running": False, "readiness": "unknown", "lastError": ""}
    error = mask_sensitive_text(getattr(channel, "_startup_error", "") or "", max_chars=500)
    if error:
        return {"running": False, "readiness": "error", "lastError": error}
    event = getattr(channel, "_startup_event", None)
    if event is not None and hasattr(event, "is_set") and event.is_set():
        return {"running": True, "readiness": "ready", "lastError": ""}
    if _manager_thread_alive(manager, channel_name):
        if normalize_channel_name(channel_name) == "web":
            return {"running": True, "readiness": "ready", "lastError": ""}
        return {"running": False, "readiness": "starting", "lastError": ""}
    return {"running": False, "readiness": "stopped", "lastError": ""}


def _context_has(context: Optional[Context], key: str, *, receiver: str = "") -> bool:
    if key == "context_token_or_msg":
        if context is None:
            return False
        msg = context.get("msg")
        if msg is not None and getattr(msg, "context_token", ""):
            return True
        return bool(context.get("context_token"))
    if key == "dingtalk_sender_staff_id":
        if context is None:
            return False
        if context.get("isgroup", False):
            return True
        return bool(context.get(key))
    if context is None:
        return False
    return bool(context.get(key))


def _required_context(channel_name: str, context: Optional[Context], receiver: str = "") -> Dict[str, Any]:
    required = list(PROACTIVE_REQUIRED_CONTEXT.get(channel_name, []))
    missing = [key for key in required if not _context_has(context, key, receiver=receiver)]
    if channel_name in {"feishu", "wecom_bot", "qq", "wechatmp_service", "wechatcom_app", "web"} and not (receiver or (context and context.get("receiver"))):
        required.append("receiver")
        missing.append("receiver")
    return {"requiredContext": required, "missingContext": missing}


def probe_messaging_adapter(
    channel_name: str,
    *,
    manager: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    context: Optional[Context] = None,
    receiver: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only adapter readiness probe.

    This never creates a channel, imports vendor SDKs, or touches private web
    queues. Unknown manager state remains explicit instead of being guessed.
    """
    name = normalize_channel_name(channel_name)
    cfg = dict(config if config is not None else conf())
    cfg_status = channel_config_status(cfg, name)
    active = name in active_channel_set(cfg)
    resolved_manager = manager if manager is not None else _default_manager()
    live = _live_channel(resolved_manager, name)
    startup = _startup_status(live, resolved_manager, name)
    requires_complete_config = channel_requires_complete_config(name)
    requires_runtime_authorization = channel_requires_runtime_authorization(name)
    if requires_complete_config:
        configured = cfg_status.get("state") == "configured"
    elif requires_runtime_authorization and cfg_status.get("state") == "not_required":
        configured = bool(startup["running"] and startup["readiness"] == "ready")
    else:
        configured = cfg_status.get("state") in {"configured", "not_required"} or (name == "web" and active)
    missing_required_config = not configured and cfg_status.get("state") != "not_required"
    missing_runtime_authorization = bool(requires_runtime_authorization and not configured)
    if missing_required_config:
        readiness = "not_configured"
    elif missing_runtime_authorization:
        readiness = "auth_required"
    elif startup["readiness"] in {"error", "starting", "ready", "stopped"}:
        readiness = startup["readiness"]
    else:
        readiness = "unknown"
    running = bool(startup["running"])
    if (requires_complete_config or requires_runtime_authorization) and not configured:
        running = False

    resolved_receiver = str(receiver or (context.get("receiver") if context else "") or "")
    requirements = _required_context(name, context, receiver=resolved_receiver)
    outbound_supported = name not in {"unknown", ""}
    proactive_supported = DELIVERY_MODES.get(name, "inbound_reply") in {"proactive", "polling_ui"}
    safe_to_send = bool(outbound_supported and active and not requirements["missingContext"])
    if not active:
        safe_to_send = False
    if startup["readiness"] in {"error", "starting", "stopped"}:
        safe_to_send = False
    if missing_required_config or missing_runtime_authorization:
        safe_to_send = False
    if name == "wechatmp":
        safe_to_send = False
    reason = ""
    if not active:
        reason = "channel is not enabled"
    elif missing_required_config:
        reason = "channel is not configured"
    elif missing_runtime_authorization:
        reason = "channel authorization is required"
    elif requirements["missingContext"]:
        reason = "missing required send context: " + ", ".join(requirements["missingContext"])
    elif startup["lastError"]:
        reason = startup["lastError"]
    elif startup["readiness"] in {"starting", "stopped"}:
        reason = f"channel is {startup['readiness']}"
    elif startup["readiness"] == "unknown":
        reason = "no live channel manager state is available"
    elif name == "wechatmp":
        reason = "wechatmp passive channel uses response cache, not proactive send"
    else:
        reason = "projection-ready"
    return {
        "version": CONTRACT_VERSION,
        "channel": name,
        "configured": configured,
        "enabled": active,
        "running": running,
        "readiness": readiness,
        "deliveryMode": DELIVERY_MODES.get(name, "inbound_reply"),
        "inboundNormalized": True,
        "outboundSupported": outbound_supported,
        "proactiveSupported": proactive_supported,
        "requiredContext": requirements["requiredContext"],
        "missingContext": requirements["missingContext"],
        "lastError": startup["lastError"],
        "safeToSend": safe_to_send,
        "reason": reason,
        "queueOwner": "ChatChannel",
        "usesHermesActiveSessionQueue": False,
        "probeMode": "projection",
    }


def test_messaging_adapter(channel_name: str, *, manager: Any = None, config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    result = probe_messaging_adapter(channel_name, manager=manager, config=config)
    result["testMode"] = "projection_dry_run"
    result["remoteConnectivityProbed"] = False
    return result


def send_messaging_reply(
    channel_name: str,
    reply: Reply,
    context: Context,
    *,
    manager: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    state = probe_messaging_adapter(channel_name, manager=manager, context=context)
    if not state["safeToSend"]:
        record_external_connection_runtime_event(
            state["channel"],
            "external_connection.delivery.blocked",
            {
                "status": "blocked",
                "reason": state.get("reason", ""),
                "adapter": state,
            },
        )
        return {"status": "blocked", "adapter": state}
    if dry_run:
        record_external_connection_runtime_event(
            state["channel"],
            "external_connection.delivery.dry_run",
            {
                "status": "dry_run",
                "adapter": state,
            },
        )
        return {"status": "dry_run", "adapter": state}
    resolved_manager = manager if manager is not None else _default_manager()
    live = _live_channel(resolved_manager, state["channel"])
    if live is None:
        state = dict(state)
        state["safeToSend"] = False
        state["reason"] = "live channel instance is unavailable"
        record_external_connection_runtime_event(
            state["channel"],
            "external_connection.delivery.blocked",
            {
                "status": "blocked",
                "reason": state.get("reason", ""),
                "adapter": state,
            },
        )
        return {"status": "blocked", "adapter": state}
    result = deliver_reply(live, reply, context, platform=state["channel"])
    return {"status": result.get("status"), "adapter": state, "delivery": result.get("delivery"), "error": result.get("error", "")}


def produce_context_once(
    channel: Any,
    context: Context,
    *,
    gate: Optional[MessageIngressGate] = None,
    platform: str = "",
) -> Dict[str, Any]:
    """Submit one inbound context to the existing ChatChannel queue once."""
    dedupe_gate = gate or DEFAULT_INGRESS_GATE
    key = ingress_dedupe_key(context, platform=platform)
    decision = dedupe_gate.check_and_mark(key)
    inbound = normalize_inbound_context(context, platform=platform)
    if not decision["accepted"]:
        logger.info(
            "[MessagingAdapter] duplicate inbound skipped: "
            f"platform={inbound.get('platform')} session={inbound.get('sessionId')}"
        )
        record_external_connection_runtime_event(
            inbound.get("platform") or platform or "unknown",
            "external_connection.ingress.duplicate",
            {
                "status": "duplicate",
                "accepted": False,
                "deduped": True,
                "dedupeKey": decision["key"],
                "inbound": inbound,
            },
        )
        return {
            "status": "duplicate",
            "accepted": False,
            "deduped": True,
            "dedupeKey": decision["key"],
            "inbound": inbound,
        }
    try:
        if not hasattr(channel, "produce"):
            raise TypeError("channel does not implement ChatChannel.produce")
        try:
            context["_adapter_dedupe_accepted"] = True
            if key:
                context["_adapter_dedupe_key"] = key
                context["_adapter_dedupe_gate"] = dedupe_gate
            channel.produce(context)
        finally:
            try:
                del context["_adapter_dedupe_accepted"]
            except Exception:
                pass
        record_external_connection_runtime_event(
            inbound.get("platform") or platform or "unknown",
            "external_connection.ingress.queued",
            {
                "status": "queued",
                "accepted": True,
                "deduped": False,
                "dedupeKey": decision["key"],
                "inbound": inbound,
            },
        )
        return {
            "status": "queued",
            "accepted": True,
            "deduped": False,
            "dedupeKey": decision["key"],
            "inbound": inbound,
        }
    except Exception:
        record_external_connection_runtime_event(
            inbound.get("platform") or platform or "unknown",
            "external_connection.ingress.failed",
            {
                "status": "failed",
                "accepted": False,
                "deduped": False,
                "dedupeKey": decision["key"],
                "inbound": inbound,
                "error": "queue_failed",
            },
        )
        dedupe_gate.forget(key)
        raise


def deliver_reply(
    channel: Any,
    reply: Reply,
    context: Context,
    *,
    decorated: bool = False,
    platform: str = "",
) -> Dict[str, Any]:
    """Send through the existing channel implementation and report the result."""
    delivery = normalize_reply_delivery(reply, context, platform=platform)
    try:
        if decorated and hasattr(channel, "_send_reply"):
            channel._send_reply(context, reply)
        elif hasattr(channel, "send"):
            channel.send(reply, context)
        else:
            raise TypeError("channel does not implement send")
        record_external_connection_runtime_event(
            delivery.get("platform") or platform or "unknown",
            "external_connection.delivery.sent",
            {
                "status": "sent",
                "delivery": delivery,
            },
        )
        return {"status": "sent", "delivery": delivery}
    except Exception as exc:
        error_summary = _public_error_summary(exc, message="delivery_failed")
        record_external_connection_runtime_event(
            delivery.get("platform") or platform or "unknown",
            "external_connection.delivery.error",
            {
                "status": "error",
                "error": "delivery_failed",
                "errorSummary": error_summary,
                "delivery": delivery,
            },
        )
        return {
            "status": "error",
            "error": "delivery_failed",
            "errorSummary": error_summary,
            "delivery": delivery,
        }


def build_adapter_contract(
    channel_name: str,
    observed: Optional[Mapping[str, Any]] = None,
    *,
    runtime_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe the adapter boundary without probing remote services."""
    projection = dict(observed or {})
    runtime = dict(runtime_state or {})
    auth = projection.get("auth") if isinstance(projection.get("auth"), dict) else {}
    agent_surface = projection.get("agentSurface") if isinstance(projection.get("agentSurface"), dict) else {}
    return {
        "version": CONTRACT_VERSION,
        "platform": channel_name,
        "readiness": {
            "configured": bool(projection.get("configured")),
            "enabled": bool(projection.get("active")),
            "running": bool(projection.get("running")),
            "status": str(projection.get("status") or runtime.get("status") or "unknown"),
            "configState": projection.get("configState") or auth.get("channelConfigState") or "unknown",
            "callable": bool(agent_surface.get("callable")),
            "lastError": mask_sensitive_text(runtime.get("last_error", ""), max_chars=500),
            "dependencyMissing": bool(runtime.get("dependency_missing") or False),
            "dependencyStatus": runtime.get("dependency_status") if isinstance(runtime.get("dependency_status"), dict) else {},
        },
        "lifecycle": {
            "start": "ChannelManager.start",
            "stop": "ChannelManager.stop",
            "readiness": "channel_observability",
            "test": "ExternalConnectionActionHandler.test",
        },
        "ingress": {
            "entrypoint": INGRESS_QUEUE,
            "normalization": "Context + ChatMessage",
            "dedupe": "MessageIngressGate scoped by platform/session/type/messageId",
            "queueOwner": "ChatChannel",
            "usesHermesActiveSessionQueue": False,
        },
        "egress": {
            "entrypoint": EGRESS_ENTRYPOINT,
            "decoratedEntrypoint": "ChatChannel._send_reply",
            "replyContract": "Reply(type, content)",
            "retryOwner": "ChatChannel._send",
        },
        "projection": {
            "backendCanonical": True,
            "frontendLocalStateCanonical": False,
            "runtimeLedgerExpected": True,
        },
    }

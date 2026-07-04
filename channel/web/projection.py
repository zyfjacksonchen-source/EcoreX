"""Runtime projection and active request Web handlers."""

import json
from typing import Any

import web

from channel.web.handler_support import (
    public_exception_message,
    public_exception_summary,
    require_auth,
    web_body_log_summary,
)
from common.log import logger


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


def runtime_projection_public_payload(value: Any, *, include_events: bool = False, _depth: int = 0) -> Any:
    if isinstance(value, list):
        return [
            runtime_projection_public_payload(item, include_events=include_events, _depth=_depth + 1)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    public = {}
    for key, item in value.items():
        if key == "events" and not (include_events and _depth == 0):
            continue
        public[key] = runtime_projection_public_payload(
            item,
            include_events=include_events,
            _depth=_depth + 1,
        )
    return public


class ActiveRequestsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        return json.dumps(_legacy_web_channel().WebChannel().active_requests_snapshot(), ensure_ascii=False)


class RequestRetryPrepareHandler:
    def POST(self, request_id):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "message": "payload too large"}, ensure_ascii=False)
            payload = json.loads(raw) if raw else {}
            session_id = str(payload.get("session_id") or "").strip()
            return json.dumps(
                _legacy_web_channel().WebChannel().prepare_request_retry(str(request_id or ""), session_id=session_id),
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.error(f"[WebChannel] retry prepare error: {web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": public_exception_message("Retry preparation failed.", exc),
                **public_exception_summary(exc),
            }, ensure_ascii=False)


class RequestQueueActionHandler:
    def POST(self, request_id):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            return json.dumps(
                _legacy_web_channel().WebChannel().queue_action_request(str(request_id or "")),
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.error(f"[WebChannel] queue action error: {web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": public_exception_message("Queue action failed.", exc),
                **public_exception_summary(exc),
            }, ensure_ascii=False)


class RuntimeProjectionHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            params = web.input(
                request_id="",
                session_id="",
                after_event_id="0",
                limit="1000",
                include_events="",
                history_page="",
                page_size="20",
            )
            request_id = str(params.request_id or "").strip()
            session_id = str(params.session_id or "").strip()
            after_event_id = int(params.after_event_id or 0)
            limit = min(max(1, int(params.limit or 1000)), 1000)
            include_events = str(params.include_events or "").strip().lower() in {"1", "true", "yes", "on"}
            history_page = int(getattr(params, "history_page", "") or 0)
            page_size = min(max(1, int(getattr(params, "page_size", 20) or 20)), 200)
            from agent.protocol import RuntimeProjectionService

            service = RuntimeProjectionService()
            if request_id:
                owner_session_id = str(service.owner_session_id_for_request(request_id) or "").strip()
                if session_id and owner_session_id and owner_session_id != session_id:
                    try:
                        web.ctx.status = "409 Conflict"
                    except Exception:
                        pass
                    return json.dumps({
                        "status": "error",
                        "code": "SESSION_MISMATCH",
                        "error_type": "session_mismatch",
                        "message": "Request does not belong to the active session. Refresh the conversation list and retry.",
                        "recoverable": True,
                        "retryable": False,
                    }, ensure_ascii=False)
                projection = service.request_projection(
                    request_id,
                    expected_session_id=session_id,
                    include_events=include_events,
                )
                if include_events:
                    projection = dict(projection)
                    projection["events"] = list(projection.get("events") or [])[-limit:]
                projection = runtime_projection_public_payload(projection, include_events=include_events)
                return json.dumps({
                    "status": "success",
                    "mode": "request",
                    "projection": projection,
                    "latest_event_id": projection.get("latest_event_id", 0),
                }, ensure_ascii=False)
            if session_id:
                if history_page > 0:
                    projection = service.session_history_projection(
                        session_id,
                        page=history_page,
                        page_size=page_size,
                        after_event_id=after_event_id,
                        limit=limit,
                        include_events=include_events,
                    )
                else:
                    projection = service.session_projection(
                        session_id,
                        after_event_id=after_event_id,
                        limit=limit,
                        include_events=include_events,
                    )
                projection = runtime_projection_public_payload(projection, include_events=include_events)
                return json.dumps({
                    "status": "success",
                    "mode": "session_history" if history_page > 0 else "session",
                    "projection": projection,
                    "latest_event_id": projection.get("latest_event_id", after_event_id),
                }, ensure_ascii=False)
            return json.dumps({"status": "error", "message": "request_id or session_id required"}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] runtime projection error: {web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": public_exception_message("Runtime projection unavailable.", exc),
                **public_exception_summary(exc),
            }, ensure_ascii=False)

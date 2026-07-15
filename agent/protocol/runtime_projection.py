"""Runtime projection helpers built from durable run events."""

from __future__ import annotations

import hashlib
import html
import copy
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common.ecorex_public_payload import mask_sensitive_text, redact_public_tool_value

from .run_event_ledger import RunEventLedger, get_run_event_ledger


TERMINAL_EVENT_STATES = {
    "run.completed": "completed",
    "run.error": "failed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}

_SUBAGENT_STATUS_VALUES = {"starting", "running", "completed", "failed", "timeout", "cancelled"}
EXTERNAL_CONNECTION_EVENT_SESSION_ID = "external_connections"
_EXTERNAL_CONNECTION_STATUS_VALUES = {
    "success",
    "configured",
    "starting",
    "stopping",
    "active",
    "ready",
    "connected",
    "sent",
    "queued",
    "duplicate",
    "dry_run",
    "blocked",
    "error",
    "failed",
    "stopped",
    "unknown",
}
_GENERIC_CONTENT_PAYLOAD_PREFIXES = {
    "content": "content",
    "contents": "content",
    "body": "body",
    "text": "text",
    "message": "message",
    "delta": "delta",
    "finaltext": "finalText",
    "prompt": "prompt",
    "prompts": "prompt",
    "instruction": "instruction",
    "instructions": "instruction",
    "input": "input",
    "output": "output",
    "query": "query",
    "answer": "answer",
    "transcript": "transcript",
    "usermessage": "userMessage",
    "assistantmessage": "assistantMessage",
    "errormessage": "errorMessage",
    "contentpreview": "content",
    "messagepreview": "message",
    "textpreview": "text",
}
_GENERIC_CONTENT_PAYLOAD_SUFFIXES = (
    "content",
    "contents",
    "body",
    "text",
    "message",
    "prompt",
    "prompts",
    "instruction",
    "instructions",
    "input",
    "output",
    "query",
    "answer",
    "transcript",
)
_GENERIC_CONTENT_PAYLOAD_PREFIX_MATCHES = (
    "prompt",
    "instruction",
)
_GENERIC_CONTENT_PAYLOAD_EXCLUSIONS = {
    "contenthash",
    "contentlength",
    "contentbytes",
    "contentredacted",
    "messageid",
    "messageids",
    "messagetype",
    "promptbytes",
    "promptcount",
    "prompthash",
    "promptid",
    "promptlength",
    "prompttokens",
    "instructionbytes",
    "instructioncount",
    "instructionhash",
    "instructionid",
    "instructionlength",
    "instructiontokens",
    "inputtokens",
    "outputtokens",
    "inputcount",
    "outputcount",
    "status",
}
_GENERIC_STRUCTURAL_HASH_KEYS = {
    "contenthash",
    "prompthash",
    "instructionhash",
}
_GENERIC_STRUCTURAL_IDENTIFIER_KEYS = {
    "messageid",
    "messageids",
    "messagetype",
    "promptid",
    "instructionid",
    "status",
}
_GENERIC_STRUCTURAL_COUNT_KEYS = {
    "contentlength",
    "contentbytes",
    "promptbytes",
    "promptcount",
    "promptlength",
    "prompttokens",
    "instructionbytes",
    "instructioncount",
    "instructionlength",
    "instructiontokens",
    "inputtokens",
    "outputtokens",
    "inputcount",
    "outputcount",
}
_GENERIC_STRUCTURAL_BOOL_KEYS = {
    "contentredacted",
}
_GENERIC_STATUS_VALUES = {
    "ok",
    "success",
    "error",
    "failed",
    "running",
    "completed",
    "cancelled",
    "canceled",
    "pending",
    "blocked",
    "warning",
    "info",
    "active",
    "inactive",
    "enabled",
    "disabled",
    "unknown",
}
_GENERIC_MESSAGE_TYPE_VALUES = {
    "user",
    "assistant",
    "system",
    "tool",
    "text",
    "image",
    "file",
    "event",
    "notification",
}


class RuntimeProjectionService:
    """Reduce runtime events into frontend-consumable request/session state."""

    def __init__(self, event_ledger: Optional[RunEventLedger] = None):
        self.event_ledger = event_ledger or get_run_event_ledger()
        self._request_projection_cache: Dict[Tuple[str, str, int, bool], Dict[str, Any]] = {}
        self._request_projection_cache_order: List[Tuple[str, str, int, bool]] = []
        self._request_projection_cache_max = 256
        self._session_projection_cache: Dict[Tuple[str, int, int, bool, int], Dict[str, Any]] = {}
        self._session_projection_cache_order: List[Tuple[str, int, int, bool, int]] = []
        self._session_projection_cache_max = 32

    def request_projection(
        self,
        request_id: str,
        *,
        expected_session_id: str = "",
        include_events: bool = True,
    ) -> Dict[str, Any]:
        owner_session_id = expected_session_id or self._owner_session_id_for_request(request_id)
        latest_event_id = self._latest_event_id_for_request(request_id)
        cached = self._request_projection_cache.get((
            str(request_id or ""),
            str(owner_session_id or ""),
            latest_event_id,
            bool(include_events),
        ))
        if cached is not None:
            return copy.deepcopy(cached)
        events = self.event_ledger.events_for_request(request_id, limit=0)
        events = _filter_request_events_for_owner(events, expected_session_id=owner_session_id)
        return self._project_request_events_cached(
            request_id,
            events,
            expected_session_id=owner_session_id,
            include_events=include_events,
        )

    def owner_session_id_for_request(self, request_id: str) -> str:
        return self._owner_session_id_for_request(request_id)

    def session_projection(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 1000,
        include_events: bool = True,
    ) -> Dict[str, Any]:
        latest_session_event_id = self._latest_event_id_for_session(session_id)
        session_cache_key = (
            str(session_id or ""),
            int(after_event_id or 0),
            int(limit or 0),
            bool(include_events),
            latest_session_event_id,
        )
        cached = self._session_projection_cache.get(session_cache_key)
        if cached is not None:
            return copy.deepcopy(cached)
        events = self.event_ledger.list_events(
            session_id=session_id,
            after_event_id=after_event_id,
            limit=limit,
        )
        latest_event_id = events[-1]["event_id"] if events else after_event_id
        owner_by_request: Dict[str, str] = {}
        events = [
            event
            for event in events
            if self._event_matches_session_owner(event, session_id, owner_by_request=owner_by_request)
        ]
        request_ids: List[str] = []
        for event in events:
            request_id = str(event.get("request_id") or "")
            if request_id and request_id not in request_ids:
                request_ids.append(request_id)
        events_by_request = self._events_for_requests(request_ids)
        projection = {
            "session_id": session_id,
            "after_event_id": after_event_id,
            "latest_event_id": latest_event_id,
            "requests": [
                self._project_request_events_cached(
                    request_id,
                    _filter_request_events_for_owner(
                        events_by_request.get(request_id) or [],
                        expected_session_id=session_id,
                    ),
                    expected_session_id=session_id,
                    include_events=include_events,
                )
                for request_id in request_ids
            ],
            "events": _safe_projection_events(events) if include_events else [],
        }
        self._session_projection_cache[session_cache_key] = copy.deepcopy(projection)
        self._session_projection_cache_order.append(session_cache_key)
        while len(self._session_projection_cache_order) > self._session_projection_cache_max:
            old_key = self._session_projection_cache_order.pop(0)
            self._session_projection_cache.pop(old_key, None)
        return projection

    def _events_for_requests(self, request_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
        batch_loader = getattr(self.event_ledger, "events_for_requests", None)
        if callable(batch_loader):
            return batch_loader(request_ids, limit=0)
        return {
            request_id: self.event_ledger.events_for_request(request_id, limit=0)
            for request_id in request_ids
        }

    def _latest_event_id_for_request(self, request_id: str) -> int:
        latest = getattr(self.event_ledger, "latest_event_id_for_request", None)
        if callable(latest):
            return int(latest(request_id) or 0)
        events = self.event_ledger.events_for_request(request_id, limit=0)
        return max((int(event.get("event_id") or 0) for event in events), default=0)

    def _latest_event_id_for_session(self, session_id: str) -> int:
        latest = getattr(self.event_ledger, "latest_event_id_for_session", None)
        if callable(latest):
            return int(latest(session_id) or 0)
        events = self.event_ledger.list_events(session_id=session_id, limit=0)
        return max((int(event.get("event_id") or 0) for event in events), default=0)

    def _project_request_events_cached(
        self,
        request_id: str,
        events: Iterable[Dict[str, Any]],
        *,
        expected_session_id: str = "",
        include_events: bool = True,
    ) -> Dict[str, Any]:
        event_list = list(events)
        latest_event_id = max((int(event.get("event_id") or 0) for event in event_list), default=0)
        key = (str(request_id or ""), str(expected_session_id or ""), latest_event_id, bool(include_events))
        cached = self._request_projection_cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
        projection = self.project_request_events(event_list, include_events=include_events)
        self._request_projection_cache[key] = copy.deepcopy(projection)
        self._request_projection_cache_order.append(key)
        while len(self._request_projection_cache_order) > self._request_projection_cache_max:
            old_key = self._request_projection_cache_order.pop(0)
            self._request_projection_cache.pop(old_key, None)
        return projection

    def session_history_projection(
        self,
        session_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        after_event_id: int = 0,
        limit: int = 1000,
        history_store: Any = None,
        include_events: bool = True,
    ) -> Dict[str, Any]:
        """Return a history page whose recent/current turns are reconciled by runtime projection."""
        if history_store is None:
            from agent.memory import get_conversation_store

            history_store = get_conversation_store()
        history = history_store.load_history_page(
            session_id=session_id,
            page=page,
            page_size=page_size,
        )
        session_projection = self.session_projection(
            session_id,
            after_event_id=after_event_id,
            limit=limit,
            include_events=include_events,
        )
        history_projection, page_request_ids = _overlay_runtime_requests_on_history(
            history,
            session_projection,
            page=page,
        )
        page_request_id_set = set(page_request_ids)
        page_requests = [
            request
            for request in session_projection.get("requests") or []
            if str(request.get("request_id") or "") in page_request_id_set
        ]
        return {
            **session_projection,
            "requests": page_requests,
            "history": history_projection,
            "history_page": int(page or 1),
            "history_page_size": int(page_size or 20),
            "history_source": "conversation_store+runtime_projection",
        }

    def external_connections_projection(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 0,
    ) -> Dict[str, Any]:
        events = self.event_ledger.list_events(
            session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID,
            after_event_id=after_event_id,
            limit=limit,
        )
        latest_event_id = events[-1]["event_id"] if events else after_event_id
        external_connections_by_platform: Dict[str, Dict[str, Any]] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            if not event_type.startswith("external_connection."):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            _reduce_external_connection_event(
                external_connections_by_platform,
                event_type,
                payload,
                event.get("event_id"),
                event.get("created_at"),
            )
        return {
            "session_id": EXTERNAL_CONNECTION_EVENT_SESSION_ID,
            "after_event_id": after_event_id,
            "latest_event_id": latest_event_id,
            "external_connections": list(external_connections_by_platform.values()),
            "events": _safe_projection_events(events),
        }

    @staticmethod
    def project_request_events(events: Iterable[Dict[str, Any]], *, include_events: bool = True) -> Dict[str, Any]:
        ordered = sorted(list(events), key=lambda event: int(event.get("event_id") or 0))
        request_id = ""
        session_id = ""
        turn_id = ""
        user_message: Dict[str, Any] = {}
        assistant: Dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "pending": False,
            "tool_calls": [],
            "artifacts": [],
        }
        assistant_started = False
        state = "unknown"
        terminal_reason = ""
        terminal_message = ""
        tools_by_id: Dict[str, Dict[str, Any]] = {}
        image_jobs_by_id: Dict[str, Dict[str, Any]] = {}
        task_observations_by_id: Dict[str, Dict[str, Any]] = {}
        skill_drafts_by_id: Dict[str, Dict[str, Any]] = {}
        external_connections_by_platform: Dict[str, Dict[str, Any]] = {}
        action_plans_by_id: Dict[str, Dict[str, Any]] = {}

        for event in ordered:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = str(event.get("event_type") or "")
            request_id = request_id or str(event.get("request_id") or "")
            session_id = session_id or str(event.get("session_id") or "")
            turn_id = turn_id or str(event.get("turn_id") or payload.get("turn_id") or "")

            if event_type == "run.queued":
                state = "queued"
            elif event_type in {"run.accepted", "run.started"}:
                state = "running"
            elif event_type == "message.user.accepted":
                user_message = {
                    "role": "user",
                    "content": _first_text(payload, "content", "message", "visible_message"),
                    "request_id": request_id,
                    "turn_id": turn_id,
                }
            elif event_type == "message.assistant.created":
                assistant_started = True
                assistant["pending"] = True
                state = "running"
            elif event_type == "assistant.delta":
                assistant_started = True
                assistant["content"] = f"{assistant.get('content', '')}{_first_text(payload, 'content', 'delta', 'text')}"
                assistant["pending"] = True
                state = "streaming"
            elif event_type == "assistant.snapshot":
                assistant_started = True
                assistant["content"] = _first_text(payload, "content", "text", "message")
                assistant["pending"] = True
                state = "streaming"
            elif event_type == "message.assistant.finalized":
                assistant_started = True
                assistant["content"] = _first_text(payload, "content", "final_text", "text", "message")
                _append_projection_artifacts(assistant, payload)
                assistant["pending"] = False
                state = "completed"
            elif event_type.startswith("tool."):
                assistant_started = True
                tool_id = str(payload.get("tool_call_id") or payload.get("id") or payload.get("tool") or event.get("event_seq"))
                record = tools_by_id.setdefault(tool_id, {"id": tool_id, "name": payload.get("tool") or payload.get("name") or ""})
                record["status"] = str(payload.get("status") or event_type.split(".", 1)[1])
                record["last_event_id"] = event.get("event_id")
                if "arguments" in payload:
                    record["arguments"] = redact_public_tool_value(payload.get("arguments"))
                if "result" in payload:
                    record["result"] = _drop_projection_quality_evidence_fields(
                        redact_public_tool_value(payload.get("result"))
                    )
                    quality_evidence = _extract_projection_quality_evidence(payload.get("result"))
                    if quality_evidence:
                        record["qualityEvidence"] = quality_evidence
            elif event_type.startswith("subagent."):
                assistant_started = True
                safe_payload = _safe_projection_subagent_payload(event_type, payload)
                child_request_id = safe_payload.get("child_request_id") or ""
                task_id = safe_payload.get("task_id") or ""
                tool_id = (
                    _safe_projection_subagent_identifier(payload.get("tool_call_id"))
                    or child_request_id
                    or task_id
                    or f"subagent-{_safe_projection_nonnegative_int(event.get('event_seq')) or 0}"
                )
                result_preview = str(safe_payload.get("result_preview") or "")
                if result_preview == "[redacted-content]":
                    result_preview = ""
                task_payload = {
                    "id": task_id,
                    "name": safe_payload.get("name") or "",
                    "role": safe_payload.get("role") or "subagent",
                    "summary": safe_payload.get("summary") or "",
                    "status": _subagent_status_from_event(event_type, safe_payload),
                    "result": result_preview,
                    "requestId": child_request_id,
                    "childSessionId": child_request_id,
                    "parentRequestId": safe_payload.get("parent_request_id") or "",
                    "parentSessionId": safe_payload.get("parent_session_id") or "",
                    "deadlineAt": safe_payload.get("deadline_at"),
                    "timeoutSeconds": safe_payload.get("timeout_seconds"),
                    "lastHeartbeatAt": safe_payload.get("last_heartbeat_at"),
                }
                record = tools_by_id.setdefault(tool_id, {"id": tool_id, "name": "subagent"})
                record["name"] = "subagent"
                record["status"] = task_payload["status"]
                record["last_event_id"] = event.get("event_id")
                record["child_request_id"] = child_request_id
                record["parent_request_id"] = task_payload["parentRequestId"]
                record["task_id"] = task_id
                record["result"] = {"task": task_payload}
            elif event_type == "artifact.created":
                assistant_started = True
                job_id = str(payload.get("job_id") or "")
                if not job_id or not _image_job_is_terminal(image_jobs_by_id.get(job_id)):
                    _append_projection_artifacts(assistant, payload)
            elif event_type.startswith("image_job."):
                assistant_started = True
                _reduce_image_job_event(image_jobs_by_id, event_type, payload, event.get("event_id"))
            elif event_type.startswith("task."):
                assistant_started = True
                task_id = (
                    _safe_projection_image_task_id(payload.get("task_id"))
                    or _safe_projection_identifier(payload.get("task_id"))
                    or f"task-{_safe_projection_nonnegative_int(event.get('event_seq')) or 0}"
                )
                record = task_observations_by_id.setdefault(task_id, {
                    "task_id": task_id,
                    "kind": _safe_projection_identifier(payload.get("kind")) or "task",
                    "title": str(payload.get("title") or "")[:120],
                    "events": [],
                })
                record["status"] = str(payload.get("status") or "").strip()[:80]
                record["health"] = str(payload.get("health") or record.get("health") or record.get("status") or "").strip()[:80]
                record["elapsed_seconds"] = _safe_projection_nonnegative_number(payload.get("elapsed_seconds"))
                record["soft_deadline_seconds"] = _safe_projection_nonnegative_int(payload.get("soft_deadline_seconds"))
                record["hard_deadline_seconds"] = _safe_projection_nonnegative_int(payload.get("hard_deadline_seconds"))
                record["lease_count"] = _safe_projection_nonnegative_int(payload.get("lease_count"))
                record["last_event_type"] = event_type
                record["last_event_id"] = event.get("event_id")
                if "job_id" in payload:
                    job_id = _safe_projection_image_job_id(payload.get("job_id"))
                    if job_id:
                        record["job_id"] = job_id
                if "progress" in payload:
                    progress = _safe_projection_progress(payload.get("progress"))
                    if progress is not None:
                        record["progress"] = progress
                if "image_job_status" in payload:
                    record["image_job_status"] = _safe_image_job_progress_status(payload.get("image_job_status"))
                if "backgrounded" in payload:
                    backgrounded = _safe_projection_bool(payload.get("backgrounded"))
                    if backgrounded is not None:
                        record["backgrounded"] = backgrounded
                if "reason" in payload:
                    reason = _safe_projection_telemetry_token(payload.get("reason"))
                    if reason is not None:
                        record["reason"] = reason
                if event_type == "task.intervention_requested":
                    record["intervention"] = {
                        "status": "waiting_user_decision",
                        "next_actions": [
                            str(item)[:40]
                            for item in (payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else [])
                            if str(item or "").strip()
                        ][:4],
                    }
                events = record.setdefault("events", [])
                if isinstance(events, list):
                    events.append({
                        "event_type": event_type,
                        "event_id": event.get("event_id"),
                        "status": record.get("status") or "",
                        "health": record.get("health") or "",
                        "elapsed_seconds": record.get("elapsed_seconds"),
                    })
                    del events[:-8]
            elif (
                event_type.startswith("skill_learning.")
                or event_type.startswith("skill_draft.")
                or event_type.startswith("skill.")
            ):
                assistant_started = True
                _reduce_skill_learning_event(skill_drafts_by_id, event_type, payload, event.get("event_id"))
            elif event_type.startswith("external_connection."):
                _reduce_external_connection_event(
                    external_connections_by_platform,
                    event_type,
                    payload,
                    event.get("event_id"),
                    event.get("created_at"),
                )
            elif event_type == "permission.requested":
                assistant_started = True
                state = "waiting_permission"
                assistant["pending"] = True
                plan = _projection_permission_action_plan(payload, event)
                if plan:
                    action_plans_by_id[str(plan.get("id") or "")] = plan
            elif event_type == "capability.policy_blocked":
                assistant_started = True
                state = "blocked"
                assistant["pending"] = False
                plan = _projection_capability_policy_action_plan(payload, event)
                if plan:
                    action_plans_by_id[str(plan.get("id") or "")] = plan
            elif event_type in TERMINAL_EVENT_STATES:
                state = TERMINAL_EVENT_STATES[event_type]
                terminal_reason = str(payload.get("terminal_reason") or payload.get("reason") or state)
                terminal_text = _first_text(payload, "content", "message", "error_message", "text")
                if terminal_text:
                    terminal_message = terminal_text
                if terminal_text and not assistant.get("content"):
                    assistant_started = True
                    assistant["content"] = terminal_text
                assistant["pending"] = False

        safe_request_id = _safe_projection_identifier(request_id) or ""
        safe_session_id = _safe_projection_identifier(session_id) or ""
        safe_turn_id = _safe_projection_identifier(turn_id) or ""
        if user_message:
            user_message["request_id"] = safe_request_id
            user_message["turn_id"] = safe_turn_id
        assistant["request_id"] = safe_request_id
        assistant["turn_id"] = safe_turn_id
        assistant["tool_calls"] = list(tools_by_id.values())
        action_plans = list(action_plans_by_id.values())
        if state != "waiting_permission":
            action_plans = [
                plan for plan in action_plans
                if not (
                    str(plan.get("kind") or "") == "permission"
                    and str(plan.get("nextAction") or "") == "confirm_permission"
                )
            ]

        return {
            "request_id": safe_request_id,
            "session_id": safe_session_id,
            "turn_id": safe_turn_id,
            "state": state,
            "terminal_reason": terminal_reason,
            "terminal_message": terminal_message,
            "first_event_id": ordered[0]["event_id"] if ordered else 0,
            "latest_event_id": ordered[-1]["event_id"] if ordered else 0,
            "created_at": ordered[0]["created_at"] if ordered else 0,
            "updated_at": ordered[-1]["created_at"] if ordered else 0,
            "event_count": len(ordered),
            "messages": [message for message in [user_message, assistant if assistant_started else {}] if message.get("role")],
            "image_jobs": list(image_jobs_by_id.values()),
            "task_observations": list(task_observations_by_id.values()),
            "skill_drafts": list(skill_drafts_by_id.values()),
            "external_connections": list(external_connections_by_platform.values()),
            "action_plans": action_plans,
            "events": _safe_projection_events(ordered) if include_events else [],
        }

    def _owner_session_id_for_request(self, request_id: str) -> str:
        try:
            return str(self.event_ledger.owner_session_id_for_request(request_id) or "").strip()
        except Exception:
            return ""

    def _event_matches_session_owner(
        self,
        event: Dict[str, Any],
        session_id: str,
        *,
        owner_by_request: Optional[Dict[str, str]] = None,
    ) -> bool:
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            return False
        if owner_by_request is not None and request_id in owner_by_request:
            owner_session_id = owner_by_request[request_id]
        else:
            owner_session_id = self._owner_session_id_for_request(request_id)
            if owner_by_request is not None:
                owner_by_request[request_id] = owner_session_id
        if not owner_session_id:
            return True
        return owner_session_id == str(session_id or "").strip()


def _first_text(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in payload:
            return str(payload.get(key) or "")
    return ""


def _filter_request_events_for_owner(
    events: Iterable[Dict[str, Any]],
    *,
    expected_session_id: str = "",
) -> List[Dict[str, Any]]:
    ordered = sorted(list(events), key=lambda event: int(event.get("event_id") or 0))
    expected_session_id = str(expected_session_id or "").strip()
    owner_session_id = expected_session_id
    if not owner_session_id:
        for event in ordered:
            candidate = str(event.get("session_id") or "").strip()
            if candidate:
                owner_session_id = candidate
                break
    if not owner_session_id:
        return ordered
    return [
        event
        for event in ordered
        if str(event.get("session_id") or "").strip() == owner_session_id
    ]


def _subagent_status_from_event(event_type: str, payload: Dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip().lower()
    if status in _SUBAGENT_STATUS_VALUES:
        return status
    return {
        "subagent.started": "starting",
        "subagent.updated": "running",
        "subagent.completed": "completed",
        "subagent.failed": "failed",
        "subagent.timeout": "timeout",
        "subagent.cancelled": "cancelled",
    }.get(event_type, "running")


def _safe_projection_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    safe_events: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        safe_event: Dict[str, Any] = {}
        for key in ("event_id", "event_seq"):
            if key in event:
                value = _safe_projection_nonnegative_int(event.get(key))
                if value is not None:
                    safe_event[key] = value
        for key in ("request_id", "session_id", "turn_id", "event_type"):
            if key in event:
                value = _safe_projection_identifier(event.get(key))
                if value is not None:
                    safe_event[key] = value
        if "source" in event:
            value = _safe_projection_source(event.get("source"))
            if value is not None:
                safe_event["source"] = value
        if "created_at" in event and isinstance(event.get("created_at"), (int, float)):
            safe_event["created_at"] = event.get("created_at")
        payload = event.get("payload")
        if isinstance(payload, dict):
            safe_event["payload"] = _safe_projection_event_payload(str(safe_event.get("event_type") or ""), payload)
        safe_events.append(safe_event)
    return safe_events


def _safe_projection_event_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if event_type == "permission.requested":
        return _safe_projection_permission_payload(payload)
    if event_type == "artifact.created":
        return _safe_projection_artifact_payload(payload)
    if event_type.startswith("image_job."):
        return _safe_projection_image_job_payload(event_type, payload)
    if event_type.startswith("subagent."):
        return _safe_projection_subagent_payload(event_type, payload)
    if event_type.startswith("external_connection."):
        return _safe_projection_external_connection_payload(event_type, payload)
    if event_type == "capability.policy_blocked":
        return _safe_projection_capability_policy_payload(payload)
    if event_type.startswith("tool."):
        return _safe_projection_tool_payload(payload)
    return _safe_projection_generic_payload(payload)


def _safe_projection_permission_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    for key in ("permission_request_id", "id"):
        if key not in payload:
            continue
        value = _safe_projection_identifier(payload.get(key))
        if value is None:
            omitted_count += 1
        else:
            safe[key] = value
    if "tool" in payload:
        tool = _safe_projection_tool_name(payload.get("tool"))
        if tool:
            safe["tool"] = tool
        else:
            omitted_count += 1
    for key in ("title", "message"):
        if key in payload:
            _append_redacted_content_summary(safe, key, payload.get(key))
    for key in payload:
        if key not in {"permission_request_id", "id", "tool", "title", "message"}:
            omitted_count += 1
    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    return safe


def _safe_projection_action_text(value: Any, *, string_limit: int = 240) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if _projection_text_has_sensitive_material(raw):
        return "[redacted]"
    safe = mask_sensitive_text(raw, max_chars=string_limit)
    if len(safe) <= string_limit:
        return safe
    return f"{safe[:string_limit]}...[truncated {len(safe) - string_limit} chars]"


def _projection_action_id(*parts: Any) -> str:
    safe_parts = []
    for part in parts:
        value = _safe_projection_identifier(part)
        if value:
            safe_parts.append(value)
    return ":".join(safe_parts)


def _projection_permission_action_plan(payload: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    safe = _safe_projection_permission_payload(payload)
    permission_request_id = str(safe.get("permission_request_id") or safe.get("id") or "").strip()
    if not permission_request_id:
        return {}
    tool = str(safe.get("tool") or "tool").strip() or "tool"
    plan_id = _projection_action_id("permission", permission_request_id)
    if not plan_id:
        return {}
    title = _safe_projection_action_text(payload.get("title"), string_limit=160)
    if not title:
        title = f"Permission required for {tool}"
    message = _safe_projection_action_text(payload.get("message") or payload.get("summary"), string_limit=360)
    return {
        "id": plan_id,
        "kind": "permission",
        "state": "waiting_permission",
        "nextAction": "confirm_permission",
        "actionLabel": "Review permission",
        "title": title,
        "message": message,
        "tool": tool,
        "permissionRequestId": permission_request_id,
        "retryable": True,
        "eventId": _safe_projection_nonnegative_int(event.get("event_id")) or 0,
    }


def _projection_capability_policy_action_plan(payload: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    safe = _safe_projection_capability_policy_payload(payload)
    pack_id = str(safe.get("pack_id") or safe.get("packId") or "").strip()
    action = str(safe.get("action") or "install").strip() or "install"
    plan_id = _projection_action_id("capability_policy", pack_id or "pack", action)
    if not plan_id:
        return {}
    error_type = str(safe.get("error_type") or safe.get("errorType") or "capability_policy_blocked")
    pack_redacted = bool(safe.get("pack_id_redacted") or safe.get("packIdRedacted"))
    title = "Capability action blocked by policy"
    if pack_id and not pack_redacted:
        title = f"Capability {pack_id} blocked by policy"
    return {
        "id": plan_id,
        "kind": "capability_policy",
        "state": "blocked",
        "nextAction": "view_capability_policy",
        "actionLabel": "View policy",
        "title": title,
        "message": error_type,
        "packId": "" if pack_redacted else pack_id,
        "requestedAction": action,
        "retryable": False,
        "eventId": _safe_projection_nonnegative_int(event.get("event_id")) or 0,
    }


def _safe_projection_tool_name(value: Any) -> str:
    raw = str(value or "").strip()
    if (
        raw
        and len(raw) <= 96
        and _is_projection_ascii_identifier(raw)
        and not _projection_identifier_has_sensitive_text(raw)
        and mask_sensitive_text(raw, max_chars=2048) == raw
    ):
        return raw
    return ""


def _safe_projection_tool_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe = _safe_projection_generic_payload({
        key: value
        for key, value in dict(payload or {}).items()
        if key not in {"arguments", "input", "result"}
    })
    for key in ("arguments", "input", "result"):
        if key in payload:
            safe[key] = _drop_projection_quality_evidence_fields(redact_public_tool_value(payload.get(key)))
    quality_evidence = _extract_projection_quality_evidence(payload.get("result"))
    if quality_evidence:
        safe["qualityEvidence"] = quality_evidence
    return safe


def _safe_projection_capability_policy_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    identifier_fields = (
        "pack_id",
        "packId",
        "action",
        "error_type",
        "errorType",
        "policy_mode",
        "policyMode",
        "policy_updated_at",
        "policyUpdatedAt",
    )
    for key in identifier_fields:
        if key not in payload:
            continue
        value = _safe_projection_identifier(payload.get(key))
        if value is None:
            omitted_count += 1
        else:
            safe[key] = value
    for key in ("policy_source", "policySource"):
        if key not in payload:
            continue
        value = _safe_projection_scalar(payload.get(key), string_limit=256)
        if value is None:
            omitted_count += 1
        else:
            safe[key] = value
    for key in ("install_allowed", "installAllowed", "pack_id_redacted", "packIdRedacted"):
        if key not in payload:
            continue
        value = _safe_projection_bool(payload.get(key))
        if value is None:
            omitted_count += 1
        else:
            safe[key] = value
    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    return safe


def _safe_projection_external_status(value: Any, *, event_type: str = "") -> str:
    status = str(value or "").strip().lower()
    if status in _EXTERNAL_CONNECTION_STATUS_VALUES:
        return status
    if event_type.endswith(".started"):
        return "active"
    if event_type.endswith(".stopped"):
        return "stopped"
    if event_type.endswith(".queued"):
        return "queued"
    if event_type.endswith(".duplicate"):
        return "duplicate"
    if event_type.endswith(".sent"):
        return "sent"
    if event_type.endswith(".blocked"):
        return "blocked"
    if event_type.endswith(".dry_run"):
        return "dry_run"
    if event_type.endswith(".failed") or event_type.endswith(".error"):
        return "error"
    if event_type.endswith(".completed") or event_type.endswith(".saved") or event_type.endswith(".updated"):
        return "success"
    return "unknown"


def _safe_projection_external_action(value: Any) -> str:
    raw = str(value or "").strip().lower()
    allowed = {
        "save_config",
        "test",
        "start",
        "stop",
        "enable",
        "disable",
        "set_home_channel",
        "clear_home_channel",
        "ingress",
        "delivery",
    }
    return raw if raw in allowed else ""


def _safe_projection_external_platform(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw and len(raw) <= 64 and _is_projection_ascii_identifier(raw) and not _projection_identifier_has_sensitive_text(raw):
        return raw
    return "unknown"


def _safe_projection_external_text(value: Any, *, string_limit: int = 500) -> str:
    safe = _safe_projection_subagent_text(value, string_limit=string_limit)
    return safe if safe is not None else ""


def _safe_projection_external_error_label(value: Any, *, fallback: str = "external_connection_error") -> str:
    raw = str(value or "").strip()
    if (
        raw
        and len(raw) <= 96
        and _is_projection_ascii_identifier(raw)
        and not _projection_identifier_has_sensitive_text(raw)
        and not _projection_text_has_sensitive_material(raw)
        and mask_sensitive_text(raw, max_chars=2048) == raw
    ):
        return raw
    return fallback if raw else ""


def _safe_projection_external_error_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: Dict[str, Any] = {}
    message = _safe_projection_external_error_label(value.get("message"), fallback="operation_failed")
    if message:
        safe["message"] = message
    raw_error_type = str(value.get("errorType") or value.get("error_type") or "").strip()
    if (
        raw_error_type
        and len(raw_error_type) <= 80
        and raw_error_type[0].isalpha()
        and all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for char in raw_error_type)
        and not _projection_identifier_has_sensitive_text(raw_error_type)
    ):
        safe["errorType"] = raw_error_type
    error_hash = str(value.get("errorHash") or value.get("error_hash") or "").strip().lower()
    if error_hash and len(error_hash) <= 64 and all(char in "0123456789abcdef" for char in error_hash):
        safe["errorHash"] = error_hash
    for key in ("errorLength", "errorBytes"):
        parsed = _safe_projection_nonnegative_int(value.get(key))
        if parsed is not None:
            safe[key] = parsed
    redacted = _safe_projection_bool(value.get("redacted"))
    if redacted is not None:
        safe["redacted"] = redacted
    return safe


def _safe_projection_external_adapter_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key in (
        "version",
        "channel",
        "configured",
        "enabled",
        "running",
        "readiness",
        "deliveryMode",
        "outboundSupported",
        "proactiveSupported",
        "safeToSend",
        "reason",
        "queueOwner",
        "usesHermesActiveSessionQueue",
        "probeMode",
        "testMode",
        "remoteConnectivityProbed",
    ):
        if key not in value:
            continue
        item = value.get(key)
        if isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, (int, float)):
            safe[key] = item
        else:
            safe[key] = _safe_projection_external_text(item, string_limit=300)
    for key in ("requiredContext", "missingContext"):
        if isinstance(value.get(key), list):
            safe[key] = [
                _safe_projection_external_text(item, string_limit=80)
                for item in value.get(key)[:12]
            ]
    return safe


def _safe_projection_hash(value: Any, *, max_chars: int = 64) -> str:
    raw = str(value or "").strip().lower()
    if raw and len(raw) <= max_chars and all(char in "0123456789abcdef" for char in raw):
        return raw
    return ""


def _safe_projection_identity_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: Dict[str, Any] = {}
    digest = _safe_projection_hash(value.get("hash"))
    if digest:
        safe["hash"] = digest
    for key in ("chars", "bytes"):
        parsed = _safe_projection_nonnegative_int(value.get(key))
        if parsed is not None:
            safe[key] = parsed
    redacted = _safe_projection_bool(value.get("redacted"))
    if redacted is not None:
        safe["redacted"] = redacted
    return safe


def _safe_projection_external_message_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key in (
        "contractVersion",
        "direction",
        "platform",
        "contextType",
        "replyType",
        "entrypoint",
        "queueOwner",
        "contentPreview",
        "contentHash",
        "contentLength",
        "contentBytes",
        "sessionHash",
        "receiverHash",
        "isGroup",
        "isAt",
    ):
        if key not in value:
            continue
        item = value.get(key)
        if isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, (int, float)):
            safe[key] = item
        else:
            safe[key] = _safe_projection_external_text(item, string_limit=200)
    for key in ("sessionHash", "receiverHash"):
        if key in safe:
            safe[key] = _safe_projection_hash(safe.get(key))
            if not safe[key]:
                safe.pop(key, None)
    for key in ("sessionSummary", "receiverSummary"):
        if isinstance(value.get(key), dict):
            summary = _safe_projection_identity_summary(value.get(key) or {})
            if summary:
                safe[key] = summary
    if isinstance(value.get("queue"), dict):
        safe["queue"] = _safe_projection_external_adapter_summary(value.get("queue") or {})
    if isinstance(value.get("message"), dict):
        message = value.get("message") or {}
        safe["message"] = {
            key: _safe_projection_external_text(message.get(key), string_limit=200)
            for key in ("platform", "messageId", "type", "contentPreview", "contentHash")
            if key in message
        }
        for key in ("contentLength", "contentBytes", "isGroup", "isAt"):
            if key in message:
                safe["message"][key] = message.get(key)
    return safe


def _safe_projection_external_connection_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in ("platform", "contractVersion", "operation_id", "operationId"):
        if key in payload:
            safe[key] = _safe_projection_external_text(payload.get(key), string_limit=128)
    if "action" in payload:
        safe["action"] = _safe_projection_external_action(payload.get("action")) or _safe_projection_external_text(payload.get("action"), string_limit=64)
    safe["status"] = _safe_projection_external_status(payload.get("status"), event_type=event_type)
    for key in ("configured", "connected", "callable", "running", "remoteConnectivityProbed", "homeChannelConfigured", "accepted", "deduped"):
        if key in payload:
            value = _safe_projection_bool(payload.get(key))
            if value is not None:
                safe[key] = value
    for key in ("mode", "reason", "dedupeKey", "homeChannelHash"):
        if key in payload:
            safe[key] = _safe_projection_external_text(payload.get(key), string_limit=500)
    if "lastError" in payload:
        safe["lastError"] = _safe_projection_external_error_label(payload.get("lastError"))
    if "error" in payload:
        safe["error"] = _safe_projection_external_error_label(payload.get("error"))
    if isinstance(payload.get("errorSummary"), dict):
        error_summary = _safe_projection_external_error_summary(payload.get("errorSummary") or {})
        if error_summary:
            safe["errorSummary"] = error_summary
    if isinstance(payload.get("adapter"), dict):
        safe["adapter"] = _safe_projection_external_adapter_summary(payload.get("adapter") or {})
    if isinstance(payload.get("inbound"), dict):
        safe["inbound"] = _safe_projection_external_message_summary(payload.get("inbound") or {})
    if isinstance(payload.get("delivery"), dict):
        safe["delivery"] = _safe_projection_external_message_summary(payload.get("delivery") or {})
    if "applied" in payload and isinstance(payload.get("applied"), list):
        safe["applied"] = [
            _safe_projection_external_text(item, string_limit=96)
            for item in payload.get("applied")[:24]
        ]
    if "masked_secret_skipped" in payload:
        value = _safe_projection_nonnegative_int(payload.get("masked_secret_skipped"))
        if value is not None:
            safe["masked_secret_skipped"] = value
    return safe


def _safe_projection_artifact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    for key in ("job_id", "task_id"):
        if key in payload:
            if key == "job_id":
                value = _safe_projection_image_job_id(payload.get(key))
            else:
                value = _safe_projection_image_task_id(payload.get(key))
            if value is None:
                omitted_count += 1
            else:
                safe[key] = value
    if "artifact_index" in payload:
        value = _safe_projection_nonnegative_int(payload.get("artifact_index"))
        if value is None:
            omitted_count += 1
        else:
            safe["artifact_index"] = value
    if "source" in payload:
        value = _safe_projection_source(payload.get("source"))
        if value is None:
            omitted_count += 1
        else:
            safe["source"] = value
    if isinstance(payload.get("artifact"), dict):
        safe["artifact"] = _safe_projection_artifact(payload.get("artifact") or {})
    raw_artifacts = payload.get("artifacts")
    if isinstance(raw_artifacts, list):
        safe["artifacts"] = [
            _safe_projection_artifact(item)
            for item in raw_artifacts
            if isinstance(item, dict)
        ]
    if not isinstance(payload.get("artifact"), dict) and _looks_like_artifact(payload):
        for key in _PROJECTION_ARTIFACT_DTO_FIELDS:
            safe.pop(key, None)
        safe.update(_safe_projection_artifact(payload))
    for key in payload:
        if key not in {"job_id", "task_id", "artifact_index", "source", "artifact", "artifacts"} and key not in _PROJECTION_ARTIFACT_DTO_FIELDS:
            omitted_count += 1
    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    return safe


def _safe_projection_image_job_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    safe = _safe_projection_image_job_base_payload(payload)
    if isinstance(payload.get("artifact"), dict):
        safe["artifact"] = _safe_projection_artifact(payload.get("artifact") or {})
    raw_tasks = payload.get("tasks")
    if isinstance(raw_tasks, list):
        safe["tasks"] = [
            _safe_projection_task(item)
            for item in raw_tasks
            if isinstance(item, dict)
        ]
    if event_type == "image_job.failed":
        safe["error_type"] = _safe_image_job_error_type(payload.get("error_type"))
        safe["error_message"] = f"{safe['error_type']}: image job failed"
    if event_type == "image_job.cancelled":
        safe["reason"] = _safe_image_job_cancel_reason(payload.get("reason") or "cancelled")
    if event_type == "image_job.progress":
        safe["status"] = _safe_image_job_progress_status(payload.get("status"))
    return safe


def _safe_projection_subagent_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False

    for key in ("parent_request_id", "parent_session_id", "child_request_id", "task_id", "tool_call_id"):
        if key in payload:
            value = _safe_projection_subagent_identifier(payload.get(key))
            if value is None:
                omitted_count += 1
            else:
                safe[key] = value

    text_fields = {
        "name": 120,
        "role": 48,
        "summary": 512,
        "result_preview": 512,
    }
    for key, limit in text_fields.items():
        if key not in payload:
            continue
        value = _safe_projection_subagent_text(payload.get(key), string_limit=limit)
        if value is None:
            omitted_count += 1
            continue
        if isinstance(payload.get(key), str) and value != payload.get(key):
            truncated = True
        safe[key] = value

    if "status" in payload:
        status = _safe_projection_subagent_status(payload.get("status"))
        if status:
            safe["status"] = status
        else:
            omitted_count += 1
    if "status" not in safe:
        safe["status"] = _subagent_status_from_event(event_type, {})

    for key in ("deadline_at", "timeout_seconds", "last_heartbeat_at"):
        if key not in payload:
            continue
        value = _safe_projection_nonnegative_int(payload.get(key))
        if value is None:
            omitted_count += 1
        else:
            safe[key] = value

    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    if truncated:
        safe["payload_truncated"] = True
    return safe


_IMAGE_JOB_EVENT_PAYLOAD_FIELDS = {
    "api_base_host_hash",
    "api_key_source",
    "artifact_count",
    "artifact_index",
    "attempt",
    "configured_max_parallel",
    "default_max_parallel",
    "effective_max_parallel",
    "elapsed_ms",
    "endpoint_host_hash",
    "attempted_provider_count",
    "fallback_from_model",
    "fallback_provider",
    "fallback_reason",
    "fallback_to_model",
    "fallback_used",
    "finalization_latency_ms",
    "hard_max_parallel",
    "image_mode",
    "input_image_count",
    "job_id",
    "latency_ms",
    "max_parallel",
    "max_retries",
    "model",
    "operation",
    "ocr_brief_hash",
    "ocr_cache_enabled",
    "ocr_cache_hit",
    "ocr_cache_key",
    "ocr_input_image_count",
    "ocr_ms",
    "ocr_provider",
    "output_count",
    "output_format",
    "parallelism_clamp_reason",
    "parallelism_clamped",
    "parallelism_defaulted",
    "parallelism_policy_version",
    "postprocess_latency_ms",
    "progress",
    "provider",
    "provider_latency_ms",
    "provider_max_parallel",
    "quality",
    "quality_latency_ms",
    "request_timeout_seconds",
    "requested_max_parallel",
    "resolved_model",
    "retry_after_cap_seconds",
    "retry_after_seconds",
    "retryable",
    "size",
    "source",
    "status_code",
    "task_count",
    "task_id",
    "task_index",
    "taxonomy",
    "total_latency_ms",
}

_IMAGE_JOB_EVENT_NONNEGATIVE_INT_FIELDS = {
    "artifact_count",
    "artifact_index",
    "attempt",
    "attempted_provider_count",
    "configured_max_parallel",
    "default_max_parallel",
    "effective_max_parallel",
    "elapsed_ms",
    "finalization_latency_ms",
    "hard_max_parallel",
    "input_image_count",
    "latency_ms",
    "max_parallel",
    "max_retries",
    "ocr_input_image_count",
    "ocr_ms",
    "output_count",
    "postprocess_latency_ms",
    "provider_latency_ms",
    "provider_max_parallel",
    "quality_latency_ms",
    "request_timeout_seconds",
    "requested_max_parallel",
    "status_code",
    "task_count",
    "task_index",
    "total_latency_ms",
}

_IMAGE_JOB_EVENT_NONNEGATIVE_NUMBER_FIELDS = {
    "retry_after_cap_seconds",
    "retry_after_seconds",
}


def _safe_projection_image_job_base_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False
    for key, value in dict(payload or {}).items():
        normalized = str(key or "")
        lowered = normalized.lower()
        if any(part in lowered for part in ("api_key", "secret", "token", "authorization")):
            safe[normalized] = "[redacted]"
            continue
        if normalized not in _IMAGE_JOB_EVENT_PAYLOAD_FIELDS:
            if normalized not in {"artifact", "artifacts", "tasks", "error_message", "error_type", "reason", "status"}:
                omitted_count += 1
            continue
        if normalized == "job_id":
            safe_value = _safe_projection_image_job_id(value)
        elif normalized == "task_id":
            safe_value = _safe_projection_image_task_id(value)
        elif normalized in _IMAGE_JOB_EVENT_NONNEGATIVE_INT_FIELDS:
            safe_value = _safe_projection_nonnegative_int(value)
        elif normalized in _IMAGE_JOB_EVENT_NONNEGATIVE_NUMBER_FIELDS:
            safe_value = _safe_projection_nonnegative_number(value)
        elif normalized == "progress":
            safe_value = _safe_projection_progress(value)
        elif normalized in {"retryable", "parallelism_clamped", "fallback_used", "ocr_cache_enabled", "ocr_cache_hit"}:
            safe_value = _safe_projection_bool(value)
        elif normalized == "source":
            safe_value = _safe_projection_source(value)
        elif normalized == "operation":
            safe_value = _safe_projection_operation(value)
        elif normalized in {
            "provider",
            "resolved_model",
            "model",
            "image_mode",
            "output_format",
            "quality",
            "size",
            "api_base_host_hash",
            "api_key_source",
            "endpoint_host_hash",
            "fallback_from_model",
            "fallback_provider",
            "fallback_reason",
            "fallback_to_model",
            "taxonomy",
            "parallelism_clamp_reason",
            "parallelism_policy_version",
            "ocr_brief_hash",
            "ocr_cache_key",
            "ocr_provider",
        }:
            safe_value = _safe_projection_telemetry_token(value)
        else:
            safe_value = _safe_projection_scalar(value, string_limit=512)
        if safe_value is None:
            omitted_count += 1
            continue
        if isinstance(safe_value, str) and isinstance(value, str) and safe_value != value:
            truncated = True
        safe[normalized] = safe_value
    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    if truncated:
        safe["payload_truncated"] = True
    return safe


def _safe_projection_task(task: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in ("task_id", "source_task_id"):
        if key in task:
            value = _safe_projection_image_task_id(task.get(key))
            if value is not None:
                safe[key] = value
    for key in ("operation",):
        if key in task:
            value = _safe_projection_operation(task.get(key))
            if value is not None:
                safe[key] = value
    if "status" in task:
        safe["status"] = _safe_image_job_progress_status(task.get("status"))
    if "terminal_job_status" in task:
        safe["terminal_job_status"] = _safe_image_job_terminal_status(task.get("terminal_job_status"))
    for key in ("task_index", "input_image_count", "output_count"):
        if key in task:
            value = _safe_projection_nonnegative_int(task.get(key))
            if value is not None:
                safe[key] = value
    if "progress" in task:
        value = _safe_projection_progress(task.get("progress"))
        if value is not None:
            safe["progress"] = value
    for key in (
        "provider",
        "model",
        "fallback_provider",
        "fallback_from_model",
        "fallback_to_model",
        "fallback_reason",
        "ocr_brief_hash",
        "ocr_cache_key",
        "ocr_provider",
    ):
        if key in task:
            value = _safe_projection_telemetry_token(task.get(key))
            if value is not None:
                safe[key] = value
    if "fallback_used" in task:
        value = _safe_projection_bool(task.get("fallback_used"))
        if value is not None:
            safe["fallback_used"] = value
    if "attempted_provider_count" in task:
        value = _safe_projection_nonnegative_int(task.get("attempted_provider_count"))
        if value is not None:
            safe["attempted_provider_count"] = value
    for key in ("ocr_cache_enabled", "ocr_cache_hit"):
        if key in task:
            value = _safe_projection_bool(task.get(key))
            if value is not None:
                safe[key] = value
    for key in ("ocr_input_image_count", "ocr_ms"):
        if key in task:
            value = _safe_projection_nonnegative_int(task.get(key))
            if value is not None:
                safe[key] = value
    return safe


def _safe_projection_generic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False
    for key, value in dict(payload or {}).items():
        normalized = str(key or "")
        lowered = normalized.lower()
        compact_key = "".join(
            char
            for char in lowered
            if char.isalnum()
        )
        content_prefix = _generic_content_payload_prefix(normalized)
        if content_prefix:
            _append_redacted_content_summary(safe, content_prefix, value)
            continue
        if compact_key in _GENERIC_CONTENT_PAYLOAD_EXCLUSIONS:
            safe_structural_value = _safe_projection_generic_structural_value(compact_key, value)
            if safe_structural_value is None:
                omitted_count += 1
            else:
                safe[normalized] = safe_structural_value
            continue
        if any(part in lowered for part in ("raw", "response", "b64", "base64")):
            omitted_count += 1
            continue
        if (
            compact_key not in _GENERIC_CONTENT_PAYLOAD_EXCLUSIONS
            and any(part in lowered for part in ("api_key", "secret", "token", "authorization"))
        ):
            safe[normalized] = "[redacted]"
            continue
        if isinstance(value, dict):
            omitted_count += 1
            continue
        if isinstance(value, (list, tuple)):
            omitted_count += 1
            continue
        if isinstance(value, str):
            if not value:
                safe[normalized] = ""
                continue
            _append_redacted_content_summary(safe, _generic_content_summary_prefix_from_key(normalized), value)
            continue
        safe_value = _safe_projection_scalar(value, string_limit=4096)
        if safe_value is None:
            omitted_count += 1
            continue
        if isinstance(safe_value, str) and isinstance(value, str) and safe_value != value:
            truncated = True
        safe[normalized] = safe_value
    if omitted_count:
        safe["payload_sanitized"] = True
        safe["omitted_payload_field_count"] = omitted_count
    if truncated:
        safe["payload_truncated"] = True
    return safe


def _safe_projection_generic_structural_value(compact_key: str, value: Any) -> Any:
    if compact_key in _GENERIC_STRUCTURAL_HASH_KEYS:
        return _safe_projection_hash(value) or None
    if compact_key in _GENERIC_STRUCTURAL_IDENTIFIER_KEYS:
        return _safe_projection_generic_identifier_value(compact_key, value)
    if compact_key in _GENERIC_STRUCTURAL_COUNT_KEYS:
        return _safe_projection_nonnegative_int(value)
    if compact_key in _GENERIC_STRUCTURAL_BOOL_KEYS:
        return _safe_projection_bool(value)
    return None


def _safe_projection_generic_identifier_value(compact_key: str, value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if compact_key == "status":
        return lowered if lowered in _GENERIC_STATUS_VALUES else None
    if compact_key == "messagetype":
        return lowered if lowered in _GENERIC_MESSAGE_TYPE_VALUES else None
    if compact_key in {"messageid", "messageids"}:
        if lowered.startswith("msg-"):
            return _safe_projection_identifier(raw)
        return _safe_projection_hash(raw) or None
    if compact_key == "promptid":
        if lowered.startswith("prompt-"):
            return _safe_projection_prefixed_identifier(raw, "prompt-")
        return None
    if compact_key == "instructionid":
        if lowered.startswith("instruction-"):
            return _safe_projection_prefixed_identifier(raw, "instruction-")
        return None
    return None


def _safe_projection_prefixed_identifier(value: Any, prefix: str) -> Optional[str]:
    raw = str(value or "").strip()
    lowered = raw.lower()
    normalized_prefix = str(prefix or "").lower()
    suffix = raw[len(prefix):] if lowered.startswith(normalized_prefix) else ""
    if (
        raw
        and len(raw) <= 128
        and lowered.startswith(normalized_prefix)
        and _is_projection_ascii_identifier(raw)
        and suffix
        and not _projection_identifier_has_sensitive_text(suffix)
        and not _projection_text_has_sensitive_material(raw)
        and mask_sensitive_text(raw, max_chars=2048) == raw
    ):
        return raw
    return None


def _generic_content_payload_prefix(key: str) -> str:
    compact = "".join(
        char
        for char in str(key or "").lower()
        if char.isalnum()
    )
    if not compact or compact in _GENERIC_CONTENT_PAYLOAD_EXCLUSIONS:
        return ""
    exact = _GENERIC_CONTENT_PAYLOAD_PREFIXES.get(compact)
    if exact:
        return exact
    if any(compact.endswith(suffix) for suffix in _GENERIC_CONTENT_PAYLOAD_SUFFIXES):
        return _generic_content_summary_prefix_from_key(key)
    if any(compact.startswith(prefix) for prefix in _GENERIC_CONTENT_PAYLOAD_PREFIX_MATCHES):
        return _generic_content_summary_prefix_from_key(key)
    return ""


def _generic_content_summary_prefix_from_key(key: str) -> str:
    parts: List[str] = []
    current: List[str] = []
    for char in str(key or ""):
        if char.isalnum():
            current.append(char)
            continue
        if current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    if not parts:
        return "content"
    prefix = parts[0][:1].lower() + parts[0][1:]
    for part in parts[1:]:
        if not part:
            continue
        prefix += part[:1].upper() + part[1:]
    return prefix[:48] or "content"


def _append_redacted_content_summary(safe: Dict[str, Any], prefix: str, value: Any) -> None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    safe[f"{prefix}Preview"] = "[redacted-content]" if text else ""
    safe[f"{prefix}Hash"] = hashlib.sha256(encoded).hexdigest()[:16] if text else ""
    safe[f"{prefix}Length"] = len(text)
    safe[f"{prefix}Bytes"] = len(encoded)
    safe[f"{prefix}Redacted"] = bool(text)


def _safe_projection_scalar(value: Any, *, string_limit: int) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    safe = mask_sensitive_text(value, max_chars=string_limit)
    if len(safe) <= string_limit:
        return safe
    return f"{safe[:string_limit]}...[truncated {len(safe) - string_limit} chars]"


def _safe_projection_identifier(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 128:
        return None
    if (
        _is_projection_ascii_identifier(raw)
        and not _projection_identifier_has_sensitive_text(raw)
        and not _projection_text_has_sensitive_material(raw)
        and mask_sensitive_text(raw, max_chars=2048) == raw
    ):
        return raw
    return None


def _safe_projection_image_job_id(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if _projection_identifier_has_sensitive_text(raw):
        return None
    if raw.startswith("image-job-") and len(raw) <= 128 and _is_projection_ascii_identifier(raw):
        return raw
    return None


def _safe_projection_image_task_id(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if _projection_identifier_has_sensitive_text(raw):
        return None
    if raw.startswith("task-") and len(raw) <= 64 and _is_projection_ascii_identifier(raw):
        return raw
    return None


def _safe_projection_subagent_identifier(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 128:
        return None
    if _projection_identifier_has_sensitive_text(raw):
        return None
    if _is_projection_ascii_identifier(raw):
        return raw
    return None


def _safe_projection_subagent_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in _SUBAGENT_STATUS_VALUES else ""


def _safe_projection_subagent_text(value: Any, *, string_limit: int) -> Optional[str]:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    raw = value.strip()
    if _projection_text_has_sensitive_material(raw):
        return "[redacted]"
    safe = html.escape(mask_sensitive_text(raw, max_chars=string_limit), quote=False)
    if len(safe) <= string_limit:
        return safe
    return f"{safe[:string_limit]}...[truncated {len(safe) - string_limit} chars]"


def _is_projection_ascii_identifier(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    return all(char in allowed for char in str(value or ""))


def _projection_identifier_has_sensitive_text(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(part in lowered for part in ("private", "prompt", "secret", "token", "password"))


def _projection_text_has_sensitive_material(value: str) -> bool:
    lowered = str(value or "").lower()
    sensitive_markers = (
        "api_key", "api key", "api-key", "apikey",
        "authorization", "bearer ",
        "password=", "secret=", "token=", "sk-",
    )
    return any(marker in lowered for marker in sensitive_markers)


def _safe_projection_nonnegative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _safe_projection_nonnegative_number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


def _safe_projection_progress(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(parsed, 1.0))


def _safe_projection_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"true", "1", "yes", "on"}:
        return True
    if raw in {"false", "0", "no", "off"}:
        return False
    return None


def _safe_projection_source(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if raw in {"image_job_service", "runtime", "web_channel", "tool", "test", "external_connections"}:
        return raw
    return None


def _safe_projection_operation(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"generate", "edit", "regenerate", "variation", "ocr", "upscale"}:
        return raw
    return "generate"


def _safe_projection_telemetry_token(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or len(raw) > 128:
        return None
    if _projection_telemetry_token_has_sensitive_material(raw):
        return None
    if _is_projection_ascii_telemetry_token(raw):
        return raw
    return None


def _projection_telemetry_token_has_sensitive_material(value: str) -> bool:
    lowered = str(value or "").lower()
    if any(marker in lowered for marker in ("http://", "https://", "file://", "data:")):
        return True
    if "/" in lowered or "\\" in lowered or ":" in lowered:
        return True
    sensitive_markers = (
        "api_key",
        "api-key",
        "apikey",
        "authorization",
        "bearer",
        "password",
        "private",
        "prompt",
        "secret",
        "sk-",
        "token",
    )
    return any(marker in lowered for marker in sensitive_markers)


def _is_projection_ascii_telemetry_token(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return all(char in allowed for char in str(value or ""))


def _append_projection_artifacts(assistant: Dict[str, Any], payload: Dict[str, Any]) -> None:
    if isinstance(payload.get("artifact"), dict):
        assistant.setdefault("artifacts", []).append(_safe_projection_artifact(payload.get("artifact") or {}))
    raw_artifacts = payload.get("artifacts")
    if isinstance(raw_artifacts, list):
        assistant.setdefault("artifacts", []).extend(
            _safe_projection_artifact(item) for item in raw_artifacts if isinstance(item, dict)
        )
    elif not isinstance(payload.get("artifact"), dict) and _looks_like_artifact(payload):
        assistant.setdefault("artifacts", []).append(_safe_projection_artifact(payload))
    assistant["artifacts"] = _sort_projection_artifacts(assistant.get("artifacts") or [])


def _looks_like_artifact(payload: Dict[str, Any]) -> bool:
    return any(key in payload for key in ("title", "path", "relativePath", "relative_path", "url", "kind", "file_name"))


_PROJECTION_ARTIFACT_DTO_FIELDS = {
    "id",
    "kind",
    "title",
    "name",
    "path",
    "relativePath",
    "relative_path",
    "url",
    "previewUrl",
    "preview_url",
    "qualityEvidence",
    "fileName",
    "file_name",
    "fileType",
    "file_type",
    "mimeType",
    "mime_type",
    "sizeBytes",
    "size_bytes",
    "width",
    "height",
    "sha256",
    "safeArtifactId",
    "task_id",
    "task_index",
    "taskIndex",
    "artifact_index",
    "artifactIndex",
    "artifact_sanitized",
    "metadata_truncated",
    "omitted_field_count",
}

_PROJECTION_ARTIFACT_PATH_FIELDS = {
    "path",
    "relativePath",
    "relative_path",
    "url",
    "previewUrl",
    "preview_url",
}


def _safe_projection_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(artifact or {})
    normalized = dict(raw)
    if "quality_evidence" in normalized and "qualityEvidence" not in normalized:
        normalized["qualityEvidence"] = normalized.get("quality_evidence")
    if "file_name" in normalized and "title" not in normalized:
        normalized["title"] = normalized.get("file_name")
    if "fileName" in normalized and "title" not in normalized:
        normalized["title"] = normalized.get("fileName")
    if "file_type" in normalized and "kind" not in normalized:
        normalized["kind"] = normalized.get("file_type")
    if "fileType" in normalized and "kind" not in normalized:
        normalized["kind"] = normalized.get("fileType")
    normalized.setdefault("kind", "file")

    result: Dict[str, Any] = {}
    omitted_count = 0
    truncated = False
    for key, value in normalized.items():
        if key not in _PROJECTION_ARTIFACT_DTO_FIELDS:
            omitted_count += 1
            continue
        safe_value, was_truncated = _safe_projection_artifact_value(key, value)
        if safe_value is None:
            omitted_count += 1
            continue
        result[key] = safe_value
        truncated = truncated or was_truncated
    if "kind" not in result:
        result["kind"] = "file"
    if omitted_count:
        result["artifact_sanitized"] = True
        result["omitted_field_count"] = omitted_count
    if truncated:
        result["metadata_truncated"] = True
    return result


def _projection_artifact_sort_key(artifact: Dict[str, Any]) -> tuple[int, int, str]:
    def _safe_index(value: Any, fallback: int = 999_999) -> int:
        try:
            number = int(value)
            return number if number >= 0 else fallback
        except (TypeError, ValueError):
            return fallback

    return (
        _safe_index(artifact.get("task_index") if "task_index" in artifact else artifact.get("taskIndex")),
        _safe_index(artifact.get("artifact_index") if "artifact_index" in artifact else artifact.get("artifactIndex")),
        str(
            artifact.get("safeArtifactId")
            or artifact.get("id")
            or artifact.get("path")
            or artifact.get("relativePath")
            or artifact.get("relative_path")
            or artifact.get("url")
            or artifact.get("title")
            or ""
        ),
    )


def _sort_projection_artifacts(artifacts: Any) -> List[Dict[str, Any]]:
    if not isinstance(artifacts, list):
        return []
    return sorted((dict(item) for item in artifacts if isinstance(item, dict)), key=_projection_artifact_sort_key)


def _safe_projection_artifact_value(key: str, value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    if key == "qualityEvidence":
        evidence = _safe_projection_quality_evidence(value)
        return (evidence, True) if evidence else (None, False)
    if isinstance(value, bool):
        return value, False
    if key in {"width", "height", "sizeBytes", "size_bytes", "task_index", "taskIndex", "artifact_index", "artifactIndex"}:
        try:
            return max(0, int(value)), False
        except (TypeError, ValueError):
            return None, False
    if isinstance(value, (int, float)):
        return value, False
    if not isinstance(value, str):
        return None, False
    if key in _PROJECTION_ARTIFACT_PATH_FIELDS and _looks_like_inline_artifact_data(value):
        return None, False
    if key in _PROJECTION_ARTIFACT_PATH_FIELDS:
        safe_reference = _safe_projection_artifact_path_reference(value)
        if safe_reference != value:
            return safe_reference, True
    if key in {"title", "name", "fileName", "file_name"} and _projection_artifact_text_requires_redaction(value):
        return _stable_projection_artifact_reference("artifact-label", value), True
    limit = 4096 if key in _PROJECTION_ARTIFACT_PATH_FIELDS else 512
    if len(value) <= limit:
        return value, False
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]", True


def _looks_like_inline_artifact_data(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("data:") or ";base64," in lowered


def _stable_projection_artifact_reference(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _projection_artifact_text_requires_redaction(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    sensitive_markers = (
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "ocr",
        "password",
        "private",
        "prompt",
        "secret",
        "sk-",
        "token",
        "xoxb-",
        "xapp-",
    )
    return any(marker in lowered for marker in sensitive_markers)


_QUALITY_EVIDENCE_ALIASES = (
    "qualityEvidence",
    "quality_evidence",
    "officePdfQualityEvidence",
    "office_pdf_quality_evidence",
)
_QUALITY_EVIDENCE_KINDS = {"presentation", "spreadsheet", "document", "pdf", "image"}
_QUALITY_EVIDENCE_STATUSES = {"pass", "fail", "warn", "pending", "skipped", "unknown"}
_QUALITY_EVIDENCE_ALLOWED_GATES = {
    "artifact-tool-authoring",
    "chart-integrity",
    "chart-render",
    "dashboard-structure",
    "design-preset",
    "export-verify",
    "font-size-check",
    "formula-audit",
    "generation-verify",
    "artifact-integrity",
    "anomaly-check",
    "decode-valid",
    "layout-bounds",
    "layout-inspection",
    "non-blank",
    "overlap-check",
    "overlay-ghosting-check",
    "page-render",
    "redline-preserve",
    "reference-fidelity",
    "render-docx",
    "render-preview",
    "seam-check",
    "subject-structure-check",
    "story-flow",
    "structure-check",
    "table-geometry",
    "table-structure",
    "text-orientation",
    "text-glyph-check",
    "typed-values",
    "visual-diff",
    "visual-inspection",
    "watermark-check",
}
_QUALITY_EVIDENCE_DETAIL_KEYS = {
    "anomaly_risk",
    "blank_pages",
    "blank_risk",
    "chart_issues",
    "charts",
    "comment_id_mismatches",
    "comment_refs",
    "comments",
    "date_text",
    "diff",
    "diff_mismatches",
    "decode_error",
    "decode_valid",
    "empty_sheets",
    "empty_slides",
    "empty_text_pages",
    "error_cells",
    "expected_min",
    "export",
    "extraction_errors",
    "formula_errors",
    "formulas",
    "finalized",
    "generated",
    "glyph_fragments",
    "glyph_issues",
    "headings",
    "image_only_pages",
    "issues",
    "manual_visual_review",
    "missing_titles",
    "non_empty_sheets",
    "numeric_text",
    "overlay_risk",
    "out_of_bounds",
    "overlaps",
    "page_count",
    "page_size_variants",
    "pages_compared",
    "paragraphs",
    "rendered",
    "reference_count",
    "reference_mismatch",
    "reference_similarity",
    "reference_status",
    "references_compared",
    "remote_references",
    "max_retries",
    "retry_count",
    "retry_gate",
    "retry_recommended",
    "rotation_issues",
    "route",
    "sections",
    "saliency_pct",
    "seam_axis",
    "seam_risk",
    "sheets",
    "size_bytes",
    "slides",
    "subject_review",
    "subject_risk",
    "table_candidates",
    "table_issues",
    "table_text_candidates",
    "tables",
    "text_density",
    "text_like_regions",
    "text_pages",
    "titles",
    "tracked_changes",
    "translucent_pct",
    "unspecified",
    "unique_color_buckets",
    "violations",
    "watermark_risk",
}
_QUALITY_EVIDENCE_DETAIL_ENUMS = {
    "artifact-tool",
    "empty",
    "decode-error",
    "filenotfounderror",
    "oserror",
    "decompressionbombwarning",
    "decompressionbomberror",
    "horizontal",
    "missing",
    "not_applicable",
    "none",
    "pass",
    "pending",
    "pillow-missing",
    "skipped",
    "syntaxerror",
    "unidentifiedimageerror",
    "valueerror",
    "template-following",
    "unspecified",
    "unknown",
    "vertical",
    "verified",
    "verified-existing-deck",
    "artifact-integrity",
    "anomaly-check",
    "decode-valid",
    "final",
    "needs_review",
    "non-blank",
    "overlay-ghosting-check",
    "reference-fidelity",
    "retry",
    "seam-check",
    "subject-structure-check",
    "text-glyph-check",
    "watermark-check",
}
_QUALITY_EVIDENCE_AUTHORING_ROUTES = {
    "artifact-tool",
    "template-following",
    "verified-existing-deck",
    "unspecified",
}
_QUALITY_EVIDENCE_METRIC_KEYS = {
    "blankCellCount",
    "blankPageRiskCount",
    "blankRisk",
    "anomalyRisk",
    "borderMismatchRatio",
    "booleanCellCount",
    "candidatePageCount",
    "cellsScanned",
    "chartCount",
    "chartIssueCount",
    "columnCount",
    "commentCount",
    "commentIdMismatchCount",
    "commentReferenceCount",
    "dateCellCount",
    "dateTextRiskCount",
    "decodeError",
    "decodeValid",
    "drawingObjectCount",
    "duplicateTileScorePct",
    "edgeDensityPct",
    "empty",
    "emptyParagraphCount",
    "emptySheetCount",
    "emptySlideCount",
    "emptyTextPageCount",
    "encrypted",
    "errorCellCount",
    "fitzInspectionAvailable",
    "finalizationStatus",
    "finalized",
    "fontUnspecifiedRunCount",
    "fontViolationCount",
    "formulaCellCount",
    "formulaErrorTokenCount",
    "glyphRiskCount",
    "glyphRiskPageCount",
    "glyphFragmentRisk",
    "hasBody",
    "hasComments",
    "hasTables",
    "hasText",
    "hasTitleOrHeading",
    "hasTrackedChanges",
    "headingParagraphCount",
    "height",
    "heightBucket",
    "hiddenSheetCount",
    "horizontalLineCount",
    "imageObjectCount",
    "imageObjectCountMatch",
    "imageObjectCountMismatchCount",
    "imageOnlyPageCount",
    "kind",
    "luminanceStdDev",
    "landscapePageCount",
    "lineCountBucket",
    "lineLikeComponentCount",
    "listParagraphCount",
    "localReferenceCount",
    "mergedRangeCount",
    "metadataKeyCount",
    "meanGradient",
    "mismatchCount",
    "missingTitleCount",
    "nonEmpty",
    "nonEmptySheetCount",
    "nullGlyphCount",
    "numericCellCount",
    "numericTextRiskCount",
    "orientation",
    "orientationMatch",
    "orientationMismatchCount",
    "outOfBoundsCount",
    "overlayGhostingRisk",
    "overlapWarningCount",
    "page",
    "pageCount",
    "pageCountMatch",
    "pageDiffEvidence",
    "pageEvidence",
    "pageRef",
    "pageSizeMismatchCount",
    "pageSizeVariantCount",
    "pagesCompared",
    "pagesInspected",
    "paragraphCount",
    "pictureCount",
    "pixelCount",
    "portraitPageCount",
    "referenceAspectMismatchRisk",
    "referenceComparedCount",
    "referenceCount",
    "referenceDecodeFailureCount",
    "referencePageCount",
    "referenceRef",
    "referenceMismatchRisk",
    "referenceSimilarityPct",
    "remoteReferenceCount",
    "replacementGlyphCount",
    "retryCount",
    "retryGate",
    "retryRecommended",
    "rotation",
    "rotatedPageCount",
    "rowCount",
    "schemaVersion",
    "sectionCount",
    "seamAxis",
    "seamRatio",
    "seamRisk",
    "seamWarning",
    "shapeCount",
    "sheet",
    "sheetRef",
    "sheetCount",
    "sheetsInspected",
    "sizeBytes",
    "sizeMatch",
    "smallEdgeComponentCount",
    "slide",
    "slideCount",
    "slidesInspected",
    "sourceRef",
    "squarePageCount",
    "status",
    "subjectStructureRisk",
    "tableCandidate",
    "tableCandidateMatch",
    "tableCandidateMismatchCount",
    "tableCellCount",
    "tableCellWidthMissingCount",
    "tableCount",
    "tableGridMissingCount",
    "tableIssueCount",
    "tableRowCount",
    "tableTextCandidatePageCount",
    "tableWidthMissingCount",
    "textCellCount",
    "textDensityPct",
    "textExtractablePageCount",
    "textExtractionError",
    "textExtractionErrorPageCount",
    "textLengthBucket",
    "textLengthBucketMatch",
    "textLengthBucketMismatchCount",
    "textLikeRegionCount",
    "textShapeCount",
    "titleLengthBucket",
    "titleParagraphCount",
    "titlePresent",
    "titleWrapRisk",
    "titleWrapRiskCount",
    "totalExtractedTextChars",
    "trackedChangeCount",
    "trackedDeleteCount",
    "trackedInsertCount",
    "transparentRatioPct",
    "translucentRatioPct",
    "truncated",
    "uniqueColorBucketCount",
    "unexpectedRotationCount",
    "verticalLineCount",
    "visible",
    "watermarkRisk",
    "width",
    "widthBucket",
    "sampleWidth",
    "sampleHeight",
    "format",
    "extension",
    "frameCount",
    "corruptRisk",
    "maxRetries",
}
_QUALITY_EVIDENCE_METRIC_ENUMS = {
    "empty",
    "fail",
    "final",
    "few",
    "landscape",
    "long",
    "many",
    "medium",
    "missing",
    "needs_review",
    "none",
    "pass",
    "png",
    "jpg",
    "jpeg",
    "webp",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    "horizontal",
    "vertical",
    "pillow-missing",
    "decode-error",
    "filenotfounderror",
    "oserror",
    "decompressionbombwarning",
    "decompressionbomberror",
    "syntaxerror",
    "unidentifiedimageerror",
    "valueerror",
    "portrait",
    "retry",
    "short",
    "some",
    "square",
    "unknown",
}
_QUALITY_EVIDENCE_ANALYSIS_KEYS = {
    "presentationAnalysis",
    "spreadsheetAnalysis",
    "documentAnalysis",
    "pdfAnalysis",
    "pdfDiffAnalysis",
    "imageAnalysis",
}
_QUALITY_EVIDENCE_ROOT_KEYS = {
    "schemaVersion",
    "kind",
    "sourceRef",
    "qualityGates",
    "checks",
    "missingQualityGates",
    "status",
    "renderedArtifacts",
    "redacted",
    "authoringRoute",
}.union(_QUALITY_EVIDENCE_ANALYSIS_KEYS)
_QUALITY_EVIDENCE_UNSAFE_KEYS = {
    "path",
    "filepath",
    "filePath",
    "file_path",
    "url",
    "previewUrl",
    "preview_url",
    "thumbnailUrl",
    "thumbnail_url",
    "renderProof",
    "rawText",
    "raw_text",
    "content",
    "body",
    "markdown",
    "prompt",
    "source",
}


def _extract_projection_quality_evidence(value: Any) -> Optional[Dict[str, Any]]:
    raw = _coerce_projection_quality_candidate(value)
    if raw is None:
        return None
    if _looks_like_projection_quality_evidence(raw):
        return _safe_projection_quality_evidence(raw)
    for key in _QUALITY_EVIDENCE_ALIASES:
        nested = raw.get(key)
        if isinstance(nested, dict) and _looks_like_projection_quality_evidence(nested):
            return _safe_projection_quality_evidence(nested)
    for container_key in ("artifact", "artifacts", "result", "output"):
        nested = raw.get(container_key)
        if isinstance(nested, dict):
            found = _extract_projection_quality_evidence(nested)
            if found:
                return found
        if isinstance(nested, list):
            for item in nested[:8]:
                found = _extract_projection_quality_evidence(item)
                if found:
                    return found
    return None


def _drop_projection_quality_evidence_fields(value: Any, *, max_depth: int = 6) -> Any:
    if max_depth <= 0:
        return value
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for key, child in value.items():
            if str(key) in _QUALITY_EVIDENCE_ALIASES:
                continue
            safe[key] = _drop_projection_quality_evidence_fields(child, max_depth=max_depth - 1)
        return safe
    if isinstance(value, list):
        return [
            _drop_projection_quality_evidence_fields(item, max_depth=max_depth - 1)
            for item in value
        ]
    return value


def _coerce_projection_quality_candidate(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.startswith("{") or len(stripped) > 128 * 1024:
            return None
        try:
            parsed = json.loads(stripped)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _looks_like_projection_quality_evidence(value: Dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("qualityGates"), list) or isinstance(value.get("checks"), list):
        return True
    kind = str(value.get("kind") or "").strip().lower()
    return kind in _QUALITY_EVIDENCE_KINDS and str(value.get("status") or "").strip().lower() in _QUALITY_EVIDENCE_STATUSES


def _safe_projection_quality_evidence(value: Any) -> Optional[Dict[str, Any]]:
    raw = _coerce_projection_quality_candidate(value)
    if not raw or not _looks_like_projection_quality_evidence(raw):
        return None

    safe: Dict[str, Any] = {}
    omitted_count = 0
    if "schemaVersion" in raw:
        version = _safe_projection_quality_text(raw.get("schemaVersion"), max_chars=24)
        if version:
            safe["schemaVersion"] = version
        else:
            omitted_count += 1
    if "kind" in raw:
        kind = _safe_projection_quality_kind(raw.get("kind"))
        if kind:
            safe["kind"] = kind
        else:
            omitted_count += 1
    if "sourceRef" in raw:
        source_ref = _safe_projection_quality_ref(raw.get("sourceRef"))
        if source_ref:
            safe["sourceRef"] = source_ref
        else:
            omitted_count += 1
    if isinstance(raw.get("qualityGates"), list):
        safe["qualityGates"] = _safe_projection_quality_gate_list(raw.get("qualityGates") or [])
    if isinstance(raw.get("missingQualityGates"), list):
        safe["missingQualityGates"] = _safe_projection_quality_gate_list(raw.get("missingQualityGates") or [])
    if isinstance(raw.get("checks"), list):
        safe["checks"] = _safe_projection_quality_checks(raw.get("checks") or [])
    if "status" in raw:
        safe["status"] = _safe_projection_quality_status(raw.get("status"))
    if isinstance(raw.get("renderedArtifacts"), list):
        safe["renderedArtifacts"] = _safe_projection_quality_rendered_artifacts(raw.get("renderedArtifacts") or [])
    if "redacted" in raw:
        safe["redacted"] = raw.get("redacted") is True
    if "authoringRoute" in raw:
        route = _safe_projection_quality_authoring_route(raw.get("authoringRoute"))
        if route:
            safe["authoringRoute"] = route
        else:
            omitted_count += 1

    for key in _QUALITY_EVIDENCE_ANALYSIS_KEYS:
        if isinstance(raw.get(key), dict):
            analysis = _safe_projection_quality_analysis(raw.get(key) or {})
            if analysis:
                safe[key] = analysis

    for key in raw:
        if key not in _QUALITY_EVIDENCE_ROOT_KEYS:
            omitted_count += 1
    if omitted_count:
        safe["qualityEvidenceSanitized"] = True
        safe["omittedQualityEvidenceFieldCount"] = omitted_count
    if "status" not in safe:
        statuses = {str(item.get("status") or "").lower() for item in safe.get("checks") or [] if isinstance(item, dict)}
        safe["status"] = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pending" if "pending" in statuses else "pass"
    safe.setdefault("redacted", True)
    return safe


def _safe_projection_quality_kind(value: Any) -> str:
    kind = _safe_projection_quality_text(value, max_chars=32).lower()
    return kind if kind in _QUALITY_EVIDENCE_KINDS else ""


def _safe_projection_quality_status(value: Any) -> str:
    status = _safe_projection_quality_text(value, max_chars=24).lower()
    return status if status in _QUALITY_EVIDENCE_STATUSES else "unknown"


def _safe_projection_quality_gate_list(items: Iterable[Any]) -> List[str]:
    safe: List[str] = []
    for item in list(items)[:40]:
        gate = _safe_projection_quality_gate(item)
        if gate:
            safe.append(gate)
    return safe


def _safe_projection_quality_checks(items: Iterable[Any]) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    for item in list(items)[:48]:
        if not isinstance(item, dict):
            continue
        check_id = _safe_projection_quality_gate(item.get("id") or item.get("gate")) or "unknown-check"
        status = _safe_projection_quality_status(item.get("status"))
        detail = _safe_projection_quality_check_detail(item.get("detail") or item.get("summary") or "")
        check = {"id": check_id, "status": status}
        if detail:
            check["detail"] = detail
        safe.append(check)
    return safe


def _safe_projection_quality_gate(value: Any) -> str:
    gate = _safe_projection_quality_text(value, max_chars=72).lower()
    return gate if gate in _QUALITY_EVIDENCE_ALLOWED_GATES else ""


def _safe_projection_quality_authoring_route(value: Any) -> str:
    route = _safe_projection_quality_text(value, max_chars=64).lower()
    return route if route in _QUALITY_EVIDENCE_AUTHORING_ROUTES else ""


def _safe_projection_quality_check_detail(value: Any) -> str:
    parts: List[str] = []
    for raw_part in str(value or "").split(";"):
        if len(parts) >= 12:
            break
        if "=" not in raw_part:
            continue
        raw_key, raw_val = raw_part.split("=", 1)
        key = raw_key.strip().lower()
        val = raw_val.strip().lower()
        if key not in _QUALITY_EVIDENCE_DETAIL_KEYS:
            continue
        if val.isdigit():
            parts.append(f"{key}={int(val)}")
        elif val in _QUALITY_EVIDENCE_DETAIL_ENUMS:
            parts.append(f"{key}={val}")
    return "; ".join(parts)[:240]


def _safe_projection_quality_rendered_artifacts(items: Iterable[Any]) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    for item in list(items)[:24]:
        if not isinstance(item, dict):
            continue
        render: Dict[str, Any] = {}
        for key in ("slide", "page", "sizeBytes", "width", "height"):
            value = item.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                render[key] = max(0, int(value))
        extension = _safe_projection_quality_text(item.get("extension"), max_chars=12).lower()
        if extension in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            render["extension"] = extension
        for key in ("artifactRef", "sourceRef"):
            ref = _safe_projection_quality_ref(item.get(key))
            if ref:
                render[key] = ref
        if render:
            safe.append(render)
    return safe


def _safe_projection_quality_analysis(raw: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in ("schemaVersion", "kind", "sourceRef"):
        if key not in raw:
            continue
        if key == "kind":
            value = _safe_projection_quality_kind(raw.get(key))
        elif key == "sourceRef":
            value = _safe_projection_quality_ref(raw.get(key))
        else:
            value = _safe_projection_quality_text(raw.get(key), max_chars=24)
        if value:
            safe[key] = value
    if isinstance(raw.get("summary"), dict):
        safe["summary"] = _safe_projection_quality_metrics(raw.get("summary") or {}, max_fields=96)
    for key in ("pageEvidence", "sheetEvidence", "slideEvidence", "documentEvidence", "diffEvidence", "pages"):
        if isinstance(raw.get(key), list):
            items = [
                _safe_projection_quality_metrics(item, max_fields=48)
                for item in list(raw.get(key) or [])[:24]
                if isinstance(item, dict)
            ]
            safe[key] = [item for item in items if item]
    return safe


def _safe_projection_quality_metrics(raw: Dict[str, Any], *, max_fields: int = 80) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    omitted_count = 0
    for index, (key, value) in enumerate(raw.items()):
        if index >= max_fields:
            omitted_count += 1
            continue
        key_text = str(key or "")
        if key_text not in _QUALITY_EVIDENCE_METRIC_KEYS:
            omitted_count += 1
            continue
        if _quality_evidence_key_is_unsafe(key_text):
            omitted_count += 1
            continue
        safe_value = _safe_projection_quality_metric_value(key_text, value)
        if safe_value is None:
            omitted_count += 1
            continue
        safe[key_text] = safe_value
    if omitted_count:
        safe["omittedMetricFieldCount"] = omitted_count
    return safe


def _safe_projection_quality_metric_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if key in {"artifactRef", "sourceRef"}:
            return _safe_projection_quality_ref(value)
        return _safe_projection_quality_metric_string(key, value)
    if isinstance(value, dict):
        return _safe_projection_quality_metrics(value, max_fields=32)
    if isinstance(value, list):
        safe_items = []
        for item in value[:16]:
            safe_item = _safe_projection_quality_metric_value(key, item)
            if safe_item is not None:
                safe_items.append(safe_item)
        return safe_items
    return None


def _safe_projection_quality_metric_string(key: str, value: Any) -> Optional[str]:
    if key.endswith("Ref"):
        return _safe_projection_quality_ref(value)
    text = _safe_projection_quality_text(value, max_chars=32).lower()
    if not text:
        return None
    if key == "kind":
        return _safe_projection_quality_kind(text) or None
    if key == "status":
        return text if text in {"pass", "fail", "warn", "pending", "skipped", "unknown"} else None
    if key == "retryGate":
        return _safe_projection_quality_gate(text) or None
    if key in {
        "decodeError",
        "extension",
        "finalizationStatus",
        "format",
        "heightBucket",
        "lineCountBucket",
        "orientation",
        "seamAxis",
        "textLengthBucket",
        "titleLengthBucket",
        "widthBucket",
    }:
        if text in _QUALITY_EVIDENCE_METRIC_ENUMS:
            return text
        if text.isdigit():
            return str(int(text))
    return None


def _quality_evidence_key_is_unsafe(key: str) -> bool:
    if key in {"artifactRef", "sourceRef"}:
        return False
    lowered = str(key or "").strip()
    normalized = lowered.replace("_", "").replace("-", "").lower()
    unsafe = {item.replace("_", "").replace("-", "").lower() for item in _QUALITY_EVIDENCE_UNSAFE_KEYS}
    if normalized in unsafe:
        return True
    return normalized.endswith("path") or normalized.endswith("url") or normalized.endswith("proof")


def _safe_projection_quality_ref(value: Any) -> str:
    ref = " ".join(str(value or "").strip().split())
    if not ref:
        return ""
    if _quality_ref_is_hmac(ref):
        return ref
    return _stable_projection_artifact_reference("quality-ref", ref)


def _quality_ref_is_hmac(value: str) -> bool:
    text = str(value or "").strip()
    if not text.lower().startswith("hmac:"):
        return False
    digest = text.split(":", 1)[1]
    return 8 <= len(digest) <= 128 and all(char in "0123456789abcdefABCDEF" for char in digest)


def _safe_projection_quality_text(value: Any, *, max_chars: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    text = mask_sensitive_text(text, max_chars=max_chars)
    if _looks_like_inline_artifact_data(text):
        return ""
    if _projection_artifact_text_requires_redaction(text):
        return _stable_projection_artifact_reference("quality-text", text)
    return text


def _safe_projection_artifact_path_reference(value: str) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if not raw:
        return raw
    if _projection_artifact_text_requires_redaction(raw):
        return _stable_projection_artifact_reference("artifact-ref", raw)
    if lowered.startswith(("http://", "https://")):
        if "?" in raw or "#" in raw:
            return _stable_projection_artifact_reference("artifact-url", raw)
        return raw
    if lowered.startswith("file://"):
        return _stable_projection_artifact_reference("artifact-path", raw)
    if _looks_like_user_home_artifact_path(raw):
        return _stable_projection_artifact_reference("artifact-path", raw)
    if os.path.isabs(raw):
        return raw
    if "\\" in raw and (":" in raw.split("\\", 1)[0] or raw.startswith("\\\\")):
        return raw
    return raw


def _looks_like_user_home_artifact_path(value: str) -> bool:
    normalized = str(value or "").replace("\\", "/").lower()
    return "/users/" in normalized or normalized.startswith("users/") or normalized.startswith("~/") or normalized.startswith("/home/")


def _overlay_runtime_requests_on_history(
    history: Dict[str, Any],
    session_projection: Dict[str, Any],
    *,
    page: int = 1,
) -> Tuple[Dict[str, Any], List[str]]:
    projected = dict(history or {})
    messages = [dict(item) for item in projected.get("messages") or [] if isinstance(item, dict)]
    history_request_ids = {
        str(item.get("request_id") or "")
        for item in messages
        if item.get("request_id")
    }
    by_request_id = {
        str(item.get("request_id") or ""): item
        for item in messages
        if item.get("role") == "assistant" and item.get("request_id")
    }
    page_request_ids: List[str] = []
    latest_history_created_at = _history_latest_created_at(messages)
    appended = False
    for request in session_projection.get("requests") or []:
        if not isinstance(request, dict):
            continue
        request_id = str(request.get("request_id") or "")
        if not request_id:
            continue
        assistant = _projection_assistant(request)
        user = _projection_user(request)
        if not assistant and not request.get("terminal_message"):
            continue
        existing = by_request_id.get(request_id)
        owns_history_row = request_id in history_request_ids or existing is not None
        should_append = owns_history_row or _should_append_runtime_request_to_history_page(
            messages,
            request,
            page=page,
            latest_history_created_at=latest_history_created_at,
        )
        if not should_append:
            continue
        if existing is None and user and not _history_has_user_request(messages, request_id, user):
            messages.append(_runtime_user_history_message(request, user))
            appended = True
        if existing is None:
            existing = _runtime_assistant_history_message(request, assistant or {})
            messages.append(existing)
            by_request_id[request_id] = existing
            appended = True
        else:
            _merge_runtime_assistant_history_message(existing, request, assistant or {})
        if request_id not in page_request_ids:
            page_request_ids.append(request_id)
    if appended:
        messages.sort(key=lambda item: (int(item.get("created_at") or 0), 0 if item.get("role") == "user" else 1))
    projected["messages"] = messages
    projected["runtime_projection"] = {
        "latest_event_id": session_projection.get("latest_event_id", 0),
        "request_count": len(page_request_ids),
        "source": "runtime_projection",
    }
    return projected, page_request_ids


def _history_latest_created_at(messages: List[Dict[str, Any]]) -> int:
    latest = 0
    for message in messages:
        try:
            latest = max(latest, int(float(message.get("created_at") or 0)))
        except Exception:
            continue
    return latest


def _should_append_runtime_request_to_history_page(
    messages: List[Dict[str, Any]],
    request: Dict[str, Any],
    *,
    page: int,
    latest_history_created_at: int,
) -> bool:
    if int(page or 1) != 1:
        return False
    if not messages:
        return True
    if _runtime_request_is_active(request):
        return True
    if latest_history_created_at <= 0:
        return False
    return _runtime_created_at(request) >= latest_history_created_at


def _runtime_request_is_active(request: Dict[str, Any]) -> bool:
    state = str(request.get("state") or "").strip().lower()
    if state and state not in {"completed", "failed", "cancelled", "interrupted"}:
        return True
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "assistant" and bool(message.get("pending")):
            return True
    for job in request.get("image_jobs") or []:
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status and status not in {"completed", "failed", "cancelled"}:
            return True
    return False


def _projection_assistant(request: Dict[str, Any]) -> Dict[str, Any]:
    for message in reversed(request.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message
    return {}


def _projection_user(request: Dict[str, Any]) -> Dict[str, Any]:
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return {}


def _history_has_user_request(messages: List[Dict[str, Any]], request_id: str, user: Optional[Dict[str, Any]] = None) -> bool:
    user_content = str((user or {}).get("content") or "").strip()
    for message in messages:
        if message.get("role") != "user":
            continue
        extras = message.get("extras") if isinstance(message.get("extras"), dict) else {}
        message_request_id = str(message.get("request_id") or extras.get("request_id") or "").strip()
        message_turn_id = str(message.get("turn_id") or extras.get("turn_id") or "").strip()
        if request_id and (message_request_id == request_id or message_turn_id == request_id):
            return True
        if user_content and str(message.get("content") or "").strip() == user_content:
            return True
    return False


def _runtime_created_at(request: Dict[str, Any]) -> int:
    raw = request.get("created_at") or request.get("updated_at") or time.time()
    try:
        return int(float(raw))
    except Exception:
        return int(time.time())


def _runtime_user_history_message(request: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    request_id = str(request.get("request_id") or user.get("request_id") or "")
    return {
        "role": "user",
        "content": str(user.get("content") or ""),
        "created_at": _runtime_created_at(request),
        "request_id": request_id,
        "turn_id": str(request.get("turn_id") or user.get("turn_id") or request_id),
        "runtime_projection": {
            "source": "runtime_projection",
            "state": request.get("state") or "",
            "latest_event_id": request.get("latest_event_id", 0),
        },
    }


def _runtime_assistant_history_message(request: Dict[str, Any], assistant: Dict[str, Any]) -> Dict[str, Any]:
    message = {
        "role": "assistant",
        "content": str(assistant.get("content") or request.get("terminal_message") or ""),
        "created_at": _runtime_created_at(request),
        "request_id": str(request.get("request_id") or assistant.get("request_id") or ""),
        "turn_id": str(request.get("turn_id") or assistant.get("turn_id") or request.get("request_id") or ""),
    }
    _merge_runtime_assistant_history_message(message, request, assistant)
    return message


def _merge_runtime_assistant_history_message(message: Dict[str, Any], request: Dict[str, Any], assistant: Dict[str, Any]) -> None:
    content = str(assistant.get("content") or request.get("terminal_message") or "")
    if content:
        message["content"] = content
    if assistant.get("tool_calls"):
        message["tool_calls"] = list(assistant.get("tool_calls") or [])
    artifacts = _merge_artifacts(
        ((message.get("extras") or {}) if isinstance(message.get("extras"), dict) else {}).get("artifacts"),
        assistant.get("artifacts"),
    )
    for job in request.get("image_jobs") or []:
        if isinstance(job, dict):
            artifacts = _merge_artifacts(artifacts, job.get("artifacts"))
    if artifacts:
        extras = message.get("extras") if isinstance(message.get("extras"), dict) else {}
        extras = dict(extras)
        extras["artifacts"] = artifacts
        message["extras"] = extras
    message["runtime_projection"] = {
        "source": "runtime_projection",
        "state": request.get("state") or "",
        "latest_event_id": request.get("latest_event_id", 0),
        "event_count": request.get("event_count", 0),
        "terminal_reason": request.get("terminal_reason") or "",
    }


def _merge_artifacts(*artifact_lists: Any) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for artifact_list in artifact_lists:
        if not isinstance(artifact_list, list):
            continue
        for artifact in artifact_list:
            if not isinstance(artifact, dict):
                continue
            key = str(
                artifact.get("safeArtifactId")
                or artifact.get("safe_artifact_id")
                or artifact.get("id")
                or artifact.get("path")
                or artifact.get("relativePath")
                or artifact.get("relative_path")
                or artifact.get("url")
                or artifact.get("title")
                or artifact.get("file_name")
                or ""
            )
            if not key:
                key = repr(sorted(artifact.items()))[:200]
            if key in seen:
                continue
            seen.add(key)
            merged.append(artifact)
    return _sort_projection_artifacts(merged)


def _reduce_image_job_event(
    jobs: Dict[str, Dict[str, Any]],
    event_type: str,
    payload: Dict[str, Any],
    event_id: Any,
) -> None:
    job_id = str(payload.get("job_id") or "")
    job_id = _safe_projection_image_job_id(job_id) or ""
    if not job_id:
        return
    job = jobs.setdefault(job_id, {
        "job_id": job_id,
        "status": "running",
        "operation": "",
        "tasks": [],
        "artifacts": [],
        "last_event_id": event_id,
    })
    job["last_event_id"] = event_id
    if event_type == "image_job.started":
        job["status"] = "running"
        job["operation"] = _safe_projection_operation(payload.get("operation"))
        job["task_count"] = _safe_projection_nonnegative_int(payload.get("task_count")) or 0
        raw_tasks = payload.get("tasks")
        if isinstance(raw_tasks, list):
            job["tasks"] = [_safe_projection_task(item) for item in raw_tasks if isinstance(item, dict)]
        for key in (
            "provider",
            "resolved_model",
            "model",
            "image_mode",
            "input_image_count",
            "max_parallel",
            "output_count",
            "api_key_source",
            "api_base_host_hash",
            "requested_max_parallel",
            "configured_max_parallel",
            "default_max_parallel",
            "provider_max_parallel",
            "hard_max_parallel",
            "effective_max_parallel",
            "parallelism_defaulted",
            "parallelism_clamped",
            "parallelism_clamp_reason",
            "parallelism_policy_version",
            "ocr_cache_enabled",
            "ocr_provider",
        ):
            if key in payload:
                if key in {
                    "provider",
                    "resolved_model",
                    "model",
                    "image_mode",
                    "api_key_source",
                    "api_base_host_hash",
                    "parallelism_clamp_reason",
                    "parallelism_policy_version",
                    "ocr_provider",
                }:
                    value = _safe_projection_telemetry_token(payload.get(key))
                elif key in {"parallelism_clamped", "parallelism_defaulted", "ocr_cache_enabled"}:
                    value = _safe_projection_bool(payload.get(key))
                else:
                    value = _safe_projection_nonnegative_int(payload.get(key))
                if value is not None:
                    job[key] = value
    elif event_type == "image_job.progress":
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        job["status"] = "running"
        task_id = str(payload.get("task_id") or "")
        task_id = _safe_projection_image_task_id(task_id) or ""
        if task_id:
            task = _image_job_task(job, task_id)
            task["status"] = _safe_image_job_progress_status(payload.get("status") or task.get("status") or "running")
            if "progress" in payload:
                progress = _safe_projection_progress(payload.get("progress"))
                if progress is not None:
                    task["progress"] = progress
            for key in (
                "provider",
                "model",
                "fallback_provider",
                "fallback_from_model",
                "fallback_to_model",
                "fallback_reason",
            ):
                if key in payload:
                    value = _safe_projection_telemetry_token(payload.get(key))
                    if value is not None:
                        task[key] = value
                        if key.startswith("fallback_"):
                            job[key] = value
                        else:
                            job[f"last_{key}"] = value
            if "fallback_used" in payload:
                value = _safe_projection_bool(payload.get("fallback_used"))
                if value is not None:
                    task["fallback_used"] = value
                    job["fallback_used"] = value
            if "attempted_provider_count" in payload:
                value = _safe_projection_nonnegative_int(payload.get("attempted_provider_count"))
                if value is not None:
                    task["attempted_provider_count"] = value
                    job["attempted_provider_count"] = value
            for key in ("ocr_brief_hash", "ocr_cache_key", "ocr_provider"):
                if key in payload:
                    value = _safe_projection_telemetry_token(payload.get(key))
                    if value is not None:
                        task[key] = value
                        if key == "ocr_provider":
                            job["ocr_provider"] = value
            for key in ("ocr_cache_enabled", "ocr_cache_hit"):
                if key in payload:
                    value = _safe_projection_bool(payload.get(key))
                    if value is not None:
                        task[key] = value
            for key in ("ocr_input_image_count", "ocr_ms"):
                if key in payload:
                    value = _safe_projection_nonnegative_int(payload.get(key))
                    if value is not None:
                        task[key] = value
                        if key == "ocr_ms":
                            job["ocr_total_ms"] = int(job.get("ocr_total_ms") or 0) + value
            for key, total_key in (
                ("provider_latency_ms", "provider_total_ms"),
                ("quality_latency_ms", "quality_total_ms"),
                ("finalization_latency_ms", "finalization_total_ms"),
                ("postprocess_latency_ms", "postprocess_total_ms"),
            ):
                if key in payload:
                    value = _safe_projection_nonnegative_int(payload.get(key))
                    if value is not None:
                        task[key] = value
                        job[total_key] = int(job.get(total_key) or 0) + value
            if payload.get("status") == "ocr" and "ocr_cache_hit" in task:
                if task.get("ocr_cache_hit"):
                    job["ocr_cache_hit_count"] = int(job.get("ocr_cache_hit_count") or 0) + 1
                else:
                    job["ocr_cache_miss_count"] = int(job.get("ocr_cache_miss_count") or 0) + 1
            task["last_event_id"] = event_id
    elif event_type == "image_job.artifact":
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        artifact = payload.get("artifact")
        if isinstance(artifact, dict):
            artifact = _safe_projection_artifact(artifact)
            job.setdefault("artifacts", []).append(artifact)
            job["artifacts"] = _sort_projection_artifacts(job.get("artifacts") or [])
            task_id = str(payload.get("task_id") or "")
            task_id = _safe_projection_image_task_id(task_id) or ""
            if task_id:
                task = _image_job_task(job, task_id)
                task.setdefault("artifacts", []).append(artifact)
                task["artifacts"] = _sort_projection_artifacts(task.get("artifacts") or [])
                task["status"] = task.get("status") or "artifact"
    elif event_type == "image_job.completed":
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        job["status"] = "completed"
        _finalize_image_job_tasks(job, "completed", event_id)
        artifact_count = _safe_projection_nonnegative_int(payload.get("artifact_count"))
        job["artifact_count"] = artifact_count if artifact_count is not None else len(job.get("artifacts") or [])
        if "total_latency_ms" in payload:
            total_latency_ms = _safe_projection_nonnegative_int(payload.get("total_latency_ms"))
            if total_latency_ms is not None:
                job["total_latency_ms"] = total_latency_ms
    elif event_type == "image_job.failed":
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        job["status"] = "failed"
        job["error_message"] = _safe_image_job_error_message(payload)
        job["error_type"] = _safe_image_job_error_type(payload.get("error_type"))
        _finalize_image_job_tasks(job, "failed", event_id)
    elif event_type == "image_job.cancelled":
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return
        job["status"] = "cancelled"
        job["cancel_reason"] = _safe_image_job_cancel_reason(payload.get("reason") or "cancelled")
        _finalize_image_job_tasks(job, "cancelled", event_id)


def _reduce_external_connection_event(
    connections_by_platform: Dict[str, Dict[str, Any]],
    event_type: str,
    payload: Dict[str, Any],
    event_id: Any,
    created_at: Any,
) -> None:
    platform = _safe_projection_external_platform(payload.get("platform"))
    record = connections_by_platform.setdefault(platform, {
        "platform": platform,
        "status": "unknown",
        "lastAction": "",
        "lastEventType": "",
        "lastEventId": 0,
        "updatedAt": 0,
    })
    status = _safe_projection_external_status(payload.get("status"), event_type=event_type)
    action = _safe_projection_external_action(payload.get("action"))
    event_id_value = _safe_projection_nonnegative_int(event_id)
    record["status"] = status
    record["lastEventType"] = event_type
    if event_id_value is not None:
        record["lastEventId"] = event_id_value
    if isinstance(created_at, (int, float)):
        record["updatedAt"] = created_at
    if action:
        record["lastAction"] = action
    for key, out_key in (
        ("configured", "configured"),
        ("connected", "connected"),
        ("callable", "callable"),
        ("running", "running"),
        ("remoteConnectivityProbed", "remoteConnectivityProbed"),
        ("homeChannelConfigured", "homeChannelConfigured"),
        ("accepted", "lastIngressAccepted"),
        ("deduped", "lastIngressDeduped"),
    ):
        if key in payload:
            value = _safe_projection_bool(payload.get(key))
            if value is not None:
                record[out_key] = value
    for key, out_key in (
        ("mode", "mode"),
        ("reason", "reason"),
        ("dedupeKey", "lastDedupeKey"),
        ("homeChannelHash", "homeChannelHash"),
        ("operation_id", "operationId"),
        ("operationId", "operationId"),
    ):
        if key in payload:
            record[out_key] = _safe_projection_external_text(payload.get(key), string_limit=500)
    if "lastError" in payload:
        record["lastError"] = _safe_projection_external_error_label(payload.get("lastError"))
    if "error" in payload:
        record["error"] = _safe_projection_external_error_label(payload.get("error"))
    if isinstance(payload.get("errorSummary"), dict):
        error_summary = _safe_projection_external_error_summary(payload.get("errorSummary") or {})
        if error_summary:
            record["errorSummary"] = error_summary
    if isinstance(payload.get("adapter"), dict):
        record["adapter"] = _safe_projection_external_adapter_summary(payload.get("adapter") or {})
    if isinstance(payload.get("inbound"), dict):
        record["lastIngress"] = _safe_projection_external_message_summary(payload.get("inbound") or {})
    if isinstance(payload.get("delivery"), dict):
        record["lastDelivery"] = _safe_projection_external_message_summary(payload.get("delivery") or {})


def _reduce_skill_learning_event(
    drafts: Dict[str, Dict[str, Any]],
    event_type: str,
    payload: Dict[str, Any],
    event_id: Any,
) -> None:
    draft_id = _safe_projection_identifier(payload.get("draftId") or payload.get("draft_id"))
    name = _safe_projection_identifier(payload.get("name"))
    if not draft_id:
        if event_type == "skill_learning.requested":
            goal = str(payload.get("goal") or "")[:120]
            digest = hashlib.sha256(goal.encode("utf-8", errors="replace")).hexdigest()[:12]
            draft_id = f"learning-{digest}"
        else:
            return
    record = drafts.setdefault(draft_id, {"draftId": draft_id, "status": "learning"})
    if name:
        record["name"] = name
    if "goal" in payload:
        record["goal"] = str(payload.get("goal") or "")[:1000]
    if "description" in payload:
        record["description"] = str(payload.get("description") or "")[:1000]
    if "manifest" in payload and isinstance(payload.get("manifest"), dict):
        record["manifest"] = _safe_projection_generic_payload(payload.get("manifest") or {})
    if event_type == "skill_draft.created":
        record["status"] = str(payload.get("status") or "draft")
        record["sources"] = _safe_projection_generic_payload({"sources": payload.get("sources") or []}).get("sources", [])
    elif event_type == "skill_draft.validation_completed":
        record["validation"] = _safe_projection_generic_payload(payload)
        if payload.get("status") != "pass":
            record["status"] = "blocked"
    elif event_type == "skill_draft.security_reviewed":
        record["security"] = _safe_projection_generic_payload(payload)
        if payload.get("status") != "pass":
            record["status"] = "blocked"
    elif event_type == "skill_draft.role_reviewed":
        record["reviewState"] = _safe_projection_generic_payload(payload)
    elif event_type == "skill_draft.approved":
        record["status"] = "approved"
    elif event_type == "skill.registered":
        record["status"] = "registered"
        record["registered"] = True
    elif event_type == "skill.materialized":
        record["materialized"] = True
        record["path"] = str(payload.get("path") or "")[:500]
    record["lastEventType"] = event_type
    record["last_event_id"] = _safe_projection_nonnegative_int(event_id) or record.get("last_event_id")


def _image_job_task(job: Dict[str, Any], task_id: str) -> Dict[str, Any]:
    tasks = job.setdefault("tasks", [])
    for task in tasks:
        if isinstance(task, dict) and str(task.get("task_id") or "") == task_id:
            return task
    task = {"task_id": task_id}
    tasks.append(task)
    return task


def _image_job_is_terminal(job: Any) -> bool:
    return isinstance(job, dict) and job.get("status") in {"completed", "failed", "cancelled"}


def _safe_image_job_cancel_reason(reason: Any) -> str:
    raw = str(reason or "").strip()
    if raw in {
        "cancel_requested",
        "cancelled",
        "deadline_exceeded",
        "provider_cancelled",
        "shutdown",
        "timeout",
        "user_cancelled",
        "user_stop",
    }:
        return raw
    return "cancelled"


def _safe_image_job_error_message(payload: Dict[str, Any]) -> str:
    error_type = _safe_image_job_error_type(payload.get("error_type"))
    return f"{error_type}: image job failed"


def _safe_image_job_error_type(error_type: Any) -> str:
    raw = str(error_type or "").strip()
    if any(part in raw.lower() for part in ("private", "prompt", "secret", "token", "password")):
        return "Error"
    if not raw or len(raw) > 80 or raw[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return "Error"
    if all(char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._" for char in raw):
        return raw
    return "Error"


_IMAGE_JOB_PROGRESS_STATUSES = {
    "artifact",
    "cancelled",
    "completed",
    "download",
    "failed",
    "fallback",
    "ocr",
    "progress",
    "provider_request",
    "provider_response",
    "quality_check",
    "queued",
    "rate_limited",
    "retry",
    "running",
    "saving",
    "started",
    "waiting",
}


def _safe_image_job_progress_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    if raw in _IMAGE_JOB_PROGRESS_STATUSES:
        return raw
    return "progress"


def _safe_image_job_terminal_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    if raw in {"completed", "failed", "cancelled", "skipped"}:
        return raw
    return "skipped"


def _finalize_image_job_tasks(job: Dict[str, Any], terminal_status: str, event_id: Any) -> None:
    tasks = job.setdefault("tasks", [])
    for task in tasks:
        if not isinstance(task, dict):
            continue
        current_status = str(task.get("status") or "")
        if current_status not in {"completed", "failed", "cancelled", "skipped"}:
            task["status"] = terminal_status if current_status else (
                "skipped" if terminal_status in {"failed", "cancelled"} else terminal_status
            )
        task["terminal_job_status"] = terminal_status
        task["last_event_id"] = event_id

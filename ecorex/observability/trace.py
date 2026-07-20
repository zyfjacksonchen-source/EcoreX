"""OpenTelemetry-compatible trace projection from immutable Runtime facts."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from ecorex.protocol import (
    EventEnvelope,
    PublicToolActivity,
    TraceProjectionResponse,
    TraceSpanProjection,
)
from ecorex.replay import ReplayIntegrityError, ReplayService


_TRACE_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$")
_TRACE_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$")
_TRACE_DISCOVERY = re.compile(
    r"^connector:[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}@"
    r"[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}/"
    r"[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}@[0-9a-f]{64}$"
)


def _trace_id(thread_id: str) -> str:
    return hashlib.sha256(f"ecorex:thread:{thread_id}".encode("utf-8")).hexdigest()[:32]


def _span_id(identity: str) -> str:
    return hashlib.sha256(f"ecorex:span:{identity}".encode("utf-8")).hexdigest()[:16]


def _unix_nano(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    seconds = calendar.timegm(normalized.utctimetuple())
    return str(seconds * 1_000_000_000 + normalized.microsecond * 1_000)


@dataclass(slots=True)
class _Span:
    identity: str
    name: str
    parent_span_id: str | None
    start: datetime
    end: datetime
    kind: str = "INTERNAL"
    status: str = "UNSET"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def span_id(self) -> str:
        return _span_id(self.identity)


class TraceProjector:
    def __init__(
        self, replay: ReplayService, *, service_version: str = "1.0.0"
    ) -> None:
        self.replay = replay
        self.service_version = service_version

    def project(
        self, thread_id: str, *, through_seq: int | None = None
    ) -> TraceProjectionResponse:
        events, _watermark, target, digest = self.replay.verified_events(
            thread_id, through_seq=through_seq
        )
        trace_id = _trace_id(thread_id)
        first, last = events[0], events[-1]
        root = _Span(
            identity=f"thread:{thread_id}",
            name="ecorex.thread",
            parent_span_id=None,
            start=first.created_at,
            end=last.created_at,
            kind="SERVER",
            attributes={
                "ecorex.thread.id": thread_id,
                "ecorex.event.through_seq": target,
                "ecorex.event.digest": digest,
            },
        )
        spans: dict[str, _Span] = {root.identity: root}
        turn_spans: dict[str, _Span] = {}
        model_spans: dict[tuple[str, int], _Span] = {}
        model_recovery_spans: dict[str, _Span] = {}
        tool_spans: dict[tuple[str, str], _Span] = {}
        recovery_spans: dict[str, _Span] = {}
        tool_items: dict[str, tuple[str, str]] = {}
        human_spans: dict[str, _Span] = {}

        for event in events:
            self._consume(
                event,
                root=root,
                spans=spans,
                turn_spans=turn_spans,
                model_spans=model_spans,
                model_recovery_spans=model_recovery_spans,
                tool_spans=tool_spans,
                recovery_spans=recovery_spans,
                tool_items=tool_items,
                human_spans=human_spans,
            )
        for span in spans.values():
            if span.end < span.start:
                span.end = span.start
            if (
                span.end == span.start
                and span is not root
                and span.name != "ecorex.artifact"
                and span.status == "UNSET"
            ):
                span.end = (
                    last.created_at if last.created_at >= span.start else span.start
                )

        projections = [
            TraceSpanProjection(
                trace_id=trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                name=span.name,
                kind=span.kind,
                start_time_unix_nano=_unix_nano(span.start),
                end_time_unix_nano=_unix_nano(span.end),
                status=span.status,
                attributes=span.attributes,
                events=span.events,
            )
            for span in spans.values()
        ]
        otlp_spans = [self._otlp_span(span) for span in projections]
        otlp = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            self._otlp_attribute("service.name", "ecorex-runtime"),
                            self._otlp_attribute(
                                "service.version", self.service_version
                            ),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {
                                "name": "ecorex.runtime.event-store",
                                "version": self.service_version,
                            },
                            "spans": otlp_spans,
                        }
                    ],
                }
            ]
        }
        return TraceProjectionResponse(
            thread_id=thread_id,
            trace_id=trace_id,
            through_seq=target,
            event_digest=digest,
            spans=projections,
            otlp=otlp,
        )

    def _consume(
        self,
        event: EventEnvelope,
        *,
        root: _Span,
        spans: dict[str, _Span],
        turn_spans: dict[str, _Span],
        model_spans: dict[tuple[str, int], _Span],
        model_recovery_spans: dict[str, _Span],
        tool_spans: dict[tuple[str, str], _Span],
        recovery_spans: dict[str, _Span],
        tool_items: dict[str, tuple[str, str]],
        human_spans: dict[str, _Span],
    ) -> None:
        payload = event.payload
        if event.event_type == "turn.accepted" and event.turn_id:
            identity = f"turn:{event.turn_id}"
            span = _Span(
                identity=identity,
                name="ecorex.turn",
                parent_span_id=root.span_id,
                start=event.created_at,
                end=event.created_at,
                attributes={
                    "ecorex.thread.id": event.thread_id,
                    "ecorex.turn.id": event.turn_id,
                    "gen_ai.request.model": str(payload.get("agent_model_id") or ""),
                    "ecorex.permission.snapshot_id": event.permission_snapshot_id or "",
                    "ecorex.capability.snapshot_id": event.capability_snapshot_id or "",
                    "ecorex.original_trace_id": event.trace_id or "",
                },
            )
            spans[identity] = span
            turn_spans[event.turn_id] = span
            return
        turn_span = turn_spans.get(event.turn_id or "")
        if event.event_type == "turn.status_changed" and turn_span:
            target = str(payload.get("to") or "")
            turn_span.end = event.created_at
            turn_span.events.append(self._span_event(event, {"turn.status": target}))
            if target == "completed":
                turn_span.status = "OK"
            elif target in {"failed", "dead_letter"}:
                turn_span.status = "ERROR"
            if target in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "superseded",
            }:
                for key, span in model_spans.items():
                    if key[0] == event.turn_id and span.status == "UNSET":
                        span.end = event.created_at
                        if target == "failed":
                            span.status = "ERROR"
                for key, span in tool_spans.items():
                    if key[0] == event.turn_id and span.status == "UNSET":
                        span.end = event.created_at
                for span in model_recovery_spans.values():
                    if (
                        span.attributes.get("ecorex.turn.id") == event.turn_id
                        and span.status == "UNSET"
                    ):
                        span.end = event.created_at
                        if target == "failed":
                            span.status = "ERROR"
                for span in recovery_spans.values():
                    if (
                        span.attributes.get("ecorex.turn.id") == event.turn_id
                        and span.status == "UNSET"
                    ):
                        span.end = event.created_at
                        if target == "failed":
                            span.status = "ERROR"
                for span in human_spans.values():
                    if span.attributes.get("ecorex.turn.id") == event.turn_id:
                        span.end = event.created_at
            return
        if event.event_type in {
            "model.requested",
            "model.continuation_requested",
            "model.continuation_recovery_requested",
        }:
            if not event.turn_id or turn_span is None:
                return
            round_index = self._round_index(payload)
            identity = f"model:{event.turn_id}:{round_index}"
            span = _Span(
                identity=identity,
                name="gen_ai.model_attempt",
                parent_span_id=turn_span.span_id,
                start=event.created_at,
                end=event.created_at,
                kind="CLIENT",
                attributes={
                    "ecorex.turn.id": event.turn_id,
                    "gen_ai.request.model": str(payload.get("agent_model_id") or ""),
                    "gen_ai.request.id": str(payload.get("request_id") or ""),
                    "gen_ai.request.round": round_index,
                    "gen_ai.request.continuation": (
                        event.event_type == "model.continuation_requested"
                    ),
                    "ecorex.model.continuation_recovery": (
                        event.event_type == "model.continuation_recovery_requested"
                    ),
                },
            )
            spans[identity] = span
            model_spans[(event.turn_id, round_index)] = span
            return
        if event.event_type == "model.continuation_recovery_planned" and event.turn_id:
            recovery_id = self._continuation_recovery_id(payload)
            from_round = self._model_recovery_round(payload, "from_round")
            next_round = self._model_recovery_round(payload, "next_round")
            prior = model_spans.get((event.turn_id, from_round))
            if prior is not None and prior.status == "UNSET":
                prior.end = event.created_at
                prior.status = "ERROR"
                prior.events.append(
                    self._span_event(
                        event,
                        {
                            "ecorex.model.continuation_blocked": True,
                            "ecorex.recovery.trigger_code": self._safe_token(
                                payload.get("trigger_code")
                            ),
                        },
                    )
                )
            span = _Span(
                identity=f"model-recovery:{event.turn_id}:{recovery_id}",
                name="ecorex.model_continuation_recovery",
                parent_span_id=(turn_span.span_id if turn_span else root.span_id),
                start=event.created_at,
                end=event.created_at,
                attributes={
                    "ecorex.turn.id": event.turn_id,
                    "ecorex.recovery.id": recovery_id,
                    "ecorex.recovery.action": self._safe_token(payload.get("action")),
                    "ecorex.recovery.trigger_code": self._safe_token(
                        payload.get("trigger_code")
                    ),
                    "ecorex.recovery.tool_output_sha256": self._safe_sha256(
                        payload.get("tool_output_sha256")
                    ),
                    "ecorex.recovery.from_round": from_round,
                    "ecorex.recovery.next_round": next_round,
                },
            )
            spans[span.identity] = span
            model_recovery_spans[recovery_id] = span
            return
        if event.event_type == "model.continuation_recovery_resolved" and event.turn_id:
            recovery_id = self._continuation_recovery_id(payload)
            span = model_recovery_spans.get(recovery_id)
            if span is None:
                raise ReplayIntegrityError(
                    "Model continuation recovery references an unknown recovery"
                )
            if span.status not in {"UNSET", "OK"}:
                raise ReplayIntegrityError(
                    "Model continuation recovery resolution is invalid"
                )
            span.end = event.created_at
            span.status = "OK"
            span.attributes["ecorex.recovery.resolved_by"] = self._safe_token(
                payload.get("resolved_by")
            )
            span.events.append(
                self._span_event(event, {"ecorex.recovery.resolved": True})
            )
            return
        if event.event_type == "model.response_completed" and event.turn_id:
            round_index = self._round_index(payload)
            span = model_spans.get((event.turn_id, round_index))
            if span:
                span.end = event.created_at
                span.status = "OK"
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        if isinstance(value, (int, float)) and not isinstance(
                            value, bool
                        ):
                            span.attributes[f"gen_ai.usage.{key}"] = value
            return
        if event.event_type == "tool.call_requested" and event.turn_id:
            call_id = event.tool_call_id or event.item_id or event.event_id
            try:
                activity = PublicToolActivity.model_validate(payload.get("activity"))
            except ValueError:
                raise ReplayIntegrityError(
                    "Tool trace public activity is invalid"
                ) from None
            if activity.tool_call_id != call_id:
                raise ReplayIntegrityError("Tool trace public identity is inconsistent")
            identity = f"tool:{event.turn_id}:{call_id}"
            span = _Span(
                identity=identity,
                name="ecorex.tool",
                parent_span_id=(turn_span.span_id if turn_span else root.span_id),
                start=event.created_at,
                end=event.created_at,
                attributes={
                    "ecorex.turn.id": event.turn_id,
                    "ecorex.tool.call_id": call_id,
                    "gen_ai.tool.name": activity.tool_id,
                },
            )
            spans[identity] = span
            tool_spans[(event.turn_id, call_id)] = span
            if event.item_id:
                tool_items[event.item_id] = (event.turn_id, call_id)
            return
        if event.event_type == "tool.result" and event.item_id:
            try:
                activity = PublicToolActivity.model_validate(payload.get("activity"))
            except ValueError:
                raise ReplayIntegrityError(
                    "Tool trace result public activity is invalid"
                ) from None
            if event.tool_call_id != activity.tool_call_id:
                raise ReplayIntegrityError(
                    "Tool trace result public identity is inconsistent"
                )
            key = tool_items.get(event.item_id)
            if key and key in tool_spans:
                span = tool_spans[key]
                span.end = event.created_at
                span.status = "OK" if activity.phase == "completed" else "ERROR"
            return
        if event.event_type == "tool.recovery_planned" and event.turn_id:
            code = self._safe_token(payload.get("code"))
            action = self._safe_token(payload.get("action"))
            source = self._safe_token(payload.get("source"))
            attempt = payload.get("automatic_attempt")
            candidates = payload.get("candidate_tool_ids")
            span = _Span(
                identity=f"tool-recovery:{event.event_id}",
                name="ecorex.tool_recovery",
                parent_span_id=(turn_span.span_id if turn_span else root.span_id),
                start=event.created_at,
                end=event.created_at,
                attributes={
                    "ecorex.turn.id": event.turn_id,
                    "ecorex.recovery.event_id": event.event_id,
                    "ecorex.recovery.code": code,
                    "ecorex.recovery.action": action,
                    "ecorex.recovery.source": source,
                    "ecorex.recovery.automatic_attempt": (
                        attempt
                        if isinstance(attempt, int) and not isinstance(attempt, bool)
                        else 0
                    ),
                    "ecorex.recovery.candidate_count": (
                        len(candidates) if isinstance(candidates, list) else 0
                    ),
                },
            )
            spans[span.identity] = span
            recovery_spans[event.event_id] = span
            return
        if event.event_type == "tool.recovery_resolved":
            recovery_event_id = payload.get("recovery_event_id")
            if not isinstance(recovery_event_id, str):
                raise ReplayIntegrityError(
                    "Tool recovery trace has no recovery identity"
                )
            span = recovery_spans.get(recovery_event_id)
            if span is None:
                raise ReplayIntegrityError(
                    "Tool recovery trace references an unknown recovery"
                )
            if span.status not in {"UNSET", "OK"}:
                raise ReplayIntegrityError("Tool recovery trace resolution is invalid")
            span.end = event.created_at
            span.status = "OK"
            span.attributes["ecorex.recovery.resolved_by_tool"] = self._safe_token(
                payload.get("resolved_by_tool_id")
            )
            span.events.append(
                self._span_event(
                    event,
                    {"ecorex.recovery.resolved": True},
                )
            )
            return
        if event.event_type.startswith("connector."):
            self._enrich_connector_span(event, tool_spans)
            return
        if event.event_type == "interaction.requested" and event.item_id:
            identity = f"human:{event.item_id}"
            span = _Span(
                identity=identity,
                name="ecorex.human_interaction",
                parent_span_id=(turn_span.span_id if turn_span else root.span_id),
                start=event.created_at,
                end=event.created_at,
                attributes={
                    "ecorex.turn.id": event.turn_id or "",
                    "ecorex.interaction.id": event.item_id,
                    "ecorex.interaction.kind": str(payload.get("kind") or "unknown"),
                },
            )
            spans[identity] = span
            human_spans[event.item_id] = span
            return
        if (
            event.event_type
            in {
                "interaction.resolved",
                "interaction.cancelled",
                "interaction.expired",
            }
            and event.item_id
        ):
            span = human_spans.get(event.item_id)
            if span:
                span.end = event.created_at
                span.status = (
                    "OK" if event.event_type == "interaction.resolved" else "UNSET"
                )
            return
        if event.event_type.startswith("artifact."):
            artifact_id = str(
                payload.get("artifact_id") or event.item_id or event.event_id
            )
            identity = f"artifact:{event.event_id}"
            spans[identity] = _Span(
                identity=identity,
                name="ecorex.artifact",
                parent_span_id=(turn_span.span_id if turn_span else root.span_id),
                start=event.created_at,
                end=event.created_at,
                status="OK",
                attributes={
                    "ecorex.turn.id": event.turn_id or "",
                    "ecorex.artifact.id": artifact_id,
                    "ecorex.artifact.event_type": event.event_type,
                    "ecorex.artifact.revision_id": str(
                        payload.get("revision_id") or ""
                    ),
                },
            )

    @staticmethod
    def _enrich_connector_span(
        event: EventEnvelope,
        tool_spans: dict[tuple[str, str], _Span],
    ) -> None:
        """Attach safe Connector identity to one pre-existing tool span only."""

        if not event.turn_id or not event.tool_call_id:
            return
        span = tool_spans.get((event.turn_id, event.tool_call_id))
        if span is None:
            # Connector facts never create spans.  The model-facing
            # tool.call_requested fact is the sole span authority.
            return
        payload = event.payload
        identities = {
            "ecorex.connector.id": payload.get("connector_id"),
            "ecorex.connector.action_id": payload.get("action_id"),
            "ecorex.connector.instance_id": payload.get("instance_id"),
            "ecorex.connector.invocation_id": (
                payload.get("invocation_id") or payload.get("connector_invocation_id")
            ),
        }
        discovery_id = payload.get("discovery_id")
        if (
            isinstance(discovery_id, str)
            and _TRACE_DISCOVERY.fullmatch(discovery_id) is not None
        ):
            identities["ecorex.connector.discovery_id"] = discovery_id
        for key, value in identities.items():
            if (
                not isinstance(value, str)
                or (
                    key == "ecorex.connector.discovery_id"
                    and _TRACE_DISCOVERY.fullmatch(value) is None
                )
                or (
                    key != "ecorex.connector.discovery_id"
                    and _TRACE_SAFE_IDENTITY.fullmatch(value) is None
                )
            ):
                continue
            existing = span.attributes.get(key)
            if existing is not None and existing != value:
                raise ReplayIntegrityError(
                    "Connector trace identity changed for one tool call"
                )
            span.attributes[key] = value

        for payload_key, attribute_key in (
            ("delivery", "ecorex.connector.delivery"),
            ("status", "ecorex.connector.status"),
        ):
            value = payload.get(payload_key)
            if (
                isinstance(value, str)
                and _TRACE_SAFE_TOKEN.fullmatch(value) is not None
            ):
                span.attributes[attribute_key] = value
        outcome = payload.get("outcome")
        if not isinstance(outcome, str):
            outcome = event.event_type.rsplit(".", 1)[-1]
        if _TRACE_SAFE_TOKEN.fullmatch(outcome) is not None:
            span.attributes["ecorex.connector.outcome"] = outcome

    @staticmethod
    def _span_event(event: EventEnvelope, attributes: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": event.event_type,
            "time_unix_nano": _unix_nano(event.created_at),
            "attributes": {"ecorex.event.seq": event.seq, **attributes},
        }

    @staticmethod
    def _round_index(payload: dict[str, Any]) -> int:
        value = payload.get("round", 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReplayIntegrityError("model attempt round is invalid")
        return value

    @staticmethod
    def _model_recovery_round(payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReplayIntegrityError("model continuation recovery round is invalid")
        return value

    @staticmethod
    def _continuation_recovery_id(payload: dict[str, Any]) -> str:
        source = payload.get("source_response_id")
        tool_call = payload.get("tool_call_id")
        action = payload.get("action")
        trigger = payload.get("trigger_code")
        output_sha256 = payload.get("tool_output_sha256")
        if (
            not isinstance(source, str)
            or not 1 <= len(source) <= 256
            or not isinstance(tool_call, str)
            or not 1 <= len(tool_call) <= 256
            or not isinstance(action, str)
            or _TRACE_SAFE_TOKEN.fullmatch(action) is None
            or not isinstance(trigger, str)
            or _TRACE_SAFE_TOKEN.fullmatch(trigger) is None
            or not isinstance(output_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None
        ):
            raise ReplayIntegrityError(
                "model continuation recovery identity is invalid"
            )
        # The trace needs a stable join key but must not publish provider or
        # tool-call identities as attributes.
        return hashlib.sha256(
            "\0".join((source, tool_call, action, trigger, output_sha256)).encode(
                "utf-8"
            )
        ).hexdigest()[:32]

    @staticmethod
    def _safe_token(value: Any) -> str:
        return (
            value
            if isinstance(value, str) and _TRACE_SAFE_TOKEN.fullmatch(value) is not None
            else "unknown"
        )

    @staticmethod
    def _safe_sha256(value: Any) -> str:
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            else "unknown"
        )

    @classmethod
    def _otlp_span(cls, span: TraceSpanProjection) -> dict[str, Any]:
        status_code = {"UNSET": 0, "OK": 1, "ERROR": 2}[span.status]
        return {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            **({"parentSpanId": span.parent_span_id} if span.parent_span_id else {}),
            "name": span.name,
            "kind": {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3}[span.kind],
            "startTimeUnixNano": span.start_time_unix_nano,
            "endTimeUnixNano": span.end_time_unix_nano,
            "attributes": [
                cls._otlp_attribute(key, value)
                for key, value in sorted(span.attributes.items())
            ],
            "events": [
                {
                    "name": str(event.get("name") or "event"),
                    "timeUnixNano": str(event.get("time_unix_nano") or "0"),
                    "attributes": [
                        cls._otlp_attribute(key, value)
                        for key, value in sorted(
                            dict(event.get("attributes") or {}).items()
                        )
                    ],
                }
                for event in span.events
            ],
            "status": {"code": status_code},
        }

    @staticmethod
    def _otlp_attribute(key: str, value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            encoded = {"boolValue": value}
        elif isinstance(value, int):
            encoded = {"intValue": str(value)}
        elif isinstance(value, float):
            encoded = {"doubleValue": value}
        else:
            encoded = {"stringValue": str(value)}
        return {"key": key, "value": encoded}

"""Durable OTLP/HTTP JSON trace export for the managed product Runtime.

The Runtime event store remains the source of truth.  A terminal Turn marks one
trace segment in the same SQLite transaction as the terminal event.  The
dispatcher later projects that immutable segment, encrypts bounded OTLP/JSON
batches at rest, and delivers them with a lease.  A crash can therefore cause
an idempotent re-send, but cannot lose a committed terminal segment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import inspect
import json
import re
import sqlite3
import threading
from typing import Any, Awaitable, Final, Mapping, Protocol
from urllib.parse import urlsplit
import uuid

import httpx

from ecorex.protocol import EventEnvelope, TERMINAL_TURN_STATUSES
from ecorex.runtime.database import SQLiteDatabase, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema
from ecorex.session import ManagedSessionService
from ecorex.replay import ReplayIntegrityError

from .audit import AuditPayloadCipher, AuditRedactor
from .trace import TraceProjector


OTLP_TRACES_PATH: Final = "/v1/traces"
_TERMINAL_TURN_VALUES: Final = frozenset(status.value for status in TERMINAL_TURN_STATUSES)
_PAYLOAD_FORMAT: Final = AuditPayloadCipher.FORMAT
_MAX_RESPONSE_BYTES: Final = 64 * 1024
_TRACE_ID: Final = re.compile(r"^[0-9A-Fa-f]{32}$")
_SPAN_ID: Final = re.compile(r"^[0-9A-Fa-f]{16}$")
_UNIX_NANO: Final = re.compile(r"^(?:0|[1-9][0-9]{0,29})$")


class TraceExportError(RuntimeError):
    """Base error with a bounded code and retry contract."""

    retryable = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)[:128]
        self.retry_after_seconds = retry_after_seconds


class RetryableTraceExportError(TraceExportError):
    retryable = True


class PermanentTraceExportError(TraceExportError):
    retryable = False


class TraceOutboxIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TraceExportBatch:
    batch_id: str
    account_id: str
    thread_id: str
    segment_kind: str
    segment_id: str
    through_seq: int
    event_digest: str
    chunk_index: int
    chunk_count: int
    span_count: int
    payload: Mapping[str, Any]
    payload_sha256: str
    attempts: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TraceMaterializeResult:
    attempted_segments: int
    materialized_segments: int
    created_batches: int
    rejected_segments: int


@dataclass(frozen=True, slots=True)
class TraceDrainResult:
    attempted: int
    published: int
    retry_scheduled: int
    rejected: int
    pending: int


@dataclass(frozen=True, slots=True)
class _QuarantinedBatch:
    batch_id: str


class TraceBatchPublisher(Protocol):
    def publish(self, batch: TraceExportBatch) -> Awaitable[None] | None:
        ...


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validated_endpoint(endpoint: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(endpoint)
    hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts if host)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("OTLP trace endpoint origin is invalid") from error
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != OTLP_TRACES_PATH
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError(
            "OTLP trace endpoint must be an allowlisted HTTPS /v1/traces endpoint"
        )
    return f"https://{hostname}{OTLP_TRACES_PATH}"


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        seconds = round((target.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return max(1, min(seconds, 300))


def _validate_otlp_payload(batch: TraceExportBatch) -> None:
    resources = batch.payload.get("resourceSpans")
    if not isinstance(resources, list) or not resources:
        raise PermanentTraceExportError(
            "invalid_payload", "OTLP trace payload has no resource spans"
        )
    spans: list[Mapping[str, Any]] = []
    for resource in resources:
        scopes = resource.get("scopeSpans") if isinstance(resource, Mapping) else None
        if not isinstance(scopes, list) or not scopes:
            raise PermanentTraceExportError(
                "invalid_payload", "OTLP trace payload has no scope spans"
            )
        for scope in scopes:
            raw_spans = scope.get("spans") if isinstance(scope, Mapping) else None
            if not isinstance(raw_spans, list) or not raw_spans:
                raise PermanentTraceExportError(
                    "invalid_payload", "OTLP trace payload has no spans"
                )
            if any(not isinstance(span, Mapping) for span in raw_spans):
                raise PermanentTraceExportError(
                    "invalid_payload", "OTLP trace span is invalid"
                )
            spans.extend(raw_spans)
    if len(spans) != batch.span_count:
        raise PermanentTraceExportError(
            "invalid_payload", "OTLP trace span count does not match its batch"
        )
    for span in spans:
        trace_id = span.get("traceId")
        span_id = span.get("spanId")
        parent_id = span.get("parentSpanId")
        start = span.get("startTimeUnixNano")
        end = span.get("endTimeUnixNano")
        kind = span.get("kind")
        if (
            not isinstance(trace_id, str)
            or _TRACE_ID.fullmatch(trace_id) is None
            or not isinstance(span_id, str)
            or _SPAN_ID.fullmatch(span_id) is None
            or (
                parent_id is not None
                and (
                    not isinstance(parent_id, str)
                    or _SPAN_ID.fullmatch(parent_id) is None
                )
            )
            or not isinstance(start, str)
            or _UNIX_NANO.fullmatch(start) is None
            or not isinstance(end, str)
            or _UNIX_NANO.fullmatch(end) is None
            or int(end) < int(start)
            or isinstance(kind, bool)
            or not isinstance(kind, int)
            or not 0 <= kind <= 5
        ):
            raise PermanentTraceExportError(
                "invalid_payload", "OTLP trace identifiers or timing are invalid"
            )
    redacted = AuditRedactor(max_string_bytes=4096).redact(dict(batch.payload))
    if redacted != dict(batch.payload):
        raise PermanentTraceExportError(
            "unsafe_payload", "OTLP trace payload contains disallowed sensitive data"
        )


class ManagedOTLPHTTPTraceExporter:
    """Standards-compliant OTLP/HTTP JSON exporter with managed auth fencing."""

    def __init__(
        self,
        *,
        endpoint: str,
        allowed_hosts: frozenset[str],
        session: ManagedSessionService,
        client: httpx.Client | None = None,
        max_request_bytes: int = 1024 * 1024,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("at least one OTLP trace host must be allowlisted")
        if session is None:
            raise ValueError("managed session authority is required")
        if not 16 * 1024 <= max_request_bytes <= 8 * 1024 * 1024:
            raise ValueError("OTLP trace request size limit is invalid")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("OTLP trace response size limit is invalid")
        self.endpoint = _validated_endpoint(endpoint, allowed_hosts)
        self.session = session
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            verify=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def aclose(self) -> None:
        self.close()

    def publish(self, batch: TraceExportBatch) -> None:
        if not isinstance(batch, TraceExportBatch):
            raise PermanentTraceExportError(
                "invalid_batch", "OTLP trace batch is invalid"
            )
        _validate_otlp_payload(batch)
        try:
            before = self.session.snapshot()
            if before.account_id != batch.account_id:
                raise PermanentTraceExportError(
                    "account_mismatch", "trace batch account does not match the session"
                )
            token = self.session.bearer_token()
            after = self.session.snapshot()
            if (
                before.account_id != after.account_id
                or before.lease_digest != after.lease_digest
                or before.generation != after.generation
            ):
                raise RetryableTraceExportError(
                    "session_changed", "managed session changed during trace export"
                )
        except TraceExportError:
            raise
        except Exception:
            raise RetryableTraceExportError(
                "session_unavailable", "managed session is unavailable for trace export"
            ) from None

        body = _canonical_json_bytes(batch.payload)
        if hashlib.sha256(body).hexdigest() != batch.payload_sha256:
            raise PermanentTraceExportError(
                "payload_integrity", "trace batch payload integrity check failed"
            )
        if len(body) > self.max_request_bytes:
            raise PermanentTraceExportError(
                "request_too_large", "trace batch exceeds the request size limit"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Idempotency-Key": batch.batch_id,
        }
        try:
            request = self.client.build_request(
                "POST", self.endpoint, headers=headers, content=body
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
            try:
                response_body = self._consume_response(response)
                self._validate_export_response(response_body)
            finally:
                response.close()
        except TraceExportError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise RetryableTraceExportError(
                "transport_unavailable", "OTLP trace collector is unavailable"
            ) from None
        finally:
            token = ""

    def _consume_response(self, response: httpx.Response) -> bytes:
        if response.is_redirect or response.history:
            raise PermanentTraceExportError(
                "redirect_refused", "OTLP trace redirects are forbidden"
            )
        if response.status_code != 200:
            if response.status_code in {401, 408, 425, 429, 502, 503, 504}:
                raise RetryableTraceExportError(
                    "authentication_stale"
                    if response.status_code == 401
                    else "remote_retryable",
                    "OTLP trace export should be retried",
                    retry_after_seconds=_retry_after_seconds(response),
                )
            raise PermanentTraceExportError(
                "remote_rejected", "OTLP trace export was permanently rejected"
            )
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise PermanentTraceExportError(
                "compressed_response", "compressed OTLP trace responses are forbidden"
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type.casefold() != "application/json":
            raise PermanentTraceExportError(
                "invalid_response_type", "OTLP JSON response content type is invalid"
            )
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                raise PermanentTraceExportError(
                    "invalid_response", "OTLP response metadata is invalid"
                ) from None
            if declared_size < 0 or declared_size > self.max_response_bytes:
                raise PermanentTraceExportError(
                    "response_too_large", "OTLP response exceeds its size limit"
                )
        chunks: list[bytes] = []
        received = 0
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > self.max_response_bytes:
                raise PermanentTraceExportError(
                    "response_too_large", "OTLP response exceeds its size limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _validate_export_response(body: bytes) -> None:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PermanentTraceExportError(
                "invalid_response", "OTLP JSON response is invalid"
            ) from None
        if not isinstance(value, dict):
            raise PermanentTraceExportError(
                "invalid_response", "OTLP JSON response must be an object"
            )
        partial = value.get("partialSuccess")
        if partial is None:
            return
        if not isinstance(partial, dict):
            raise PermanentTraceExportError(
                "invalid_response", "OTLP partial success response is invalid"
            )
        rejected = partial.get("rejectedSpans", "0")
        try:
            rejected_count = int(rejected)
        except (TypeError, ValueError):
            raise PermanentTraceExportError(
                "invalid_response", "OTLP rejected span count is invalid"
            ) from None
        if rejected_count < 0:
            raise PermanentTraceExportError(
                "invalid_response", "OTLP rejected span count is invalid"
            )
        if rejected_count:
            # Retrying the same request would duplicate the spans that the
            # collector already accepted. Preserve a terminal diagnostic
            # instead of silently entering an infinite partial-success loop.
            raise PermanentTraceExportError(
                "partial_success", "OTLP collector rejected part of the trace batch"
            )


class TraceOutbox:
    """Encrypted terminal-segment outbox backed by the Runtime SQLite WAL."""

    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        account_id: str,
        cipher: AuditPayloadCipher,
        projector: TraceProjector,
        publisher: TraceBatchPublisher | None = None,
        max_spans_per_batch: int = 64,
        max_request_bytes: int = 1024 * 1024,
        lease_seconds: int = 30,
        retention_days: int = 7,
        redactor: AuditRedactor | None = None,
        initialize: bool = True,
    ) -> None:
        if not account_id:
            raise ValueError("trace account_id is required")
        if not 1 <= max_spans_per_batch <= 512:
            raise ValueError("trace span batch limit is invalid")
        if not 16 * 1024 <= max_request_bytes <= 8 * 1024 * 1024:
            raise ValueError("trace request size limit is invalid")
        if not 1 <= lease_seconds <= 300:
            raise ValueError("trace lease is invalid")
        if not 1 <= retention_days <= 30:
            raise ValueError("trace retention is invalid")
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.account_id = account_id
        self.cipher = cipher
        self.projector = projector
        self.publisher = publisher
        self.max_spans_per_batch = max_spans_per_batch
        self.max_request_bytes = max_request_bytes
        self.lease_seconds = lease_seconds
        self.retention_days = retention_days
        self.redactor = redactor or AuditRedactor(max_string_bytes=4096)
        self._startup_lock = threading.Lock()
        self._startup_converged = False
        if initialize:
            self.initialize()
        else:
            self.validate()

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def validate(self) -> None:
        """Validate the encrypted product schema without creating facts."""

        with self.database.reader() as connection:
            validate_product_schema(connection)
            legacy = connection.execute(
                "SELECT 1 FROM observability_trace_outbox "
                "WHERE payload_format != ? LIMIT 1",
                (_PAYLOAD_FORMAT,),
            ).fetchone()
            if legacy is not None:
                raise TraceOutboxIntegrityError(
                    "trace payload storage requires a signed encryption migration"
                )

    def initialize(self) -> None:
        """Initialize the durable cursor during healthy startup convergence."""

        with self._startup_lock:
            if self._startup_converged:
                return
            self.validate()
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO observability_trace_cursors(account_id) VALUES (?) "
                    "ON CONFLICT(account_id) DO NOTHING",
                    (self.account_id,),
                )
            self._startup_converged = True

    def converge_startup(self) -> None:
        """Idempotently enable writes after healthy startup convergence."""

        self.initialize()

    def _require_converged(self) -> None:
        if not self._startup_converged:
            raise TraceOutboxIntegrityError(
                "trace outbox startup has not converged"
            )

    def record_in_transaction(
        self, connection: sqlite3.Connection, event: EventEnvelope
    ) -> None:
        self._require_converged()
        if not connection.in_transaction:
            raise RuntimeError("trace segment recording requires an active transaction")
        segment = self._terminal_segment(event)
        if segment is not None:
            kind, segment_id = segment
            timestamp = event.created_at.astimezone(UTC).isoformat()
            connection.execute(
                "INSERT INTO observability_trace_segments("
                "account_id, thread_id, segment_kind, segment_id, target_seq, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, thread_id, segment_kind, segment_id) "
                "DO UPDATE SET target_seq = MAX(target_seq, excluded.target_seq), "
                "rejected_at = NULL, last_error_code = NULL",
                (
                    self.account_id,
                    event.thread_id,
                    kind,
                    segment_id,
                    event.seq,
                    timestamp,
                ),
            )
        row = connection.execute(
            "SELECT rowid AS source_rowid FROM events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE observability_trace_cursors "
                "SET last_event_rowid = MAX(last_event_rowid, ?) WHERE account_id = ?",
                (int(row["source_rowid"]), self.account_id),
            )

    @staticmethod
    def _terminal_segment(event: EventEnvelope) -> tuple[str, str] | None:
        if event.event_type == "turn.status_changed" and event.turn_id:
            target = str(event.payload.get("to") or "")
            if target in _TERMINAL_TURN_VALUES:
                return "turn", event.turn_id
        if event.event_type == "thread.archived":
            return "thread", event.thread_id
        return None

    def backfill_events(self, *, batch_size: int = 1000) -> int:
        self._require_converged()
        if not 1 <= batch_size <= 10_000:
            raise ValueError("trace backfill batch size is invalid")
        from ecorex.runtime.event_store import EventStore

        total = 0
        while True:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    "SELECT last_event_rowid FROM observability_trace_cursors "
                    "WHERE account_id = ?",
                    (self.account_id,),
                ).fetchone()
                rows = connection.execute(
                    "SELECT rowid AS source_rowid, * FROM events WHERE rowid > ? "
                    "ORDER BY rowid LIMIT ?",
                    (int(cursor["last_event_rowid"]), batch_size),
                ).fetchall()
                for row in rows:
                    self.record_in_transaction(connection, EventStore._from_row(row))
            total += len(rows)
            if len(rows) < batch_size:
                return total

    def materialize(self, *, limit_segments: int = 16) -> TraceMaterializeResult:
        self._require_converged()
        if not 1 <= limit_segments <= 256:
            raise ValueError("trace materialization limit is invalid")
        now = datetime.now(UTC).isoformat()
        with self.database.reader() as connection:
            segments = connection.execute(
                "SELECT * FROM observability_trace_segments "
                "WHERE rejected_at IS NULL ORDER BY created_at, thread_id, segment_id LIMIT ?",
                (limit_segments,),
            ).fetchall()
        materialized = created = rejected = 0
        for segment in segments:
            try:
                batches = self._project_segment(segment)
                with self.database.transaction() as connection:
                    for batch in batches:
                        if self._persist_batch(connection, batch):
                            created += 1
                    connection.execute(
                        "DELETE FROM observability_trace_segments WHERE account_id = ? "
                        "AND thread_id = ? AND segment_kind = ? AND segment_id = ? "
                        "AND target_seq = ?",
                        (
                            self.account_id,
                            segment["thread_id"],
                            segment["segment_kind"],
                            segment["segment_id"],
                            segment["target_seq"],
                        ),
                    )
                materialized += 1
            except (ReplayIntegrityError, TraceOutboxIntegrityError, ValueError) as error:
                rejected += 1
                code = getattr(error, "code", type(error).__name__)[:128]
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE observability_trace_segments SET rejected_at = ?, "
                        "last_error_code = ? WHERE account_id = ? AND thread_id = ? "
                        "AND segment_kind = ? AND segment_id = ? AND target_seq = ?",
                        (
                            now,
                            code,
                            self.account_id,
                            segment["thread_id"],
                            segment["segment_kind"],
                            segment["segment_id"],
                            segment["target_seq"],
                        ),
                    )
        return TraceMaterializeResult(
            attempted_segments=len(segments),
            materialized_segments=materialized,
            created_batches=created,
            rejected_segments=rejected,
        )

    def _project_segment(self, segment: sqlite3.Row) -> tuple[TraceExportBatch, ...]:
        target_seq = int(segment["target_seq"])
        projection = self.projector.project(
            str(segment["thread_id"]), through_seq=target_seq
        )
        if projection.through_seq != target_seq:
            raise TraceOutboxIntegrityError("trace projection watermark mismatch")
        resource_spans = projection.otlp.get("resourceSpans")
        if not isinstance(resource_spans, list) or len(resource_spans) != 1:
            raise TraceOutboxIntegrityError("trace OTLP resource shape is invalid")
        resource = resource_spans[0]
        scopes = resource.get("scopeSpans") if isinstance(resource, dict) else None
        if not isinstance(scopes, list) or len(scopes) != 1:
            raise TraceOutboxIntegrityError("trace OTLP scope shape is invalid")
        otlp_spans = scopes[0].get("spans") if isinstance(scopes[0], dict) else None
        if not isinstance(otlp_spans, list):
            raise TraceOutboxIntegrityError("trace OTLP spans are invalid")
        kind = str(segment["segment_kind"])
        segment_id = str(segment["segment_id"])
        if kind == "turn":
            selected_ids = {
                span.span_id
                for span in projection.spans
                if span.attributes.get("ecorex.turn.id") == segment_id
            }
        elif kind == "thread":
            selected_ids = {
                span.span_id
                for span in projection.spans
                if span.name == "ecorex.thread" and span.parent_span_id is None
            }
        else:
            raise TraceOutboxIntegrityError("trace segment kind is invalid")
        selected = [
            span
            for span in otlp_spans
            if isinstance(span, dict) and span.get("spanId") in selected_ids
        ]
        if not selected:
            raise TraceOutboxIntegrityError("trace segment contains no spans")
        base_resource = {
            key: value for key, value in resource.items() if key != "scopeSpans"
        }
        base_scope = {
            key: value for key, value in scopes[0].items() if key != "spans"
        }
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for raw_span in selected:
            redacted = self.redactor.redact(raw_span)
            if not isinstance(redacted, dict):
                raise TraceOutboxIntegrityError("trace span redaction failed")
            candidate = [*current, redacted]
            payload = self._payload(base_resource, base_scope, candidate)
            if (
                current
                and (
                    len(candidate) > self.max_spans_per_batch
                    or len(_canonical_json_bytes(payload)) > self.max_request_bytes
                )
            ):
                groups.append(current)
                current = [redacted]
                payload = self._payload(base_resource, base_scope, current)
            if len(_canonical_json_bytes(payload)) > self.max_request_bytes:
                raise TraceOutboxIntegrityError("one trace span exceeds the request bound")
            current.append(redacted) if current != [redacted] else None
        if current:
            groups.append(current)
        chunk_count = len(groups)
        result: list[TraceExportBatch] = []
        for index, spans in enumerate(groups):
            payload = self._payload(base_resource, base_scope, spans)
            body = _canonical_json_bytes(payload)
            digest = hashlib.sha256(body).hexdigest()
            identity = "\x1f".join(
                (
                    self.account_id,
                    projection.thread_id,
                    kind,
                    segment_id,
                    str(target_seq),
                    projection.event_digest,
                    str(index),
                    digest,
                )
            )
            result.append(
                TraceExportBatch(
                    batch_id="tracebatch_" + hashlib.sha256(identity.encode()).hexdigest(),
                    account_id=self.account_id,
                    thread_id=projection.thread_id,
                    segment_kind=kind,
                    segment_id=segment_id,
                    through_seq=target_seq,
                    event_digest=projection.event_digest,
                    chunk_index=index,
                    chunk_count=chunk_count,
                    span_count=len(spans),
                    payload=payload,
                    payload_sha256=digest,
                    attempts=0,
                    created_at=datetime.now(UTC),
                )
            )
        return tuple(result)

    @staticmethod
    def _payload(
        resource: Mapping[str, Any],
        scope: Mapping[str, Any],
        spans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    **dict(resource),
                    "scopeSpans": [{**dict(scope), "spans": spans}],
                }
            ]
        }

    @staticmethod
    def _associated_data(values: Mapping[str, Any]) -> str:
        return "\x1f".join(
            str(values[key])
            for key in ("batch_id", "account_id", "thread_id", "segment_id")
        )

    def _persist_batch(
        self, connection: sqlite3.Connection, batch: TraceExportBatch
    ) -> bool:
        self._require_converged()
        existing = connection.execute(
            "SELECT * FROM observability_trace_outbox WHERE batch_id = ?",
            (batch.batch_id,),
        ).fetchone()
        plaintext = _canonical_json_bytes(batch.payload).decode("utf-8")
        if existing is not None:
            if (
                existing["payload_sha256"] != batch.payload_sha256
                or self._plaintext(existing) != plaintext
            ):
                raise TraceOutboxIntegrityError(
                    "trace batch identity was reused with different content"
                )
            return False
        values = {
            "batch_id": batch.batch_id,
            "account_id": batch.account_id,
            "thread_id": batch.thread_id,
            "segment_id": batch.segment_id,
        }
        encrypted = self.cipher.encrypt(
            plaintext, associated_data=self._associated_data(values)
        )
        connection.execute(
            "INSERT INTO observability_trace_outbox("
            "batch_id, account_id, thread_id, segment_kind, segment_id, through_seq, "
            "event_digest, chunk_index, chunk_count, span_count, payload_json, "
            "payload_format, payload_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch.batch_id,
                batch.account_id,
                batch.thread_id,
                batch.segment_kind,
                batch.segment_id,
                batch.through_seq,
                batch.event_digest,
                batch.chunk_index,
                batch.chunk_count,
                batch.span_count,
                encrypted,
                _PAYLOAD_FORMAT,
                batch.payload_sha256,
                batch.created_at.isoformat(),
            ),
        )
        return True

    def _plaintext(self, row: sqlite3.Row) -> str:
        if row["payload_format"] != _PAYLOAD_FORMAT:
            raise TraceOutboxIntegrityError("trace payload format is invalid")
        try:
            return self.cipher.decrypt(
                str(row["payload_json"]),
                associated_data=self._associated_data(row),
            )
        except Exception:
            raise TraceOutboxIntegrityError(
                "trace payload authentication failed"
            ) from None

    def _from_row(self, row: sqlite3.Row) -> TraceExportBatch:
        plaintext = self._plaintext(row)
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise TraceOutboxIntegrityError("trace payload digest is invalid")
        payload = json_loads(plaintext, None)
        if not isinstance(payload, dict):
            raise TraceOutboxIntegrityError("trace payload is invalid")
        if _canonical_json_bytes(payload).decode("utf-8") != plaintext:
            raise TraceOutboxIntegrityError("trace payload is not canonical JSON")
        return TraceExportBatch(
            batch_id=str(row["batch_id"]),
            account_id=str(row["account_id"]),
            thread_id=str(row["thread_id"]),
            segment_kind=str(row["segment_kind"]),
            segment_id=str(row["segment_id"]),
            through_seq=int(row["through_seq"]),
            event_digest=str(row["event_digest"]),
            chunk_index=int(row["chunk_index"]),
            chunk_count=int(row["chunk_count"]),
            span_count=int(row["span_count"]),
            payload=payload,
            payload_sha256=str(row["payload_sha256"]),
            attempts=int(row["attempts"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def count(self, *, pending_only: bool = False) -> int:
        where = (
            " WHERE published_at IS NULL AND rejected_at IS NULL"
            if pending_only
            else ""
        )
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM observability_trace_outbox" + where
            ).fetchone()
        return int(row["count"])

    def get(self, batch_id: str) -> TraceExportBatch:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM observability_trace_outbox "
                "WHERE batch_id = ? AND account_id = ?",
                (batch_id, self.account_id),
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return self._from_row(row)

    def list(
        self,
        *,
        thread_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TraceExportBatch, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("trace list limit must be between 1 and 1000")
        clauses = ["account_id = ?"]
        parameters: list[Any] = [self.account_id]
        if thread_id is not None:
            clauses.append("thread_id = ?")
            parameters.append(thread_id)
        parameters.append(limit)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM observability_trace_outbox WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, batch_id LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    async def drain(self, *, limit: int = 32) -> TraceDrainResult:
        self._require_converged()
        if self.publisher is None:
            raise RuntimeError("trace publisher is not configured")
        if not 1 <= limit <= 256:
            raise ValueError("trace drain limit is invalid")
        attempted = published = retried = rejected = 0
        for _ in range(limit):
            claimed = await asyncio.to_thread(self._claim_next)
            if claimed is None:
                break
            if isinstance(claimed, _QuarantinedBatch):
                attempted += 1
                rejected += 1
                continue
            batch, lease_token = claimed
            attempted += 1
            try:
                operation = self.publisher.publish
                if inspect.iscoroutinefunction(operation):
                    result = operation(batch)
                else:
                    result = await asyncio.to_thread(operation, batch)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                code = str(getattr(error, "code", type(error).__name__))[:128]
                if getattr(error, "retryable", True) is False:
                    rejected += 1
                    await asyncio.to_thread(
                        self._mark_rejected, batch.batch_id, lease_token, code
                    )
                else:
                    retried += 1
                    await asyncio.to_thread(
                        self._mark_retry,
                        batch.batch_id,
                        lease_token,
                        code,
                        getattr(error, "retry_after_seconds", None),
                    )
            else:
                published += 1
                await asyncio.to_thread(
                    self._mark_published, batch.batch_id, lease_token
                )
        return TraceDrainResult(
            attempted=attempted,
            published=published,
            retry_scheduled=retried,
            rejected=rejected,
            pending=self.count(pending_only=True),
        )

    def _claim_next(
        self,
    ) -> tuple[TraceExportBatch, str] | _QuarantinedBatch | None:
        self._require_converged()
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM observability_trace_outbox "
                "WHERE published_at IS NULL AND rejected_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at, batch_id LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE observability_trace_outbox SET lease_token = ?, "
                "lease_expires_at = ?, attempts = attempts + 1, next_attempt_at = NULL "
                "WHERE batch_id = ? AND published_at IS NULL AND rejected_at IS NULL",
                (
                    token,
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    row["batch_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM observability_trace_outbox WHERE batch_id = ?",
                (row["batch_id"],),
            ).fetchone()
            try:
                batch = self._from_row(updated)
            except TraceOutboxIntegrityError:
                connection.execute(
                    "UPDATE observability_trace_outbox SET rejected_at = ?, "
                    "lease_token = NULL, lease_expires_at = NULL, "
                    "next_attempt_at = NULL, last_error_code = ? "
                    "WHERE batch_id = ? AND lease_token = ?",
                    (
                        datetime.now(UTC).isoformat(),
                        "payload_integrity",
                        row["batch_id"],
                        token,
                    ),
                )
                return _QuarantinedBatch(str(row["batch_id"]))
            return batch, token

    def _mark_published(self, batch_id: str, lease_token: str) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE observability_trace_outbox SET published_at = ?, "
                "lease_token = NULL, lease_expires_at = NULL, next_attempt_at = NULL, "
                "last_error_code = NULL WHERE batch_id = ? AND lease_token = ? "
                "AND published_at IS NULL AND rejected_at IS NULL",
                (datetime.now(UTC).isoformat(), batch_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise TraceOutboxIntegrityError("trace publish lease was lost")

    def _mark_retry(
        self,
        batch_id: str,
        lease_token: str,
        error_code: str,
        retry_after_seconds: int | None,
    ) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM observability_trace_outbox WHERE batch_id = ? "
                "AND lease_token = ? AND published_at IS NULL AND rejected_at IS NULL",
                (batch_id, lease_token),
            ).fetchone()
            if row is None:
                raise TraceOutboxIntegrityError("trace retry lease was lost")
            attempts = int(row["attempts"])
            base = min(300, 2 ** min(attempts, 8))
            jitter_seed = hashlib.sha256(f"{batch_id}:{attempts}".encode()).digest()[0]
            jittered = max(1, min(300, round(base * (0.8 + jitter_seed / 637.5))))
            delay = (
                max(1, min(int(retry_after_seconds), 300))
                if isinstance(retry_after_seconds, int)
                else jittered
            )
            connection.execute(
                "UPDATE observability_trace_outbox SET lease_token = NULL, "
                "lease_expires_at = NULL, next_attempt_at = ?, last_error_code = ? "
                "WHERE batch_id = ? AND lease_token = ? AND rejected_at IS NULL",
                (
                    (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                    error_code,
                    batch_id,
                    lease_token,
                ),
            )

    def _mark_rejected(
        self, batch_id: str, lease_token: str, error_code: str
    ) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE observability_trace_outbox SET rejected_at = ?, "
                "lease_token = NULL, lease_expires_at = NULL, next_attempt_at = NULL, "
                "last_error_code = ? WHERE batch_id = ? AND lease_token = ? "
                "AND published_at IS NULL AND rejected_at IS NULL",
                (
                    datetime.now(UTC).isoformat(),
                    error_code,
                    batch_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise TraceOutboxIntegrityError("trace reject lease was lost")

    def enforce_retention(self, *, now: datetime | None = None) -> int:
        self._require_converged()
        cutoff = (
            (now or datetime.now(UTC)).astimezone(UTC)
            - timedelta(days=self.retention_days)
        ).isoformat()
        with self.database.transaction() as connection:
            batches = connection.execute(
                "DELETE FROM observability_trace_outbox WHERE "
                "(published_at IS NOT NULL AND published_at < ?) OR "
                "(rejected_at IS NOT NULL AND rejected_at < ?)",
                (cutoff, cutoff),
            ).rowcount
            segments = connection.execute(
                "DELETE FROM observability_trace_segments WHERE "
                "rejected_at IS NOT NULL AND rejected_at < ?",
                (cutoff,),
            ).rowcount
        return max(0, batches) + max(0, segments)


class TraceDispatcher:
    def __init__(
        self,
        outbox: TraceOutbox,
        *,
        poll_seconds: float = 5.0,
        segment_batch_size: int = 16,
        publish_batch_size: int = 32,
    ) -> None:
        if not 0.1 <= poll_seconds <= 300:
            raise ValueError("trace dispatch interval is invalid")
        self.outbox = outbox
        self.poll_seconds = poll_seconds
        self.segment_batch_size = segment_batch_size
        self.publish_batch_size = publish_batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error_code: str | None = None
        self.last_error_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None:
            return
        self.outbox._require_converged()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ecorex-trace-dispatcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        self.outbox._require_converged()
        last_retention_day: str | None = None
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(
                    self.outbox.materialize, limit_segments=self.segment_batch_size
                )
                if self.outbox.publisher is not None:
                    await self.outbox.drain(limit=self.publish_batch_size)
                today = datetime.now(UTC).date().isoformat()
                if today != last_retention_day:
                    await asyncio.to_thread(self.outbox.enforce_retention)
                    last_retention_day = today
                self.last_error_code = None
                self.last_error_at = None
            except Exception as error:
                self.last_error_code = type(error).__name__[:128]
                self.last_error_at = datetime.now(UTC)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


__all__ = [
    "ManagedOTLPHTTPTraceExporter",
    "OTLP_TRACES_PATH",
    "PermanentTraceExportError",
    "RetryableTraceExportError",
    "TraceDispatcher",
    "TraceDrainResult",
    "TraceExportBatch",
    "TraceExportError",
    "TraceMaterializeResult",
    "TraceOutbox",
    "TraceOutboxIntegrityError",
]

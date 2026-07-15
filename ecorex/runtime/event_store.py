"""Append-only event facts with a monotonic sequence per thread."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import re
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from ecorex.protocol import EventEnvelope

from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import ConflictError, IdempotencyConflictError
from .ids import new_id
from .public_tools import validate_public_tool_event_payload


_EVENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_JOB_STATUS_BY_EVENT = {
    "job.queued": "queued",
    "job.leased": "leased",
    "job.started": "running",
    "job.waiting_human": "waiting_human",
    "job.retry_scheduled": "retry_scheduled",
    "job.completed": "completed",
    "job.failed": "failed",
    "job.deadline_exceeded": "failed",
    "job.cancelled": "cancelled",
    "job.dead_lettered": "dead_letter",
    "job.reclaimed": "queued",
    "job.resumed": "queued",
}


def _public_job_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project an internal scheduler fact into the public Event protocol.

    Event pages and SSE are user-visible transport. Worker identity, lease
    fencing, raw failures/reasons, checkpoints, tool arguments, and paths stay
    in the durable jobs table or controlled audit storage.
    """

    public: dict[str, Any] = {}
    for field in ("attempt", "available_at"):
        if field in payload:
            public[field] = payload[field]
    status = _JOB_STATUS_BY_EVENT.get(event_type)
    if status is not None:
        public["status"] = status
    if event_type == "job.queued":
        for field in ("kind", "priority", "max_attempts", "deadline"):
            if field in payload:
                public[field] = payload[field]
    elif event_type == "job.retry_scheduled" and "max_attempts" in payload:
        public["max_attempts"] = payload["max_attempts"]
    if event_type == "job.heartbeat":
        digest = payload.get("checkpoint_sha256")
        if isinstance(digest, str) and _SHA256.fullmatch(digest):
            public["checkpoint_sha256"] = digest
        elif payload.get("checkpoint") is not None:
            public["checkpoint_sha256"] = hashlib.sha256(
                json_dumps(payload["checkpoint"]).encode("utf-8")
            ).hexdigest()

    reason_code = {
        "job.retry_scheduled": "retry_scheduled",
        "job.dead_lettered": "attempts_exhausted",
        "job.failed": "execution_failed",
        "job.deadline_exceeded": "deadline_exceeded",
        "job.cancelled": "cancelled",
        "job.reclaimed": "lease_recovered",
    }.get(event_type)
    if reason_code is not None:
        public["reason_code"] = reason_code
    diagnostics = {
        field: payload[field]
        for field in ("error", "reason")
        if payload.get(field) is not None
    }
    if diagnostics:
        public["diagnostic_sha256"] = hashlib.sha256(
            json_dumps(diagnostics).encode("utf-8")
        ).hexdigest()
    return public


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_storage(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _from_storage(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EventPage:
    events: list[EventEnvelope]
    after_seq: int
    watermark: int
    has_more: bool


class TransactionalEventSink(Protocol):
    def record_in_transaction(
        self, connection: sqlite3.Connection, event: EventEnvelope
    ) -> None:
        ...


class _EventNotificationHub:
    """Thread-safe, process-local wakeups for committed Event facts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}
        self._waiters: dict[
            str,
            dict[object, tuple[asyncio.AbstractEventLoop, asyncio.Future[int]]],
        ] = {}

    def generation(self, thread_id: str) -> int:
        with self._lock:
            return self._generations.get(thread_id, 0)

    @staticmethod
    def _resolve(future: asyncio.Future[int], generation: int) -> None:
        if not future.done():
            future.set_result(generation)

    def publish(self, thread_id: str) -> None:
        """Wake every local waiter; this callback must never fail a commit."""

        with self._lock:
            generation = self._generations.get(thread_id, 0) + 1
            self._generations[thread_id] = generation
            waiters = tuple(self._waiters.pop(thread_id, {}).values())
        for loop, future in waiters:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._resolve, future, generation)

    async def wait(
        self,
        thread_id: str,
        observed_generation: int,
        *,
        timeout: float,
    ) -> int:
        if observed_generation < 0:
            raise ValueError("event notification generation cannot be negative")
        if timeout < 0:
            raise ValueError("event notification timeout cannot be negative")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        waiter_id = object()
        with self._lock:
            current = self._generations.get(thread_id, 0)
            if current != observed_generation:
                return current
            self._waiters.setdefault(thread_id, {})[waiter_id] = (loop, future)
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except TimeoutError:
                # A publisher may have advanced the generation while the
                # timeout callback was being delivered. Return that fact so
                # the caller loops immediately instead of sleeping again.
                return self.generation(thread_id)
        finally:
            with self._lock:
                thread_waiters = self._waiters.get(thread_id)
                if thread_waiters is not None:
                    thread_waiters.pop(waiter_id, None)
                    if not thread_waiters:
                        self._waiters.pop(thread_id, None)
            if not future.done():
                future.cancel()


_NOTIFICATION_HUBS_LOCK = threading.Lock()
_NOTIFICATION_HUBS: weakref.WeakValueDictionary[str, _EventNotificationHub] = (
    weakref.WeakValueDictionary()
)


def _notification_hub(database: SQLiteDatabase) -> _EventNotificationHub:
    key = str(database.path)
    with _NOTIFICATION_HUBS_LOCK:
        hub = _NOTIFICATION_HUBS.get(key)
        if hub is None:
            hub = _EventNotificationHub()
            _NOTIFICATION_HUBS[key] = hub
        return hub


class EventStore:
    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        default_permission_snapshot_id: str | None = None,
        event_sink: TransactionalEventSink | None = None,
    ):
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.default_permission_snapshot_id = default_permission_snapshot_id
        self.event_sink = event_sink
        self._notifications = _notification_hub(self.database)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EventEnvelope:
        payload = validate_public_tool_event_payload(
            str(row["event_type"]),
            json_loads(row["payload_json"], {}),
            tool_call_id=row["tool_call_id"],
        )
        return EventEnvelope(
            schema_version=row["schema_version"],
            event_id=row["event_id"],
            seq=row["seq"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            item_id=row["item_id"],
            job_id=row["job_id"],
            tool_call_id=row["tool_call_id"],
            client_message_id=row["client_message_id"],
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
            trace_id=row["trace_id"],
            config_snapshot_id=row["config_snapshot_id"],
            capability_snapshot_id=row["capability_snapshot_id"],
            permission_snapshot_id=row["permission_snapshot_id"],
            extension_snapshot_id=row["extension_snapshot_id"],
            event_type=row["event_type"],
            created_at=_from_storage(row["created_at"]),
            payload=payload,
        )

    def append(
        self,
        *,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        job_id: str | None = None,
        tool_call_id: str | None = None,
        client_message_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        config_snapshot_id: str | None = None,
        capability_snapshot_id: str | None = None,
        permission_snapshot_id: str | None = None,
        extension_snapshot_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: datetime | None = None,
    ) -> EventEnvelope:
        with self.database.transaction() as connection:
            return self.append_in_transaction(
                connection,
                thread_id=thread_id,
                event_type=event_type,
                payload=payload,
                turn_id=turn_id,
                item_id=item_id,
                job_id=job_id,
                tool_call_id=tool_call_id,
                client_message_id=client_message_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
                config_snapshot_id=config_snapshot_id,
                capability_snapshot_id=capability_snapshot_id,
                permission_snapshot_id=permission_snapshot_id,
                extension_snapshot_id=extension_snapshot_id,
                idempotency_key=idempotency_key,
                created_at=created_at,
            )

    def append_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        job_id: str | None = None,
        tool_call_id: str | None = None,
        client_message_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        config_snapshot_id: str | None = None,
        capability_snapshot_id: str | None = None,
        permission_snapshot_id: str | None = None,
        extension_snapshot_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: datetime | None = None,
    ) -> EventEnvelope:
        if not connection.in_transaction:
            raise RuntimeError("append_in_transaction requires an active transaction")
        if not thread_id:
            raise ValueError("thread_id is required")
        if not _EVENT_TYPE.fullmatch(event_type):
            raise ValueError("event_type must be a safe protocol token")
        payload = payload or {}
        if event_type.startswith("job."):
            payload = _public_job_payload(event_type, payload)
        payload = validate_public_tool_event_payload(
            event_type,
            payload,
            tool_call_id=tool_call_id,
        )
        payload_json = json_dumps(payload)
        requested_created_at = (
            None if created_at is None else _to_storage(created_at)
        )
        if turn_id is not None and event_type != "turn.accepted":
            accepted = connection.execute(
                "SELECT config_snapshot_id, capability_snapshot_id, "
                "permission_snapshot_id, extension_snapshot_id, trace_id, correlation_id FROM events "
                "WHERE thread_id = ? AND turn_id = ? "
                "AND event_type = 'turn.accepted' ORDER BY seq LIMIT 1",
                (thread_id, turn_id),
            ).fetchone()
            if accepted is not None:
                requested_context = {
                    "config_snapshot_id": config_snapshot_id,
                    "capability_snapshot_id": capability_snapshot_id,
                    "permission_snapshot_id": permission_snapshot_id,
                    "extension_snapshot_id": extension_snapshot_id,
                    "trace_id": trace_id,
                }
                drifted = [
                    field
                    for field, requested in requested_context.items()
                    if requested is not None and requested != accepted[field]
                ]
                if drifted:
                    raise ConflictError(
                        "turn snapshot context is immutable: "
                        + ", ".join(sorted(drifted))
                    )
                config_snapshot_id = (
                    config_snapshot_id or accepted["config_snapshot_id"]
                )
                capability_snapshot_id = (
                    capability_snapshot_id or accepted["capability_snapshot_id"]
                )
                permission_snapshot_id = (
                    permission_snapshot_id or accepted["permission_snapshot_id"]
                )
                extension_snapshot_id = (
                    extension_snapshot_id or accepted["extension_snapshot_id"]
                )
                trace_id = trace_id or accepted["trace_id"]
                correlation_id = correlation_id or accepted["correlation_id"]
        permission_snapshot_id = (
            permission_snapshot_id or self.default_permission_snapshot_id
        )

        if idempotency_key is not None:
            duplicate = connection.execute(
                "SELECT * FROM events WHERE thread_id = ? AND idempotency_key = ?",
                (thread_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                requested_fields = {
                    "event_type": event_type,
                    "payload_json": payload_json,
                    "turn_id": turn_id,
                    "item_id": item_id,
                    "job_id": job_id,
                    "tool_call_id": tool_call_id,
                    "client_message_id": client_message_id,
                    "causation_id": causation_id,
                    "correlation_id": correlation_id,
                    "trace_id": trace_id,
                    "config_snapshot_id": config_snapshot_id,
                    "capability_snapshot_id": capability_snapshot_id,
                    "permission_snapshot_id": permission_snapshot_id,
                    "extension_snapshot_id": extension_snapshot_id,
                }
                if any(
                    duplicate[column] != value
                    for column, value in requested_fields.items()
                ) or (
                    requested_created_at is not None
                    and duplicate["created_at"] != requested_created_at
                ):
                    raise IdempotencyConflictError(
                        f"event idempotency key {idempotency_key!r} was reused"
                    )
                return self._from_row(duplicate)

        connection.execute(
            "INSERT INTO thread_heads(thread_id, last_seq) VALUES (?, 0) "
            "ON CONFLICT(thread_id) DO NOTHING",
            (thread_id,),
        )
        current = connection.execute(
            "SELECT last_seq FROM thread_heads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        sequence = int(current["last_seq"]) + 1

        envelope = EventEnvelope(
            event_id=new_id("evt"),
            seq=sequence,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            job_id=job_id,
            tool_call_id=tool_call_id,
            client_message_id=client_message_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            config_snapshot_id=config_snapshot_id,
            capability_snapshot_id=capability_snapshot_id,
            permission_snapshot_id=permission_snapshot_id,
            extension_snapshot_id=extension_snapshot_id,
            event_type=event_type,
            created_at=created_at or _utc_now(),
            payload=payload,
        )
        connection.execute(
            "UPDATE thread_heads SET last_seq = ? WHERE thread_id = ?",
            (sequence, thread_id),
        )
        connection.execute(
            """
            INSERT INTO events(
                event_id, schema_version, thread_id, seq, turn_id, item_id,
                job_id, tool_call_id, client_message_id, causation_id,
                correlation_id, trace_id, config_snapshot_id,
                capability_snapshot_id, permission_snapshot_id, extension_snapshot_id, event_type,
                created_at, payload_json, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.event_id,
                envelope.schema_version,
                envelope.thread_id,
                envelope.seq,
                envelope.turn_id,
                envelope.item_id,
                envelope.job_id,
                envelope.tool_call_id,
                envelope.client_message_id,
                envelope.causation_id,
                envelope.correlation_id,
                envelope.trace_id,
                envelope.config_snapshot_id,
                envelope.capability_snapshot_id,
                envelope.permission_snapshot_id,
                envelope.extension_snapshot_id,
                envelope.event_type,
                _to_storage(envelope.created_at),
                payload_json,
                idempotency_key,
            ),
        )
        # The task catalog is ordered by the last committed fact. The initial
        # thread.created event intentionally precedes insertion of its thread
        # projection, so this is a no-op for that one event.
        connection.execute(
            "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
            (_to_storage(envelope.created_at), thread_id),
        )
        if self.event_sink is not None:
            self.event_sink.record_in_transaction(connection, envelope)
        register = getattr(connection, "add_after_commit", None)
        if not callable(register):
            raise RuntimeError(
                "event notification requires the Runtime transaction boundary"
            )
        register(lambda: self._notifications.publish(thread_id))
        return envelope

    def notification_generation(self, thread_id: str) -> int:
        if not thread_id:
            raise ValueError("thread_id is required")
        return self._notifications.generation(thread_id)

    async def wait_for_notification(
        self,
        thread_id: str,
        observed_generation: int,
        *,
        timeout: float,
    ) -> int:
        if not thread_id:
            raise ValueError("thread_id is required")
        return await self._notifications.wait(
            thread_id,
            observed_generation,
            timeout=timeout,
        )

    def watermark(self, thread_id: str, connection: sqlite3.Connection | None = None) -> int:
        if connection is not None:
            row = connection.execute(
                "SELECT last_seq FROM thread_heads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return 0 if row is None else int(row["last_seq"])
        with self.database.reader() as reader:
            return self.watermark(thread_id, reader)

    def page(self, thread_id: str, *, after_seq: int = 0, limit: int = 200) -> EventPage:
        if after_seq < 0:
            raise ValueError("after_seq cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE thread_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (thread_id, after_seq, limit + 1),
            ).fetchall()
            watermark = self.watermark(thread_id, connection)
        has_more = len(rows) > limit
        return EventPage(
            events=[self._from_row(row) for row in rows[:limit]],
            after_seq=after_seq,
            watermark=watermark,
            has_more=has_more,
        )

    def get(self, event_id: str) -> EventEnvelope | None:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

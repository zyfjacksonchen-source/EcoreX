"""Persistent lease-based scheduler used by turns and background work."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from ecorex.protocol import (
    DurableJob,
    EventEnvelope,
    InteractionStatus,
    ItemStatus,
    JOB_TRANSITIONS,
    JobStatus,
    TERMINAL_TURN_STATUSES,
    TurnStatus,
)

from .commit_guard import transaction_commit_guard
from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import (
    IdempotencyConflictError,
    InvalidTransitionError,
    LeaseError,
    NotFoundError,
)
from .event_store import EventStore
from .ids import new_id
from .invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeExecutionPermit,
)
from .reasoning import archive_visible_reasoning_in_transaction


PRESERVE_ATTEMPT_CHECKPOINT_KEY = "_ecorex_preserve_job_attempt"
_COW_TURN_EXECUTION: ContextVar[bool] = ContextVar(
    "ecorex_cow_turn_execution",
    default=False,
)


def bind_cow_turn_execution():
    return _COW_TURN_EXECUTION.set(True)


def reset_cow_turn_execution(token: Any) -> None:
    _COW_TURN_EXECUTION.reset(token)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None, *, default: datetime | None = None) -> datetime:
    value = value if value is not None else default
    if value is None:
        raise ValueError("a datetime value is required")
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc)


def _store_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat(timespec="microseconds")


def _read_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


class DurableJobStore:
    def __init__(
        self,
        database: SQLiteDatabase | str,
        events: EventStore | None = None,
        *,
        execution_gate: RuntimeExecutionGate | None = None,
    ):
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.events = events or EventStore(self.database)
        self._execution_gate = execution_gate
        self._execution_gate_lock = threading.Lock()
        self._execution_permit_lock = threading.Lock()
        self._execution_permits: dict[
            tuple[str, str], RuntimeExecutionPermit
        ] = {}

    @property
    def execution_gate(self) -> RuntimeExecutionGate | None:
        with self._execution_gate_lock:
            return self._execution_gate

    def bind_execution_gate(self, gate: RuntimeExecutionGate) -> None:
        if not isinstance(gate, RuntimeExecutionGate):
            raise TypeError("execution gate is invalid")
        with self._execution_gate_lock:
            if self._execution_gate is not None and self._execution_gate is not gate:
                raise RuntimeError("Durable Job Store already has an execution gate")
            self._execution_gate = gate
        with self._execution_permit_lock:
            # Permits are process/gate-local. Binding cannot adopt a lease that
            # was issued before this exact gate became authoritative.
            self._execution_permits.clear()

    @staticmethod
    def _permit_subject(job_id: str, lease_token: str) -> str:
        token_digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        return f"{job_id}:{token_digest}"

    @classmethod
    def durable_permit_subject(cls, job_id: str, lease_token: str) -> str:
        """Return the non-secret execution identity used by update draining."""

        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease_token is required")
        return cls._permit_subject(job_id, lease_token)

    def _remember_execution_permit(
        self,
        job_id: str,
        lease_token: str,
        permit: RuntimeExecutionPermit,
    ) -> bool:
        expected_subject = self._permit_subject(job_id, lease_token)
        if permit.scope != "durable_job" or permit.subject != expected_subject:
            raise RuntimeExecutionDenied("job execution permit binding is invalid")
        with self._execution_permit_lock:
            # Publication occurs just after the lease commit. A delayed old
            # lease caller must not overwrite a newer generation that already
            # committed and published. Holding the map lock across this read
            # makes every competing publisher observe one total order.
            with self.database.reader() as connection:
                row = connection.execute(
                    "SELECT lease_token FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if row is None or row["lease_token"] != lease_token:
                return False
            for key in tuple(self._execution_permits):
                if key[0] == job_id:
                    self._execution_permits.pop(key, None)
            self._execution_permits[(job_id, lease_token)] = permit
            return True

    def retire_execution_permit(self, job_id: str, lease_token: str) -> None:
        with self._execution_permit_lock:
            self._execution_permits.pop((job_id, lease_token), None)

    def _retire_row_execution_permit(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> None:
        token = row["lease_token"]
        if isinstance(token, str) and token:
            job_id = str(row["job_id"])

            def callback() -> None:
                self.retire_execution_permit(job_id, token)

            register = getattr(connection, "add_after_commit", None)
            if not callable(register):
                raise RuntimeError(
                    "job permit retirement requires the Runtime transaction boundary"
                )
            register(callback)

    def _execution_permit(
        self, job_id: str, lease_token: str
    ) -> RuntimeExecutionPermit:
        with self._execution_permit_lock:
            permit = self._execution_permits.get((job_id, lease_token))
        if permit is None:
            raise LeaseError(f"job {job_id!r} has no current execution permit")
        if (
            permit.scope != "durable_job"
            or permit.subject != self._permit_subject(job_id, lease_token)
        ):
            raise LeaseError(f"job {job_id!r} execution permit is invalid")
        return permit

    def _assert_live_execution_binding(
        self, job_id: str, lease_token: str, *, now: datetime | None = None
    ) -> None:
        observed = _as_utc(now, default=_utc_now())
        with self.database.reader() as connection:
            self._assert_live_execution_binding_in_transaction(
                connection,
                job_id,
                lease_token,
                now=observed,
            )

    @staticmethod
    def _assert_live_execution_binding_in_transaction(
        connection: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        *,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT status,lease_token,lease_expires_at FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise LeaseError(f"job {job_id!r} does not exist")
        if not lease_token or row["lease_token"] != lease_token:
            raise LeaseError(f"job {job_id!r} lease fencing token is stale")
        expires_at = _read_time(row["lease_expires_at"])
        if expires_at is None or expires_at <= now:
            raise LeaseError(f"job {job_id!r} lease has expired")
        if JobStatus(row["status"]) not in {JobStatus.LEASED, JobStatus.RUNNING}:
            raise LeaseError(f"job {job_id!r} is not executable")

    def capture_execution_permit(
        self, job_id: str, lease_token: str
    ) -> RuntimeExecutionPermit | None:
        """Capture a permit at an async provider checkpoint without holding a lock."""

        gate = self.execution_gate
        if gate is None:
            return None
        permit = self._execution_permit(job_id, lease_token)
        try:
            gate.assert_permit(permit)
            self._assert_live_execution_binding(job_id, lease_token)
        except RuntimeExecutionDenied as error:
            raise LeaseError(
                f"job {job_id!r} execution epoch is closed"
            ) from error
        return permit

    def assert_execution_permit(
        self,
        job_id: str,
        lease_token: str,
        permit: RuntimeExecutionPermit | None,
    ) -> None:
        """Recheck one captured permit after await and before result handling."""

        gate = self.execution_gate
        if gate is None:
            if permit is not None:
                raise LeaseError(f"job {job_id!r} execution permit is invalid")
            return
        current = self._execution_permit(job_id, lease_token)
        if permit is None or current != permit:
            raise LeaseError(f"job {job_id!r} execution permit is stale")
        try:
            gate.assert_permit(permit)
            self._assert_live_execution_binding(job_id, lease_token)
        except RuntimeExecutionDenied as error:
            raise LeaseError(
                f"job {job_id!r} execution epoch is closed"
            ) from error

    @contextmanager
    def execution_admission(
        self,
        job_id: str,
        lease_token: str,
        *,
        verify_live_lease: bool = True,
    ) -> Iterator[RuntimeExecutionPermit | None]:
        """Fence one *synchronous* external side effect by lease epoch.

        Never keep this threading context across an ``await``. Async callers
        use ``capture_execution_permit`` immediately before provider dispatch
        and ``assert_execution_permit`` after await/before result handling.
        """

        gate = self.execution_gate
        if gate is None:
            if verify_live_lease:
                self._assert_live_execution_binding(job_id, lease_token)
            yield None
            return
        permit = self._execution_permit(job_id, lease_token)
        try:
            gate.assert_permit(permit)
            if verify_live_lease:
                self._assert_live_execution_binding(job_id, lease_token)
            yield permit
            gate.assert_permit(permit)
        except RuntimeExecutionDenied as error:
            raise LeaseError(
                f"job {job_id!r} execution epoch is closed"
            ) from error

    @contextmanager
    def execution_transaction(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """BEGIN/commit one business mutation under the exact lease permit."""

        observed = _as_utc(now, default=_utc_now())
        gate = self.execution_gate
        if gate is None:
            with self.database.transaction() as connection:
                self._assert_live_execution_binding_in_transaction(
                    connection,
                    job_id,
                    lease_token,
                    now=observed,
                )
                yield connection
            return
        permit = self._execution_permit(job_id, lease_token)
        if _COW_TURN_EXECUTION.get() and not gate.snapshot().draining:
            with self.database.transaction() as connection:
                self._assert_live_execution_binding_in_transaction(
                    connection,
                    job_id,
                    lease_token,
                    now=observed,
                )
                yield connection
            return
        try:
            gate.assert_permit(permit)
            connection = self.database.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_live_execution_binding_in_transaction(
                    connection,
                    job_id,
                    lease_token,
                    now=observed,
                )
                yield connection
                gate.assert_permit(permit)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        except RuntimeExecutionDenied as error:
            raise LeaseError(
                f"job {job_id!r} execution epoch is closed"
            ) from error

    @contextmanager
    def control_admission(
        self, *, scope: str, subject: str | None
    ) -> Iterator[RuntimeExecutionPermit | None]:
        """Fence an unleased user/control mutation in the current epoch."""

        stable_subject = subject if isinstance(subject, str) and subject else scope
        gate = self.execution_gate
        if gate is None:
            yield None
            return
        try:
            permit = gate.issue_permit(scope=scope, subject=stable_subject)
            yield permit
            gate.assert_permit(permit)
        except RuntimeExecutionDenied as error:
            raise LeaseError("runtime execution epoch is closed") from error

    @contextmanager
    def control_transaction(
        self, *, scope: str, subject: str | None
    ) -> Iterator[sqlite3.Connection]:
        stable_subject = subject if isinstance(subject, str) and subject else scope
        gate = self.execution_gate
        if gate is None or _COW_TURN_EXECUTION.get():
            with self.database.transaction() as connection:
                yield connection
            return
        try:
            permit = gate.issue_permit(scope=scope, subject=stable_subject)
            connection = self.database.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                gate.assert_permit(permit)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        except RuntimeExecutionDenied as error:
            raise LeaseError("runtime execution epoch is closed") from error

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DurableJob:
        return DurableJob(
            job_id=row["job_id"],
            kind=row["kind"],
            payload=json_loads(row["payload_json"], {}),
            status=JobStatus(row["status"]),
            priority=row["priority"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_read_time(row["lease_expires_at"]),
            heartbeat_at=_read_time(row["heartbeat_at"]),
            available_at=_read_time(row["available_at"]),
            deadline=_read_time(row["deadline"]),
            checkpoint=json_loads(row["checkpoint_json"]),
            idempotency_key=row["idempotency_key"],
            last_error=row["last_error"],
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
        )

    def _append_job_event(
        self,
        connection: sqlite3.Connection,
        *,
        row_or_values: sqlite3.Row | dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        created_at: datetime | None = None,
        occurrence: str | None = None,
    ) -> EventEnvelope | None:
        thread_id = row_or_values["thread_id"]
        if thread_id is None:
            return None
        job_id = row_or_values["job_id"]
        turn_id = row_or_values["turn_id"]
        if isinstance(row_or_values, dict):
            context = row_or_values.get("_event_context") or {}
        else:
            context_row = connection.execute(
                "SELECT config_snapshot_id, capability_snapshot_id, "
                "permission_snapshot_id, model_catalog_snapshot_id, extension_snapshot_id "
                "FROM job_runtime_contexts WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            context = dict(context_row) if context_row is not None else {}
        if not isinstance(context, dict):
            raise RuntimeError("durable job runtime context is invalid")
        occurrence_key = (
            hashlib.sha256(occurrence.encode("utf-8")).hexdigest()[:24]
            if occurrence is not None
            else str(payload.get("attempt", ""))
        )
        return self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            job_id=job_id,
            event_type=event_type,
            payload=payload,
            config_snapshot_id=context.get("config_snapshot_id"),
            capability_snapshot_id=context.get("capability_snapshot_id"),
            permission_snapshot_id=context.get("permission_snapshot_id"),
            extension_snapshot_id=context.get("extension_snapshot_id"),
            idempotency_key=f"{job_id}:{event_type}:{occurrence_key}",
            created_at=created_at,
        )

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        available_at: datetime | None = None,
        deadline: datetime | None = None,
        event_context: dict[str, str] | None = None,
    ) -> DurableJob:
        with self.control_transaction(
            scope="job_enqueue",
            subject=idempotency_key,
        ) as connection:
            return self.enqueue_in_transaction(
                connection,
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                thread_id=thread_id,
                turn_id=turn_id,
                priority=priority,
                max_attempts=max_attempts,
                available_at=available_at,
                deadline=deadline,
                event_context=event_context,
            )

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        available_at: datetime | None = None,
        deadline: datetime | None = None,
        job_id: str | None = None,
        now: datetime | None = None,
        event_context: dict[str, str] | None = None,
    ) -> DurableJob:
        if not kind:
            raise ValueError("kind is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if not -100 <= priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        event_context = dict(event_context or {})
        allowed_context = {
            "config_snapshot_id",
            "capability_snapshot_id",
            "permission_snapshot_id",
            "model_catalog_snapshot_id",
            "extension_snapshot_id",
        }
        if set(event_context) - allowed_context or any(
            not isinstance(value, str) or not value
            for value in event_context.values()
        ):
            raise ValueError("job event context is invalid")
        now = _as_utc(now, default=_utc_now())
        requested_available_at = available_at
        available_at = _as_utc(available_at, default=now)
        deadline = None if deadline is None else _as_utc(deadline)
        request = {
            "kind": kind,
            "payload": payload,
            "event_context": event_context,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "priority": priority,
            "max_attempts": max_attempts,
            "available_at": _store_time(requested_available_at),
            "deadline": _store_time(deadline),
        }
        request_fingerprint = _fingerprint(request)
        duplicate = connection.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if duplicate is not None:
            if duplicate["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflictError(
                    f"job idempotency key {idempotency_key!r} was reused"
                )
            return self._from_row(duplicate)

        values = {
            "job_id": job_id or new_id("job"),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "_event_context": event_context,
        }
        self._append_job_event(
            connection,
            row_or_values=values,
            event_type="job.queued",
            payload={
                "kind": kind,
                "priority": priority,
                "attempt": 0,
                "max_attempts": max_attempts,
                "available_at": _store_time(available_at),
                "deadline": _store_time(deadline),
            },
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, kind, payload_json, status, priority, attempt,
                max_attempts, thread_id, turn_id, lease_owner,
                lease_token, lease_expires_at, heartbeat_at, available_at, deadline,
                checkpoint_json, idempotency_key, request_fingerprint,
                last_error, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL, NULL,
                ?, ?, NULL, ?, ?, NULL, ?, ?
            )
            """,
            (
                values["job_id"],
                kind,
                json_dumps(payload),
                JobStatus.QUEUED.value,
                priority,
                max_attempts,
                thread_id,
                turn_id,
                _store_time(available_at),
                _store_time(deadline),
                idempotency_key,
                request_fingerprint,
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (values["job_id"],)
        ).fetchone()
        if event_context:
            missing = allowed_context - set(event_context)
            if missing:
                raise ValueError(
                    "job event context is incomplete: " + ", ".join(sorted(missing))
                )
            connection.execute(
                "INSERT INTO job_runtime_contexts("
                "job_id, config_snapshot_id, capability_snapshot_id, "
                "permission_snapshot_id, model_catalog_snapshot_id, extension_snapshot_id"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    values["job_id"],
                    event_context["config_snapshot_id"],
                    event_context["capability_snapshot_id"],
                    event_context["permission_snapshot_id"],
                    event_context["model_catalog_snapshot_id"],
                    event_context["extension_snapshot_id"],
                ),
            )
        return self._from_row(row)

    def get(self, job_id: str) -> DurableJob:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        return self._from_row(row)

    @staticmethod
    def _assert_transition(current: JobStatus, target: JobStatus) -> None:
        if current == target:
            return
        if target not in JOB_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"job cannot transition from {current.value} to {target.value}"
            )

    def _expire_deadlines_in_transaction(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        *,
        job_id: str | None = None,
    ) -> list[str]:
        parameters: list[Any] = [
            JobStatus.QUEUED.value,
            JobStatus.LEASED.value,
            JobStatus.RUNNING.value,
            JobStatus.WAITING_HUMAN.value,
            JobStatus.RETRY_SCHEDULED.value,
            _store_time(now),
        ]
        job_clause = ""
        if job_id is not None:
            job_clause = " AND job_id = ?"
            parameters.append(job_id)
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?, ?, ?, ?) "
            "AND deadline IS NOT NULL AND deadline <= ?"
            + job_clause
            + " ORDER BY deadline ASC, job_id ASC",
            parameters,
        ).fetchall()
        expired: list[str] = []
        for row in rows:
            deadline_fact = self._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.deadline_exceeded",
                payload={"attempt": row["attempt"], "reason": "deadline_exceeded"},
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                "updated_at = ? WHERE job_id = ?",
                (
                    JobStatus.FAILED.value,
                    "deadline_exceeded",
                    _store_time(now),
                    row["job_id"],
                ),
            )
            self._retire_row_execution_permit(connection, row)
            self._fail_linked_turn_in_transaction(
                connection,
                row,
                now,
                reason="deadline_exceeded",
                causation_id=(
                    None if deadline_fact is None else deadline_fact.event_id
                ),
            )
            expired.append(row["job_id"])
        return expired

    def _fail_linked_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        now: datetime,
        *,
        reason: str,
        causation_id: str | None = None,
    ) -> None:
        if job["turn_id"] is None or job["thread_id"] is None:
            return
        turn = connection.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (job["turn_id"],)
        ).fetchone()
        if turn is None or TurnStatus(turn["status"]) in TERMINAL_TURN_STATUSES:
            return
        interactions = connection.execute(
            "SELECT * FROM interactions WHERE turn_id = ? AND status = ?",
            (job["turn_id"], InteractionStatus.PENDING.value),
        ).fetchall()
        for interaction in interactions:
            self.events.append_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                job_id=job["job_id"],
                item_id=interaction["interaction_id"],
                event_type="interaction.expired",
                payload={"reason": reason},
                causation_id=causation_id,
                idempotency_key=f"{interaction['interaction_id']}:{reason}",
                created_at=now,
            )
            connection.execute(
                "UPDATE interactions SET status = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    InteractionStatus.EXPIRED.value,
                    _store_time(now),
                    interaction["interaction_id"],
                ),
            )
        items = connection.execute(
            "SELECT * FROM items WHERE turn_id = ?", (job["turn_id"],)
        ).fetchall()
        for item in items:
            status = ItemStatus(item["status"])
            if status in {ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED}:
                continue
            self.events.append_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                item_id=item["item_id"],
                event_type="item.status_changed",
                payload={
                    "from": status.value,
                    "to": ItemStatus.FAILED.value,
                    "reason": reason,
                },
                causation_id=causation_id,
                idempotency_key=f"{item['item_id']}:{reason}",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                (ItemStatus.FAILED.value, _store_time(now), item["item_id"]),
            )
        turn_event = self.events.append_in_transaction(
            connection,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            job_id=job["job_id"],
            event_type="turn.status_changed",
            payload={
                "from": turn["status"],
                "to": TurnStatus.FAILED.value,
                "reason": reason,
            },
            causation_id=causation_id,
            idempotency_key=f"{job['turn_id']}:{reason}",
            created_at=now,
        )
        archive_visible_reasoning_in_transaction(
            connection,
            self.events,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            terminal_event_id=turn_event.event_id,
            terminal_status=TurnStatus.FAILED,
            reason=reason,
            now=now,
        )
        connection.execute(
            "UPDATE turns SET status = ?, terminal_reason = ?, updated_at = ? "
            "WHERE turn_id = ?",
            (
                TurnStatus.FAILED.value,
                reason,
                _store_time(now),
                job["turn_id"],
            ),
        )

    def _move_linked_turn_to_retry_wait_in_transaction(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        now: datetime,
        *,
        causation_id: str | None,
    ) -> None:
        """Converge phases that cannot be resumed without their lost owner.

        QUEUED/PREPARING and an already RETRY_WAIT Turn are directly resumable.
        FINALIZING is handled by the committed-response recovery below.  The
        remaining provider/tool phases must explicitly enter RETRY_WAIT before
        a new attempt can execute.
        """

        if (
            job["kind"] != "agent_turn"
            or job["turn_id"] is None
            or job["thread_id"] is None
        ):
            return
        turn = connection.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (job["turn_id"],)
        ).fetchone()
        if turn is None:
            return
        current = TurnStatus(turn["status"])
        retry_phases = {
            TurnStatus.MODEL_REQUESTED,
            TurnStatus.STREAMING,
            TurnStatus.TOOL_PENDING,
            TurnStatus.TOOL_RUNNING,
        }
        if current not in retry_phases:
            return
        self.events.append_in_transaction(
            connection,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            job_id=job["job_id"],
            event_type="turn.status_changed",
            payload={
                "from": current.value,
                "to": TurnStatus.RETRY_WAIT.value,
                "reason": "lease_expired",
            },
            causation_id=causation_id,
            idempotency_key=(
                f"{job['job_id']}:lease-expired:turn-retry:{job['attempt']}"
            ),
            created_at=now,
        )
        connection.execute(
            "UPDATE turns SET status = ?, terminal_reason = NULL, updated_at = ? "
            "WHERE turn_id = ?",
            (TurnStatus.RETRY_WAIT.value, _store_time(now), job["turn_id"]),
        )

    def _recover_finalizing_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        now: datetime,
    ) -> bool:
        """Finish an Agent Turn whose terminal model fact already committed.

        The worker persists ``model.response_completed`` and enters FINALIZING
        before the atomic Turn/Job settlement. A process death in that narrow
        interval must not turn a successful response into a retry or dead
        letter, including when the expired lease was the final attempt.
        """

        if (
            job["kind"] != "agent_turn"
            or job["turn_id"] is None
            or job["thread_id"] is None
        ):
            return False
        turn = connection.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (job["turn_id"],)
        ).fetchone()
        if turn is None or TurnStatus(turn["status"]) is not TurnStatus.FINALIZING:
            return False
        completed = connection.execute(
            "SELECT event_id FROM events WHERE thread_id = ? AND turn_id = ? "
            "AND event_type = 'model.response_completed' "
            "ORDER BY seq DESC LIMIT 1",
            (job["thread_id"], job["turn_id"]),
        ).fetchone()
        if completed is None:
            return False

        job_fact = self._append_job_event(
            connection,
            row_or_values=job,
            event_type="job.completed",
            payload={
                "attempt": job["attempt"],
                "reason": "finalizing_recovered_after_lease_expiry",
            },
            created_at=now,
        )
        causation_id = None if job_fact is None else job_fact.event_id
        connection.execute(
            "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL, heartbeat_at = NULL, last_error = NULL, "
            "updated_at = ? WHERE job_id = ?",
            (JobStatus.COMPLETED.value, _store_time(now), job["job_id"]),
        )
        self._retire_row_execution_permit(connection, job)

        # RuntimeKernel terminal settlement applies the same rule: all work and
        # interaction projections owned by the Turn close in the terminal tx.
        for dependent_job in connection.execute(
            "SELECT * FROM jobs WHERE turn_id = ? AND job_id != ?",
            (job["turn_id"], job["job_id"]),
        ).fetchall():
            status = JobStatus(dependent_job["status"])
            if status in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.DEAD_LETTER,
            }:
                continue
            self._append_job_event(
                connection,
                row_or_values=dependent_job,
                event_type="job.completed",
                payload={
                    "attempt": dependent_job["attempt"],
                    "reason": "turn_finalizing_recovered",
                },
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, last_error = NULL, "
                "updated_at = ? WHERE job_id = ?",
                (
                    JobStatus.COMPLETED.value,
                    _store_time(now),
                    dependent_job["job_id"],
                ),
            )
            self._retire_row_execution_permit(connection, dependent_job)

        for interaction in connection.execute(
            "SELECT * FROM interactions WHERE turn_id = ? AND status = ?",
            (job["turn_id"], InteractionStatus.PENDING.value),
        ).fetchall():
            self.events.append_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                job_id=interaction["job_id"],
                item_id=interaction["interaction_id"],
                event_type="interaction.cancelled",
                payload={"reason": "turn_completed"},
                causation_id=causation_id,
                idempotency_key=(
                    f"{interaction['interaction_id']}:finalizing-recovered"
                ),
                created_at=now,
            )
            connection.execute(
                "UPDATE interactions SET status = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    InteractionStatus.CANCELLED.value,
                    _store_time(now),
                    interaction["interaction_id"],
                ),
            )

        for item in connection.execute(
            "SELECT * FROM items WHERE turn_id = ?", (job["turn_id"],)
        ).fetchall():
            status = ItemStatus(item["status"])
            if status in {
                ItemStatus.COMPLETED,
                ItemStatus.FAILED,
                ItemStatus.CANCELLED,
            }:
                continue
            self.events.append_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                item_id=item["item_id"],
                event_type="item.status_changed",
                payload={
                    "from": status.value,
                    "to": ItemStatus.COMPLETED.value,
                    "reason": "finalizing_recovered_after_lease_expiry",
                },
                causation_id=causation_id,
                idempotency_key=f"{item['item_id']}:finalizing-recovered",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                (ItemStatus.COMPLETED.value, _store_time(now), item["item_id"]),
            )

        turn_event = self.events.append_in_transaction(
            connection,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            job_id=job["job_id"],
            event_type="turn.status_changed",
            payload={
                "from": TurnStatus.FINALIZING.value,
                "to": TurnStatus.COMPLETED.value,
                "reason": "finalizing_recovered_after_lease_expiry",
            },
            causation_id=causation_id,
            idempotency_key=f"{job['turn_id']}:finalizing-recovered",
            created_at=now,
        )
        archive_visible_reasoning_in_transaction(
            connection,
            self.events,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            terminal_event_id=turn_event.event_id,
            terminal_status=TurnStatus.COMPLETED,
            reason="finalizing_recovered_after_lease_expiry",
            now=now,
        )
        connection.execute(
            "UPDATE turns SET status = ?, terminal_reason = ?, updated_at = ? "
            "WHERE turn_id = ?",
            (
                TurnStatus.COMPLETED.value,
                "finalizing_recovered_after_lease_expiry",
                _store_time(now),
                job["turn_id"],
            ),
        )
        return True

    def _reclaim_expired_in_transaction(
        self, connection: sqlite3.Connection, now: datetime
    ) -> list[str]:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ? "
            "ORDER BY lease_expires_at ASC, job_id ASC",
            (JobStatus.LEASED.value, JobStatus.RUNNING.value, _store_time(now)),
        ).fetchall()
        reclaimed: list[str] = []
        for row in rows:
            if self._recover_finalizing_turn_in_transaction(connection, row, now):
                reclaimed.append(row["job_id"])
                continue
            exhausted = row["attempt"] >= row["max_attempts"]
            target = JobStatus.DEAD_LETTER if exhausted else JobStatus.QUEUED
            event_type = "job.dead_lettered" if exhausted else "job.reclaimed"
            reclaim_fact = self._append_job_event(
                connection,
                row_or_values=row,
                event_type=event_type,
                payload={
                    "attempt": row["attempt"],
                    "reason": "lease_expired",
                    "available_at": _store_time(now),
                },
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, available_at = ?, "
                "last_error = ?, updated_at = ? WHERE job_id = ?",
                (
                    target.value,
                    _store_time(now),
                    "lease_expired",
                    _store_time(now),
                    row["job_id"],
                ),
            )
            self._retire_row_execution_permit(connection, row)
            if exhausted:
                self._fail_linked_turn_in_transaction(
                    connection,
                    row,
                    now,
                    reason="lease_attempts_exhausted",
                    causation_id=(
                        None if reclaim_fact is None else reclaim_fact.event_id
                    ),
                )
            else:
                self._move_linked_turn_to_retry_wait_in_transaction(
                    connection,
                    row,
                    now,
                    causation_id=(
                        None if reclaim_fact is None else reclaim_fact.event_id
                    ),
                )
            reclaimed.append(row["job_id"])
        return reclaimed

    def reclaim_expired(self, *, now: datetime | None = None) -> list[str]:
        now = _as_utc(now, default=_utc_now())
        with self.control_transaction(
            scope="job_maintenance", subject="reclaim_expired"
        ) as connection:
            self._expire_deadlines_in_transaction(connection, now)
            return self._reclaim_expired_in_transaction(connection, now)

    def expire_deadline(
        self, job_id: str, *, now: datetime | None = None
    ) -> bool:
        """Persist deadline convergence before an owner operation can fail."""

        now = _as_utc(now, default=_utc_now())
        with self.control_transaction(
            scope="job_maintenance", subject=f"deadline:{job_id}"
        ) as connection:
            return bool(
                self._expire_deadlines_in_transaction(
                    connection, now, job_id=job_id
                )
            )

    def lease_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 30,
        kinds: list[str] | None = None,
        now: datetime | None = None,
    ) -> DurableJob | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = _as_utc(now, default=_utc_now())
        with self._execution_gate_lock:
            gate = self._execution_gate
            if gate is None:
                # Binding and an unguarded legacy lease share this lock. A gate
                # cannot appear bound before an already-admitted lease commits.
                return self._lease_next_in_admission(
                    worker_id,
                    lease_seconds=lease_seconds,
                    kinds=kinds,
                    now=now,
                )
        try:
            scheduler_permit = gate.issue_permit(
                scope="job_lease",
                subject=worker_id,
            )
        except RuntimeExecutionDenied:
            return None
        permit_holder: list[RuntimeExecutionPermit] = []

        def prepare_permit(leased: DurableJob) -> None:
            assert leased.lease_token is not None
            permit_holder.append(
                gate.issue_permit(
                    scope="durable_job",
                    subject=self._permit_subject(
                        leased.job_id,
                        leased.lease_token,
                    ),
                )
            )

        def validate_commit() -> None:
            gate.assert_permit(
                permit_holder[0] if permit_holder else scheduler_permit
            )

        try:
            with transaction_commit_guard(validate_commit):
                leased = self._lease_next_in_admission(
                    worker_id,
                    lease_seconds=lease_seconds,
                    kinds=kinds,
                    now=now,
                    before_commit=prepare_permit,
                )
        except RuntimeExecutionDenied:
            return None
        if leased is None:
            return None
        permit = permit_holder[0]
        published = self._remember_execution_permit(
            leased.job_id,
            leased.lease_token or "",
            permit,
        )
        return leased if published else None

    def _lease_next_in_admission(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        kinds: list[str] | None,
        now: datetime,
        before_commit: Callable[[DurableJob], None] | None = None,
    ) -> DurableJob | None:
        """Select and commit one lease while the caller retains gate admission."""

        with self.database.transaction() as connection:
            self._expire_deadlines_in_transaction(connection, now)
            self._reclaim_expired_in_transaction(connection, now)
            parameters: list[Any] = [
                JobStatus.QUEUED.value,
                JobStatus.RETRY_SCHEDULED.value,
                _store_time(now),
                _store_time(now),
                JobStatus.QUEUED.value,
                JobStatus.LEASED.value,
                JobStatus.RUNNING.value,
                JobStatus.WAITING_HUMAN.value,
                JobStatus.RETRY_SCHEDULED.value,
            ]
            kind_clause = ""
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                kind_clause = f" AND j.kind IN ({placeholders})"
                parameters.extend(kinds)
            row = connection.execute(
                """
                SELECT j.* FROM jobs AS j
                LEFT JOIN scheduler_threads AS scheduled
                  ON scheduled.scheduling_key = COALESCE(
                      j.thread_id, '__kind__:' || j.kind
                  )
                WHERE j.status IN (?, ?)
                  AND j.available_at <= ?
                  AND (j.deadline IS NULL OR j.deadline > ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs AS earlier
                      WHERE j.thread_id IS NOT NULL
                        AND earlier.thread_id = j.thread_id
                        AND earlier.rowid < j.rowid
                        AND earlier.status IN (?, ?, ?, ?, ?)
                  )
                """
                + kind_clause
                + " ORDER BY j.priority DESC, "
                + "CASE WHEN scheduled.last_leased_at IS NULL THEN 0 ELSE 1 END ASC, "
                + "scheduled.last_leased_at ASC, j.created_at ASC, j.rowid ASC LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                return None
            target = JobStatus.LEASED
            self._assert_transition(JobStatus(row["status"]), target)
            checkpoint = json_loads(row["checkpoint_json"], {})
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            preserve_attempt = bool(
                checkpoint.pop(PRESERVE_ATTEMPT_CHECKPOINT_KEY, False) is True
            )
            attempt = int(row["attempt"]) + int(not preserve_attempt)
            lease_token = new_id("fence")
            expires_at = now + timedelta(seconds=lease_seconds)
            self._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.leased",
                payload={"attempt": attempt},
                created_at=now,
                occurrence=lease_token,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, attempt = ?, lease_owner = ?, lease_token = ?, "
                "lease_expires_at = ?, heartbeat_at = ?, checkpoint_json = ?, updated_at = ? "
                "WHERE job_id = ?",
                (
                    target.value,
                    attempt,
                    worker_id,
                    lease_token,
                    _store_time(expires_at),
                    _store_time(now),
                    json_dumps(checkpoint) if checkpoint else None,
                    _store_time(now),
                    row["job_id"],
                ),
            )
            connection.execute(
                "INSERT INTO scheduler_threads(scheduling_key, last_leased_at, last_job_id) "
                "VALUES (?, ?, ?) ON CONFLICT(scheduling_key) DO UPDATE SET "
                "last_leased_at = excluded.last_leased_at, "
                "last_job_id = excluded.last_job_id",
                (
                    row["thread_id"] or f"__kind__:{row['kind']}",
                    _store_time(now),
                    row["job_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            leased = self._from_row(updated)
            if before_commit is not None:
                before_commit(leased)
            return leased

    def _owned_row(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id!r} does not exist")
        if row["lease_owner"] != worker_id:
            raise LeaseError(f"job {job_id!r} is not leased by {worker_id!r}")
        if not lease_token or row["lease_token"] != lease_token:
            raise LeaseError(f"job {job_id!r} lease fencing token is stale")
        expires_at = _read_time(row["lease_expires_at"])
        if expires_at is None or expires_at <= now:
            raise LeaseError(f"job {job_id!r} lease has expired")
        return row

    def start(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> DurableJob:
        now = _as_utc(now, default=_utc_now())
        deadline_expired = False
        updated: sqlite3.Row | None = None
        with self.execution_transaction(
            job_id,
            lease_token,
            now=now,
        ) as connection:
            if self._expire_deadlines_in_transaction(
                connection, now, job_id=job_id
            ):
                deadline_expired = True
            else:
                row = self._owned_row(
                    connection, job_id, worker_id, lease_token, now
                )
                self._assert_transition(JobStatus(row["status"]), JobStatus.RUNNING)
                self._append_job_event(
                    connection,
                    row_or_values=row,
                    event_type="job.started",
                    payload={"attempt": row["attempt"]},
                    created_at=now,
                    occurrence=lease_token,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                    (JobStatus.RUNNING.value, _store_time(now), job_id),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        if deadline_expired:
            self.retire_execution_permit(job_id, lease_token)
            raise LeaseError(f"job {job_id!r} deadline has expired")
        assert updated is not None
        return self._from_row(updated)

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 30,
        checkpoint: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DurableJob:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = _as_utc(now, default=_utc_now())
        deadline_expired = False
        updated: sqlite3.Row | None = None
        with self.execution_transaction(
            job_id,
            lease_token,
            now=now,
        ) as connection:
            if self._expire_deadlines_in_transaction(
                connection, now, job_id=job_id
            ):
                deadline_expired = True
            else:
                row = self._owned_row(
                    connection, job_id, worker_id, lease_token, now
                )
                if JobStatus(row["status"]) not in {
                    JobStatus.LEASED,
                    JobStatus.RUNNING,
                }:
                    raise InvalidTransitionError(
                        "only leased or running jobs can heartbeat"
                    )
                expires_at = now + timedelta(seconds=lease_seconds)
                if row["thread_id"] is not None:
                    self.events.append_in_transaction(
                        connection,
                        thread_id=row["thread_id"],
                        turn_id=row["turn_id"],
                        job_id=row["job_id"],
                        event_type="job.heartbeat",
                        payload={
                            "attempt": row["attempt"],
                            "checkpoint_sha256": (
                                None
                                if checkpoint is None
                                else _fingerprint(checkpoint)
                            ),
                        },
                        created_at=now,
                    )
                connection.execute(
                    "UPDATE jobs SET lease_expires_at = ?, heartbeat_at = ?, "
                    "checkpoint_json = COALESCE(?, checkpoint_json), updated_at = ? "
                    "WHERE job_id = ?",
                    (
                        _store_time(expires_at),
                        _store_time(now),
                        None if checkpoint is None else json_dumps(checkpoint),
                        _store_time(now),
                        job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        if deadline_expired:
            self.retire_execution_permit(job_id, lease_token)
            raise LeaseError(f"job {job_id!r} deadline has expired")
        assert updated is not None
        return self._from_row(updated)

    def _finish_owned(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        target: JobStatus,
        *,
        event_type: str,
        error: str | None = None,
        available_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DurableJob:
        now = _as_utc(now, default=_utc_now())
        deadline_expired = False
        updated: sqlite3.Row | None = None
        with self.execution_transaction(
            job_id,
            lease_token,
            now=now,
        ) as connection:
            if self._expire_deadlines_in_transaction(
                connection, now, job_id=job_id
            ):
                deadline_expired = True
                row = None
            else:
                row = self._owned_row(
                    connection, job_id, worker_id, lease_token, now
                )
            if row is not None:
                if row["kind"] == "agent_turn" and row["turn_id"] is not None:
                    turn = connection.execute(
                        "SELECT status FROM turns WHERE turn_id = ?",
                        (row["turn_id"],),
                    ).fetchone()
                    if turn is not None:
                        turn_status = TurnStatus(turn["status"])
                        if target == JobStatus.WAITING_HUMAN:
                            raise InvalidTransitionError(
                                "agent turns must enter HITL through "
                                "RuntimeKernel.request_interaction"
                            )
                        elif target == JobStatus.RETRY_SCHEDULED:
                            if turn_status != TurnStatus.RETRY_WAIT:
                                raise InvalidTransitionError(
                                    "agent turn retry must be coordinated with "
                                    "the turn state"
                                )
                        elif target in {
                            JobStatus.COMPLETED,
                            JobStatus.FAILED,
                            JobStatus.CANCELLED,
                            JobStatus.DEAD_LETTER,
                        } and turn_status not in TERMINAL_TURN_STATUSES:
                            raise InvalidTransitionError(
                                "agent turn jobs must finish through "
                                "RuntimeKernel.finish_turn_job"
                            )
                self._assert_transition(JobStatus(row["status"]), target)
                resolved_available_at = available_at or now
                self._append_job_event(
                    connection,
                    row_or_values=row,
                    event_type=event_type,
                    payload={
                        "attempt": row["attempt"],
                        "error": error,
                        "available_at": _store_time(resolved_available_at),
                    },
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, available_at = ?, "
                    "last_error = ?, updated_at = ? WHERE job_id = ?",
                    (
                        target.value,
                        _store_time(resolved_available_at),
                        error,
                        _store_time(now),
                        job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        if deadline_expired:
            self.retire_execution_permit(job_id, lease_token)
            raise LeaseError(f"job {job_id!r} deadline has expired")
        assert updated is not None
        result = self._from_row(updated)
        self.retire_execution_permit(job_id, lease_token)
        return result

    def complete(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> DurableJob:
        return self._finish_owned(
            job_id,
            worker_id,
            lease_token,
            JobStatus.COMPLETED,
            event_type="job.completed",
            now=now,
        )

    def fail(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
        now: datetime | None = None,
    ) -> DurableJob:
        now = _as_utc(now, default=_utc_now())
        current = self.get(job_id)
        if retryable and current.attempt < current.max_attempts:
            target = JobStatus.RETRY_SCHEDULED
            event_type = "job.retry_scheduled"
            available_at = now + timedelta(seconds=max(0, retry_delay_seconds))
        elif retryable:
            target = JobStatus.DEAD_LETTER
            event_type = "job.dead_lettered"
            available_at = now
        else:
            target = JobStatus.FAILED
            event_type = "job.failed"
            available_at = now
        return self._finish_owned(
            job_id,
            worker_id,
            lease_token,
            target,
            event_type=event_type,
            error=error,
            available_at=available_at,
            now=now,
        )

    def wait_for_human(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> DurableJob:
        return self._finish_owned(
            job_id,
            worker_id,
            lease_token,
            JobStatus.WAITING_HUMAN,
            event_type="job.waiting_human",
            now=now,
        )

    def resume_waiting(
        self, job_id: str, *, now: datetime | None = None
    ) -> DurableJob:
        now = _as_utc(now, default=_utc_now())
        updated: sqlite3.Row | None = None
        with self.control_transaction(
            scope="job_resume", subject=job_id
        ) as connection:
            if self._expire_deadlines_in_transaction(
                connection, now, job_id=job_id
            ):
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"job {job_id!r} does not exist")
                pending = connection.execute(
                    "SELECT 1 FROM interactions WHERE job_id = ? "
                    "AND status = 'pending' LIMIT 1",
                    (job_id,),
                ).fetchone()
                if pending is not None:
                    raise InvalidTransitionError(
                        "a job cannot resume while its interaction is pending"
                    )
                self._assert_transition(JobStatus(row["status"]), JobStatus.QUEUED)
                self._append_job_event(
                    connection,
                    row_or_values=row,
                    event_type="job.resumed",
                    payload={
                        "attempt": row["attempt"],
                        "available_at": _store_time(now),
                    },
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, available_at = ?, updated_at = ? "
                    "WHERE job_id = ?",
                    (
                        JobStatus.QUEUED.value,
                        _store_time(now),
                        _store_time(now),
                        job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        assert updated is not None
        return self._from_row(updated)

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> DurableJob:
        now = _utc_now()
        with self.control_transaction(
            scope="job_cancel", subject=job_id
        ) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            current = JobStatus(row["status"])
            if current == JobStatus.CANCELLED:
                return self._from_row(row)
            self._assert_transition(current, JobStatus.CANCELLED)
            self._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.cancelled",
                payload={"attempt": row["attempt"], "reason": reason},
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                "updated_at = ? WHERE job_id = ?",
                (JobStatus.CANCELLED.value, reason, _store_time(now), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        if row["lease_token"]:
            self.retire_execution_permit(job_id, str(row["lease_token"]))
        return self._from_row(updated)

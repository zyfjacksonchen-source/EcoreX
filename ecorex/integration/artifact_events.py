"""Durable outbox for public Artifact API facts.

Artifact domain writes and their outbox intent share one SQLite transaction.
Only notification/publishing happens after that transaction commits.  A failed
publish therefore leaves a recoverable pending row, while a failed intent write
rolls the domain mutation back with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Awaitable, Mapping, Protocol
import uuid

from ecorex.artifacts.api import ArtifactApiEvent
from ecorex.runtime.commit_guard import transaction_commit_guard
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
)


class ArtifactEventOutboxError(RuntimeError):
    pass


class ArtifactEventOutboxConflict(ArtifactEventOutboxError):
    pass


class ArtifactEventPublishTimeout(ArtifactEventOutboxError, TimeoutError):
    """A bounded provider attempt timed out and remains durably retryable."""


class ArtifactEventPublisher(Protocol):
    def publish(
        self,
        event_id: str,
        event: ArtifactApiEvent,
    ) -> Awaitable[None] | None:
        ...


class _ThreadEventStore(Protocol):
    database: Any

    def append_in_transaction(self, connection: sqlite3.Connection, **kwargs: Any) -> Any:
        ...


class RuntimeArtifactEventPublisher:
    """Append scoped Artifact facts to the owning Thread event stream."""

    def __init__(self, event_store: _ThreadEventStore, *, account_id: str) -> None:
        if not account_id:
            raise ValueError("artifact event publisher requires an account_id")
        self.event_store = event_store
        self.account_id = account_id

    def publish(self, event_id: str, event: ArtifactApiEvent) -> None:
        if event.account_id != self.account_id:
            raise ArtifactEventOutboxError(
                "artifact event account does not match the local Runtime account"
            )
        if event.thread_id is None:
            # Imported/account-level artifacts remain durably audited in the
            # outbox even though they have no conversational event stream.
            return
        with self.event_store.database.transaction() as connection:
            thread = connection.execute(
                "SELECT thread_id FROM threads WHERE thread_id = ?",
                (event.thread_id,),
            ).fetchone()
            if thread is None:
                raise ArtifactEventOutboxError(
                    "artifact event references an unknown thread"
                )
            context: Mapping[str, Any] = {}
            if event.turn_id is not None:
                turn = connection.execute(
                    "SELECT thread_id FROM turns WHERE turn_id = ?",
                    (event.turn_id,),
                ).fetchone()
                if turn is None or turn["thread_id"] != event.thread_id:
                    raise ArtifactEventOutboxError(
                        "artifact event turn does not belong to its thread"
                    )
                accepted = connection.execute(
                    "SELECT config_snapshot_id, capability_snapshot_id, "
                    "permission_snapshot_id FROM events "
                    "WHERE thread_id = ? AND turn_id = ? "
                    "AND event_type = 'turn.accepted' ORDER BY seq LIMIT 1",
                    (event.thread_id, event.turn_id),
                ).fetchone()
                if accepted is None:
                    raise ArtifactEventOutboxError(
                        "artifact event turn has no acceptance snapshot"
                    )
                context = dict(accepted)
            self.event_store.append_in_transaction(
                connection,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                job_id=event.job_id,
                correlation_id=event.client_request_id,
                config_snapshot_id=context.get("config_snapshot_id"),
                capability_snapshot_id=context.get("capability_snapshot_id"),
                permission_snapshot_id=context.get("permission_snapshot_id"),
                event_type=event.event_type,
                payload={
                    "artifact_id": event.artifact_id,
                    "revision_id": event.revision_id,
                    "client_request_id": event.client_request_id,
                    "artifact_event_id": event_id,
                    "data": dict(event.payload),
                },
                idempotency_key=f"artifact-api:{event.idempotency_key}",
            )


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: str
    idempotency_key: str
    event_type: str
    artifact_id: str
    account_id: str
    thread_id: str | None
    turn_id: str | None
    payload_sha256: str
    payload: Mapping[str, Any]
    attempts: int
    published_at: str | None
    last_error_code: str | None


class ArtifactEventOutbox:
    def __init__(
        self,
        database_path: SQLiteDatabase | str | Path,
        *,
        publisher: ArtifactEventPublisher | None = None,
        lease_seconds: int = 30,
        provider_timeout_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        if not 1 <= lease_seconds <= 300:
            raise ValueError("artifact outbox lease must be between 1 and 300 seconds")
        if not 0.01 <= provider_timeout_seconds <= 3600:
            raise ValueError("artifact outbox provider timeout is invalid")
        heartbeat_interval = (
            lease_seconds / 3
            if heartbeat_interval_seconds is None
            else float(heartbeat_interval_seconds)
        )
        if not 0.01 <= heartbeat_interval < lease_seconds:
            raise ValueError("artifact outbox heartbeat interval is invalid")
        self.database = (
            database_path
            if isinstance(database_path, SQLiteDatabase)
            else SQLiteDatabase(database_path)
        )
        self.database_path = self.database.path
        self.publisher = publisher
        self.lease_seconds = lease_seconds
        self.provider_timeout_seconds = float(provider_timeout_seconds)
        self.heartbeat_interval_seconds = heartbeat_interval

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    @staticmethod
    def _encoded_event(event: ArtifactApiEvent) -> tuple[str, str, str]:
        payload = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        event_id = "artevt_" + hashlib.sha256(
            event.idempotency_key.encode("utf-8")
        ).hexdigest()
        return event_id, payload, digest

    def _require_own_active_transaction(
        self, connection: sqlite3.Connection
    ) -> None:
        try:
            active = connection.in_transaction
        except sqlite3.ProgrammingError as error:
            raise ArtifactEventOutboxError(
                "artifact event intent requires an open Runtime transaction"
            ) from error
        if not active:
            raise ArtifactEventOutboxError(
                "artifact event intent requires an active Runtime transaction"
            )
        try:
            databases = connection.execute("PRAGMA database_list").fetchall()
        except sqlite3.Error as error:
            raise ArtifactEventOutboxError(
                "artifact event intent cannot verify its Runtime transaction"
            ) from error
        main_path = next(
            (str(row[2]) for row in databases if str(row[1]) == "main"),
            "",
        )
        if not main_path:
            raise ArtifactEventOutboxError(
                "artifact event intent requires the authoritative Runtime database"
            )
        observed = os.path.normcase(str(Path(main_path).resolve()))
        expected = os.path.normcase(str(self.database_path.resolve()))
        if observed != expected:
            raise ArtifactEventOutboxError(
                "artifact event intent transaction belongs to another database"
            )

    def persist_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: ArtifactApiEvent,
    ) -> str:
        """Append one idempotent event intent without owning the commit.

        The caller must pass the same active SQLite transaction that mutates the
        Artifact domain.  This method deliberately never commits or publishes.
        """

        self._require_own_active_transaction(connection)
        event_id, payload, digest = self._encoded_event(event)
        row = connection.execute(
            "SELECT event_id, payload_sha256, payload_json "
            "FROM artifact_event_outbox WHERE idempotency_key = ?",
            (event.idempotency_key,),
        ).fetchone()
        if row is not None:
            if row["payload_sha256"] != digest or row["payload_json"] != payload:
                raise ArtifactEventOutboxConflict(
                    "artifact event idempotency key was reused with different content"
                )
            return str(row["event_id"])
        connection.execute(
            "INSERT INTO artifact_event_outbox("
            "event_id, idempotency_key, event_type, artifact_id, "
            "account_id, thread_id, turn_id, "
            "payload_sha256, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event.idempotency_key,
                event.event_type,
                event.artifact_id,
                event.account_id,
                event.thread_id,
                event.turn_id,
                digest,
                payload,
                datetime.now(UTC).isoformat(),
            ),
        )
        return event_id

    async def persist_then_publish(
        self,
        event: ArtifactApiEvent,
        *,
        execution_validator: Callable[[], None] | None = None,
    ) -> bool:
        """Persist *event* and publish it if this worker acquires the lease.

        ``True`` means the row is durably marked published when this call
        returns. ``False`` means it remains pending or another worker owns the
        active lease.  The stable event ID makes crash recovery at-least-once;
        downstream publishers must deduplicate that ID.
        """
        await asyncio.to_thread(self._persist, event)
        return await self.publish_persisted(
            event,
            execution_validator=execution_validator,
        )

    async def publish_persisted(
        self,
        event: ArtifactApiEvent,
        *,
        execution_validator: Callable[[], None] | None = None,
    ) -> bool:
        """Publish an intent already committed by its Artifact transaction."""

        event_id, _payload, digest = self._encoded_event(event)
        record = await asyncio.to_thread(self.get, event_id)
        if (
            record.idempotency_key != event.idempotency_key
            or record.payload_sha256 != digest
        ):
            raise ArtifactEventOutboxConflict(
                "persisted artifact event does not match the publication request"
            )
        if record.published_at is not None:
            return True
        if self.publisher is None:
            return False
        lease_token = await asyncio.to_thread(self._claim, event_id, digest)
        if lease_token is None:
            return False
        try:
            await self._run_provider_attempt(
                event_id,
                digest,
                lease_token,
                event,
                execution_validator=execution_validator,
            )
        except RuntimeExecutionDenied:
            # The Runtime/local supervisor epoch is closed. Its commit guard
            # intentionally also prevents releasing this lease; restart recovery
            # may reclaim it after expiry without acknowledging a late result.
            raise
        except Exception as error:
            await asyncio.to_thread(
                self._mark_failed,
                event_id,
                lease_token,
                type(error).__name__,
            )
            raise
        await asyncio.to_thread(self._mark_published, event_id, digest, lease_token)
        return True

    async def _run_provider_attempt(
        self,
        event_id: str,
        expected_digest: str,
        lease_token: str,
        event: ArtifactApiEvent,
        *,
        execution_validator: Callable[[], None] | None,
    ) -> None:
        """Invoke one provider while renewing its durable ownership lease."""

        stop_heartbeat = asyncio.Event()
        provider_task = asyncio.create_task(
            self._invoke_provider(
                event_id,
                event,
                execution_validator=execution_validator,
            ),
            name=f"ecorex-artifact-event-provider:{event_id}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_lease(
                event_id,
                expected_digest,
                lease_token,
                stop=stop_heartbeat,
            ),
            name=f"ecorex-artifact-event-heartbeat:{event_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {provider_task, heartbeat_task},
                timeout=self.provider_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    raise heartbeat_error
                raise ArtifactEventOutboxError(
                    "artifact outbox heartbeat stopped before provider completion"
                )
            if provider_task not in done:
                raise ArtifactEventPublishTimeout(
                    "artifact event provider timed out"
                )
            await provider_task
        finally:
            stop_heartbeat.set()
            if not provider_task.done():
                provider_task.cancel()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(
                provider_task,
                heartbeat_task,
                return_exceptions=True,
            )

    async def _invoke_provider(
        self,
        event_id: str,
        event: ArtifactApiEvent,
        *,
        execution_validator: Callable[[], None] | None,
    ) -> None:
        publisher = self.publisher
        if publisher is None:
            raise ArtifactEventOutboxError("artifact event publisher is not configured")
        operation = publisher.publish
        if inspect.iscoroutinefunction(operation):
            self._validate_execution(execution_validator)
            result = operation(event_id, event)
        else:
            result = await asyncio.to_thread(
                self._invoke_sync_provider,
                operation,
                event_id,
                event,
                execution_validator,
            )
        if inspect.isawaitable(result):
            # A decorated async method or a synchronous adapter may return an
            # awaitable. Revalidate in the coroutine's actual execution context.
            self._validate_execution(execution_validator)
            await result
            self._validate_execution(execution_validator)

    @staticmethod
    def _invoke_sync_provider(
        operation: Callable[[str, ArtifactApiEvent], Awaitable[None] | None],
        event_id: str,
        event: ArtifactApiEvent,
        execution_validator: Callable[[], None] | None,
    ) -> Awaitable[None] | None:
        ArtifactEventOutbox._validate_execution(execution_validator)
        result = operation(event_id, event)
        if not inspect.isawaitable(result):
            ArtifactEventOutbox._validate_execution(execution_validator)
        return result

    @staticmethod
    def _validate_execution(validator: Callable[[], None] | None) -> None:
        if validator is not None:
            validator()

    async def _heartbeat_lease(
        self,
        event_id: str,
        expected_digest: str,
        lease_token: str,
        *,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                await asyncio.to_thread(
                    self._renew_lease,
                    event_id,
                    expected_digest,
                    lease_token,
                )

    def _persist(self, event: ArtifactApiEvent) -> tuple[str, str, str]:
        event_id, payload, digest = self._encoded_event(event)
        with self.database.transaction() as connection:
            stored_event_id = self.persist_in_transaction(connection, event)
        return stored_event_id or event_id, payload, digest

    def _claim(self, event_id: str, expected_digest: str) -> str | None:
        connection = self._connect()
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256, published_at, lease_expires_at "
                "FROM artifact_event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None or row["payload_sha256"] != expected_digest:
                raise ArtifactEventOutboxError("artifact outbox claim identity is invalid")
            if row["published_at"] is not None:
                connection.commit()
                return None
            lease_expires_at = (
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"]
                else None
            )
            if lease_expires_at is not None and lease_expires_at > now:
                connection.commit()
                return None
            connection.execute(
                "UPDATE artifact_event_outbox "
                "SET lease_token = ?, lease_expires_at = ?, attempts = attempts + 1 "
                "WHERE event_id = ? AND published_at IS NULL",
                (
                    token,
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    event_id,
                ),
            )
            connection.commit()
            return token
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_published(
        self,
        event_id: str,
        expected_digest: str,
        lease_token: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE artifact_event_outbox "
                "SET published_at = ?, last_error_code = NULL, "
                "lease_token = NULL, lease_expires_at = NULL "
                "WHERE event_id = ? AND payload_sha256 = ? "
                "AND lease_token = ? AND published_at IS NULL",
                (
                    datetime.now(UTC).isoformat(),
                    event_id,
                    expected_digest,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT payload_sha256, published_at, lease_token "
                    "FROM artifact_event_outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if (
                    row is not None
                    and row["payload_sha256"] == expected_digest
                    and row["published_at"] is not None
                ):
                    connection.commit()
                    return
                raise ArtifactEventOutboxError("artifact outbox publish state is inconsistent")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _renew_lease(
        self,
        event_id: str,
        expected_digest: str,
        lease_token: str,
    ) -> None:
        connection = self._connect()
        now = datetime.now(UTC)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE artifact_event_outbox SET lease_expires_at = ? "
                "WHERE event_id = ? AND payload_sha256 = ? "
                "AND lease_token = ? AND published_at IS NULL",
                (
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    event_id,
                    expected_digest,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ArtifactEventOutboxError(
                    "artifact outbox heartbeat lost its lease"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_failed(self, event_id: str, lease_token: str, error_code: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE artifact_event_outbox "
                "SET last_error_code = ?, lease_token = NULL, lease_expires_at = NULL "
                "WHERE event_id = ? AND lease_token = ? AND published_at IS NULL",
                (error_code[:128], event_id, lease_token),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, event_id: str) -> OutboxRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM artifact_event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            connection.rollback()
        finally:
            connection.close()
        if row is None:
            raise KeyError(event_id)
        payload_json = str(row["payload_json"])
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise ArtifactEventOutboxError("artifact outbox payload digest is invalid")
        payload = json.loads(payload_json)
        return OutboxRecord(
            event_id=str(row["event_id"]),
            idempotency_key=str(row["idempotency_key"]),
            event_type=str(row["event_type"]),
            artifact_id=str(row["artifact_id"]),
            account_id=str(row["account_id"]),
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            payload_sha256=str(row["payload_sha256"]),
            payload=payload,
            attempts=int(row["attempts"]),
            published_at=row["published_at"],
            last_error_code=row["last_error_code"],
        )

    def pending(self, *, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be between 1 and 1000")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT event_id FROM artifact_event_outbox "
                "WHERE published_at IS NULL ORDER BY created_at, event_id LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self.get(str(row["event_id"])) for row in rows)

    async def drain(
        self,
        *,
        limit: int = 100,
        execution_validator: Callable[[], None] | None = None,
    ) -> int:
        if self.publisher is None:
            raise ArtifactEventOutboxError("artifact event publisher is not configured")
        published = 0
        for record in await asyncio.to_thread(self.pending, limit=limit):
            event = _event_from_payload(record.payload)
            if await self.persist_then_publish(
                event,
                execution_validator=execution_validator,
            ):
                published += 1
        return published


@dataclass(frozen=True, slots=True)
class ArtifactEventOutboxSupervisorSnapshot:
    running: bool
    cycles: int
    published: int
    failures: int
    last_error_code: str | None


class ArtifactEventOutboxSupervisor:
    """Continuously recover pending Artifact facts under the Runtime epoch.

    The Artifact mutation and its outbox intent are an idempotent saga.  This
    lifecycle closes the prior recovery gap where a failed publish was retried
    only if the user repeated the same request.  The provider task inherits a
    commit guard, so a Runtime epoch close cannot publish or acknowledge a late
    event.
    """

    def __init__(
        self,
        outbox: ArtifactEventOutbox,
        *,
        execution_gate: RuntimeExecutionGate,
        interval_seconds: float = 2.0,
        batch_size: int = 100,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("artifact outbox interval must be positive")
        if not 1 <= batch_size <= 1000:
            raise ValueError("artifact outbox batch size is invalid")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("artifact outbox shutdown timeout must be positive")
        self.outbox = outbox
        self.execution_gate = execution_gate
        self.interval_seconds = float(interval_seconds)
        self.batch_size = batch_size
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._cycles = 0
        self._published = 0
        self._failures = 0
        self._last_error_code: str | None = None
        self._epoch = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> ArtifactEventOutboxSupervisorSnapshot:
        return ArtifactEventOutboxSupervisorSnapshot(
            running=self.running,
            cycles=self._cycles,
            published=self._published,
            failures=self._failures,
            last_error_code=self._last_error_code,
        )

    async def start(self) -> None:
        if self.running:
            return
        self._epoch += 1
        epoch = self._epoch
        self._wake.clear()
        self._task = asyncio.create_task(
            self._run(epoch),
            name="ecorex-artifact-event-outbox",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._epoch += 1
        self._wake.set()
        if task is None:
            return
        task.cancel()
        done, pending = await asyncio.wait(
            {task},
            timeout=self.shutdown_timeout_seconds,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for unfinished in pending:
            unfinished.add_done_callback(_consume_task_result)

    async def _run(self, epoch: int) -> None:
        while epoch == self._epoch:
            await self._attempt_drain(epoch)
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                pass

    async def _attempt_drain(self, epoch: int) -> None:
        if epoch != self._epoch or not self.execution_gate.snapshot().healthy:
            return
        try:
            permit = self.execution_gate.issue_permit(
                scope="artifact_outbox",
                subject="pending_event_drain",
            )

            def validate_attempt() -> None:
                if epoch != self._epoch:
                    raise RuntimeExecutionDenied(
                        "artifact outbox supervisor epoch closed"
                    )
                self.execution_gate.assert_permit(permit)

            with transaction_commit_guard(validate_attempt):
                task = asyncio.create_task(
                    self.outbox.drain(
                        limit=self.batch_size,
                        execution_validator=validate_attempt,
                    )
                )
            published = await task
            validate_attempt()
        except RuntimeExecutionDenied:
            return
        except Exception as error:
            self._failures += 1
            self._last_error_code = type(error).__name__.casefold()[:128]
            return
        self._cycles += 1
        self._published += published
        self._last_error_code = None


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a late cancelled task result without logging an orphan warning."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


def _event_from_payload(raw: Mapping[str, Any]) -> ArtifactApiEvent:
    return ArtifactApiEvent(
        event_type=str(raw["event_type"]),
        idempotency_key=str(raw["idempotency_key"]),
        artifact_id=str(raw["artifact_id"]),
        revision_id=str(raw["revision_id"]) if raw.get("revision_id") else None,
        job_id=str(raw["job_id"]) if raw.get("job_id") else None,
        client_request_id=str(raw["client_request_id"]),
        payload=dict(raw["payload"]),
        account_id=str(raw.get("account_id") or "local-user"),
        thread_id=str(raw["thread_id"]) if raw.get("thread_id") else None,
        turn_id=str(raw["turn_id"]) if raw.get("turn_id") else None,
        schema_version=int(raw.get("schema_version", 1)),
    )

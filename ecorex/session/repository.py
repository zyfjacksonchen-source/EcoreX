"""Durable two-phase state for managed-session credential installation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping
import uuid

from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema

from .models import (
    LeaseValidationError,
    SessionAuditRecord,
    SessionConflict,
    SessionLogoutReceipt,
    SessionUnavailable,
    SignedManagedSessionLease,
    StaleSessionRequest,
    redacted_hash,
)


_INSTALL_STATES = frozenset(
    {"staged", "vault_written", "committed", "superseded", "aborted"}
)
_HEX_SHA256 = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class SessionStateRecord:
    generation: int
    high_water_revision: int
    active_intent_id: str | None
    pending_intent_id: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class SessionInstallIntent:
    intent_id: str
    client_request_hash: str
    request_fingerprint: str
    status: str
    attempt: int
    base_generation: int
    target_revision: int
    lease: SignedManagedSessionLease
    lease_digest: str
    credential_ref: str
    previous_credential_ref: str | None
    failure_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StagedInstall:
    intent: SessionInstallIntent
    already_committed: bool = False


@dataclass(frozen=True, slots=True)
class ActiveSessionRecord:
    state: SessionStateRecord
    intent: SessionInstallIntent


class ManagedSessionRepository:
    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        initialize: bool = True,
    ) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self._startup_lock = threading.RLock()
        self._startup_converged = False
        if initialize:
            self.initialize()
        else:
            self.validate()

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def validate(self) -> None:
        """Validate the signed product schema without creating business state."""

        with self.database.reader() as connection:
            validate_product_schema(connection)
            state = connection.execute(
                "SELECT * FROM managed_session_state WHERE singleton=1"
            ).fetchone()
            if state is not None:
                self._validate_schema(connection)

    def initialize(self) -> None:
        """Create the singleton only after the Runtime invariant audit passes."""

        with self._startup_lock:
            if self._startup_converged:
                return
            # The compiled product catalog is the only schema authority. This
            # convergence step may initialize state rows, but never runs DDL.
            self.validate()
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO managed_session_state("
                    "singleton,generation,high_water_revision,updated_at"
                    ") VALUES(1,0,0,?) ON CONFLICT(singleton) DO NOTHING",
                    ("1970-01-01T00:00:00Z",),
                )
                self._validate_schema(connection)
            self._startup_converged = True

    def converge_startup(self) -> None:
        self.initialize()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        state = connection.execute(
            "SELECT * FROM managed_session_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise SessionUnavailable("managed session state is unavailable")
        if state["active_intent_id"] and state["pending_intent_id"] == state["active_intent_id"]:
            raise SessionUnavailable("managed session state is inconsistent")

    def state(self) -> SessionStateRecord:
        with self.database.reader() as connection:
            return self._state(connection)

    @contextmanager
    def _transaction(
        self,
        before_commit: Callable[[], None] | None = None,
    ) -> Iterator[sqlite3.Connection]:
        with self.database.transaction() as connection:
            yield connection
            if before_commit is not None:
                before_commit()

    def stage_install(
        self,
        lease: SignedManagedSessionLease,
        *,
        client_request_hash: str,
        request_fingerprint: str,
        now: str,
        before_commit: Callable[[], None] | None = None,
    ) -> StagedInstall:
        _digest(client_request_hash, "client request hash")
        _digest(request_fingerprint, "request fingerprint")
        lease_digest = lease.digest
        with self._transaction(before_commit) as connection:
            state = self._state(connection)
            existing_row = connection.execute(
                "SELECT * FROM managed_session_installs WHERE client_request_hash=?",
                (client_request_hash,),
            ).fetchone()
            if existing_row is not None:
                existing = self._intent(existing_row)
                if existing.request_fingerprint != request_fingerprint:
                    raise SessionConflict(
                        "client request id was reused with different session material"
                    )
                if existing.lease_digest != lease_digest:
                    raise SessionConflict("managed session request identity is inconsistent")
                if existing.status == "committed":
                    if state.active_intent_id != existing.intent_id:
                        raise StaleSessionRequest(
                            "managed session install was superseded by a newer session"
                        )
                    return StagedInstall(existing, already_committed=True)
                if existing.status in {"staged", "vault_written"}:
                    if state.pending_intent_id != existing.intent_id:
                        raise StaleSessionRequest(
                            "managed session install is no longer current"
                        )
                    return StagedInstall(existing)
                if existing.status == "aborted":
                    if (
                        state.pending_intent_id is not None
                        or state.generation != existing.base_generation
                        or existing.target_revision <= state.high_water_revision
                    ):
                        raise StaleSessionRequest(
                            "managed session install cannot be retried after newer state"
                        )
                    old_ref = existing.credential_ref
                    new_ref = _credential_reference()
                    previous_ref = self._active_credential_ref(connection, state)
                    self._queue_cleanup(
                        connection,
                        old_ref,
                        reason_code="aborted_install_replaced",
                        now=now,
                    )
                    connection.execute(
                        "UPDATE managed_session_installs SET status='staged', "
                        "attempt=attempt+1, base_generation=?, credential_ref=?, "
                        "previous_credential_ref=?, failure_code=NULL, updated_at=? "
                        "WHERE intent_id=? AND status='aborted'",
                        (
                            state.generation,
                            new_ref,
                            previous_ref,
                            now,
                            existing.intent_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE managed_session_state SET pending_intent_id=?, updated_at=? "
                        "WHERE singleton=1",
                        (existing.intent_id, now),
                    )
                    refreshed = self._get_intent(connection, existing.intent_id)
                    self._append_audit(
                        connection,
                        event_type="session.install.restaged",
                        outcome="accepted",
                        reason_code=None,
                        client_request_hash=client_request_hash,
                        lease=lease,
                        generation=state.generation,
                        details={"attempt": refreshed.attempt},
                        now=now,
                    )
                    return StagedInstall(refreshed)
                raise StaleSessionRequest(
                    "managed session install was superseded by a newer request"
                )

            if lease.claims.revision <= state.high_water_revision:
                raise StaleSessionRequest("managed session lease revision is not monotonic")

            if state.pending_intent_id is not None:
                pending = self._get_intent(connection, state.pending_intent_id)
                if lease.claims.revision <= pending.target_revision:
                    raise SessionConflict(
                        "another managed session install is already in progress"
                    )
                connection.execute(
                    "UPDATE managed_session_installs SET status='superseded', "
                    "failure_code='higher_revision_staged', updated_at=? "
                    "WHERE intent_id=? AND status IN ('staged','vault_written')",
                    (now, pending.intent_id),
                )
                self._queue_cleanup(
                    connection,
                    pending.credential_ref,
                    reason_code="superseded_install",
                    now=now,
                )
                self._append_audit(
                    connection,
                    event_type="session.install.superseded",
                    outcome="superseded",
                    reason_code="higher_revision_staged",
                    client_request_hash=pending.client_request_hash,
                    lease=pending.lease,
                    generation=state.generation,
                    details={},
                    now=now,
                )

            intent_id = "session_install_" + uuid.uuid4().hex
            credential_ref = _credential_reference()
            previous_ref = self._active_credential_ref(connection, state)
            connection.execute(
                "INSERT INTO managed_session_installs("
                "intent_id,client_request_hash,request_fingerprint,status,attempt,"
                "base_generation,target_revision,lease_json,lease_digest,credential_ref,"
                "previous_credential_ref,failure_code,created_at,updated_at"
                ") VALUES(?,?,?,'staged',1,?,?,?,?,?,?,NULL,?,?)",
                (
                    intent_id,
                    client_request_hash,
                    request_fingerprint,
                    state.generation,
                    lease.claims.revision,
                    lease.to_json(),
                    lease_digest,
                    credential_ref,
                    previous_ref,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE managed_session_state SET pending_intent_id=?, updated_at=? "
                "WHERE singleton=1",
                (intent_id, now),
            )
            intent = self._get_intent(connection, intent_id)
            self._append_audit(
                connection,
                event_type="session.install.staged",
                outcome="accepted",
                reason_code=None,
                client_request_hash=client_request_hash,
                lease=lease,
                generation=state.generation,
                details={"attempt": 1},
                now=now,
            )
            return StagedInstall(intent)

    def mark_vault_written(
        self,
        intent_id: str,
        *,
        credential_ref: str,
        expected_lease_digest: str,
        now: str,
        before_commit: Callable[[], None] | None = None,
    ) -> str:
        _digest(expected_lease_digest, "lease digest")
        with self._transaction(before_commit) as connection:
            state = self._state(connection)
            intent = self._get_intent(connection, intent_id)
            if (
                intent.status == "committed"
                and state.active_intent_id == intent.intent_id
                and intent.credential_ref == credential_ref
            ):
                return "committed"
            if (
                intent.status not in {"staged", "vault_written"}
                or state.pending_intent_id != intent.intent_id
                or state.generation != intent.base_generation
                or intent.target_revision <= state.high_water_revision
                or intent.credential_ref != credential_ref
                or intent.lease_digest != expected_lease_digest
            ):
                self._supersede_in_transaction(
                    connection,
                    intent,
                    state,
                    reason_code="stale_vault_write",
                    now=now,
                )
                return "stale"
            if intent.status == "staged":
                connection.execute(
                    "UPDATE managed_session_installs SET status='vault_written', "
                    "updated_at=? WHERE intent_id=? AND status='staged'",
                    (now, intent.intent_id),
                )
                self._append_audit(
                    connection,
                    event_type="session.install.vault_written",
                    outcome="accepted",
                    reason_code=None,
                    client_request_hash=intent.client_request_hash,
                    lease=intent.lease,
                    generation=state.generation,
                    details={"attempt": intent.attempt},
                    now=now,
                )
            return "ready"

    def finalize_install(
        self,
        intent_id: str,
        *,
        credential_ref: str,
        expected_lease_digest: str,
        now: str,
        before_commit: Callable[[], None] | None = None,
    ) -> tuple[str, int]:
        _digest(expected_lease_digest, "lease digest")
        with self._transaction(before_commit) as connection:
            state = self._state(connection)
            intent = self._get_intent(connection, intent_id)
            if (
                intent.status == "committed"
                and state.active_intent_id == intent.intent_id
                and intent.credential_ref == credential_ref
                and intent.lease_digest == expected_lease_digest
            ):
                return "committed", state.generation
            if (
                intent.status not in {"staged", "vault_written"}
                or state.pending_intent_id != intent.intent_id
                or state.generation != intent.base_generation
                or intent.target_revision <= state.high_water_revision
                or intent.credential_ref != credential_ref
                or intent.lease_digest != expected_lease_digest
            ):
                self._supersede_in_transaction(
                    connection,
                    intent,
                    state,
                    reason_code="stale_finalize",
                    now=now,
                )
                return "stale", state.generation
            generation = state.generation + 1
            connection.execute(
                "UPDATE managed_session_installs SET status='committed', "
                "failure_code=NULL, updated_at=? WHERE intent_id=?",
                (now, intent.intent_id),
            )
            connection.execute(
                "UPDATE managed_session_state SET generation=?, high_water_revision=?, "
                "active_intent_id=?, pending_intent_id=NULL, updated_at=? WHERE singleton=1",
                (generation, intent.target_revision, intent.intent_id, now),
            )
            if (
                intent.previous_credential_ref
                and intent.previous_credential_ref != intent.credential_ref
            ):
                self._queue_cleanup(
                    connection,
                    intent.previous_credential_ref,
                    reason_code="replaced_active_session",
                    now=now,
                )
            self._append_audit(
                connection,
                event_type="session.install.committed",
                outcome="success",
                reason_code=None,
                client_request_hash=intent.client_request_hash,
                lease=intent.lease,
                generation=generation,
                details={"attempt": intent.attempt},
                now=now,
            )
            return "committed", generation

    def abort_install(
        self,
        intent_id: str,
        *,
        credential_ref: str,
        reason_code: str,
        now: str,
    ) -> bool:
        reason_code = _reason(reason_code)
        with self.database.transaction() as connection:
            state = self._state(connection)
            intent = self._get_intent(connection, intent_id)
            if intent.status in {"committed", "superseded", "aborted"}:
                return False
            if intent.credential_ref != credential_ref:
                return False
            connection.execute(
                "UPDATE managed_session_installs SET status='aborted', failure_code=?, "
                "updated_at=? WHERE intent_id=? AND status IN ('staged','vault_written')",
                (reason_code, now, intent.intent_id),
            )
            if state.pending_intent_id == intent.intent_id:
                connection.execute(
                    "UPDATE managed_session_state SET pending_intent_id=NULL, updated_at=? "
                    "WHERE singleton=1",
                    (now,),
                )
            self._queue_cleanup(
                connection,
                intent.credential_ref,
                reason_code="aborted_install",
                now=now,
            )
            self._append_audit(
                connection,
                event_type="session.install.aborted",
                outcome="failed",
                reason_code=reason_code,
                client_request_hash=intent.client_request_hash,
                lease=intent.lease,
                generation=state.generation,
                details={"attempt": intent.attempt},
                now=now,
            )
            return True

    def pending_install(self) -> SessionInstallIntent | None:
        with self.database.reader() as connection:
            state = self._state(connection)
            if state.pending_intent_id is None:
                return None
            return self._get_intent(connection, state.pending_intent_id)

    def get_install(self, intent_id: str) -> SessionInstallIntent:
        with self.database.reader() as connection:
            return self._get_intent(connection, intent_id)

    def active(self, *, require_quiescent: bool = True) -> ActiveSessionRecord:
        with self.database.reader() as connection:
            state = self._state(connection)
            if require_quiescent and state.pending_intent_id is not None:
                raise SessionConflict("managed session change is in progress")
            if state.active_intent_id is None:
                raise SessionUnavailable("managed session is not signed in")
            intent = self._get_intent(connection, state.active_intent_id)
            if intent.status != "committed":
                raise SessionUnavailable("managed session state is inconsistent")
            return ActiveSessionRecord(state=state, intent=intent)

    def identity_is_current(self, active: ActiveSessionRecord) -> bool:
        with self.database.reader() as connection:
            state = self._state(connection)
            if state.pending_intent_id is not None:
                return False
            if (
                state.generation != active.state.generation
                or state.active_intent_id != active.intent.intent_id
            ):
                return False
            row = connection.execute(
                "SELECT status,lease_digest,credential_ref FROM managed_session_installs "
                "WHERE intent_id=?",
                (active.intent.intent_id,),
            ).fetchone()
            return bool(
                row
                and row["status"] == "committed"
                and row["lease_digest"] == active.intent.lease_digest
                and row["credential_ref"] == active.intent.credential_ref
            )

    def logout(
        self,
        *,
        client_request_hash: str,
        expected_lease_digest: str | None,
        now: str,
    ) -> SessionLogoutReceipt:
        _digest(client_request_hash, "client request hash")
        if expected_lease_digest is not None:
            _digest(expected_lease_digest, "expected lease digest")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM managed_session_logouts WHERE client_request_hash=?",
                (client_request_hash,),
            ).fetchone()
            if existing is not None:
                if existing["expected_lease_digest"] != expected_lease_digest:
                    raise SessionConflict(
                        "client request id was reused with a different logout target"
                    )
                return SessionLogoutReceipt(
                    generation=int(existing["result_generation"]),
                    client_request_hash=client_request_hash,
                    already_applied=True,
                )

            state = self._state(connection)
            active: SessionInstallIntent | None = None
            if state.active_intent_id is not None:
                active = self._get_intent(connection, state.active_intent_id)
            if active is None:
                if expected_lease_digest is not None:
                    raise StaleSessionRequest("logout target is no longer active")
            elif expected_lease_digest != active.lease_digest:
                raise StaleSessionRequest("logout target is no longer active")

            if state.pending_intent_id is not None:
                pending = self._get_intent(connection, state.pending_intent_id)
                if pending.status in {"staged", "vault_written"}:
                    connection.execute(
                        "UPDATE managed_session_installs SET status='superseded', "
                        "failure_code='logout', updated_at=? WHERE intent_id=?",
                        (now, pending.intent_id),
                    )
                    self._queue_cleanup(
                        connection,
                        pending.credential_ref,
                        reason_code="logout_pending_install",
                        now=now,
                    )
            if active is not None:
                self._queue_cleanup(
                    connection,
                    active.credential_ref,
                    reason_code="logout_active_session",
                    now=now,
                )

            generation = state.generation + 1
            connection.execute(
                "INSERT INTO managed_session_logouts("
                "client_request_hash,expected_lease_digest,result_generation,created_at"
                ") VALUES(?,?,?,?)",
                (client_request_hash, expected_lease_digest, generation, now),
            )
            connection.execute(
                "UPDATE managed_session_state SET generation=?, active_intent_id=NULL, "
                "pending_intent_id=NULL, updated_at=? WHERE singleton=1",
                (generation, now),
            )
            self._append_audit(
                connection,
                event_type="session.logout.committed",
                outcome="success",
                reason_code=None,
                client_request_hash=client_request_hash,
                lease=active.lease if active else None,
                generation=generation,
                details={},
                now=now,
            )
            return SessionLogoutReceipt(
                generation=generation,
                client_request_hash=client_request_hash,
                already_applied=False,
            )

    def record_remote_logout_state(
        self,
        *,
        client_request_hash: str,
        expected_lease_digest: str,
        state_name: str,
        reason_code: str | None,
        now: str,
    ) -> None:
        _digest(client_request_hash, "client request hash")
        _digest(expected_lease_digest, "expected lease digest")
        if state_name not in {"remote_revocation_pending", "remote_revoked"}:
            raise ValueError("remote logout state is invalid")
        with self.database.transaction() as connection:
            state = self._state(connection)
            if state.active_intent_id is None:
                raise StaleSessionRequest("logout target is no longer active")
            active = self._get_intent(connection, state.active_intent_id)
            if active.lease_digest != expected_lease_digest:
                raise StaleSessionRequest("logout target is no longer active")
            self._append_audit(
                connection,
                event_type=f"session.logout.{state_name}",
                outcome=(
                    "uncertain"
                    if state_name == "remote_revocation_pending"
                    else "confirmed"
                ),
                reason_code=reason_code,
                client_request_hash=client_request_hash,
                lease=active.lease,
                generation=state.generation,
                details={
                    "remote_confirmed": state_name == "remote_revoked",
                },
                now=now,
            )

    def cleanup_pending(self) -> tuple[str, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT credential_ref FROM managed_session_credential_cleanup "
                "WHERE state='pending' ORDER BY created_at,credential_ref"
            ).fetchall()
        return tuple(str(row["credential_ref"]) for row in rows)

    def terminal_credential_references(self) -> tuple[str, ...]:
        """Return terminal install references for boot-time orphan reconciliation.

        A superseded writer can race a cleanup, write after the first delete and
        then crash.  Rechecking every terminal reference during recovery closes
        that narrow cross-store race without ever persisting token material.
        """

        with self.database.reader() as connection:
            state = self._state(connection)
            live = {
                value
                for value in (state.active_intent_id, state.pending_intent_id)
                if value is not None
            }
            rows = connection.execute(
                "SELECT intent_id,credential_ref FROM managed_session_installs "
                "WHERE status IN ('superseded','aborted') ORDER BY updated_at,intent_id"
            ).fetchall()
        return tuple(
            str(row["credential_ref"])
            for row in rows
            if row["intent_id"] not in live
        )

    def reference_is_live(self, credential_ref: str) -> bool:
        with self.database.reader() as connection:
            state = self._state(connection)
            live_ids = tuple(
                value
                for value in (state.active_intent_id, state.pending_intent_id)
                if value is not None
            )
            if not live_ids:
                return False
            placeholders = ",".join("?" for _ in live_ids)
            row = connection.execute(
                f"SELECT 1 FROM managed_session_installs WHERE intent_id IN ({placeholders}) "
                "AND credential_ref=? LIMIT 1",
                (*live_ids, credential_ref),
            ).fetchone()
            return row is not None

    def mark_cleanup_done(self, credential_ref: str, *, now: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE managed_session_credential_cleanup SET state='done', updated_at=? "
                "WHERE credential_ref=? AND state='pending'",
                (now, credential_ref),
            )
            return cursor.rowcount == 1

    def record_audit(
        self,
        *,
        event_type: str,
        outcome: str,
        reason_code: str | None,
        client_request_hash: str | None,
        lease: SignedManagedSessionLease | None,
        generation: int,
        details: Mapping[str, Any] | None,
        now: str,
    ) -> None:
        with self.database.transaction() as connection:
            self._append_audit(
                connection,
                event_type=event_type,
                outcome=outcome,
                reason_code=reason_code,
                client_request_hash=client_request_hash,
                lease=lease,
                generation=generation,
                details=details or {},
                now=now,
            )

    def audit_records(self, *, limit: int = 1000) -> tuple[SessionAuditRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("audit limit is invalid")
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM managed_session_audit ORDER BY sequence DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            SessionAuditRecord(
                sequence=int(row["sequence"]),
                event_type=row["event_type"],
                outcome=row["outcome"],
                reason_code=row["reason_code"],
                client_request_hash=row["client_request_hash"],
                account_hash=row["account_hash"],
                organization_hash=row["organization_hash"],
                lease_digest=row["lease_digest"],
                revision=row["revision"],
                generation=int(row["generation"]),
                details=json_loads(row["details_json"], {}),
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        )

    def _supersede_in_transaction(
        self,
        connection: sqlite3.Connection,
        intent: SessionInstallIntent,
        state: SessionStateRecord,
        *,
        reason_code: str,
        now: str,
    ) -> None:
        if intent.status in {"staged", "vault_written"}:
            connection.execute(
                "UPDATE managed_session_installs SET status='superseded', failure_code=?, "
                "updated_at=? WHERE intent_id=? AND status IN ('staged','vault_written')",
                (reason_code, now, intent.intent_id),
            )
        if state.pending_intent_id == intent.intent_id:
            connection.execute(
                "UPDATE managed_session_state SET pending_intent_id=NULL, updated_at=? "
                "WHERE singleton=1",
                (now,),
            )
        self._queue_cleanup(
            connection,
            intent.credential_ref,
            reason_code="superseded_install",
            now=now,
        )
        self._append_audit(
            connection,
            event_type="session.install.superseded",
            outcome="superseded",
            reason_code=reason_code,
            client_request_hash=intent.client_request_hash,
            lease=intent.lease,
            generation=state.generation,
            details={"attempt": intent.attempt},
            now=now,
        )

    @staticmethod
    def _queue_cleanup(
        connection: sqlite3.Connection,
        credential_ref: str,
        *,
        reason_code: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO managed_session_credential_cleanup("
            "credential_ref,reason_code,state,created_at,updated_at"
            ") VALUES(?,?,'pending',?,?) "
            "ON CONFLICT(credential_ref) DO UPDATE SET "
            "reason_code=excluded.reason_code, state='pending', updated_at=excluded.updated_at",
            (credential_ref, reason_code, now, now),
        )

    def _active_credential_ref(
        self,
        connection: sqlite3.Connection,
        state: SessionStateRecord,
    ) -> str | None:
        if state.active_intent_id is None:
            return None
        return self._get_intent(connection, state.active_intent_id).credential_ref

    @staticmethod
    def _state(connection: sqlite3.Connection) -> SessionStateRecord:
        row = connection.execute(
            "SELECT * FROM managed_session_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise SessionUnavailable("managed session state is unavailable")
        generation = int(row["generation"])
        high_water = int(row["high_water_revision"])
        if generation < 0 or high_water < 0:
            raise SessionUnavailable("managed session state is invalid")
        return SessionStateRecord(
            generation=generation,
            high_water_revision=high_water,
            active_intent_id=row["active_intent_id"],
            pending_intent_id=row["pending_intent_id"],
            updated_at=row["updated_at"],
        )

    def _get_intent(
        self, connection: sqlite3.Connection, intent_id: str
    ) -> SessionInstallIntent:
        row = connection.execute(
            "SELECT * FROM managed_session_installs WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise SessionUnavailable("managed session install state is missing")
        return self._intent(row)

    @staticmethod
    def _intent(row: sqlite3.Row) -> SessionInstallIntent:
        status = str(row["status"])
        if status not in _INSTALL_STATES:
            raise SessionUnavailable("managed session install state is invalid")
        try:
            lease = SignedManagedSessionLease.from_json(row["lease_json"])
        except LeaseValidationError as error:
            raise SessionUnavailable("managed session lease storage is invalid") from error
        lease_digest = str(row["lease_digest"])
        _digest(lease_digest, "stored lease digest")
        if lease.claims.revision != int(row["target_revision"]):
            raise SessionUnavailable("managed session revision storage is inconsistent")
        return SessionInstallIntent(
            intent_id=row["intent_id"],
            client_request_hash=row["client_request_hash"],
            request_fingerprint=row["request_fingerprint"],
            status=status,
            attempt=int(row["attempt"]),
            base_generation=int(row["base_generation"]),
            target_revision=int(row["target_revision"]),
            lease=lease,
            lease_digest=lease_digest,
            credential_ref=row["credential_ref"],
            previous_credential_ref=row["previous_credential_ref"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _append_audit(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        outcome: str,
        reason_code: str | None,
        client_request_hash: str | None,
        lease: SignedManagedSessionLease | None,
        generation: int,
        details: Mapping[str, Any],
        now: str,
    ) -> None:
        event_type = _event(event_type)
        outcome = _reason(outcome)
        reason_code = _reason(reason_code) if reason_code is not None else None
        if client_request_hash is not None:
            _digest(client_request_hash, "client request hash")
        safe_details = _safe_details(details)
        claims = lease.claims if lease else None
        connection.execute(
            "INSERT INTO managed_session_audit("
            "audit_id,event_type,outcome,reason_code,client_request_hash,account_hash,"
            "organization_hash,lease_digest,revision,generation,details_json,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "session_audit_" + uuid.uuid4().hex,
                event_type,
                outcome,
                reason_code,
                client_request_hash,
                redacted_hash("account", claims.account_id) if claims else None,
                redacted_hash("organization", claims.organization_id) if claims else None,
                lease.digest if lease else None,
                claims.revision if claims else None,
                generation,
                json_dumps(safe_details),
                now,
            ),
        )


def _credential_reference() -> str:
    return "ecorex/session/" + uuid.uuid4().hex


def _digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_SHA256 for character in value)
    ):
        raise SessionUnavailable(f"{label} is invalid")
    return value


def _event(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._" for character in value)
    ):
        raise ValueError("session audit event type is invalid")
    return value


def _reason(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value)
    ):
        raise ValueError("session audit reason is invalid")
    return value


def _safe_details(value: Mapping[str, Any]) -> dict[str, int | bool | None]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ValueError("session audit details are invalid")
    result: dict[str, int | bool | None] = {}
    for raw_key, item in value.items():
        key = _reason(str(raw_key))
        if isinstance(item, bool) or item is None:
            result[key] = item
        elif isinstance(item, int) and not isinstance(item, bool) and abs(item) <= 10**18:
            result[key] = item
        else:
            raise ValueError("session audit details must be redacted scalars")
    return result


def client_request_hash(client_request_id: str) -> str:
    if (
        not isinstance(client_request_id, str)
        or not 8 <= len(client_request_id) <= 512
        or "\x00" in client_request_id
    ):
        raise ValueError("client_request_id is invalid")
    return hashlib.sha256(
        b"ecorex-managed-session-client-request-v1\n"
        + client_request_id.encode("utf-8")
    ).hexdigest()


def install_request_fingerprint(lease: SignedManagedSessionLease) -> str:
    return hashlib.sha256(
        b"ecorex-managed-session-install-v1\n" + lease.digest.encode("ascii")
    ).hexdigest()


__all__ = [
    "ActiveSessionRecord",
    "ManagedSessionRepository",
    "SessionInstallIntent",
    "SessionStateRecord",
    "StagedInstall",
    "client_request_hash",
    "install_request_fingerprint",
]

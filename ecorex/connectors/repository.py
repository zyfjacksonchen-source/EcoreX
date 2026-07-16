"""Durable connector state stored alongside the Runtime SQLite event store.

Only public metadata, opaque credential references, hashes, and JSON-safe action
results are stored here.  OAuth state and credential material are deliberately
kept behind :mod:`ecorex.connectors.vault`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast
import uuid

from .models import (
    ConnectorAuthKind,
    ConnectorDefinition,
    ConnectorHealth,
    ConnectorInstance,
    ConnectorInvocationContext,
    ConnectorInvocationRecord,
)
from .errors import ConnectorReconciliationPending

if TYPE_CHECKING:
    from ecorex.runtime.database import SQLiteDatabase


_SCHEMA_VERSION = 6


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("connector timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ConnectorFlowRecord:
    flow_id: str
    connector_id: str
    auth_kind: ConnectorAuthKind
    private_ref: str
    expires_at: datetime
    operation_token: str
    reauthorize_instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class FlowConsumption:
    record: ConnectorFlowRecord | None
    reason: Literal["consumed", "expired", "unavailable"]
    cleanup_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorOutboxEvent:
    event_id: str
    event_type: str
    aggregate_id: str
    aggregate_seq: int
    payload: Mapping[str, Any]
    created_at: datetime
    lease_token: str
    attempts: int


@dataclass(frozen=True, slots=True)
class RecoveryReference:
    kind: Literal["flow", "pending_instance", "disconnecting_instance"]
    record_id: str
    credential_ref: str
    recovery_token: str


@dataclass(frozen=True, slots=True)
class ConnectorOperationLease:
    operation_id: str
    instance_id: str
    lease_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InvocationReservation:
    outcome: Literal[
        "reserved",
        "replay",
        "conflict",
        "uncertain",
        "staged",
        "in_progress",
    ]
    invocation_id: str
    result: Any = None


@dataclass(frozen=True, slots=True)
class ConnectorResultStage:
    """Secret-free local authority for finalizing one provider result.

    Large result bytes live only in the Artifact CAS.  The stage binds their
    digest to the original invocation/operation fence and Runtime execution
    scope, so restart recovery can finish locally without calling the
    provider again.
    """

    invocation: ConnectorInvocationRecord
    operation_id: str
    lease_token_sha256: str
    result_sha256: str
    size_bytes: int
    delivery_hint: Literal["inline", "artifact", "unavailable"]
    inline_data: Any
    discovery_id: str
    requested_name: str
    owner_account_id: str
    thread_id: str
    turn_id: str
    created_by_tool_id: Literal["connector_read", "connector_write"]
    completion_path: Literal["provider_result", "late_provider_result"]
    status: Literal["staged", "finalized"]
    artifact_id: str | None
    revision_id: str | None
    result: Any = None


@dataclass(frozen=True, slots=True)
class LifecycleRequestReservation:
    outcome: Literal["reserved", "replay", "failed", "in_progress", "conflict"]
    lease_token: str | None = None
    result: Mapping[str, Any] | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorInteractionLoginBinding:
    interaction_id: str
    connector_id: str
    mode: Literal["connect", "reauthorize"]
    target_instance_id: str | None
    generation: int
    status: Literal[
        "starting",
        "awaiting_callback",
        "completing",
        "failed",
        "completed",
        "cancelled",
        "reauthorization_required",
        "authorization_required",
    ]
    lifecycle_request_id: str
    flow_id: str | None
    completed_instance_id: str | None
    expires_at: datetime | None
    operation_token: str | None
    operation_lease_expires_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class InteractionLoginReservation:
    outcome: Literal["reserved", "replay", "in_progress", "completed"]
    binding: ConnectorInteractionLoginBinding


@dataclass(frozen=True, slots=True)
class ReauthorizationRecovery:
    transition_id: str
    instance_id: str
    status: Literal["preparing", "swapped"]
    cleanup_ref: str
    recovery_token: str


class SQLiteConnectorRepository:
    """Connector repository using short, fenced SQLite transactions.

    A separate connection is opened for each operation, so this repository can
    safely share the Runtime database between threads and processes. Product
    schema, WAL mode, foreign keys, and durability settings are owned by the
    shared :class:`SQLiteDatabase` boundary.
    """

    def __init__(
        self,
        database: "SQLiteDatabase | str | Path",
        *,
        uri: bool = False,
        busy_timeout_ms: int = 5_000,
        initialize: bool = True,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        from ecorex.runtime.database import SQLiteDatabase

        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._temporary: TemporaryDirectory[str] | None = None
        self._before_commit = before_commit
        if isinstance(database, SQLiteDatabase):
            if uri:
                raise ValueError("uri cannot be combined with SQLiteDatabase")
            self._runtime_database = database
        else:
            raw = str(database)
            is_volatile = raw == ":memory:" or (uri and "mode=memory" in raw)
            if uri and not is_volatile:
                raise ValueError("connector repository URI mode only supports volatile storage")
            if is_volatile:
                self._temporary = TemporaryDirectory(prefix="ecorex-connectors-")
                raw = str(Path(self._temporary.name) / "runtime.db")
            self._runtime_database = SQLiteDatabase(raw)
        self.database = str(self._runtime_database.path)
        if initialize:
            self.initialize()
        else:
            self.validate()

    @classmethod
    def volatile(
        cls, *, initialize: bool = True
    ) -> "SQLiteConnectorRepository":
        return cls(":memory:", initialize=initialize)

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def set_before_commit_validator(
        self, validator: Callable[[], None] | None
    ) -> None:
        """Install the Runtime epoch validator used by every Connector write."""

        if validator is not None and not callable(validator):
            raise TypeError("connector commit validator must be callable")
        self._before_commit = validator

    def _new_connection(self) -> sqlite3.Connection:
        connection = self._runtime_database.connect()
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                try:
                    if self._before_commit is not None:
                        self._before_commit()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def validate(self) -> None:
        """Validate present Connector metadata without creating any facts."""

        from ecorex.runtime.schema_catalog import validate_product_schema

        with self._connection() as connection:
            validate_product_schema(connection)
            row = connection.execute(
                "SELECT schema_version FROM connector_schema WHERE singleton=1"
            ).fetchone()
        if row is None:
            return
        version = int(row["schema_version"])
        if version != _SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported connector storage schema "
                f"{version}; expected {_SCHEMA_VERSION}"
            )

    def initialize(self) -> None:
        """Persist or verify Connector version metadata during convergence."""

        self.validate()
        with self._write() as connection:
            row = connection.execute(
                "SELECT schema_version FROM connector_schema WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO connector_schema(singleton, schema_version) VALUES (1, ?)",
                    (_SCHEMA_VERSION,),
                )
                return
            version = int(row["schema_version"])
            if version != _SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported connector storage schema "
                    f"{version}; expected {_SCHEMA_VERSION}"
                )

    def converge_startup(self) -> None:
        """Alias used by the healthy startup convergence coordinator."""

        self.initialize()

    def sync_definitions(self, definitions: Iterable[ConnectorDefinition]) -> None:
        now = _iso(_utcnow())
        with self._write() as connection:
            for definition in definitions:
                encoded = _canonical_json(definition.to_dict())
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT INTO connector_definitions(
                        connector_id, contract_version, definition_json,
                        definition_sha256, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(connector_id) DO UPDATE SET
                        contract_version=excluded.contract_version,
                        definition_json=excluded.definition_json,
                        definition_sha256=excluded.definition_sha256,
                        updated_at=excluded.updated_at
                    WHERE connector_definitions.definition_sha256 != excluded.definition_sha256
                    """,
                    (
                        definition.connector_id,
                        definition.contract_version,
                        encoded,
                        digest,
                        now,
                    ),
                )

    def reserve_lifecycle_request(
        self,
        *,
        client_request_id: str,
        operation_kind: str,
        request_sha256: str,
        lease_seconds: int = 300,
    ) -> LifecycleRequestReservation:
        now = _utcnow()
        lease_token = "connrequest_" + uuid.uuid4().hex
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_lifecycle_requests "
                "WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO connector_lifecycle_requests(
                        client_request_id, operation_kind, request_sha256, status,
                        lease_token, lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                    """,
                    (
                        client_request_id,
                        operation_kind,
                        request_sha256,
                        lease_token,
                        _iso(expires_at),
                        _iso(now),
                        _iso(now),
                    ),
                )
                return LifecycleRequestReservation("reserved", lease_token=lease_token)
            if (
                not hmac.compare_digest(str(row["operation_kind"]), operation_kind)
                or not hmac.compare_digest(str(row["request_sha256"]), request_sha256)
            ):
                return LifecycleRequestReservation("conflict")
            status = str(row["status"])
            if status == "completed":
                decoded = json.loads(str(row["result_json"] or "{}"))
                if not isinstance(decoded, dict):
                    raise RuntimeError("connector lifecycle result is corrupt")
                return LifecycleRequestReservation("replay", result=decoded)
            if status == "failed":
                return LifecycleRequestReservation(
                    "failed",
                    error_code=str(row["error_code"] or "connector_unavailable"),
                )
            current_expiry = row["lease_expires_at"]
            if current_expiry is not None and _parse_time(str(current_expiry)) > now:
                return LifecycleRequestReservation("in_progress")
            cursor = connection.execute(
                """
                UPDATE connector_lifecycle_requests
                SET lease_token=?, lease_expires_at=?, updated_at=?
                WHERE client_request_id=? AND status='running'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    lease_token,
                    _iso(expires_at),
                    _iso(now),
                    client_request_id,
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                return LifecycleRequestReservation("in_progress")
            return LifecycleRequestReservation("reserved", lease_token=lease_token)

    def complete_lifecycle_request(
        self,
        client_request_id: str,
        lease_token: str,
        *,
        result: Mapping[str, Any],
    ) -> None:
        with self._write() as connection:
            self._complete_lifecycle_request(
                connection,
                client_request_id,
                lease_token,
                result=result,
            )

    def fail_lifecycle_request(
        self,
        client_request_id: str,
        lease_token: str,
        *,
        error_code: str,
    ) -> None:
        with self._write() as connection:
            self._fail_lifecycle_request(
                connection,
                client_request_id,
                lease_token,
                error_code=error_code,
            )

    @staticmethod
    def _complete_lifecycle_request(
        connection: sqlite3.Connection,
        client_request_id: str,
        lease_token: str,
        *,
        result: Mapping[str, Any],
    ) -> None:
        encoded = _canonical_json(dict(result))
        now = _iso(_utcnow())
        cursor = connection.execute(
            """
            UPDATE connector_lifecycle_requests
            SET status='completed', result_json=?, error_code=NULL,
                lease_token=NULL, lease_expires_at=NULL, updated_at=?
            WHERE client_request_id=? AND status='running' AND lease_token=?
            """,
            (encoded, now, client_request_id, lease_token),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT status, result_json FROM connector_lifecycle_requests "
                "WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if row is None or row["status"] != "completed" or row["result_json"] != encoded:
                raise RuntimeError("connector lifecycle request lease was lost")

    @staticmethod
    def _fail_lifecycle_request(
        connection: sqlite3.Connection,
        client_request_id: str,
        lease_token: str,
        *,
        error_code: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE connector_lifecycle_requests
            SET status='failed', result_json=NULL, error_code=?,
                lease_token=NULL, lease_expires_at=NULL, updated_at=?
            WHERE client_request_id=? AND status='running' AND lease_token=?
            """,
            (error_code, _iso(_utcnow()), client_request_id, lease_token),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT status, error_code FROM connector_lifecycle_requests "
                "WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if row is None or row["status"] != "failed" or row["error_code"] != error_code:
                raise RuntimeError("connector lifecycle request lease was lost")

    def lifecycle_request_state(self, client_request_id: str) -> Mapping[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT operation_kind, request_sha256, status, result_json, error_code
                FROM connector_lifecycle_requests WHERE client_request_id=?
                """,
                (client_request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "operation_kind": str(row["operation_kind"]),
            "request_sha256": str(row["request_sha256"]),
            "status": str(row["status"]),
            "result": (
                json.loads(str(row["result_json"]))
                if row["result_json"] is not None
                else None
            ),
            "error_code": (
                str(row["error_code"]) if row["error_code"] is not None else None
            ),
        }

    def reserve_interaction_login(
        self,
        *,
        interaction_id: str,
        connector_id: str,
        mode: Literal["connect", "reauthorize"],
        target_instance_id: str | None,
    ) -> InteractionLoginReservation:
        if mode not in {"connect", "reauthorize"}:
            raise ValueError("connector login mode is invalid")
        if (mode == "connect") != (target_instance_id is None):
            raise ValueError("connector login target is inconsistent")
        now = _utcnow()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE interaction_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (interaction_id,),
            ).fetchone()
            if row is not None:
                existing = self._interaction_login_from_row(row)
                same_identity = (
                    existing.connector_id == connector_id
                    and existing.mode == mode
                    and existing.target_instance_id == target_instance_id
                )
                if not same_identity and existing.status not in {"failed", "cancelled"}:
                    raise RuntimeError("connector login binding identity changed")
                if existing.status == "completed":
                    return InteractionLoginReservation("completed", existing)
                if existing.status in {
                    "authorization_required",
                    "reauthorization_required",
                }:
                    if not same_identity:
                        raise RuntimeError("connector login binding identity changed")
                    operation_token = "connloginlease_" + uuid.uuid4().hex
                    operation_expires = now + timedelta(seconds=120)
                    cursor = connection.execute(
                        "UPDATE connector_interaction_logins SET status='starting', "
                        "operation_token=?, operation_lease_expires_at=?, updated_at=? "
                        "WHERE interaction_id=? AND generation=? "
                        "AND status=?",
                        (
                            operation_token,
                            _iso(operation_expires),
                            _iso(now),
                            interaction_id,
                            existing.generation,
                            existing.status,
                        ),
                    )
                    if cursor.rowcount != 1:
                        return InteractionLoginReservation("in_progress", existing)
                    started = connection.execute(
                        "SELECT * FROM connector_interaction_logins "
                        "WHERE interaction_id=? AND generation=?",
                        (interaction_id, existing.generation),
                    ).fetchone()
                    return InteractionLoginReservation(
                        "reserved", self._interaction_login_from_row(started)
                    )
                if (
                    existing.status == "awaiting_callback"
                    and existing.expires_at is not None
                    and existing.expires_at > now
                ):
                    return InteractionLoginReservation("replay", existing)
                if existing.status == "starting":
                    if not same_identity:
                        raise RuntimeError("connector login binding identity changed")
                    if (
                        existing.operation_lease_expires_at is not None
                        and existing.operation_lease_expires_at > now
                    ):
                        return InteractionLoginReservation("in_progress", existing)
                    operation_token = "connloginlease_" + uuid.uuid4().hex
                    operation_expires = now + timedelta(seconds=120)
                    cursor = connection.execute(
                        "UPDATE connector_interaction_logins SET operation_token=?, "
                        "operation_lease_expires_at=?, updated_at=? "
                        "WHERE interaction_id=? AND generation=? AND status='starting' "
                        "AND (operation_lease_expires_at IS NULL "
                        "OR operation_lease_expires_at <= ?)",
                        (
                            operation_token,
                            _iso(operation_expires),
                            _iso(now),
                            interaction_id,
                            existing.generation,
                            _iso(now),
                        ),
                    )
                    if cursor.rowcount != 1:
                        return InteractionLoginReservation("in_progress", existing)
                    reclaimed = connection.execute(
                        "SELECT * FROM connector_interaction_logins "
                        "WHERE interaction_id=? AND generation=?",
                        (interaction_id, existing.generation),
                    ).fetchone()
                    return InteractionLoginReservation(
                        "reserved", self._interaction_login_from_row(reclaimed)
                    )
                if existing.status == "completing":
                    if (
                        existing.operation_lease_expires_at is not None
                        and existing.operation_lease_expires_at > now
                    ):
                        return InteractionLoginReservation("in_progress", existing)
                    connection.execute(
                        "UPDATE connector_interaction_logins SET status='failed', "
                        "last_error_code='auth_completion_interrupted', "
                        "operation_token=NULL, operation_lease_expires_at=NULL, "
                        "updated_at=? WHERE interaction_id=? AND generation=? "
                        "AND status='completing'",
                        (_iso(now), interaction_id, existing.generation),
                    )
                elif existing.status == "awaiting_callback":
                    connection.execute(
                        "UPDATE connector_interaction_logins SET status='failed', "
                        "last_error_code='auth_flow_expired', updated_at=? "
                        "WHERE interaction_id=? AND generation=? "
                        "AND status='awaiting_callback'",
                        (_iso(now), interaction_id, existing.generation),
                    )
                generation = existing.generation + 1
            else:
                generation = 0
            lifecycle_request_id = "interaction_login_" + hashlib.sha256(
                f"{interaction_id}\0{generation}".encode("utf-8")
            ).hexdigest()
            operation_token = "connloginlease_" + uuid.uuid4().hex
            operation_expires = now + timedelta(seconds=120)
            timestamp = _iso(now)
            connection.execute(
                "INSERT INTO connector_interaction_logins("
                "interaction_id, connector_id, mode, target_instance_id, "
                "generation, status, lifecycle_request_id, operation_token, "
                "operation_lease_expires_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?)",
                (
                    interaction_id,
                    connector_id,
                    mode,
                    target_instance_id,
                    generation,
                    lifecycle_request_id,
                    operation_token,
                    _iso(operation_expires),
                    timestamp,
                    timestamp,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM connector_interaction_logins "
                "WHERE interaction_id=? AND generation=?",
                (interaction_id, generation),
            ).fetchone()
            return InteractionLoginReservation(
                "reserved", self._interaction_login_from_row(updated)
            )

    def activate_interaction_login(
        self,
        interaction_id: str,
        generation: int,
        *,
        flow_id: str,
        expires_at: datetime,
        operation_token: str,
    ) -> ConnectorInteractionLoginBinding:
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE connector_interaction_logins SET status='awaiting_callback', "
                "flow_id=?, expires_at=?, operation_token=NULL, "
                "operation_lease_expires_at=NULL, updated_at=? "
                "WHERE interaction_id=? AND generation=? AND status='starting' "
                "AND operation_token=? AND operation_lease_expires_at > ?",
                (
                    flow_id,
                    _iso(expires_at),
                    _iso(_utcnow()),
                    interaction_id,
                    generation,
                    operation_token,
                    _iso(_utcnow()),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector login binding activation was lost")
            row = connection.execute(
                "SELECT * FROM connector_interaction_logins "
                "WHERE interaction_id=? AND generation=?",
                (interaction_id, generation),
            ).fetchone()
            return self._interaction_login_from_row(row)

    def fail_interaction_login(
        self,
        interaction_id: str,
        generation: int,
        *,
        error_code: str,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE connector_interaction_logins SET status='failed', "
                "last_error_code=?, operation_token=NULL, "
                "operation_lease_expires_at=NULL, updated_at=? "
                "WHERE interaction_id=? AND generation=? AND status='starting'",
                (error_code[:128], _iso(_utcnow()), interaction_id, generation),
            )

    def fail_interaction_login_by_flow(
        self,
        flow_id: str,
        *,
        operation_token: str,
        error_code: str,
    ) -> None:
        with self._write() as connection:
            connection.execute(
                "UPDATE connector_interaction_logins SET status='failed', "
                "last_error_code=?, operation_token=NULL, "
                "operation_lease_expires_at=NULL, updated_at=? "
                "WHERE flow_id=? AND status='completing' AND operation_token=?",
                (error_code[:128], _iso(_utcnow()), flow_id, operation_token),
            )

    def interaction_login_binding(
        self, interaction_id: str
    ) -> ConnectorInteractionLoginBinding | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE interaction_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (interaction_id,),
            ).fetchone()
        return None if row is None else self._interaction_login_from_row(row)

    def recover_expired_interaction_logins(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Repair expired login leases without making provider calls.

        A completed begin lifecycle already names its exact active flow, so a
        crash before the API returned can be repaired by binding that flow.
        All other expired starts/callbacks are terminalized for an explicit
        user retry; completed generations are never changed.
        """

        current = now or _utcnow()
        with self._write() as connection:
            recovered: list[str] = []
            starting_rows = connection.execute(
                "SELECT * FROM connector_interaction_logins "
                "WHERE status='starting' "
                "AND operation_lease_expires_at IS NOT NULL "
                "AND operation_lease_expires_at <= ?",
                (_iso(current),),
            ).fetchall()
            for row in starting_rows:
                lifecycle = connection.execute(
                    "SELECT status, result_json FROM connector_lifecycle_requests "
                    "WHERE client_request_id=?",
                    (str(row["lifecycle_request_id"]),),
                ).fetchone()
                flow = None
                if lifecycle is not None and lifecycle["status"] == "completed":
                    try:
                        result = json.loads(str(lifecycle["result_json"] or "{}"))
                        flow_id = str(result.get("flow_id", ""))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        flow_id = ""
                    if flow_id:
                        flow = connection.execute(
                            "SELECT * FROM connector_auth_flows "
                            "WHERE flow_id=? AND connector_id=? AND status='active' "
                            "AND expires_at > ?",
                            (flow_id, str(row["connector_id"]), _iso(current)),
                        ).fetchone()
                if flow is not None:
                    cursor = connection.execute(
                        "UPDATE connector_interaction_logins "
                        "SET status='awaiting_callback', flow_id=?, expires_at=?, "
                        "operation_token=NULL, operation_lease_expires_at=NULL, "
                        "updated_at=? WHERE interaction_id=? AND generation=? "
                        "AND status='starting' AND operation_lease_expires_at <= ?",
                        (
                            str(flow["flow_id"]),
                            str(flow["expires_at"]),
                            _iso(current),
                            str(row["interaction_id"]),
                            int(row["generation"]),
                            _iso(current),
                        ),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE connector_interaction_logins SET status='failed', "
                        "last_error_code='auth_start_interrupted', "
                        "operation_token=NULL, operation_lease_expires_at=NULL, "
                        "updated_at=? WHERE interaction_id=? AND generation=? "
                        "AND status='starting' AND operation_lease_expires_at <= ?",
                        (
                            _iso(current),
                            str(row["interaction_id"]),
                            int(row["generation"]),
                            _iso(current),
                        ),
                    )
                if cursor.rowcount == 1:
                    recovered.append(str(row["interaction_id"]))

            awaiting_rows = connection.execute(
                "SELECT interaction_id, generation FROM connector_interaction_logins "
                "WHERE status='awaiting_callback' AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (_iso(current),),
            ).fetchall()
            for row in awaiting_rows:
                cursor = connection.execute(
                    "UPDATE connector_interaction_logins SET status='failed', "
                    "last_error_code='auth_flow_expired', updated_at=? "
                    "WHERE interaction_id=? AND generation=? "
                    "AND status='awaiting_callback' AND expires_at <= ?",
                    (
                        _iso(current),
                        str(row["interaction_id"]),
                        int(row["generation"]),
                        _iso(current),
                    ),
                )
                if cursor.rowcount == 1:
                    recovered.append(str(row["interaction_id"]))

            rows = connection.execute(
                "SELECT interaction_id, generation FROM connector_interaction_logins "
                "WHERE status='completing' "
                "AND operation_lease_expires_at IS NOT NULL "
                "AND operation_lease_expires_at <= ?",
                (_iso(current),),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    "UPDATE connector_interaction_logins SET status='failed', "
                    "last_error_code='auth_completion_interrupted', "
                    "operation_token=NULL, operation_lease_expires_at=NULL, "
                    "updated_at=? WHERE interaction_id=? AND generation=? "
                    "AND status='completing' AND operation_lease_expires_at <= ?",
                    (
                        _iso(current),
                        str(row["interaction_id"]),
                        int(row["generation"]),
                        _iso(current),
                    ),
                )
                if cursor.rowcount == 1:
                    recovered.append(str(row["interaction_id"]))
            return tuple(recovered)

    @staticmethod
    def _record_auth_completion_in_transaction(
        connection: sqlite3.Connection,
        *,
        flow_id: str,
        connector_id: str,
        target_instance_id: str | None,
        completed_instance_id: str,
        now: datetime,
    ) -> None:
        binding = connection.execute(
            "SELECT * FROM connector_interaction_logins WHERE flow_id=?",
            (flow_id,),
        ).fetchone()
        # Menu-originated login flows deliberately have no Interaction binding.
        # They still produce a completion fact, while model-originated flows must
        # already be exclusively claimed by consume_flow().
        if binding is not None:
            if binding["status"] != "completing":
                raise RuntimeError("connector login binding was not claimed by callback")
            if (
                binding["connector_id"] != connector_id
                or binding["target_instance_id"] != target_instance_id
            ):
                raise RuntimeError("connector auth completion identity is inconsistent")
        connection.execute(
            "INSERT INTO connector_auth_completions("
            "flow_id, connector_id, target_instance_id, completed_instance_id, completed_at"
            ") VALUES (?, ?, ?, ?, ?) ON CONFLICT(flow_id) DO NOTHING",
            (
                flow_id,
                connector_id,
                target_instance_id,
                completed_instance_id,
                _iso(now),
            ),
        )
        completion = connection.execute(
            "SELECT connector_id, target_instance_id, completed_instance_id "
            "FROM connector_auth_completions WHERE flow_id=?",
            (flow_id,),
        ).fetchone()
        if completion is None or (
            str(completion["connector_id"]) != connector_id
            or (
                str(completion["target_instance_id"])
                if completion["target_instance_id"] is not None
                else None
            )
            != target_instance_id
            or str(completion["completed_instance_id"]) != completed_instance_id
        ):
            raise RuntimeError("connector auth completion replay identity changed")
        if binding is not None:
            cursor = connection.execute(
                "UPDATE connector_interaction_logins SET status='completed', "
                "completed_instance_id=?, operation_token=NULL, "
                "operation_lease_expires_at=NULL, updated_at=? "
                "WHERE interaction_id=? AND generation=? AND status='completing'",
                (
                    completed_instance_id,
                    _iso(now),
                    str(binding["interaction_id"]),
                    int(binding["generation"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector login completion fence was lost")

    def interaction_login_completion(
        self, interaction_id: str
    ) -> tuple[ConnectorInteractionLoginBinding, str] | None:
        with self._connection() as connection:
            row = connection.execute(
                "WITH binding AS (SELECT * FROM connector_interaction_logins "
                "WHERE interaction_id=? ORDER BY generation DESC LIMIT 1) "
                "SELECT binding.*, completion.completed_instance_id AS exact_instance "
                "FROM binding "
                "JOIN connector_auth_completions AS completion "
                "ON completion.flow_id=binding.flow_id "
                "AND completion.connector_id=binding.connector_id "
                "AND completion.target_instance_id IS binding.target_instance_id "
                "WHERE binding.status='completed'",
                (interaction_id,),
            ).fetchone()
        if row is None:
            return None
        return self._interaction_login_from_row(row), str(row["exact_instance"])

    def mark_interaction_reauthorization_required(
        self,
        interaction_id: str,
        *,
        target_instance_id: str,
        error_code: str,
    ) -> ConnectorInteractionLoginBinding:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE interaction_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("connector login completion is unavailable")
            if row["status"] == "reauthorization_required":
                existing = self._interaction_login_from_row(row)
                if existing.target_instance_id != target_instance_id:
                    raise RuntimeError("connector reauthorization target changed")
                return existing
            if row["status"] != "completed":
                raise RuntimeError("connector login completion is unavailable")
            if row["completed_instance_id"] is None or str(
                row["completed_instance_id"]
            ) != target_instance_id:
                raise RuntimeError("connector login exact completion changed")
            generation = int(row["generation"]) + 1
            lifecycle_request_id = "interaction_login_" + hashlib.sha256(
                f"{interaction_id}\0{generation}".encode("utf-8")
            ).hexdigest()
            now = _iso(_utcnow())
            connection.execute(
                "INSERT INTO connector_interaction_logins("
                "interaction_id, connector_id, mode, target_instance_id, "
                "generation, status, lifecycle_request_id, completed_instance_id, "
                "last_error_code, created_at, updated_at"
                ") VALUES (?, ?, 'reauthorize', ?, ?, "
                "'reauthorization_required', ?, ?, ?, ?, ?)",
                (
                    interaction_id,
                    str(row["connector_id"]),
                    target_instance_id,
                    generation,
                    lifecycle_request_id,
                    target_instance_id,
                    error_code[:128],
                    now,
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM connector_interaction_logins "
                "WHERE interaction_id=? AND generation=?",
                (interaction_id, generation),
            ).fetchone()
            return self._interaction_login_from_row(updated)

    def mark_interaction_connect_required(
        self,
        interaction_id: str,
        *,
        completed_instance_id: str,
        error_code: str,
    ) -> ConnectorInteractionLoginBinding:
        """Append a fresh-connect generation when the exact account vanished."""

        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE interaction_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("connector login completion is unavailable")
            if row["status"] == "authorization_required":
                return self._interaction_login_from_row(row)
            if (
                row["status"] != "completed"
                or str(row["completed_instance_id"] or "") != completed_instance_id
            ):
                raise RuntimeError("connector login exact completion changed")
            generation = int(row["generation"]) + 1
            lifecycle_request_id = "interaction_login_" + hashlib.sha256(
                f"{interaction_id}\0{generation}".encode("utf-8")
            ).hexdigest()
            now = _iso(_utcnow())
            connection.execute(
                "INSERT INTO connector_interaction_logins("
                "interaction_id, connector_id, mode, target_instance_id, generation, "
                "status, lifecycle_request_id, completed_instance_id, last_error_code, "
                "created_at, updated_at) VALUES (?, ?, 'connect', NULL, ?, "
                "'authorization_required', ?, ?, ?, ?, ?)",
                (
                    interaction_id,
                    str(row["connector_id"]),
                    generation,
                    lifecycle_request_id,
                    completed_instance_id,
                    error_code[:128],
                    now,
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM connector_interaction_logins "
                "WHERE interaction_id=? AND generation=?",
                (interaction_id, generation),
            ).fetchone()
            return self._interaction_login_from_row(updated)

    @staticmethod
    def _interaction_login_from_row(
        row: sqlite3.Row,
    ) -> ConnectorInteractionLoginBinding:
        return ConnectorInteractionLoginBinding(
            interaction_id=str(row["interaction_id"]),
            connector_id=str(row["connector_id"]),
            mode=str(row["mode"]),
            target_instance_id=(
                str(row["target_instance_id"])
                if row["target_instance_id"] is not None
                else None
            ),
            generation=int(row["generation"]),
            status=str(row["status"]),
            lifecycle_request_id=str(row["lifecycle_request_id"]),
            flow_id=str(row["flow_id"]) if row["flow_id"] is not None else None,
            completed_instance_id=(
                str(row["completed_instance_id"])
                if row["completed_instance_id"] is not None
                else None
            ),
            expires_at=(
                _parse_time(str(row["expires_at"]))
                if row["expires_at"] is not None
                else None
            ),
            operation_token=(
                str(row["operation_token"])
                if row["operation_token"] is not None
                else None
            ),
            operation_lease_expires_at=(
                _parse_time(str(row["operation_lease_expires_at"]))
                if row["operation_lease_expires_at"] is not None
                else None
            ),
            last_error_code=(
                str(row["last_error_code"])
                if row["last_error_code"] is not None
                else None
            ),
        )

    def create_preparing_flow(
        self,
        *,
        flow_id: str,
        connector_id: str,
        auth_kind: ConnectorAuthKind,
        state_sha256: str,
        private_ref: str,
        expires_at: datetime,
        operation_token: str,
        reauthorize_instance_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        now = _utcnow()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO connector_auth_flows(
                    flow_id, connector_id, auth_kind, state_sha256, private_ref, expires_at,
                    status, operation_token, operation_lease_expires_at, created_at,
                    reauthorize_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'preparing', ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    connector_id,
                    auth_kind.value,
                    state_sha256,
                    private_ref,
                    _iso(expires_at),
                    operation_token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    reauthorize_instance_id,
                ),
            )

    def flow_id_for_oauth_state(self, state: str) -> str | None:
        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT flow_id FROM connector_auth_flows
                WHERE state_sha256=? AND status='active' AND expires_at > ?
                """,
                (digest, _iso(_utcnow())),
            ).fetchone()
        return None if row is None else str(row["flow_id"])

    def activate_flow(
        self,
        flow_id: str,
        operation_token: str,
        *,
        lifecycle_request: tuple[str, str] | None = None,
        interaction_binding: tuple[str, int, str] | None = None,
    ) -> None:
        with self._write() as connection:
            now = _utcnow()
            flow = connection.execute(
                "SELECT connector_id, expires_at FROM connector_auth_flows "
                "WHERE flow_id=? AND status='preparing' AND operation_token=?",
                (flow_id, operation_token),
            ).fetchone()
            if flow is None:
                raise RuntimeError("connector auth flow could not be activated")
            cursor = connection.execute(
                """
                UPDATE connector_auth_flows
                SET status='active', operation_token=NULL,
                    operation_lease_expires_at=NULL
                WHERE flow_id=? AND status='preparing' AND operation_token=?
                  AND operation_lease_expires_at > ?
                """,
                (flow_id, operation_token, _iso(now)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector auth flow could not be activated")
            if lifecycle_request is not None:
                self._complete_lifecycle_request(
                    connection,
                    lifecycle_request[0],
                    lifecycle_request[1],
                    result={"flow_id": flow_id},
                )
            if interaction_binding is not None:
                interaction_id, generation, login_operation_token = interaction_binding
                claimed = connection.execute(
                    "UPDATE connector_interaction_logins "
                    "SET status='awaiting_callback', flow_id=?, expires_at=?, "
                    "operation_token=NULL, operation_lease_expires_at=NULL, updated_at=? "
                    "WHERE interaction_id=? AND generation=? AND status='starting' "
                    "AND connector_id=? AND operation_token=? "
                    "AND operation_lease_expires_at > ?",
                    (
                        flow_id,
                        str(flow["expires_at"]),
                        _iso(now),
                        interaction_id,
                        generation,
                        str(flow["connector_id"]),
                        login_operation_token,
                        _iso(now),
                    ),
                )
                if claimed.rowcount != 1:
                    raise RuntimeError("connector login binding activation was lost")

    def bind_active_flow_to_interaction(
        self,
        flow_id: str,
        *,
        interaction_id: str,
        generation: int,
        operation_token: str,
    ) -> None:
        """Atomically claim a replayed active flow for a reclaimed binding."""

        now = _utcnow()
        with self._write() as connection:
            flow = connection.execute(
                "SELECT connector_id, expires_at FROM connector_auth_flows "
                "WHERE flow_id=? AND status='active' AND expires_at > ?",
                (flow_id, _iso(now)),
            ).fetchone()
            if flow is None:
                raise RuntimeError("connector authorization replay is unavailable")
            cursor = connection.execute(
                "UPDATE connector_interaction_logins "
                "SET status='awaiting_callback', flow_id=?, expires_at=?, "
                "operation_token=NULL, operation_lease_expires_at=NULL, updated_at=? "
                "WHERE interaction_id=? AND generation=? AND status='starting' "
                "AND connector_id=? AND operation_token=? "
                "AND operation_lease_expires_at > ?",
                (
                    flow_id,
                    str(flow["expires_at"]),
                    _iso(now),
                    interaction_id,
                    generation,
                    str(flow["connector_id"]),
                    operation_token,
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector login replay binding was lost")

    def get_active_flow(self, flow_id: str) -> ConnectorFlowRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM connector_auth_flows
                WHERE flow_id=? AND status='active' AND expires_at > ?
                """,
                (flow_id, _iso(_utcnow())),
            ).fetchone()
        if row is None or row["private_ref"] is None:
            return None
        return ConnectorFlowRecord(
            flow_id=str(row["flow_id"]),
            connector_id=str(row["connector_id"]),
            auth_kind=ConnectorAuthKind(str(row["auth_kind"])),
            private_ref=str(row["private_ref"]),
            expires_at=_parse_time(str(row["expires_at"])),
            operation_token="",
            reauthorize_instance_id=(
                str(row["reauthorize_instance_id"])
                if row["reauthorize_instance_id"] is not None
                else None
            ),
        )

    def remove_flow(self, flow_id: str, operation_token: str) -> None:
        with self._write() as connection:
            connection.execute(
                "DELETE FROM connector_auth_flows WHERE flow_id=? AND operation_token=?",
                (flow_id, operation_token),
            )

    def consume_flow(
        self,
        flow_id: str,
        *,
        operation_token: str,
        now: datetime | None = None,
        lease_seconds: int = 120,
    ) -> FlowConsumption:
        current = now or _utcnow()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_auth_flows WHERE flow_id=?", (flow_id,)
            ).fetchone()
            if row is None or row["status"] != "active":
                return FlowConsumption(None, "unavailable")
            binding = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE flow_id=?",
                (flow_id,),
            ).fetchone()
            if binding is not None and binding["status"] != "awaiting_callback":
                return FlowConsumption(None, "unavailable")
            private_ref = str(row["private_ref"])
            expires_at = _parse_time(str(row["expires_at"]))
            if expires_at <= current:
                connection.execute(
                    """
                    UPDATE connector_auth_flows
                    SET status='expired', consumed_at=?, operation_token=?,
                        operation_lease_expires_at=?
                    WHERE flow_id=? AND status='active'
                    """,
                    (
                        _iso(current),
                        operation_token,
                        _iso(current + timedelta(seconds=lease_seconds)),
                        flow_id,
                    ),
                )
                if binding is not None:
                    connection.execute(
                        "UPDATE connector_interaction_logins SET status='failed', "
                        "last_error_code='auth_flow_expired', updated_at=? "
                        "WHERE interaction_id=? AND status='awaiting_callback'",
                        (_iso(current), str(binding["interaction_id"])),
                    )
                return FlowConsumption(None, "expired", private_ref)
            if binding is not None:
                claimed = connection.execute(
                    "UPDATE connector_interaction_logins SET status='completing', "
                    "operation_token=?, operation_lease_expires_at=?, updated_at=? "
                    "WHERE interaction_id=? AND generation=? "
                    "AND status='awaiting_callback'",
                    (
                        operation_token,
                        _iso(current + timedelta(seconds=lease_seconds)),
                        _iso(current),
                        str(binding["interaction_id"]),
                        int(binding["generation"]),
                    ),
                )
                if claimed.rowcount != 1:
                    return FlowConsumption(None, "unavailable")
            cursor = connection.execute(
                """
                UPDATE connector_auth_flows
                SET status='consumed', consumed_at=?, operation_token=?,
                    operation_lease_expires_at=?
                WHERE flow_id=? AND status='active'
                """,
                (
                    _iso(current),
                    operation_token,
                    _iso(current + timedelta(seconds=lease_seconds)),
                    flow_id,
                ),
            )
            if cursor.rowcount != 1:
                return FlowConsumption(None, "unavailable")
            return FlowConsumption(
                ConnectorFlowRecord(
                    flow_id=str(row["flow_id"]),
                    connector_id=str(row["connector_id"]),
                    auth_kind=ConnectorAuthKind(str(row["auth_kind"])),
                    private_ref=private_ref,
                    expires_at=expires_at,
                    operation_token=operation_token,
                    reauthorize_instance_id=(
                        str(row["reauthorize_instance_id"])
                        if row["reauthorize_instance_id"] is not None
                        else None
                    ),
                ),
                "consumed",
                private_ref,
            )

    def cancel_interaction_login(
        self,
        interaction_id: str,
    ) -> tuple[str, str, str] | None:
        """Fence a bound auth flow before vault cleanup and interaction resolve."""

        now = _utcnow()
        operation_token = "connflowcancel_" + uuid.uuid4().hex
        with self._write() as connection:
            binding = connection.execute(
                "SELECT * FROM connector_interaction_logins WHERE interaction_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (interaction_id,),
            ).fetchone()
            if binding is None:
                return None
            if binding["status"] == "cancelled":
                return None
            if binding["status"] in {"completing", "completed"}:
                raise RuntimeError("connector login completion already started")
            connection.execute(
                "UPDATE connector_interaction_logins SET status='cancelled', "
                "operation_token=NULL, operation_lease_expires_at=NULL, "
                "updated_at=? WHERE interaction_id=? AND generation=?",
                (_iso(now), interaction_id, int(binding["generation"])),
            )
            flow_id = binding["flow_id"]
            if flow_id is None:
                return None
            flow = connection.execute(
                "SELECT * FROM connector_auth_flows WHERE flow_id=?",
                (str(flow_id),),
            ).fetchone()
            if flow is None or flow["status"] != "active" or flow["private_ref"] is None:
                return None
            cursor = connection.execute(
                "UPDATE connector_auth_flows SET status='consumed', consumed_at=?, "
                "operation_token=?, operation_lease_expires_at=? "
                "WHERE flow_id=? AND status='active'",
                (
                    _iso(now),
                    operation_token,
                    _iso(now + timedelta(seconds=120)),
                    str(flow_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector auth flow cancellation lost its fence")
            return str(flow_id), str(flow["private_ref"]), operation_token

    def cancel_auth_flow(self, flow_id: str) -> tuple[str, str, str] | None:
        """Fence an active flow whose Interaction handoff was cancelled/lost."""

        now = _utcnow()
        operation_token = "connflowcancel_" + uuid.uuid4().hex
        with self._write() as connection:
            flow = connection.execute(
                "SELECT * FROM connector_auth_flows WHERE flow_id=?",
                (flow_id,),
            ).fetchone()
            if (
                flow is None
                or flow["status"] != "active"
                or flow["private_ref"] is None
            ):
                return None
            cursor = connection.execute(
                "UPDATE connector_auth_flows SET status='consumed', consumed_at=?, "
                "operation_token=?, operation_lease_expires_at=? "
                "WHERE flow_id=? AND status='active'",
                (
                    _iso(now),
                    operation_token,
                    _iso(now + timedelta(seconds=120)),
                    flow_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return flow_id, str(flow["private_ref"]), operation_token

    def finalize_flow_cleanup(self, flow_id: str, operation_token: str) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_auth_flows
                SET private_ref=NULL, operation_token=NULL,
                    operation_lease_expires_at=NULL
                WHERE flow_id=? AND operation_token=?
                """,
                (flow_id, operation_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector auth cleanup lease was lost")

    def renew_consumed_flow(
        self,
        flow_id: str,
        operation_token: str,
        *,
        lease_seconds: int = 300,
    ) -> bool:
        now = _utcnow()
        expires = _iso(now + timedelta(seconds=lease_seconds))
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE connector_auth_flows SET operation_lease_expires_at=? "
                "WHERE flow_id=? AND status='consumed' AND operation_token=?",
                (expires, flow_id, operation_token),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "UPDATE connector_interaction_logins "
                "SET operation_lease_expires_at=?, updated_at=? "
                "WHERE flow_id=? AND status='completing' AND operation_token=?",
                (expires, _iso(now), flow_id, operation_token),
            )
            return True

    def insert_pending_instance(
        self,
        instance: ConnectorInstance,
        *,
        transition_token: str,
        lease_seconds: int = 30,
    ) -> None:
        now = _utcnow()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO connector_runtime_instances(
                    instance_id, connector_id, account_subject, account_display_name,
                    credential_ref, granted_scopes_json, health, enabled, lifecycle,
                    transition_token, transition_lease_expires_at,
                    created_at, updated_at, last_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    instance.instance_id,
                    instance.connector_id,
                    instance.account_subject,
                    instance.account_display_name,
                    instance.credential_ref,
                    _canonical_json(sorted(instance.granted_scopes)),
                    ConnectorHealth.AUTHENTICATING.value,
                    int(instance.enabled),
                    transition_token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(instance.created_at),
                    _iso(instance.updated_at),
                    instance.last_error_code,
                ),
            )

    def activate_instance(
        self,
        instance_id: str,
        transition_token: str,
        *,
        auth_flow_id: str | None = None,
        auth_connector_id: str | None = None,
    ) -> ConnectorInstance:
        if (auth_flow_id is None) != (auth_connector_id is None):
            raise ValueError("connector auth completion identity is incomplete")
        current = _utcnow()
        now = _iso(current)
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET lifecycle='active', health=?, updated_at=?,
                    transition_token=NULL, transition_lease_expires_at=NULL
                WHERE instance_id=? AND lifecycle='pending' AND transition_token=?
                  AND transition_lease_expires_at > ?
                """,
                (
                    ConnectorHealth.CONNECTED.value,
                    now,
                    instance_id,
                    transition_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector instance could not be activated")
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            self._append_outbox(
                connection,
                event_type="connector.instance.connected",
                aggregate_id=instance_id,
                payload={
                    "instance_id": instance_id,
                    "connector_id": str(row["connector_id"]),
                    "health": ConnectorHealth.CONNECTED.value,
                },
            )
            if auth_flow_id is not None:
                self._record_auth_completion_in_transaction(
                    connection,
                    flow_id=auth_flow_id,
                    connector_id=str(auth_connector_id),
                    target_instance_id=None,
                    completed_instance_id=instance_id,
                    now=current,
                )
            return self._instance_from_row(row)

    def remove_pending_instance(self, instance_id: str, transition_token: str) -> None:
        with self._write() as connection:
            connection.execute(
                """
                DELETE FROM connector_runtime_instances
                WHERE instance_id=? AND lifecycle='pending' AND transition_token=?
                """,
                (instance_id, transition_token),
            )

    def list_instances(self) -> tuple[ConnectorInstance, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM connector_runtime_instances
                WHERE lifecycle != 'pending'
                ORDER BY created_at, instance_id
                """
            ).fetchall()
        return tuple(self._instance_from_row(row) for row in rows)

    def get_instance(
        self,
        instance_id: str,
        *,
        include_transitional: bool = False,
    ) -> ConnectorInstance | None:
        sql = "SELECT * FROM connector_runtime_instances WHERE instance_id=?"
        params: tuple[Any, ...] = (instance_id,)
        if not include_transitional:
            sql += " AND lifecycle='active'"
        with self._connection() as connection:
            row = connection.execute(sql, params).fetchone()
        return None if row is None else self._instance_from_row(row)

    def get_instance_state(
        self, instance_id: str
    ) -> tuple[ConnectorInstance, str] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._instance_from_row(row), str(row["lifecycle"])

    def list_disconnect_recovery_candidates(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Return disconnect sagas that may be claimed by maintenance.

        ``draining`` has not crossed the provider side-effect boundary yet.
        ``revoking`` is eligible only after its durable claim expires, so two
        Runtime processes cannot concurrently repeat provider revocation.
        """

        current = now or _utcnow()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT instance_id, lifecycle
                FROM connector_runtime_instances
                WHERE lifecycle='draining'
                   OR (
                       lifecycle='revoking'
                       AND (
                           transition_lease_expires_at IS NULL
                           OR transition_lease_expires_at <= ?
                       )
                   )
                ORDER BY updated_at, instance_id
                """,
                (_iso(current),),
            ).fetchall()
        return tuple(
            (str(row["instance_id"]), str(row["lifecycle"])) for row in rows
        )

    def prepare_reauthorization(
        self,
        *,
        transition_id: str,
        instance_id: str,
        new_credential_ref: str,
        operation_token: str,
        lease_seconds: int = 300,
    ) -> ConnectorInstance:
        now = _utcnow()
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT * FROM connector_runtime_instances
                WHERE instance_id=? AND lifecycle='active' AND enabled=1
                """,
                (instance_id,),
            ).fetchone()
            if row is None:
                raise KeyError(instance_id)
            connection.execute(
                """
                INSERT INTO connector_vault_transitions(
                    transition_id, instance_id, old_credential_ref,
                    new_credential_ref, status, operation_token,
                    operation_lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'preparing', ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    instance_id,
                    str(row["credential_ref"]),
                    new_credential_ref,
                    operation_token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    _iso(now),
                ),
            )
            return self._instance_from_row(row)

    def commit_reauthorization(
        self,
        transition_id: str,
        operation_token: str,
        *,
        account_subject: str,
        account_display_name: str,
        granted_scopes: frozenset[str],
        lease_seconds: int = 300,
        auth_flow_id: str | None = None,
        auth_connector_id: str | None = None,
    ) -> ConnectorInstance:
        if (auth_flow_id is None) != (auth_connector_id is None):
            raise ValueError("connector auth completion identity is incomplete")
        now = _utcnow()
        with self._write() as connection:
            transition = connection.execute(
                """
                SELECT * FROM connector_vault_transitions
                WHERE transition_id=? AND status='preparing'
                  AND operation_token=? AND operation_lease_expires_at > ?
                """,
                (transition_id, operation_token, _iso(now)),
            ).fetchone()
            if transition is None:
                raise RuntimeError("connector reauthorization lease was lost")
            instance = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (str(transition["instance_id"]),),
            ).fetchone()
            if (
                instance is None
                or instance["lifecycle"] != "active"
                or not bool(instance["enabled"])
                or instance["credential_ref"] != transition["old_credential_ref"]
                or instance["account_subject"] != account_subject
            ):
                raise RuntimeError("connector instance changed during reauthorization")
            self._mark_expired_operation_leases_unknown(connection, now=now)
            if connection.execute(
                "SELECT 1 FROM connector_operation_leases "
                "WHERE instance_id=? LIMIT 1",
                (str(transition["instance_id"]),),
            ).fetchone() is not None:
                raise RuntimeError(
                    "connector instance still has unreconciled operations"
                )
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET credential_ref=?, account_display_name=?, granted_scopes_json=?,
                    health=?, last_error_code='credential_cleanup_pending', updated_at=?
                WHERE instance_id=? AND lifecycle='active' AND enabled=1
                  AND credential_ref=? AND account_subject=?
                """,
                (
                    str(transition["new_credential_ref"]),
                    account_display_name,
                    _canonical_json(sorted(granted_scopes)),
                    ConnectorHealth.DEGRADED.value,
                    _iso(now),
                    str(transition["instance_id"]),
                    str(transition["old_credential_ref"]),
                    account_subject,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector instance reauthorization fence was lost")
            connection.execute(
                """
                UPDATE connector_vault_transitions
                SET status='swapped', operation_lease_expires_at=?, updated_at=?
                WHERE transition_id=? AND operation_token=?
                """,
                (
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    transition_id,
                    operation_token,
                ),
            )
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (str(transition["instance_id"]),),
            ).fetchone()
            self._append_outbox(
                connection,
                event_type="connector.instance.reauthorized",
                aggregate_id=str(transition["instance_id"]),
                payload={
                    "instance_id": str(transition["instance_id"]),
                    "connector_id": str(row["connector_id"]),
                    "health": ConnectorHealth.DEGRADED.value,
                },
            )
            if auth_flow_id is not None:
                self._record_auth_completion_in_transaction(
                    connection,
                    flow_id=auth_flow_id,
                    connector_id=str(auth_connector_id),
                    target_instance_id=str(transition["instance_id"]),
                    completed_instance_id=str(transition["instance_id"]),
                    now=now,
                )
            return self._instance_from_row(row)

    def finalize_reauthorization(
        self,
        transition_id: str,
        operation_token: str,
    ) -> ConnectorInstance:
        with self._write() as connection:
            transition = connection.execute(
                """
                SELECT * FROM connector_vault_transitions
                WHERE transition_id=? AND status='swapped' AND operation_token=?
                """,
                (transition_id, operation_token),
            ).fetchone()
            if transition is None:
                raise RuntimeError("connector reauthorization cleanup lease was lost")
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET health=?, last_error_code=NULL, updated_at=?
                WHERE instance_id=? AND credential_ref=? AND lifecycle='active'
                """,
                (
                    ConnectorHealth.CONNECTED.value,
                    _iso(_utcnow()),
                    str(transition["instance_id"]),
                    str(transition["new_credential_ref"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector reauthorization cleanup fence was lost")
            connection.execute(
                "DELETE FROM connector_vault_transitions "
                "WHERE transition_id=? AND operation_token=?",
                (transition_id, operation_token),
            )
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (str(transition["instance_id"]),),
            ).fetchone()
            return self._instance_from_row(row)

    def cancel_reauthorization(self, transition_id: str, operation_token: str) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                """
                DELETE FROM connector_vault_transitions
                WHERE transition_id=? AND status='preparing' AND operation_token=?
                """,
                (transition_id, operation_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector reauthorization cancellation fence was lost")

    def has_pending_reauthorization(self, instance_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM connector_vault_transitions WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
        return row is not None

    def claim_reauthorization_recovery(
        self,
        *,
        now: datetime | None = None,
        lease_seconds: int = 30,
    ) -> tuple[ReauthorizationRecovery, ...]:
        current = now or _utcnow()
        recovery_token = "connreauthrecover_" + uuid.uuid4().hex
        with self._write() as connection:
            rows = connection.execute(
                """
                SELECT * FROM connector_vault_transitions
                WHERE operation_lease_expires_at <= ?
                ORDER BY created_at, transition_id
                """,
                (_iso(current),),
            ).fetchall()
            claimed: list[sqlite3.Row] = []
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE connector_vault_transitions
                    SET operation_token=?, operation_lease_expires_at=?, updated_at=?
                    WHERE transition_id=? AND operation_lease_expires_at <= ?
                    """,
                    (
                        recovery_token,
                        _iso(current + timedelta(seconds=lease_seconds)),
                        _iso(current),
                        str(row["transition_id"]),
                        _iso(current),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append(row)
        return tuple(
            ReauthorizationRecovery(
                transition_id=str(row["transition_id"]),
                instance_id=str(row["instance_id"]),
                status=str(row["status"]),  # type: ignore[arg-type]
                cleanup_ref=str(
                    row["new_credential_ref"]
                    if row["status"] == "preparing"
                    else row["old_credential_ref"]
                ),
                recovery_token=recovery_token,
            )
            for row in claimed
        )

    def claim_disconnect_cleanup(
        self,
        instance_id: str,
        *,
        lease_seconds: int = 30,
    ) -> tuple[ConnectorInstance, str] | None:
        now = _utcnow()
        token = "conndisconnect_" + uuid.uuid4().hex
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET transition_token=?, transition_lease_expires_at=?
                WHERE instance_id=? AND lifecycle='disconnecting'
                  AND transition_lease_expires_at <= ?
                """,
                (
                    token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    instance_id,
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            return self._instance_from_row(row), token

    def update_health(
        self,
        instance_id: str,
        *,
        health: ConnectorHealth,
        last_error_code: str | None,
        operation_lease: ConnectorOperationLease,
        lifecycle_request: tuple[str, str] | None = None,
        lifecycle_failure_code: str | None = None,
    ) -> ConnectorInstance:
        now = _iso(_utcnow())
        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET health=?, last_error_code=?, updated_at=?
                WHERE instance_id=? AND lifecycle IN ('active', 'draining')
                """,
                (health.value, last_error_code, now, instance_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(instance_id)
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            self._append_outbox(
                connection,
                event_type="connector.instance.health_changed",
                aggregate_id=instance_id,
                payload={
                    "instance_id": instance_id,
                    "connector_id": str(row["connector_id"]),
                    "health": health.value,
                    "error_code": last_error_code,
                },
            )
            if lifecycle_request is not None:
                if lifecycle_failure_code is None:
                    self._complete_lifecycle_request(
                        connection,
                        lifecycle_request[0],
                        lifecycle_request[1],
                        result={"instance_id": instance_id},
                    )
                else:
                    self._fail_lifecycle_request(
                        connection,
                        lifecycle_request[0],
                        lifecycle_request[1],
                        error_code=lifecycle_failure_code,
                    )
            return self._instance_from_row(row)

    def begin_draining(self, instance_id: str) -> ConnectorInstance | None:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
            if row is None:
                return None
            if row["lifecycle"] == "active":
                connection.execute(
                    """
                    UPDATE connector_runtime_instances
                    SET lifecycle='draining', enabled=0, health=?,
                        last_error_code='disconnect_draining', updated_at=?
                    WHERE instance_id=? AND lifecycle='active'
                    """,
                    (ConnectorHealth.DISABLED.value, _iso(_utcnow()), instance_id),
                )
                row = connection.execute(
                    "SELECT * FROM connector_runtime_instances WHERE instance_id=?", (instance_id,)
                ).fetchone()
            return self._instance_from_row(row)

    def claim_revocation(
        self,
        instance_id: str,
        *,
        lease_seconds: int = 300,
    ) -> tuple[ConnectorInstance, str] | None:
        """Own the provider-revocation boundary with a durable lease."""

        if lease_seconds <= 0:
            raise ValueError("connector revocation lease must be positive")
        now = _utcnow()
        transition_token = "connrevoke_" + uuid.uuid4().hex
        with self._write() as connection:
            self._mark_expired_operation_leases_unknown(connection, now=now)
            live = connection.execute(
                """
                SELECT 1 FROM connector_operation_leases
                WHERE instance_id=? LIMIT 1
                """,
                (instance_id,),
            ).fetchone()
            if live is not None:
                raise RuntimeError("connector instance still has active operations")
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET lifecycle='revoking', enabled=0, health=?,
                    transition_token=?, transition_lease_expires_at=?,
                    last_error_code='remote_revocation_pending', updated_at=?
                WHERE instance_id=? AND (
                    lifecycle='draining'
                    OR (
                        lifecycle='revoking'
                        AND (
                            transition_lease_expires_at IS NULL
                            OR transition_lease_expires_at <= ?
                        )
                    )
                )
                """,
                (
                    ConnectorHealth.DISABLED.value,
                    transition_token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    instance_id,
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT 1 FROM connector_runtime_instances WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(instance_id)
                return None
            row = connection.execute(
                "SELECT * FROM connector_runtime_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            return self._instance_from_row(row), transition_token

    def mark_revoking(self, instance_id: str) -> ConnectorInstance:
        """Compatibility helper used by recovery fixtures.

        Product code uses :meth:`claim_revocation` and retains its fencing
        token across the provider call.
        """

        claimed = self.claim_revocation(instance_id)
        if claimed is None:
            raise RuntimeError("connector revocation is already claimed")
        return claimed[0]

    def mark_revocation_uncertain(
        self,
        instance_id: str,
        *,
        transition_token: str,
        lifecycle_request: tuple[str, str] | None = None,
    ) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET lifecycle='revoking', enabled=0, health=?,
                    transition_lease_expires_at=?,
                    last_error_code='remote_revocation_uncertain', updated_at=?
                WHERE instance_id=? AND lifecycle='revoking'
                  AND transition_token=?
                """,
                (
                    ConnectorHealth.ERROR.value,
                    _iso(_utcnow()),
                    _iso(_utcnow()),
                    instance_id,
                    transition_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector revocation fence was lost")
            if lifecycle_request is not None:
                self._fail_lifecycle_request(
                    connection,
                    lifecycle_request[0],
                    lifecycle_request[1],
                    error_code="connector_invocation_uncertain",
                )

    def mark_remote_revoked(
        self,
        instance_id: str,
        *,
        transition_token: str,
        lease_seconds: int = 30,
    ) -> None:
        now = _utcnow()
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_runtime_instances
                SET lifecycle='disconnecting',
                    transition_lease_expires_at=?,
                    last_error_code='credential_cleanup_pending', updated_at=?
                WHERE instance_id=? AND lifecycle='revoking'
                  AND transition_token=?
                """,
                (
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    instance_id,
                    transition_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector revocation fence was lost")

    def finalize_disconnect(
        self,
        instance_id: str,
        transition_token: str,
        *,
        lifecycle_request: tuple[str, str] | None = None,
    ) -> None:
        with self._write() as connection:
            row = connection.execute(
                "SELECT connector_id FROM connector_runtime_instances "
                "WHERE instance_id=? AND lifecycle='disconnecting' AND transition_token=?",
                (instance_id, transition_token),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                DELETE FROM connector_runtime_instances
                WHERE instance_id=? AND transition_token=?
                """,
                (instance_id, transition_token),
            )
            self._append_outbox(
                connection,
                event_type="connector.instance.disconnected",
                aggregate_id=instance_id,
                payload={
                    "instance_id": instance_id,
                    "connector_id": str(row["connector_id"]),
                },
            )
            if lifecycle_request is not None:
                self._complete_lifecycle_request(
                    connection,
                    lifecycle_request[0],
                    lifecycle_request[1],
                    result={"instance_id": instance_id, "disconnected": True},
                )

    def recovery_references(self, *, now: datetime | None = None) -> tuple[RecoveryReference, ...]:
        current = now or _utcnow()
        recovery_token = "connrecover_" + uuid.uuid4().hex
        recovery_expires = _iso(current + timedelta(seconds=30))
        with self._write() as connection:
            connection.execute(
                """
                UPDATE connector_auth_flows
                SET status='expired', consumed_at=?, operation_token=NULL,
                    operation_lease_expires_at=?
                WHERE status='active' AND expires_at <= ?
                """,
                (_iso(current), _iso(current), _iso(current)),
            )
            flow_rows = connection.execute(
                """
                SELECT flow_id, private_ref FROM connector_auth_flows
                WHERE private_ref IS NOT NULL
                  AND status IN ('preparing', 'consumed', 'expired')
                  AND operation_lease_expires_at <= ?
                """
                , (_iso(current),)
            ).fetchall()
            claimed_flows = []
            for row in flow_rows:
                cursor = connection.execute(
                    """
                    UPDATE connector_auth_flows
                    SET operation_token=?, operation_lease_expires_at=?
                    WHERE flow_id=? AND operation_lease_expires_at <= ?
                    """,
                    (
                        recovery_token,
                        recovery_expires,
                        str(row["flow_id"]),
                        _iso(current),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_flows.append(row)
            instance_rows = connection.execute(
                """
                SELECT instance_id, credential_ref, lifecycle
                FROM connector_runtime_instances
                WHERE lifecycle IN ('pending', 'disconnecting')
                  AND transition_lease_expires_at <= ?
                """
                , (_iso(current),)
            ).fetchall()
            claimed_instances = []
            for row in instance_rows:
                cursor = connection.execute(
                    """
                    UPDATE connector_runtime_instances
                    SET transition_token=?, transition_lease_expires_at=?
                    WHERE instance_id=? AND transition_lease_expires_at <= ?
                    """,
                    (
                        recovery_token,
                        recovery_expires,
                        str(row["instance_id"]),
                        _iso(current),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_instances.append(row)
        recovered = [
            RecoveryReference(
                "flow", str(row["flow_id"]), str(row["private_ref"]), recovery_token
            )
            for row in claimed_flows
        ]
        recovered.extend(
            RecoveryReference(
                "pending_instance" if row["lifecycle"] == "pending" else "disconnecting_instance",
                str(row["instance_id"]),
                str(row["credential_ref"]),
                recovery_token,
            )
            for row in claimed_instances
        )
        return tuple(recovered)

    def acquire_instance_operation(
        self,
        instance_id: str,
        *,
        operation_kind: str,
        lease_seconds: int = 30,
        operation_id: str | None = None,
        lease_token: str | None = None,
        uncertainty_policy: Literal[
            "auto_release", "manual_reconcile"
        ] = "manual_reconcile",
    ) -> tuple[ConnectorInstance, ConnectorOperationLease] | None:
        now = _utcnow()
        operation_id = operation_id or ("connop_" + uuid.uuid4().hex)
        lease_token = lease_token or ("connlease_" + uuid.uuid4().hex)
        if not operation_id.startswith("connop_") or not lease_token.startswith(
            "connlease_"
        ):
            raise ValueError("connector operation lease identity is invalid")
        if uncertainty_policy not in {"auto_release", "manual_reconcile"}:
            raise ValueError("connector uncertainty policy is invalid")
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            self._mark_expired_operation_leases_unknown(connection, now=now)
            row = connection.execute(
                """
                SELECT * FROM connector_runtime_instances
                WHERE instance_id=? AND lifecycle='active' AND enabled=1
                  AND NOT EXISTS (
                      SELECT 1 FROM connector_vault_transitions AS transition
                      WHERE transition.instance_id=connector_runtime_instances.instance_id
                        AND transition.status IN ('preparing', 'swapped')
                  )
                """,
                (instance_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                INSERT INTO connector_operation_leases(
                    operation_id, instance_id, lease_token, operation_kind,
                    uncertainty_policy, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    instance_id,
                    lease_token,
                    operation_kind,
                    uncertainty_policy,
                    _iso(expires_at),
                    _iso(now),
                ),
            )
            return self._instance_from_row(row), ConnectorOperationLease(
                operation_id=operation_id,
                instance_id=instance_id,
                lease_token=lease_token,
                expires_at=expires_at,
            )

    def renew_instance_operation(
        self,
        lease: ConnectorOperationLease,
        *,
        lease_seconds: int = 30,
    ) -> bool:
        now = _utcnow()
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_operation_leases SET expires_at=?
                WHERE operation_id=? AND instance_id=? AND lease_token=?
                  AND status='active' AND expires_at > ?
                """,
                (
                    _iso(now + timedelta(seconds=lease_seconds)),
                    lease.operation_id,
                    lease.instance_id,
                    lease.lease_token,
                    _iso(now),
                ),
            )
            return cursor.rowcount == 1

    def release_instance_operation(self, lease: ConnectorOperationLease) -> None:
        with self._write() as connection:
            connection.execute(
                """
                DELETE FROM connector_operation_leases
                WHERE operation_id=? AND instance_id=? AND lease_token=?
                """,
                (lease.operation_id, lease.instance_id, lease.lease_token),
            )

    def has_live_instance_operations(self, instance_id: str) -> bool:
        with self._write() as connection:
            self._mark_expired_operation_leases_unknown(connection)
            return connection.execute(
                "SELECT 1 FROM connector_operation_leases WHERE instance_id=? LIMIT 1",
                (instance_id,),
            ).fetchone() is not None

    def _assert_operation_lease(
        self,
        connection: sqlite3.Connection,
        lease: ConnectorOperationLease,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM connector_operation_leases
            WHERE operation_id=? AND instance_id=? AND lease_token=?
              AND status='active' AND expires_at > ?
            """,
            (
                lease.operation_id,
                lease.instance_id,
                lease.lease_token,
                _iso(_utcnow()),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("connector operation lease was lost")

    def _mark_expired_operation_leases_unknown(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or _utcnow()
        connection.execute(
            "DELETE FROM connector_operation_leases "
            "WHERE status='active' AND expires_at <= ? "
            "AND EXISTS (SELECT 1 FROM connector_invocations AS invocation "
            "WHERE invocation.operation_id=connector_operation_leases.operation_id "
            "AND invocation.status='completed')",
            (_iso(current),),
        )
        auto_linked = connection.execute(
            "SELECT lease.operation_id, invocation.* "
            "FROM connector_operation_leases AS lease "
            "JOIN connector_invocations AS invocation "
            "ON invocation.operation_id=lease.operation_id "
            "WHERE lease.status='active' AND lease.expires_at <= ? "
            "AND lease.uncertainty_policy='auto_release' "
            "AND invocation.status='running' "
            "AND NOT EXISTS (SELECT 1 FROM connector_result_staging AS stage "
            "WHERE stage.invocation_id=invocation.invocation_id "
            "AND stage.status='staged')",
            (_iso(current),),
        ).fetchall()
        for row in auto_linked:
            connection.execute(
                "DELETE FROM connector_idempotency WHERE invocation_id=? "
                "AND status='running'",
                (str(row["invocation_id"]),),
            )
            connection.execute(
                "UPDATE connector_invocations SET status='completed', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_iso(current), str(row["invocation_id"])),
            )
        connection.execute(
            "DELETE FROM connector_operation_leases "
            "WHERE status='active' AND expires_at <= ? "
            "AND uncertainty_policy='auto_release' "
            "AND NOT EXISTS (SELECT 1 FROM connector_result_staging AS stage "
            "WHERE stage.operation_id=connector_operation_leases.operation_id "
            "AND stage.status='staged')",
            (_iso(current),),
        )
        linked = connection.execute(
            "SELECT lease.operation_id, invocation.* "
            "FROM connector_operation_leases AS lease "
            "JOIN connector_invocations AS invocation "
            "ON invocation.operation_id=lease.operation_id "
            "WHERE lease.status='active' AND lease.expires_at <= ? "
            "AND lease.uncertainty_policy='manual_reconcile' "
            "AND invocation.status='running' "
            "AND NOT EXISTS (SELECT 1 FROM connector_result_staging AS stage "
            "WHERE stage.invocation_id=invocation.invocation_id "
            "AND stage.status='staged')",
            (_iso(current),),
        ).fetchall()
        connection.execute(
            """
            UPDATE connector_operation_leases SET status='outcome_unknown'
            WHERE status='active' AND expires_at <= ?
              AND uncertainty_policy='manual_reconcile'
              AND NOT EXISTS (
                  SELECT 1 FROM connector_result_staging AS stage
                  WHERE stage.operation_id=connector_operation_leases.operation_id
                    AND stage.status='staged'
              )
            """,
            (_iso(current),),
        )
        for row in linked:
            changed = connection.execute(
                "UPDATE connector_invocations SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_iso(current), str(row["invocation_id"])),
            )
            connection.execute(
                "UPDATE connector_idempotency SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_iso(current), str(row["invocation_id"])),
            )
            if changed.rowcount != 1:
                continue
            started = connection.execute(
                "SELECT payload_json FROM connector_outbox "
                "WHERE aggregate_id=? AND event_type='connector.invocation.started' "
                "ORDER BY aggregate_seq DESC LIMIT 1",
                (str(row["invocation_id"]),),
            ).fetchone()
            runtime_payload: dict[str, Any] = {}
            if started is not None:
                try:
                    started_payload = json.loads(str(started["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    started_payload = {}
                if isinstance(started_payload.get("runtime"), dict):
                    runtime_payload["runtime"] = started_payload["runtime"]
            self._append_outbox(
                connection,
                event_type="connector.invocation.outcome_unknown",
                aggregate_id=str(row["invocation_id"]),
                payload={
                    "invocation_id": str(row["invocation_id"]),
                    "instance_id": str(row["instance_id"]),
                    "connector_id": str(row["connector_id"]),
                    "action_id": str(row["action_id"]),
                    "operation_id": str(row["operation_id"]),
                    "status": "outcome_unknown",
                    "reason": "operation_lease_expired",
                    **runtime_payload,
                },
            )

    def recover_expired_operation_leases(self) -> None:
        """Promote expired leases from an explicit recovery boundary.

        Public GET projections intentionally never call this method. Runtime
        startup and the lifecycle-managed maintenance loop own expiry writes,
        idempotency transitions, and outcome-unknown outbox facts.
        """

        with self._write() as connection:
            self._mark_expired_operation_leases_unknown(connection)

    def uncertain_operation_ids(self, instance_id: str) -> tuple[str, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT operation_id FROM connector_operation_leases
                WHERE instance_id=? AND status='outcome_unknown'
                ORDER BY created_at, operation_id
                """,
                (instance_id,),
            ).fetchall()
        return tuple(str(row["operation_id"]) for row in rows)

    def resolve_uncertain_operation(
        self,
        instance_id: str,
        operation_id: str,
        *,
        resolution: Literal["manually_reconciled", "confirmed_not_executed"],
    ) -> None:
        if resolution not in {"manually_reconciled", "confirmed_not_executed"}:
            raise ValueError("invalid connector operation resolution")
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM connector_invocations WHERE operation_id=?",
                (operation_id,),
            ).fetchone() is not None:
                raise RuntimeError(
                    "connector invocation operations require invocation reconciliation"
                )
            cursor = connection.execute(
                """
                DELETE FROM connector_operation_leases
                WHERE instance_id=? AND operation_id=? AND status='outcome_unknown'
                """,
                (instance_id, operation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(operation_id)
            self._append_outbox(
                connection,
                event_type="connector.operation.reconciled",
                aggregate_id=instance_id,
                payload={
                    "instance_id": instance_id,
                    "operation_id": operation_id,
                    "resolution": resolution,
                },
            )

    def refresh_invocation_admission(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        admission_policy_sha256: str,
        denied: bool,
    ) -> None:
        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            cursor = connection.execute(
                "UPDATE connector_invocations SET admission_policy_sha256=?, "
                "updated_at=? WHERE invocation_id=? AND status='running' "
                "AND operation_id=?",
                (
                    admission_policy_sha256,
                    _iso(_utcnow()),
                    record.invocation_id,
                    operation_lease.operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector invocation admission fact was lost")
            if not denied:
                return
            connection.execute(
                "DELETE FROM connector_idempotency WHERE invocation_id=? "
                "AND status='running'",
                (record.invocation_id,),
            )
            connection.execute(
                "UPDATE connector_invocations SET status='completed', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_iso(_utcnow()), record.invocation_id),
            )
            self._append_outbox(
                connection,
                event_type="connector.invocation.denied",
                aggregate_id=record.invocation_id,
                payload={
                    "invocation_id": record.invocation_id,
                    "instance_id": record.instance_id,
                    "connector_id": record.connector_id,
                    "action_id": record.action_id,
                    "admission_policy_sha256": admission_policy_sha256,
                    "status": "denied",
                    **self._runtime_context_payload(record.runtime_context),
                },
            )
    def abort_invocation_before_dispatch(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
    ) -> None:
        """Release an invocation reservation proven not sent to the provider."""

        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            connection.execute(
                "DELETE FROM connector_idempotency WHERE invocation_id=? "
                "AND status='running'",
                (record.invocation_id,),
            )
            cursor = connection.execute(
                "UPDATE connector_invocations SET status='completed', updated_at=? "
                "WHERE invocation_id=? AND status='running' AND operation_id=?",
                (
                    _iso(_utcnow()),
                    record.invocation_id,
                    operation_lease.operation_id,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM connector_invocations WHERE invocation_id=?",
                    (record.invocation_id,),
                ).fetchone()
                if row is None or row["status"] != "completed":
                    raise RuntimeError("connector invocation abort fence was lost")

    def reserve_invocation(
        self,
        record: ConnectorInvocationRecord,
        *,
        operation_lease: ConnectorOperationLease,
        retain_on_uncertainty: bool = False,
    ) -> InvocationReservation:
        if not record.idempotency_key_sha256:
            with self._write() as connection:
                self._assert_operation_lease(connection, operation_lease)
                if retain_on_uncertainty:
                    self._set_manual_reconciliation_policy(
                        connection, operation_lease
                    )
                self._insert_invocation(
                    connection,
                    record,
                    status="running",
                    operation_id=operation_lease.operation_id,
                )
            return InvocationReservation("reserved", record.invocation_id)
        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            self._mark_expired_operation_leases_unknown(connection)
            if retain_on_uncertainty:
                self._set_manual_reconciliation_policy(
                    connection, operation_lease
                )
            account = connection.execute(
                """
                SELECT connector_id, account_subject FROM connector_runtime_instances
                WHERE instance_id=?
                """,
                (record.instance_id,),
            ).fetchone()
            if account is None:
                raise RuntimeError("connector instance disappeared during invocation")
            account_scope_sha256 = hashlib.sha256(
                (
                    str(account["connector_id"])
                    + "\x00"
                    + str(account["account_subject"])
                ).encode("utf-8")
            ).hexdigest()
            key = (
                account_scope_sha256,
                record.action_id,
                record.idempotency_key_sha256,
            )
            existing = connection.execute(
                """
                SELECT input_sha256, invocation_id, status, result_json
                FROM connector_idempotency
                WHERE account_scope_sha256=? AND action_id=?
                  AND idempotency_key_sha256=?
                """,
                key,
            ).fetchone()
            if existing is not None:
                if str(existing["input_sha256"]) != record.input_sha256:
                    return InvocationReservation("conflict", str(existing["invocation_id"]))
                if existing["status"] == "completed":
                    return InvocationReservation(
                        "replay",
                        str(existing["invocation_id"]),
                        json.loads(str(existing["result_json"])),
                    )
                stage = connection.execute(
                    "SELECT status FROM connector_result_staging "
                    "WHERE invocation_id=?",
                    (str(existing["invocation_id"]),),
                ).fetchone()
                if stage is not None:
                    # A provider result already crossed the durable local
                    # staging boundary.  Recovery must finalize from CAS and
                    # must never dispatch the provider operation again.
                    return InvocationReservation(
                        "staged", str(existing["invocation_id"])
                    )
                if existing["status"] == "running":
                    return InvocationReservation(
                        "in_progress", str(existing["invocation_id"])
                    )
                if self._invocation_has_active_completion_owner(
                    connection, str(existing["invocation_id"])
                ):
                    return InvocationReservation(
                        "in_progress", str(existing["invocation_id"])
                    )
                return InvocationReservation("uncertain", str(existing["invocation_id"]))
            self._insert_invocation(
                connection,
                record,
                status="running",
                operation_id=operation_lease.operation_id,
            )
            connection.execute(
                """
                INSERT INTO connector_idempotency(
                    instance_id, account_scope_sha256, action_id,
                    idempotency_key_sha256, input_sha256, invocation_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    record.instance_id,
                    *key,
                    record.input_sha256,
                    record.invocation_id,
                    _iso(record.created_at),
                    _iso(record.created_at),
                ),
            )
            return InvocationReservation("reserved", record.invocation_id)

    def invocation_completion_state(
        self,
        invocation_id: str,
    ) -> InvocationReservation:
        """Read one exact local completion state without changing authority."""

        with self._connection() as connection:
            invocation = connection.execute(
                "SELECT status, result_json FROM connector_invocations "
                "WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                raise KeyError(invocation_id)
            if invocation["status"] == "completed":
                if invocation["result_json"] is None:
                    raise RuntimeError("connector invocation result is unavailable")
                return InvocationReservation(
                    "replay",
                    invocation_id,
                    json.loads(str(invocation["result_json"])),
                )
            stage = connection.execute(
                "SELECT status FROM connector_result_staging WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if stage is not None:
                return InvocationReservation("staged", invocation_id)
            if invocation["status"] == "outcome_unknown":
                if self._invocation_has_active_completion_owner(
                    connection, invocation_id
                ):
                    return InvocationReservation("in_progress", invocation_id)
                return InvocationReservation("uncertain", invocation_id)
            return InvocationReservation("in_progress", invocation_id)

    @staticmethod
    def _invocation_has_active_completion_owner(
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> bool:
        return connection.execute(
            "SELECT 1 FROM connector_invocations AS invocation "
            "JOIN connector_operation_leases AS lease "
            "ON lease.operation_id=invocation.operation_id "
            "AND lease.instance_id=invocation.instance_id "
            "WHERE invocation.invocation_id=? AND lease.status='active' "
            "AND lease.expires_at > ?",
            (invocation_id, _iso(_utcnow())),
        ).fetchone() is not None

    @staticmethod
    def _set_manual_reconciliation_policy(
        connection: sqlite3.Connection,
        lease: ConnectorOperationLease,
    ) -> None:
        cursor = connection.execute(
            "UPDATE connector_operation_leases "
            "SET uncertainty_policy='manual_reconcile' "
            "WHERE operation_id=? AND instance_id=? AND lease_token=? "
            "AND status='active'",
            (lease.operation_id, lease.instance_id, lease.lease_token),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("connector operation uncertainty policy was lost")

    def stage_connector_result(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        result_sha256: str,
        size_bytes: int,
        delivery_hint: Literal["inline", "artifact", "unavailable"],
        inline_data: Any = None,
        requested_name: str,
        owner_account_id: str,
        created_by_tool_id: Literal["connector_read", "connector_write"],
        completion_path: Literal["provider_result", "late_provider_result"],
    ) -> ConnectorResultStage:
        """Bind a bounded inline result or CAS identity to an invocation."""

        context = record.runtime_context
        digest = str(result_sha256 or "").casefold()
        if context is None:
            raise ValueError("connector result staging requires Runtime context")
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ValueError("connector result digest is invalid")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 0 <= size_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("connector result size is invalid")
        if delivery_hint not in {"inline", "artifact", "unavailable"}:
            raise ValueError("connector result delivery hint is invalid")
        inline_data_json: str | None = None
        if delivery_hint == "inline":
            inline_data_json = _canonical_json(inline_data)
            if len(inline_data_json.encode("utf-8")) != size_bytes:
                raise ValueError("inline Connector result identity is inconsistent")
            if size_bytes > 512 * 1024:
                raise ValueError("inline Connector result exceeds the staging limit")
            if hashlib.sha256(inline_data_json.encode("utf-8")).hexdigest() != digest:
                raise ValueError("inline Connector result digest is inconsistent")
        elif delivery_hint == "unavailable":
            inline_data_json = _canonical_json(inline_data)
            if (
                not isinstance(inline_data, Mapping)
                or len(inline_data_json.encode("utf-8")) > 16 * 1024
            ):
                raise ValueError("Connector unavailable receipt is invalid")
        elif inline_data is not None:
            raise ValueError("Artifact Connector result must not enter SQLite")
        if not str(requested_name or "").strip() or len(requested_name) > 512:
            raise ValueError("connector result Artifact name is invalid")
        if not str(owner_account_id or "").strip() or len(owner_account_id) > 256:
            raise ValueError("connector result owner is invalid")
        if created_by_tool_id not in {"connector_read", "connector_write"}:
            raise ValueError("connector result tool identity is invalid")
        if completion_path not in {"provider_result", "late_provider_result"}:
            raise ValueError("connector result completion path is invalid")
        immutable = {
            "operation_id": operation_lease.operation_id,
            "lease_token_sha256": hashlib.sha256(
                operation_lease.lease_token.encode("utf-8")
            ).hexdigest(),
            "result_sha256": digest,
            "size_bytes": size_bytes,
            "delivery_hint": delivery_hint,
            "inline_data_json": inline_data_json,
            "discovery_id": context.discovery_id,
            "requested_name": requested_name.strip(),
            "owner_account_id": owner_account_id.strip(),
            "thread_id": context.thread_id,
            "turn_id": context.turn_id,
            "created_by_tool_id": created_by_tool_id,
            "runtime_context_json": _canonical_json(context.to_dict()),
            "completion_path": completion_path,
        }
        now = _iso(_utcnow())
        with self._write() as connection:
            invocation = connection.execute(
                "SELECT operation_id, instance_id, status FROM connector_invocations "
                "WHERE invocation_id=?",
                (record.invocation_id,),
            ).fetchone()
            if invocation is None:
                raise RuntimeError("connector invocation disappeared before result staging")
            if str(invocation["operation_id"]) != operation_lease.operation_id:
                raise RuntimeError("connector result operation identity changed")
            if invocation["status"] not in {"running", "outcome_unknown"}:
                existing = self._result_stage_in_transaction(connection, record.invocation_id)
                if existing is not None and existing.status == "finalized":
                    return existing
                raise RuntimeError("connector invocation cannot accept a staged result")
            lease = connection.execute(
                "SELECT lease_token, status FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=?",
                (operation_lease.operation_id, operation_lease.instance_id),
            ).fetchone()
            if (
                lease is None
                or lease["status"] not in {"active", "outcome_unknown"}
                or not hmac.compare_digest(
                    str(lease["lease_token"]), operation_lease.lease_token
                )
            ):
                raise RuntimeError("connector result operation fence was lost")
            existing_row = connection.execute(
                "SELECT * FROM connector_result_staging WHERE invocation_id=?",
                (record.invocation_id,),
            ).fetchone()
            if existing_row is not None:
                for field, expected in immutable.items():
                    observed: Any = existing_row[field]
                    if field == "size_bytes":
                        observed = int(observed)
                    elif expected is None:
                        observed = None if observed is None else str(observed)
                    else:
                        observed = str(observed)
                    if observed != expected:
                        raise RuntimeError(
                            "connector result stage was reused with different authority"
                        )
                stage = self._result_stage_in_transaction(connection, record.invocation_id)
                assert stage is not None
                return stage
            connection.execute(
                """
                INSERT INTO connector_result_staging(
                    invocation_id, operation_id, lease_token_sha256,
                    result_sha256, size_bytes, delivery_hint, inline_data_json,
                    discovery_id, requested_name,
                    owner_account_id, thread_id, turn_id, created_by_tool_id,
                    runtime_context_json, completion_path, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?)
                """,
                (
                    record.invocation_id,
                    immutable["operation_id"],
                    immutable["lease_token_sha256"],
                    immutable["result_sha256"],
                    immutable["size_bytes"],
                    immutable["delivery_hint"],
                    immutable["inline_data_json"],
                    immutable["discovery_id"],
                    immutable["requested_name"],
                    immutable["owner_account_id"],
                    immutable["thread_id"],
                    immutable["turn_id"],
                    immutable["created_by_tool_id"],
                    immutable["runtime_context_json"],
                    immutable["completion_path"],
                    now,
                    now,
                ),
            )
            stage = self._result_stage_in_transaction(connection, record.invocation_id)
            assert stage is not None
            return stage

    def get_result_stage(self, invocation_id: str) -> ConnectorResultStage | None:
        with self._connection() as connection:
            return self._result_stage_in_transaction(connection, invocation_id)

    def get_result_stage_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> ConnectorResultStage | None:
        self._require_runtime_transaction(connection)
        return self._result_stage_in_transaction(connection, invocation_id)

    def pending_result_stages(self, *, limit: int = 100) -> tuple[ConnectorResultStage, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("connector result stage limit is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT invocation_id FROM connector_result_staging "
                "WHERE status='staged' ORDER BY created_at, invocation_id LIMIT ?",
                (limit,),
            ).fetchall()
            stages = [
                self._result_stage_in_transaction(connection, str(row["invocation_id"]))
                for row in rows
            ]
        return tuple(stage for stage in stages if stage is not None)

    def complete_runtime_invocation_in_transaction(
        self,
        connection: sqlite3.Connection,
        record: ConnectorInvocationRecord,
        *,
        result: Any,
        completion_path: Literal["provider_result", "late_provider_result"],
        operation_lease: ConnectorOperationLease | None = None,
        stage: ConnectorResultStage | None = None,
        artifact_id: str | None = None,
        revision_id: str | None = None,
    ) -> bool:
        """Linearize a model-originated result in the caller's Runtime transaction.

        The caller may insert Artifact metadata and a Runtime Item/Event before
        this method.  No nested connection or commit occurs here.
        """

        self._require_runtime_transaction(connection)
        if (operation_lease is None) == (stage is None):
            raise ValueError("exactly one connector result completion fence is required")
        if completion_path not in {"provider_result", "late_provider_result"}:
            raise ValueError("connector result completion path is invalid")
        result_json = _canonical_json(result)
        now = _iso(_utcnow())
        invocation = connection.execute(
            "SELECT * FROM connector_invocations WHERE invocation_id=?",
            (record.invocation_id,),
        ).fetchone()
        if invocation is None:
            raise RuntimeError("connector invocation disappeared before completion")
        if invocation["status"] == "completed":
            if str(invocation["result_json"] or "") != result_json:
                raise RuntimeError("connector invocation completed with a different result")
            return False
        if invocation["status"] not in {"running", "outcome_unknown"}:
            raise RuntimeError("connector invocation cannot be completed")

        operation_id = str(invocation["operation_id"])
        lease = connection.execute(
            "SELECT * FROM connector_operation_leases "
            "WHERE operation_id=? AND instance_id=?",
            (operation_id, str(invocation["instance_id"])),
        ).fetchone()
        if lease is None or lease["status"] not in {"active", "outcome_unknown"}:
            raise RuntimeError("connector result completion fence was lost")
        if stage is not None:
            durable_stage = self._result_stage_in_transaction(
                connection, record.invocation_id
            )
            if durable_stage is None:
                raise RuntimeError("connector result stage disappeared")
            if durable_stage.status == "finalized":
                if durable_stage.result != result:
                    raise RuntimeError("connector result stage finalized differently")
                return False
            if (
                durable_stage.operation_id != operation_id
                or durable_stage.result_sha256 != stage.result_sha256
                or durable_stage.size_bytes != stage.size_bytes
                or durable_stage.delivery_hint != stage.delivery_hint
                or durable_stage.inline_data != stage.inline_data
                or durable_stage.discovery_id != stage.discovery_id
                or not hmac.compare_digest(
                    hashlib.sha256(str(lease["lease_token"]).encode("utf-8")).hexdigest(),
                    durable_stage.lease_token_sha256,
                )
            ):
                raise RuntimeError("connector result stage authority changed")
            if durable_stage.delivery_hint == "artifact":
                if not artifact_id or not revision_id:
                    raise RuntimeError("Artifact Connector result identity is required")
            elif artifact_id is not None or revision_id is not None:
                raise RuntimeError("inline Connector result cannot claim an Artifact")
        else:
            assert operation_lease is not None
            if (
                operation_id != operation_lease.operation_id
                or str(invocation["instance_id"]) != operation_lease.instance_id
                or not hmac.compare_digest(
                    str(lease["lease_token"]), operation_lease.lease_token
                )
            ):
                raise RuntimeError("connector result completion authority changed")

        changed = connection.execute(
            "UPDATE connector_invocations SET status='completed', result_json=?, "
            "updated_at=? WHERE invocation_id=? "
            "AND status IN ('running','outcome_unknown')",
            (result_json, now, record.invocation_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("connector invocation lost its completion reservation")
        if record.idempotency_key_sha256:
            idempotency = connection.execute(
                "UPDATE connector_idempotency SET status='completed', result_json=?, "
                "updated_at=? WHERE invocation_id=? "
                "AND status IN ('running','outcome_unknown')",
                (result_json, now, record.invocation_id),
            )
            if idempotency.rowcount != 1:
                raise RuntimeError("connector idempotency reservation was lost")
        delivery = result.get("delivery") if isinstance(result, Mapping) else None
        result_digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        self._append_outbox(
            connection,
            event_type="connector.invocation.completed",
            aggregate_id=record.invocation_id,
            payload={
                "invocation_id": record.invocation_id,
                "instance_id": record.instance_id,
                "connector_id": record.connector_id,
                "action_id": record.action_id,
                "input_sha256": record.input_sha256,
                "idempotency_key_sha256": record.idempotency_key_sha256,
                "result_envelope_sha256": result_digest,
                "delivery": delivery,
                "status": "completed",
                "completion_path": completion_path,
                **self._runtime_context_payload(record.runtime_context),
            },
        )
        connection.execute(
            "DELETE FROM connector_operation_leases WHERE operation_id=?",
            (operation_id,),
        )
        if stage is not None:
            updated = connection.execute(
                "UPDATE connector_result_staging SET status='finalized', "
                "artifact_id=?, revision_id=?, result_json=?, updated_at=? "
                "WHERE invocation_id=? AND status='staged'",
                (
                    artifact_id,
                    revision_id,
                    result_json,
                    now,
                    record.invocation_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("connector result stage lost finalization authority")
        return True

    def _result_stage_in_transaction(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> ConnectorResultStage | None:
        row = connection.execute(
            """
            SELECT stage.*,
                   invocation.instance_id AS invocation_instance_id,
                   invocation.connector_id AS invocation_connector_id,
                   invocation.action_id AS invocation_action_id,
                   invocation.input_sha256 AS invocation_input_sha256,
                   invocation.idempotency_key_sha256 AS invocation_idempotency_sha256,
                   invocation.admission_policy_sha256 AS invocation_admission_sha256,
                   invocation.created_at AS invocation_created_at,
                   invocation.status AS invocation_status
            FROM connector_result_staging AS stage
            JOIN connector_invocations AS invocation
              ON invocation.invocation_id=stage.invocation_id
            WHERE stage.invocation_id=?
            """,
            (invocation_id,),
        ).fetchone()
        if row is None:
            return None
        runtime_raw = json.loads(str(row["runtime_context_json"]))
        if not isinstance(runtime_raw, dict):
            raise RuntimeError("connector result stage Runtime context is corrupt")
        context = ConnectorInvocationContext(**runtime_raw)
        result = (
            json.loads(str(row["result_json"]))
            if row["result_json"] is not None
            else None
        )
        return ConnectorResultStage(
            invocation=ConnectorInvocationRecord(
                invocation_id=str(row["invocation_id"]),
                instance_id=str(row["invocation_instance_id"]),
                connector_id=str(row["invocation_connector_id"]),
                action_id=str(row["invocation_action_id"]),
                input_sha256=str(row["invocation_input_sha256"]),
                idempotency_key_sha256=(
                    str(row["invocation_idempotency_sha256"])
                    if row["invocation_idempotency_sha256"] is not None
                    else None
                ),
                status=str(row["invocation_status"]),
                created_at=_parse_time(str(row["invocation_created_at"])),
                runtime_context=context,
                admission_policy_sha256=str(row["invocation_admission_sha256"]),
            ),
            operation_id=str(row["operation_id"]),
            lease_token_sha256=str(row["lease_token_sha256"]),
            result_sha256=str(row["result_sha256"]),
            size_bytes=int(row["size_bytes"]),
            delivery_hint=cast(
                Literal["inline", "artifact", "unavailable"],
                str(row["delivery_hint"]),
            ),
            inline_data=(
                json.loads(str(row["inline_data_json"]))
                if row["inline_data_json"] is not None
                else None
            ),
            discovery_id=str(row["discovery_id"]),
            requested_name=str(row["requested_name"]),
            owner_account_id=str(row["owner_account_id"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            created_by_tool_id=cast(
                Literal["connector_read", "connector_write"],
                str(row["created_by_tool_id"]),
            ),
            completion_path=cast(
                Literal["provider_result", "late_provider_result"],
                str(row["completion_path"]),
            ),
            status=cast(
                Literal["staged", "finalized"], str(row["status"])
            ),
            artifact_id=(str(row["artifact_id"]) if row["artifact_id"] else None),
            revision_id=(str(row["revision_id"]) if row["revision_id"] else None),
            result=result,
        )

    def _require_runtime_transaction(self, connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise RuntimeError("connector completion requires an active transaction")
        databases = connection.execute("PRAGMA database_list").fetchall()
        main_path = next((str(row[2]) for row in databases if str(row[1]) == "main"), "")
        if not main_path or Path(main_path).resolve() != Path(self.database).resolve():
            raise RuntimeError("connector completion transaction belongs to another database")

    def complete_invocation(
        self,
        record: ConnectorInvocationRecord,
        *,
        result: Any,
        operation_lease: ConnectorOperationLease,
    ) -> None:
        result_json = _canonical_json(result)
        now = _iso(_utcnow())
        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            cursor = connection.execute(
                """
                UPDATE connector_invocations
                SET status='completed', result_json=?, updated_at=?
                WHERE invocation_id=? AND status='running'
                """,
                (result_json, now, record.invocation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector invocation lost its reservation")
            if record.idempotency_key_sha256:
                cursor = connection.execute(
                    """
                    UPDATE connector_idempotency
                    SET status='completed', result_json=?, updated_at=?
                    WHERE invocation_id=? AND status='running'
                    """,
                    (result_json, now, record.invocation_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("connector idempotency reservation was lost")
            self._append_outbox(
                connection,
                event_type="connector.invocation.completed",
                aggregate_id=record.invocation_id,
                payload={
                    "invocation_id": record.invocation_id,
                    "instance_id": record.instance_id,
                    "connector_id": record.connector_id,
                    "action_id": record.action_id,
                    "input_sha256": record.input_sha256,
                    "idempotency_key_sha256": (
                        record.idempotency_key_sha256
                    ),
                    "status": "completed",
                    **self._runtime_context_payload(record.runtime_context),
                },
            )
            connection.execute(
                "DELETE FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=? AND lease_token=? "
                "AND status='active'",
                (
                    operation_lease.operation_id,
                    operation_lease.instance_id,
                    operation_lease.lease_token,
                ),
            )

    def complete_late_invocation(
        self,
        record: ConnectorInvocationRecord,
        *,
        result: Any,
        operation_lease: ConnectorOperationLease,
    ) -> None:
        """Commit a timed-out provider task that later returned successfully."""

        result_json = _canonical_json(result)
        now = _iso(_utcnow())
        with self._write() as connection:
            self._assert_operation_lease(connection, operation_lease)
            cursor = connection.execute(
                "UPDATE connector_invocations SET status='completed', result_json=?, updated_at=? "
                "WHERE invocation_id=? AND operation_id=? "
                "AND status='outcome_unknown'",
                (result_json, now, record.invocation_id, operation_lease.operation_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM connector_invocations WHERE invocation_id=?",
                    (record.invocation_id,),
                ).fetchone()
                if row is None or row["status"] != "completed":
                    raise RuntimeError("late connector invocation fact was lost")
            if record.idempotency_key_sha256:
                cursor = connection.execute(
                    "UPDATE connector_idempotency SET status='completed', "
                    "result_json=?, updated_at=? WHERE invocation_id=? "
                    "AND status='outcome_unknown'",
                    (result_json, now, record.invocation_id),
                )
                if cursor.rowcount != 1:
                    replay = connection.execute(
                        "SELECT status, result_json FROM connector_idempotency "
                        "WHERE invocation_id=?",
                        (record.invocation_id,),
                    ).fetchone()
                    if (
                        replay is None
                        or replay["status"] != "completed"
                        or str(replay["result_json"]) != result_json
                    ):
                        raise RuntimeError("late connector idempotency fact was lost")
            self._append_outbox(
                connection,
                event_type="connector.invocation.completed",
                aggregate_id=record.invocation_id,
                payload={
                    "invocation_id": record.invocation_id,
                    "instance_id": record.instance_id,
                    "connector_id": record.connector_id,
                    "action_id": record.action_id,
                    "input_sha256": record.input_sha256,
                    "idempotency_key_sha256": record.idempotency_key_sha256,
                    "status": "completed",
                    "completion_path": "late_provider_result",
                    **self._runtime_context_payload(record.runtime_context),
                },
            )
            connection.execute(
                "DELETE FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=? AND lease_token=? "
                "AND status='active'",
                (
                    operation_lease.operation_id,
                    operation_lease.instance_id,
                    operation_lease.lease_token,
                ),
            )

    def mark_invocation_unknown(self, record: ConnectorInvocationRecord) -> None:
        now = _iso(_utcnow())
        with self._write() as connection:
            connection.execute(
                """
                UPDATE connector_invocations
                SET status='outcome_unknown', updated_at=?
                WHERE invocation_id=? AND status='running'
                """,
                (now, record.invocation_id),
            )
            connection.execute(
                """
                UPDATE connector_idempotency
                SET status='outcome_unknown', updated_at=?
                WHERE invocation_id=? AND status='running'
                """,
                (now, record.invocation_id),
            )
            if record.runtime_context is not None:
                self._append_outbox(
                    connection,
                    event_type="connector.invocation.outcome_unknown",
                    aggregate_id=record.invocation_id,
                    payload={
                        "invocation_id": record.invocation_id,
                        "instance_id": record.instance_id,
                        "connector_id": record.connector_id,
                        "action_id": record.action_id,
                        "status": "outcome_unknown",
                        **self._runtime_context_payload(record.runtime_context),
                    },
                )

    def mark_invocation_operation_unknown(
        self,
        record: ConnectorInvocationRecord,
        operation_lease: ConnectorOperationLease,
        *,
        adapter_running: bool,
    ) -> None:
        """Persist a provider-uncertain invocation and retain its drain fence."""

        now = _iso(_utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT operation_id FROM connector_invocations "
                "WHERE invocation_id=?",
                (record.invocation_id,),
            ).fetchone()
            if row is None or str(row["operation_id"]) != operation_lease.operation_id:
                raise RuntimeError("connector invocation operation identity changed")
            connection.execute(
                "UPDATE connector_invocations SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (now, record.invocation_id),
            )
            connection.execute(
                "UPDATE connector_idempotency SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (now, record.invocation_id),
            )
            lease_row = connection.execute(
                "SELECT status FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=? AND lease_token=?",
                (
                    operation_lease.operation_id,
                    operation_lease.instance_id,
                    operation_lease.lease_token,
                ),
            ).fetchone()
            if lease_row is None or lease_row["status"] not in {
                "active",
                "outcome_unknown",
            }:
                raise RuntimeError("connector invocation drain fence was lost")
            if not adapter_running and lease_row["status"] == "active":
                connection.execute(
                    "UPDATE connector_operation_leases SET status='outcome_unknown', "
                    "expires_at=? WHERE operation_id=? AND instance_id=? "
                    "AND lease_token=? AND status='active'",
                    (
                        now,
                        operation_lease.operation_id,
                        operation_lease.instance_id,
                        operation_lease.lease_token,
                    ),
                )
            if record.runtime_context is not None:
                self._append_outbox(
                    connection,
                    event_type="connector.invocation.outcome_unknown",
                    aggregate_id=record.invocation_id,
                    payload={
                        "invocation_id": record.invocation_id,
                        "instance_id": record.instance_id,
                        "connector_id": record.connector_id,
                        "action_id": record.action_id,
                        "status": "outcome_unknown",
                        "operation_id": operation_lease.operation_id,
                        **self._runtime_context_payload(record.runtime_context),
                    },
                )

    def mark_operation_outcome_unknown(
        self,
        operation_lease: ConnectorOperationLease,
    ) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE connector_operation_leases SET status='outcome_unknown', "
                "expires_at=? WHERE operation_id=? AND instance_id=? "
                "AND lease_token=? AND status='active'",
                (
                    _iso(_utcnow()),
                    operation_lease.operation_id,
                    operation_lease.instance_id,
                    operation_lease.lease_token,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM connector_operation_leases "
                    "WHERE operation_id=? AND instance_id=?",
                    (operation_lease.operation_id, operation_lease.instance_id),
                ).fetchone()
                if row is None or row["status"] != "outcome_unknown":
                    raise RuntimeError("connector invocation drain fence was lost")

    def record_invocation_replay(
        self,
        invocation_id: str,
        runtime_context: ConnectorInvocationContext,
    ) -> None:
        """Persist a correlation edge for an idempotent provider replay."""

        with self._write() as connection:
            row = connection.execute(
                "SELECT instance_id, connector_id, action_id, status "
                "FROM connector_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is None or row["status"] != "completed":
                raise RuntimeError("connector replay target is unavailable")
            self._append_outbox(
                connection,
                event_type="connector.invocation.replayed",
                aggregate_id=invocation_id,
                payload={
                    "invocation_id": invocation_id,
                    "instance_id": str(row["instance_id"]),
                    "connector_id": str(row["connector_id"]),
                    "action_id": str(row["action_id"]),
                    "status": "completed",
                    **self._runtime_context_payload(runtime_context),
                },
            )

    def resolve_uncertain_invocation(
        self,
        invocation_id: str,
        resolution: Literal["confirmed_not_executed", "manually_reconciled"],
        *,
        wait_seconds: float = 2.0,
        poll_seconds: float = 0.02,
    ) -> None:
        """Resolve one provider-uncertain action without automatic replay.

        ``confirmed_not_executed`` removes only the idempotency reservation so
        an explicit human retry may reuse the same stable key.  The immutable
        invocation row remains as a terminal audit fact.  ``manually_reconciled``
        records a small replay sentinel and prevents any future retransmission.

        A provider can finish after its request timeout while the late-result
        watcher is still staging/finalizing the authoritative result.  Retry
        decisions therefore wait for that durable fact for a small bounded
        interval.  Each observation uses its own short transaction: no SQLite
        lock is held while sleeping, and expiry still fails closed with
        :class:`ConnectorReconciliationPending` instead of dispatching again.
        """

        if resolution not in {"confirmed_not_executed", "manually_reconciled"}:
            raise ValueError("invalid connector invocation resolution")
        if not 0 <= wait_seconds <= 5:
            raise ValueError("connector reconciliation wait is invalid")
        if not 0.005 <= poll_seconds <= 0.5:
            raise ValueError("connector reconciliation poll interval is invalid")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                self._resolve_uncertain_invocation_once(invocation_id, resolution)
                return
            except ConnectorReconciliationPending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(poll_seconds, remaining))

    def _resolve_uncertain_invocation_once(
        self,
        invocation_id: str,
        resolution: Literal["confirmed_not_executed", "manually_reconciled"],
    ) -> None:
        now = _iso(_utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM connector_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(invocation_id)
            staged = connection.execute(
                "SELECT status FROM connector_result_staging WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if staged is not None and staged["status"] == "staged":
                raise ConnectorReconciliationPending(
                    "connector result is already staged for local finalization"
                )
            if row["status"] == "completed":
                # Idempotent replay after a process restart or repeated click.
                return
            if row["status"] != "outcome_unknown":
                raise RuntimeError("connector invocation is not outcome-unknown")
            idempotency = connection.execute(
                "SELECT * FROM connector_idempotency WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if idempotency is None or idempotency["status"] != "outcome_unknown":
                raise RuntimeError("connector invocation idempotency fact is unavailable")
            operation = connection.execute(
                "SELECT status FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=?",
                (str(row["operation_id"]), str(row["instance_id"])),
            ).fetchone()
            if operation is None or operation["status"] != "outcome_unknown":
                raise ConnectorReconciliationPending(
                    "connector invocation is still executing or its drain fence is unavailable"
                )
            if resolution == "confirmed_not_executed":
                connection.execute(
                    "DELETE FROM connector_idempotency "
                    "WHERE invocation_id=? AND status='outcome_unknown'",
                    (invocation_id,),
                )
            else:
                sentinel = {
                    "status": "completed",
                    "result_delivery": "manually_reconciled",
                    "invocation_id": invocation_id,
                }
                connection.execute(
                    "UPDATE connector_idempotency SET status='completed', "
                    "result_json=?, updated_at=? "
                    "WHERE invocation_id=? AND status='outcome_unknown'",
                    (_canonical_json(sentinel), now, invocation_id),
                )
            connection.execute(
                "UPDATE connector_invocations SET status='completed', updated_at=? "
                "WHERE invocation_id=? AND status='outcome_unknown'",
                (now, invocation_id),
            )
            connection.execute(
                "DELETE FROM connector_operation_leases "
                "WHERE operation_id=? AND instance_id=? AND status='outcome_unknown'",
                (str(row["operation_id"]), str(row["instance_id"])),
            )
            self._append_outbox(
                connection,
                event_type="connector.invocation.reconciled",
                aggregate_id=invocation_id,
                payload={
                    "invocation_id": invocation_id,
                    "instance_id": str(row["instance_id"]),
                    "connector_id": str(row["connector_id"]),
                    "action_id": str(row["action_id"]),
                    "resolution": resolution,
                    "status": "completed",
                },
            )

    def claim_outbox(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> tuple[ConnectorOutboxEvent, ...]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("outbox limit and lease_seconds must be positive")
        now = _utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        token = "connoutbox_" + uuid.uuid4().hex
        with self._write() as connection:
            candidates = connection.execute(
                """
                SELECT o.* FROM connector_outbox AS o
                WHERE o.published_at IS NULL
                  AND o.dead_lettered_at IS NULL
                  AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
                  AND (o.lease_expires_at IS NULL OR o.lease_expires_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM connector_outbox AS earlier
                      WHERE earlier.aggregate_id=o.aggregate_id
                        AND earlier.aggregate_seq < o.aggregate_seq
                        AND earlier.published_at IS NULL
                  )
                ORDER BY o.created_at, o.event_id
                LIMIT ?
                """,
                (_iso(now), _iso(now), max(100, limit * 10)),
            ).fetchall()
            valid_rows = []
            for row in candidates:
                payload_json = str(row["payload_json"])
                expected_digest = hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest()
                try:
                    decoded_payload = json.loads(payload_json)
                except json.JSONDecodeError:
                    decoded_payload = None
                valid = hmac.compare_digest(
                    expected_digest, str(row["payload_sha256"])
                ) and isinstance(decoded_payload, dict)
                if not valid:
                    connection.execute(
                        """
                        UPDATE connector_outbox
                        SET dead_lettered_at=?, attempts=attempts+1,
                            lease_token=NULL, lease_expires_at=NULL
                        WHERE event_id=? AND published_at IS NULL
                        """,
                        (_iso(now), str(row["event_id"])),
                    )
                    continue
                valid_rows.append((row, decoded_payload))
                if len(valid_rows) >= limit:
                    break
            ids = [str(row["event_id"]) for row, _payload in valid_rows]
            if not ids:
                return ()
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE connector_outbox
                SET lease_token=?, lease_expires_at=?, attempts=attempts+1
                WHERE event_id IN ({placeholders}) AND published_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (token, _iso(expires), *ids, _iso(now)),
            )
            claimed = connection.execute(
                """
                SELECT * FROM connector_outbox
                WHERE lease_token=? AND published_at IS NULL AND dead_lettered_at IS NULL
                ORDER BY created_at, event_id
                """,
                (token,),
            ).fetchall()
        decoded_by_id = {
            str(row["event_id"]): payload for row, payload in valid_rows
        }
        events = []
        for row in claimed:
            events.append(ConnectorOutboxEvent(
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                aggregate_id=str(row["aggregate_id"]),
                aggregate_seq=int(row["aggregate_seq"]),
                payload=decoded_by_id[str(row["event_id"])],
                created_at=_parse_time(str(row["created_at"])),
                lease_token=token,
                attempts=int(row["attempts"]),
            ))
        return tuple(events)

    def renew_outbox(
        self,
        event_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 30,
    ) -> bool:
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_outbox SET lease_expires_at=?
                WHERE event_id=? AND lease_token=? AND published_at IS NULL
                  AND dead_lettered_at IS NULL
                """,
                (
                    _iso(_utcnow() + timedelta(seconds=lease_seconds)),
                    event_id,
                    lease_token,
                ),
            )
            return cursor.rowcount == 1

    def mark_outbox_published(self, event_id: str, lease_token: str) -> None:
        now = _iso(_utcnow())
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE connector_outbox
                SET published_at=?, lease_token=NULL, lease_expires_at=NULL
                WHERE event_id=? AND lease_token=? AND published_at IS NULL
                  AND lease_expires_at > ?
                """,
                (
                    now,
                    event_id,
                    lease_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("connector outbox publish lease was lost")

    def release_outbox(self, event_id: str, lease_token: str) -> None:
        with self._write() as connection:
            connection.execute(
                """
                UPDATE connector_outbox SET lease_token=NULL, lease_expires_at=NULL
                WHERE event_id=? AND lease_token=? AND published_at IS NULL
                """,
                (event_id, lease_token),
            )

    def fail_outbox(
        self,
        event_id: str,
        lease_token: str,
        *,
        attempts: int,
        max_attempts: int = 5,
    ) -> None:
        now = _utcnow()
        with self._write() as connection:
            if attempts >= max_attempts:
                cursor = connection.execute(
                    """
                    UPDATE connector_outbox
                    SET dead_lettered_at=?, lease_token=NULL, lease_expires_at=NULL
                    WHERE event_id=? AND lease_token=? AND published_at IS NULL
                    """,
                    (_iso(now), event_id, lease_token),
                )
            else:
                delay_seconds = min(300, 2 ** max(0, attempts - 1))
                cursor = connection.execute(
                    """
                    UPDATE connector_outbox
                    SET next_attempt_at=?, lease_token=NULL, lease_expires_at=NULL
                    WHERE event_id=? AND lease_token=? AND published_at IS NULL
                    """,
                    (
                        _iso(now + timedelta(seconds=delay_seconds)),
                        event_id,
                        lease_token,
                    ),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("connector outbox failure lease was lost")

    def pending_outbox_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM connector_outbox
                    WHERE published_at IS NULL AND dead_lettered_at IS NULL
                    """
                ).fetchone()[0]
            )

    def record_recovery_deferred(
        self,
        *,
        recovery_kind: str,
        record_id: str,
        error_code: str = "credential_cleanup_deferred",
    ) -> None:
        aggregate_id = f"connector-recovery:{recovery_kind}:{record_id}"
        payload = {
            "recovery_kind": recovery_kind,
            "record_id": record_id,
            "error_code": error_code,
        }
        encoded = _canonical_json(payload)
        with self._write() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM connector_outbox "
                "WHERE aggregate_id=? AND event_type='connector.recovery.deferred' "
                "AND payload_json=? LIMIT 1",
                (aggregate_id, encoded),
            ).fetchone()
            if duplicate is not None:
                return
            self._append_outbox(
                connection,
                event_type="connector.recovery.deferred",
                aggregate_id=aggregate_id,
                payload=payload,
            )

    def record_result_recovery_deferred(
        self,
        *,
        invocation_id: str,
        stage_status: str = "staged",
        error_code: str = "local_finalize_deferred",
    ) -> None:
        if stage_status not in {"staged", "finalized"}:
            raise ValueError("connector result recovery stage status is invalid")
        if error_code not in {
            "artifact_cas_unavailable",
            "local_finalize_deferred",
        }:
            raise ValueError("connector result recovery error code is invalid")
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM connector_result_staging WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                return
            payload = {
                "invocation_id": invocation_id,
                "stage_status": str(row["status"]),
                "error_code": error_code,
            }
            previous = connection.execute(
                "SELECT payload_json FROM connector_outbox "
                "WHERE aggregate_id=? "
                "AND event_type='connector.result.recovery_deferred' "
                "ORDER BY aggregate_seq DESC LIMIT 1",
                (invocation_id,),
            ).fetchone()
            if previous is not None:
                try:
                    prior_payload = json.loads(str(previous["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    prior_payload = None
                if prior_payload == payload:
                    return
            self._append_outbox(
                connection,
                event_type="connector.result.recovery_deferred",
                aggregate_id=invocation_id,
                payload=payload,
            )

    def dead_letter_outbox_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM connector_outbox WHERE dead_lettered_at IS NOT NULL"
                ).fetchone()[0]
            )

    def _insert_invocation(
        self,
        connection: sqlite3.Connection,
        record: ConnectorInvocationRecord,
        *,
        status: str,
        operation_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO connector_invocations(
                invocation_id, operation_id, instance_id, connector_id, action_id,
                input_sha256, idempotency_key_sha256, admission_policy_sha256,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.invocation_id,
                operation_id,
                record.instance_id,
                record.connector_id,
                record.action_id,
                record.input_sha256,
                (
                    record.idempotency_key_sha256
                ),
                record.admission_policy_sha256,
                status,
                _iso(record.created_at),
                _iso(record.created_at),
            ),
        )
        if record.runtime_context is not None:
            self._append_outbox(
                connection,
                event_type="connector.invocation.started",
                aggregate_id=record.invocation_id,
                payload={
                    "invocation_id": record.invocation_id,
                    "instance_id": record.instance_id,
                    "connector_id": record.connector_id,
                    "action_id": record.action_id,
                    "input_sha256": record.input_sha256,
                    "idempotency_key_sha256": record.idempotency_key_sha256,
                    "admission_policy_sha256": record.admission_policy_sha256,
                    "status": "running",
                    **self._runtime_context_payload(record.runtime_context),
                },
            )

    @staticmethod
    def _runtime_context_payload(
        context: ConnectorInvocationContext | None,
    ) -> dict[str, Any]:
        return {} if context is None else {"runtime": context.to_dict()}

    def _append_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        encoded = _canonical_json(dict(payload))
        aggregate_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(aggregate_seq), 0) + 1
                FROM connector_outbox WHERE aggregate_id=?
                """,
                (aggregate_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO connector_outbox(
                event_id, event_type, aggregate_id, payload_json,
                payload_sha256, aggregate_seq, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "connevent_" + uuid.uuid4().hex,
                event_type,
                aggregate_id,
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                aggregate_seq,
                _iso(_utcnow()),
            ),
        )

    @staticmethod
    def _instance_from_row(row: sqlite3.Row) -> ConnectorInstance:
        return ConnectorInstance(
            instance_id=str(row["instance_id"]),
            connector_id=str(row["connector_id"]),
            account_subject=str(row["account_subject"]),
            account_display_name=str(row["account_display_name"]),
            credential_ref=str(row["credential_ref"]),
            granted_scopes=frozenset(json.loads(str(row["granted_scopes_json"]))),
            health=ConnectorHealth(str(row["health"])),
            enabled=bool(row["enabled"]),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
            last_error_code=(
                str(row["last_error_code"]) if row["last_error_code"] is not None else None
            ),
        )


__all__ = [
    "ConnectorFlowRecord",
    "ConnectorOperationLease",
    "ConnectorOutboxEvent",
    "FlowConsumption",
    "InvocationReservation",
    "RecoveryReference",
    "SQLiteConnectorRepository",
]

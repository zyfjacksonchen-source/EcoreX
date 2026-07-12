"""Durable OAuth-style device authorization without exposing tokens to WebUI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import re
import secrets
import sqlite3
from typing import Any, Awaitable, Protocol, TypeVar
from urllib.parse import urlsplit

from ecorex.connectors.vault import CredentialVault
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeExecutionPermit,
)
from ecorex.runtime.schema_catalog import validate_product_schema

from .models import ManagedSessionSnapshot, SignedManagedSessionLease
from .service import ManagedSessionService


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")


class DeviceAuthorizationError(RuntimeError):
    code = "device_authorization_error"


class DeviceAuthorizationNotFound(DeviceAuthorizationError):
    code = "device_authorization_not_found"


class DeviceAuthorizationConflict(DeviceAuthorizationError):
    code = "device_authorization_conflict"


class DeviceAuthorizationUnavailable(DeviceAuthorizationError):
    code = "device_authorization_unavailable"


class DeviceFlowStatus(StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


class BrokerPollStatus(StrEnum):
    PENDING = "pending"
    SLOW_DOWN = "slow_down"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class BrokerDeviceChallenge:
    provider_flow_id: str
    device_code: str
    user_code: str
    verification_url: str
    expires_at: datetime
    poll_interval_seconds: int

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.provider_flow_id):
            raise ValueError("device provider flow identity is invalid")
        if not isinstance(self.device_code, str) or not 16 <= len(self.device_code) <= 4096:
            raise ValueError("device authorization secret is invalid")
        if not isinstance(self.user_code, str) or not 4 <= len(self.user_code) <= 64:
            raise ValueError("device user code is invalid")
        parsed = urlsplit(self.verification_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(self.verification_url) > 4096
        ):
            raise ValueError("device verification URL must be credential-free HTTPS")
        if self.expires_at.tzinfo is None:
            raise ValueError("device challenge expiry must be timezone-aware")
        if not 1 <= self.poll_interval_seconds <= 300:
            raise ValueError("device poll interval is invalid")


@dataclass(frozen=True, slots=True)
class BrokerDeviceGrant:
    lease: SignedManagedSessionLease
    access_token: str
    refresh_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, SignedManagedSessionLease):
            raise ValueError("device grant lease is invalid")
        for value in (self.access_token, self.refresh_token):
            if not isinstance(value, str) or not value or len(value) > 64 * 1024:
                raise ValueError("device grant token material is invalid")


@dataclass(frozen=True, slots=True)
class BrokerPollResult:
    status: BrokerPollStatus
    grant: BrokerDeviceGrant | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.status is BrokerPollStatus.AUTHORIZED:
            if self.grant is None:
                raise ValueError("authorized device result requires a grant")
        elif self.grant is not None:
            raise ValueError("non-authorized device result cannot contain a grant")
        if self.retry_after_seconds is not None and not 1 <= self.retry_after_seconds <= 300:
            raise ValueError("device retry interval is invalid")


class DeviceAuthorizationBroker(Protocol):
    async def begin(self, *, idempotency_key: str) -> BrokerDeviceChallenge:
        ...

    async def poll(
        self,
        *,
        provider_flow_id: str,
        device_code: str,
        idempotency_key: str,
    ) -> BrokerPollResult:
        ...


@dataclass(frozen=True, slots=True)
class DeviceFlowProjection:
    flow_id: str
    status: DeviceFlowStatus
    user_code: str
    verification_url: str
    expires_at: datetime
    poll_interval_seconds: int
    next_poll_at: datetime
    restart_required: bool
    session_generation: int | None = None
    error_code: str | None = None


Clock = Callable[[], datetime]
_T = TypeVar("_T")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("device timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DeviceAuthorizationConflict("stored device timestamp is invalid")
    return parsed.astimezone(UTC)


def _request_hash(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise ValueError("device client_request_id is invalid")
    return hashlib.sha256(b"ecorex-device-request-v1\0" + value.encode()).hexdigest()


class ManagedDeviceAuthorizationService:
    """Persist and supervise one cloud device-login flow at a time."""

    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        session: ManagedSessionService,
        vault: CredentialVault,
        broker: DeviceAuthorizationBroker,
        clock: Clock = _now,
        poll_lease_seconds: int = 30,
        broker_timeout_seconds: float = 30.0,
        execution_gate: RuntimeExecutionGate | None = None,
        initialize: bool = True,
    ) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.session = session
        if (
            self.session.repository.database.path.resolve()
            != self.database.path.resolve()
        ):
            raise ValueError(
                "device authorization and managed session must use one database"
            )
        self.vault = vault
        self.broker = broker
        self.execution_gate = execution_gate
        self.clock = clock
        if not 5 <= poll_lease_seconds <= 300:
            raise ValueError("device poll lease is invalid")
        if not 1 <= broker_timeout_seconds <= 120:
            raise ValueError("device broker timeout is invalid")
        self.poll_lease_seconds = poll_lease_seconds
        self.broker_timeout_seconds = float(broker_timeout_seconds)
        self._startup_converged = False
        if initialize:
            self.initialize()
        else:
            self.validate()

    def bind_execution_gate(self, gate: RuntimeExecutionGate) -> None:
        if not isinstance(gate, RuntimeExecutionGate):
            raise TypeError("device Runtime execution gate is invalid")
        if self.execution_gate is not None and self.execution_gate is not gate:
            raise RuntimeError("device authorization already has an execution gate")
        self.execution_gate = gate

    def _issue_permit(
        self,
        *,
        scope: str,
        subject: str,
    ) -> RuntimeExecutionPermit | None:
        if self.execution_gate is None:
            return None
        try:
            return self.execution_gate.issue_permit(scope=scope, subject=subject)
        except RuntimeExecutionDenied as error:
            raise DeviceAuthorizationUnavailable(
                "device authorization is unavailable while Runtime is read-only"
            ) from error

    def _assert_permit(self, permit: RuntimeExecutionPermit | None) -> None:
        if self.execution_gate is None:
            if permit is not None:
                raise DeviceAuthorizationUnavailable(
                    "device execution permit is invalid"
                )
            return
        if permit is None:
            raise DeviceAuthorizationUnavailable(
                "device authorization has no Runtime execution permit"
            )
        try:
            self.execution_gate.assert_permit(permit)
        except RuntimeExecutionDenied as error:
            raise DeviceAuthorizationUnavailable(
                "device authorization epoch closed before completion"
            ) from error

    def _before_commit(
        self, permit: RuntimeExecutionPermit | None
    ) -> Callable[[], None]:
        return lambda: self._assert_permit(permit)

    async def _await_with_permit(
        self,
        permit: RuntimeExecutionPermit | None,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        self._assert_permit(permit)
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._assert_permit(permit)
            raise
        self._assert_permit(permit)
        return result

    async def _to_thread_with_permit(
        self,
        permit: RuntimeExecutionPermit | None,
        operation: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        return await self._await_with_permit(
            permit,
            lambda: asyncio.to_thread(operation, *args, **kwargs),
        )

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def validate(self) -> None:
        with self.database.reader() as connection:
            validate_product_schema(connection)

    def initialize(self) -> None:
        """Enable device mutations after the compiled schema validates."""

        self.validate()
        self._startup_converged = True

    def converge_startup(self) -> None:
        self.initialize()

    def _require_converged(self) -> None:
        if not self._startup_converged:
            raise DeviceAuthorizationUnavailable(
                "device authorization startup has not converged"
            )

    async def begin(self, *, client_request_id: str) -> DeviceFlowProjection:
        self._require_converged()
        request_hash = _request_hash(client_request_id)
        existing = await asyncio.to_thread(self._by_request_hash, request_hash)
        if existing is not None:
            return self._projection(existing)
        permit = self._issue_permit(
            scope="device_authorization",
            subject=f"begin:{request_hash}",
        )
        try:
            challenge = await self._await_with_permit(
                permit,
                lambda: asyncio.wait_for(
                    self.broker.begin(
                        idempotency_key=f"device-begin:{request_hash}"
                    ),
                    timeout=self.broker_timeout_seconds,
                ),
            )
        except Exception as exc:
            raise DeviceAuthorizationUnavailable(
                f"device authorization provider failed: {type(exc).__name__}"
            ) from None
        now = self._utc_now()
        expires_at = challenge.expires_at.astimezone(UTC)
        if expires_at <= now or expires_at - now > timedelta(minutes=30):
            raise DeviceAuthorizationUnavailable("device authorization expiry is invalid")
        flow_id = "devflow_" + hashlib.sha256(
            b"ecorex-device-flow-v1\0"
            + challenge.provider_flow_id.encode()
            + b"\0"
            + request_hash.encode()
        ).hexdigest()[:32]
        credential_ref = f"ecorex/session/device/{flow_id}"
        try:
            await self._to_thread_with_permit(
                permit,
                self.vault.put,
                credential_ref,
                {"device_code": challenge.device_code},
            )
        except Exception:
            raise DeviceAuthorizationUnavailable(
                "device authorization secret could not be secured"
            ) from None
        try:
            existing = await asyncio.to_thread(
                self._persist_started_challenge,
                request_hash=request_hash,
                challenge=challenge,
                flow_id=flow_id,
                credential_ref=credential_ref,
                expires_at=expires_at,
                now=now,
                before_commit=self._before_commit(permit),
            )
        except DeviceAuthorizationConflict:
            await asyncio.to_thread(self._delete_secret, credential_ref)
            raise
        except sqlite3.IntegrityError as exc:
            raise DeviceAuthorizationConflict("device authorization identity conflicted") from exc
        assert existing is not None
        return self._projection(existing)

    def get(self, flow_id: str) -> DeviceFlowProjection:
        """Return the durable projection without expiry or vault side effects."""

        return self._projection(self._require(flow_id))

    def expire_due(self, *, limit: int = 100) -> tuple[str, ...]:
        """Explicitly converge expired flows and scrub their device secrets."""

        self._require_converged()
        permit = self._issue_permit(
            scope="device_maintenance",
            subject="expire_due",
        )
        now = self._utc_now()
        bounded_limit = max(1, min(int(limit), 500))
        cleanup_refs: list[str] = []
        expired_ids: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM managed_device_flows WHERE status=? "
                "AND expires_at<=? ORDER BY expires_at,created_at,flow_id LIMIT ?",
                (
                    DeviceFlowStatus.PENDING.value,
                    _iso(now),
                    bounded_limit,
                ),
            ).fetchall()
            for row in rows:
                self._mark_terminal(
                    connection,
                    row,
                    DeviceFlowStatus.EXPIRED,
                    error_code=None,
                    now=now,
                )
                expired_ids.append(str(row["flow_id"]))
                cleanup_refs.append(str(row["credential_ref"]))
            self._assert_permit(permit)
        for reference in cleanup_refs:
            self._assert_permit(permit)
            self._delete_secret(reference)
            self._assert_permit(permit)
        return tuple(expired_ids)

    async def poll_once(self, flow_id: str) -> DeviceFlowProjection:
        self._require_converged()
        now = self._utc_now()
        lease_token = secrets.token_hex(24)
        permit = self._issue_permit(
            scope="device_poll",
            subject=f"{flow_id}:{lease_token}",
        )
        row, cleanup_ref, claimed = await asyncio.to_thread(
            self._claim_poll,
            flow_id,
            lease_token,
            now,
            self._before_commit(permit),
        )
        if cleanup_ref:
            await self._to_thread_with_permit(
                permit, self._delete_secret, cleanup_ref
            )
            return await asyncio.to_thread(self.get, flow_id)
        if not claimed:
            return self._projection(row)
        try:
            secret = await self._to_thread_with_permit(
                permit, self.vault.get, row["credential_ref"]
            )
            device_code = secret["device_code"]
            result = await self._await_with_permit(
                permit,
                lambda: asyncio.wait_for(
                    self.broker.poll(
                        provider_flow_id=row["provider_flow_id"],
                        device_code=device_code,
                        idempotency_key=(
                            f"device-poll:{flow_id}:{row['poll_attempt']}"
                        ),
                    ),
                    timeout=self.broker_timeout_seconds,
                ),
            )
        except Exception as exc:
            return await asyncio.to_thread(
                self._release_retry,
                flow_id,
                lease_token,
                retry_after=int(row["poll_interval_seconds"]),
                error_code=type(exc).__name__.casefold()[:128],
                before_commit=self._before_commit(permit),
            )
        if result.status is BrokerPollStatus.AUTHORIZED:
            assert result.grant is not None
            try:
                session_snapshot = await self._to_thread_with_permit(
                    permit,
                    self.session.install,
                    result.grant.lease,
                    access_token=result.grant.access_token,
                    refresh_token=result.grant.refresh_token,
                    client_request_id=f"device-login:{flow_id}",
                    before_commit=self._before_commit(permit),
                )
            except Exception as exc:
                return await asyncio.to_thread(
                    self._release_retry,
                    flow_id,
                    lease_token,
                    retry_after=int(row["poll_interval_seconds"]),
                    error_code=type(exc).__name__.casefold()[:128],
                    before_commit=self._before_commit(permit),
                )
            completed = await asyncio.to_thread(
                self._complete_authorized,
                flow_id,
                lease_token,
                session_snapshot=session_snapshot,
                before_commit=self._before_commit(permit),
            )
            await self._to_thread_with_permit(
                permit, self._delete_secret, row["credential_ref"]
            )
            return completed
        if result.status in {BrokerPollStatus.DENIED, BrokerPollStatus.EXPIRED}:
            target = (
                DeviceFlowStatus.DENIED
                if result.status is BrokerPollStatus.DENIED
                else DeviceFlowStatus.EXPIRED
            )
            completed = await asyncio.to_thread(
                self._complete_terminal,
                flow_id,
                lease_token,
                target,
                self._before_commit(permit),
            )
            await self._to_thread_with_permit(
                permit, self._delete_secret, row["credential_ref"]
            )
            return completed
        retry_after = result.retry_after_seconds or int(row["poll_interval_seconds"])
        if result.status is BrokerPollStatus.SLOW_DOWN:
            retry_after = min(300, retry_after + 5)
        return await asyncio.to_thread(
            self._release_retry,
            flow_id,
            lease_token,
            retry_after=retry_after,
            error_code=None,
            before_commit=self._before_commit(permit),
        )

    def _persist_started_challenge(
        self,
        *,
        request_hash: str,
        challenge: BrokerDeviceChallenge,
        flow_id: str,
        credential_ref: str,
        expires_at: datetime,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> sqlite3.Row:
        """Commit the public challenge and audit atomically off the event loop."""

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM managed_device_flows WHERE client_request_hash=?",
                (request_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO managed_device_flows("
                    "flow_id,client_request_hash,provider_flow_id,credential_ref,status,"
                    "user_code,verification_url,expires_at,poll_interval_seconds,"
                    "next_poll_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        flow_id,
                        request_hash,
                        challenge.provider_flow_id,
                        credential_ref,
                        DeviceFlowStatus.PENDING.value,
                        challenge.user_code,
                        challenge.verification_url,
                        _iso(expires_at),
                        challenge.poll_interval_seconds,
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM managed_device_flows WHERE flow_id=?",
                    (flow_id,),
                ).fetchone()
                self._audit(
                    connection,
                    existing,
                    event_type="device.started",
                    now=now,
                )
            elif (
                existing["provider_flow_id"] != challenge.provider_flow_id
                or existing["credential_ref"] != credential_ref
            ):
                raise DeviceAuthorizationConflict(
                    "device request identity changed during authorization"
                )
            assert existing is not None
            if before_commit is not None:
                before_commit()
            return existing

    def _claim_poll(
        self,
        flow_id: str,
        lease_token: str,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> tuple[sqlite3.Row, str | None, bool]:
        """Lease one poll in the same transaction as its durable audit fact."""

        with self.database.transaction() as connection:
            row = self._require_in(connection, flow_id)
            if DeviceFlowStatus(row["status"]) is not DeviceFlowStatus.PENDING:
                return row, None, False
            if _time(row["expires_at"]) <= now:
                row = self._mark_terminal(
                    connection,
                    row,
                    DeviceFlowStatus.EXPIRED,
                    error_code=None,
                    now=now,
                )
                if before_commit is not None:
                    before_commit()
                return row, str(row["credential_ref"]), False
            if _time(row["next_poll_at"]) > now:
                return row, None, False
            if (
                row["poll_lease_token"]
                and row["poll_lease_expires_at"]
                and _time(row["poll_lease_expires_at"]) > now
            ):
                return row, None, False
            attempt = int(row["poll_attempt"]) + 1
            connection.execute(
                "UPDATE managed_device_flows SET poll_attempt=?,poll_lease_token=?,"
                "poll_lease_expires_at=?,updated_at=? WHERE flow_id=?",
                (
                    attempt,
                    lease_token,
                    _iso(now + timedelta(seconds=self.poll_lease_seconds)),
                    _iso(now),
                    flow_id,
                ),
            )
            row = self._require_in(connection, flow_id)
            self._audit(
                connection,
                row,
                event_type="device.poll_leased",
                now=now,
            )
            if before_commit is not None:
                before_commit()
            return row, None, True

    def due_flow_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        now = self._utc_now()
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT flow_id FROM managed_device_flows WHERE status='pending' "
                "AND next_poll_at<=? ORDER BY next_poll_at,created_at LIMIT ?",
                (_iso(now), max(1, min(limit, 100))),
            ).fetchall()
        return tuple(row["flow_id"] for row in rows)

    def _release_retry(
        self,
        flow_id: str,
        lease_token: str,
        *,
        retry_after: int,
        error_code: str | None,
        before_commit: Callable[[], None] | None = None,
    ) -> DeviceFlowProjection:
        now = self._utc_now()
        with self.database.transaction() as connection:
            row = self._require_owned(connection, flow_id, lease_token)
            if _time(row["expires_at"]) <= now:
                row = self._mark_terminal(
                    connection, row, DeviceFlowStatus.EXPIRED, error_code=None, now=now
                )
            else:
                connection.execute(
                    "UPDATE managed_device_flows SET poll_interval_seconds=?,next_poll_at=?,"
                    "poll_lease_token=NULL,poll_lease_expires_at=NULL,error_code=?,updated_at=? "
                    "WHERE flow_id=?",
                    (
                        retry_after,
                        _iso(now + timedelta(seconds=retry_after)),
                        error_code,
                        _iso(now),
                        flow_id,
                    ),
                )
                row = self._require_in(connection, flow_id)
                self._audit(
                    connection,
                    row,
                    event_type="device.poll_retry_scheduled",
                    now=now,
                )
            if before_commit is not None:
                before_commit()
        return self._projection(row)

    def _complete_authorized(
        self,
        flow_id: str,
        lease_token: str,
        *,
        session_snapshot: ManagedSessionSnapshot,
        before_commit: Callable[[], None] | None = None,
    ) -> DeviceFlowProjection:
        now = self._utc_now()
        with self.database.transaction() as connection:
            row = self._require_in(connection, flow_id)
            if DeviceFlowStatus(row["status"]) is DeviceFlowStatus.AUTHORIZED:
                if row["lease_digest"] != session_snapshot.lease_digest:
                    raise DeviceAuthorizationConflict("device grant identity changed")
                if before_commit is not None:
                    before_commit()
                return self._projection(row)
            self._assert_owned(row, lease_token)
            connection.execute(
                "UPDATE managed_device_flows SET status='authorized',session_generation=?,"
                "lease_digest=?,poll_lease_token=NULL,poll_lease_expires_at=NULL,"
                "error_code=NULL,updated_at=? WHERE flow_id=?",
                (
                    session_snapshot.generation,
                    session_snapshot.lease_digest,
                    _iso(now),
                    flow_id,
                ),
            )
            completed = self._require_in(connection, flow_id)
            self._audit(
                connection,
                completed,
                event_type="device.authorized",
                now=now,
            )
            if before_commit is not None:
                before_commit()
            return self._projection(completed)

    def _complete_terminal(
        self,
        flow_id: str,
        lease_token: str,
        target: DeviceFlowStatus,
        before_commit: Callable[[], None] | None = None,
    ) -> DeviceFlowProjection:
        now = self._utc_now()
        with self.database.transaction() as connection:
            row = self._require_owned(connection, flow_id, lease_token)
            row = self._mark_terminal(connection, row, target, error_code=None, now=now)
            if before_commit is not None:
                before_commit()
            return self._projection(row)

    @staticmethod
    def _mark_terminal(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: DeviceFlowStatus,
        *,
        error_code: str | None,
        now: datetime,
    ) -> sqlite3.Row:
        connection.execute(
            "UPDATE managed_device_flows SET status=?,poll_lease_token=NULL,"
            "poll_lease_expires_at=NULL,error_code=?,updated_at=? WHERE flow_id=?",
            (target.value, error_code, _iso(now), row["flow_id"]),
        )
        completed = connection.execute(
            "SELECT * FROM managed_device_flows WHERE flow_id=?", (row["flow_id"],)
        ).fetchone()
        ManagedDeviceAuthorizationService._audit(
            connection,
            completed,
            event_type=f"device.{target.value}",
            now=now,
        )
        return completed

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        event_type: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO managed_device_audit("
            "flow_id,event_type,status,poll_attempt,error_code,created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                row["flow_id"],
                event_type,
                row["status"],
                int(row["poll_attempt"]),
                row["error_code"],
                _iso(now),
            ),
        )

    def _by_request_hash(self, value: str) -> sqlite3.Row | None:
        with self.database.reader() as connection:
            return connection.execute(
                "SELECT * FROM managed_device_flows WHERE client_request_hash=?",
                (value,),
            ).fetchone()

    def _require(self, flow_id: str) -> sqlite3.Row:
        with self.database.reader() as connection:
            return self._require_in(connection, flow_id)

    @staticmethod
    def _require_in(connection: sqlite3.Connection, flow_id: str) -> sqlite3.Row:
        if not isinstance(flow_id, str) or not re.fullmatch(r"devflow_[0-9a-f]{32}", flow_id):
            raise DeviceAuthorizationNotFound("device authorization was not found")
        row = connection.execute(
            "SELECT * FROM managed_device_flows WHERE flow_id=?", (flow_id,)
        ).fetchone()
        if row is None:
            raise DeviceAuthorizationNotFound("device authorization was not found")
        return row

    def _require_owned(
        self, connection: sqlite3.Connection, flow_id: str, lease_token: str
    ) -> sqlite3.Row:
        row = self._require_in(connection, flow_id)
        self._assert_owned(row, lease_token)
        return row

    @staticmethod
    def _assert_owned(row: sqlite3.Row, lease_token: str) -> None:
        if not secrets.compare_digest(str(row["poll_lease_token"] or ""), lease_token):
            raise DeviceAuthorizationConflict("device poll lease was lost")

    @staticmethod
    def _projection(row: sqlite3.Row) -> DeviceFlowProjection:
        status = DeviceFlowStatus(row["status"])
        return DeviceFlowProjection(
            flow_id=row["flow_id"],
            status=status,
            user_code=row["user_code"],
            verification_url=row["verification_url"],
            expires_at=_time(row["expires_at"]),
            poll_interval_seconds=int(row["poll_interval_seconds"]),
            next_poll_at=_time(row["next_poll_at"]),
            restart_required=status is DeviceFlowStatus.AUTHORIZED,
            session_generation=row["session_generation"],
            error_code=row["error_code"],
        )

    def _delete_secret(self, reference: str) -> None:
        try:
            self.vault.delete(reference)
        except Exception:
            return

    def _utc_now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("device authorization clock must be timezone-aware")
        return value.astimezone(UTC)


class DeviceAuthorizationSupervisor:
    def __init__(
        self,
        service: ManagedDeviceAuthorizationService,
        *,
        poll_seconds: float = 1.0,
        on_authorized: Callable[[DeviceFlowProjection], None] | None = None,
        close_broker_on_stop: bool = False,
        max_concurrent_polls: int = 4,
        poll_timeout_seconds: float = 30.0,
        broker_close_timeout_seconds: float = 5.0,
        authorized_callback_timeout_seconds: float = 5.0,
        maintenance_allowed: Callable[[], bool] | None = None,
        execution_gate: RuntimeExecutionGate | None = None,
    ) -> None:
        if not 0.05 <= poll_seconds <= 30:
            raise ValueError("device supervisor poll interval is invalid")
        self.service = service
        self.poll_seconds = poll_seconds
        if on_authorized is not None and not callable(on_authorized):
            raise ValueError("device authorization callback must be callable")
        self.on_authorized = on_authorized
        self.close_broker_on_stop = bool(close_broker_on_stop)
        if not 1 <= max_concurrent_polls <= 20:
            raise ValueError("device supervisor concurrency is invalid")
        if not 0.05 <= poll_timeout_seconds <= 120:
            raise ValueError("device supervisor poll timeout is invalid")
        if not 0.1 <= broker_close_timeout_seconds <= 30:
            raise ValueError("device broker close timeout is invalid")
        if not 0.05 <= authorized_callback_timeout_seconds <= 30:
            raise ValueError("device authorization callback timeout is invalid")
        self.max_concurrent_polls = int(max_concurrent_polls)
        self.poll_timeout_seconds = float(poll_timeout_seconds)
        self.broker_close_timeout_seconds = float(broker_close_timeout_seconds)
        self.authorized_callback_timeout_seconds = float(
            authorized_callback_timeout_seconds
        )
        if maintenance_allowed is not None and not callable(maintenance_allowed):
            raise ValueError("device maintenance authority must be callable")
        self.maintenance_allowed = maintenance_allowed or (lambda: True)
        if execution_gate is not None:
            self.service.bind_execution_gate(execution_gate)
        self.execution_gate = execution_gate or self.service.execution_gate
        self._broker_closed = False
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def broker_closed(self) -> bool:
        return self._broker_closed

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="ecorex-device-authorization")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            await self._close_broker()
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self._close_broker()

    def notify(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            if not self._maintenance_is_allowed():
                await self._wait_for_work()
                continue
            try:
                await asyncio.to_thread(self.service.expire_due)
                due = await asyncio.to_thread(
                    self.service.due_flow_ids,
                    limit=self.max_concurrent_polls,
                )
                if due:
                    await asyncio.gather(*(self._poll(flow_id) for flow_id in due))
            except asyncio.CancelledError:
                raise
            except DeviceAuthorizationError:
                # A closing Runtime epoch is expected during fail-closed
                # transitions. Keep the supervisor alive but start no work.
                pass
            await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
        except TimeoutError:
            pass

    async def _poll(self, flow_id: str) -> None:
        if not self._maintenance_is_allowed():
            return
        try:
            result = await asyncio.wait_for(
                self.service.poll_once(flow_id),
                timeout=self.poll_timeout_seconds,
            )
            if (
                result.status is DeviceFlowStatus.AUTHORIZED
                and self.on_authorized is not None
            ):
                await asyncio.wait_for(
                    asyncio.to_thread(self.on_authorized, result),
                    timeout=self.authorized_callback_timeout_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable poll lease makes timeouts and restarts recoverable.
            return

    def _maintenance_is_allowed(self) -> bool:
        try:
            return bool(self.maintenance_allowed()) and (
                self.execution_gate is None
                or self.execution_gate.snapshot().healthy
            )
        except Exception:
            return False

    async def _close_broker(self) -> None:
        if not self.close_broker_on_stop or self._broker_closed:
            return
        close = getattr(self.service.broker, "aclose", None)
        if callable(close):
            try:
                await asyncio.wait_for(
                    close(),
                    timeout=self.broker_close_timeout_seconds,
                )
            except Exception:
                # Runtime shutdown must remain bounded even when a provider
                # transport refuses to close. The supervisor is not restarted.
                pass
        self._broker_closed = True


__all__ = [
    "BrokerDeviceChallenge",
    "BrokerDeviceGrant",
    "BrokerPollResult",
    "BrokerPollStatus",
    "DeviceAuthorizationBroker",
    "DeviceAuthorizationConflict",
    "DeviceAuthorizationError",
    "DeviceAuthorizationNotFound",
    "DeviceAuthorizationSupervisor",
    "DeviceAuthorizationUnavailable",
    "DeviceFlowProjection",
    "DeviceFlowStatus",
    "ManagedDeviceAuthorizationService",
]

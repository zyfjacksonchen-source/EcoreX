"""Durable, single-flight managed-session access-token rotation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
import sqlite3
from typing import Protocol

from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.schema_catalog import validate_product_schema

from .device import BrokerDeviceGrant
from .device_transport import DeviceRefreshInvalidGrant
from .models import (
    ManagedSessionSnapshot,
    SessionConflict,
    SessionRefreshContext,
    SessionUnavailable,
)
from .service import ManagedSessionService


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SessionRefreshError(RuntimeError):
    pass


class SessionReauthorizationRequired(SessionRefreshError):
    pass


class ManagedSessionRefreshBroker(Protocol):
    async def refresh(
        self,
        *,
        lease_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> BrokerDeviceGrant: ...


@dataclass(frozen=True, slots=True)
class SessionRefreshProjection:
    status: str
    source_lease_digest: str | None
    attempt: int
    next_attempt_at: datetime | None
    error_code: str | None


class ManagedSessionRefreshRepository:
    def __init__(
        self, database: SQLiteDatabase | str, *, initialize: bool = True
    ) -> None:
        self.database = (
            database
            if isinstance(database, SQLiteDatabase)
            else SQLiteDatabase(database)
        )
        self.validate()
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO managed_session_refresh_state("
                "singleton,status,attempt,updated_at) VALUES(1,'idle',0,?) "
                "ON CONFLICT(singleton) DO NOTHING",
                ("1970-01-01T00:00:00Z",),
            )

    def validate(self) -> None:
        with self.database.reader() as connection:
            validate_product_schema(connection)

    def projection(self) -> SessionRefreshProjection:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM managed_session_refresh_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            return SessionRefreshProjection("idle", None, 0, None, None)
        return self._projection(row)

    def claim(
        self,
        *,
        source_lease_digest: str,
        request_hash: str,
        now: datetime,
        lease_seconds: int,
    ) -> tuple[SessionRefreshProjection, str | None]:
        _require_digest(source_lease_digest)
        _require_digest(request_hash)
        token = secrets.token_hex(24)
        with self.database.transaction() as connection:
            row = self._row(connection)
            if (
                row["status"] == "reauthorization_required"
                and row["source_lease_digest"] == source_lease_digest
            ):
                return self._projection(row), None
            if (
                row["status"] == "refreshing"
                and row["source_lease_digest"] == source_lease_digest
                and row["claim_expires_at"]
                and _time(row["claim_expires_at"]) > now
            ):
                return self._projection(row), None
            if (
                row["status"] == "retry_scheduled"
                and row["source_lease_digest"] == source_lease_digest
                and row["next_attempt_at"]
                and _time(row["next_attempt_at"]) > now
            ):
                return self._projection(row), None
            attempt = (
                int(row["attempt"]) + 1
                if row["source_lease_digest"] == source_lease_digest
                else 1
            )
            connection.execute(
                "UPDATE managed_session_refresh_state SET status='refreshing',"
                "source_lease_digest=?,request_hash=?,attempt=?,claim_token=?,"
                "claim_expires_at=?,next_attempt_at=NULL,error_code=NULL,updated_at=? "
                "WHERE singleton=1",
                (
                    source_lease_digest,
                    request_hash,
                    attempt,
                    token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                ),
            )
            return self._projection(self._row(connection)), token

    def retry(
        self,
        *,
        source_lease_digest: str,
        claim_token: str,
        now: datetime,
        retry_seconds: int,
        error_code: str,
    ) -> SessionRefreshProjection:
        with self.database.transaction() as connection:
            self._require_claim(connection, source_lease_digest, claim_token)
            connection.execute(
                "UPDATE managed_session_refresh_state SET status='retry_scheduled',"
                "claim_token=NULL,claim_expires_at=NULL,next_attempt_at=?,error_code=?,"
                "updated_at=? WHERE singleton=1",
                (_iso(now + timedelta(seconds=retry_seconds)), error_code, _iso(now)),
            )
            return self._projection(self._row(connection))

    def require_reauthorization(
        self,
        *,
        source_lease_digest: str,
        claim_token: str,
        now: datetime,
    ) -> SessionRefreshProjection:
        with self.database.transaction() as connection:
            self._require_claim(connection, source_lease_digest, claim_token)
            connection.execute(
                "UPDATE managed_session_refresh_state SET "
                "status='reauthorization_required',claim_token=NULL,"
                "claim_expires_at=NULL,next_attempt_at=NULL,error_code='invalid_grant',"
                "updated_at=? WHERE singleton=1",
                (_iso(now),),
            )
            return self._projection(self._row(connection))

    def complete(
        self,
        *,
        source_lease_digest: str,
        claim_token: str,
        active_lease_digest: str,
        now: datetime,
    ) -> SessionRefreshProjection:
        _require_digest(active_lease_digest)
        with self.database.transaction() as connection:
            self._require_claim(connection, source_lease_digest, claim_token)
            connection.execute(
                "UPDATE managed_session_refresh_state SET status='idle',"
                "source_lease_digest=?,request_hash=NULL,attempt=0,claim_token=NULL,"
                "claim_expires_at=NULL,next_attempt_at=NULL,error_code=NULL,updated_at=? "
                "WHERE singleton=1",
                (active_lease_digest, _iso(now)),
            )
            return self._projection(self._row(connection))

    def recover(
        self, *, active_lease_digest: str | None, now: datetime
    ) -> SessionRefreshProjection:
        with self.database.transaction() as connection:
            row = self._row(connection)
            if (
                row["status"] != "idle"
                and active_lease_digest is not None
                and active_lease_digest != row["source_lease_digest"]
            ):
                connection.execute(
                    "UPDATE managed_session_refresh_state SET status='idle',"
                    "source_lease_digest=?,request_hash=NULL,attempt=0,"
                    "claim_token=NULL,claim_expires_at=NULL,next_attempt_at=NULL,"
                    "error_code=NULL,updated_at=? WHERE singleton=1",
                    (active_lease_digest, _iso(now)),
                )
                return self._projection(self._row(connection))
            if row["status"] == "refreshing":
                if not row["claim_expires_at"] or _time(row["claim_expires_at"]) <= now:
                    connection.execute(
                        "UPDATE managed_session_refresh_state SET status='retry_scheduled',"
                        "claim_token=NULL,claim_expires_at=NULL,next_attempt_at=?,"
                        "error_code='refresh_interrupted',updated_at=? WHERE singleton=1",
                        (_iso(now), _iso(now)),
                    )
            return self._projection(self._row(connection))

    @staticmethod
    def _projection(row: sqlite3.Row) -> SessionRefreshProjection:
        return SessionRefreshProjection(
            status=str(row["status"]),
            source_lease_digest=row["source_lease_digest"],
            attempt=int(row["attempt"]),
            next_attempt_at=(
                _time(row["next_attempt_at"]) if row["next_attempt_at"] else None
            ),
            error_code=row["error_code"],
        )

    @staticmethod
    def _row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_session_refresh_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise SessionUnavailable("managed session refresh state is unavailable")
        return row

    def _require_claim(
        self, connection: sqlite3.Connection, source_digest: str, claim_token: str
    ) -> None:
        row = self._row(connection)
        if (
            row["status"] != "refreshing"
            or row["source_lease_digest"] != source_digest
            or row["claim_token"] != claim_token
        ):
            raise SessionConflict("managed session refresh claim changed")


class ManagedSessionRefreshService:
    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        session: ManagedSessionService,
        broker: ManagedSessionRefreshBroker,
        refresh_ahead_seconds: int = 180,
        request_timeout_seconds: float = 30,
        claim_lease_seconds: int = 60,
        clock=lambda: datetime.now(UTC),
        initialize: bool = True,
    ) -> None:
        if not 60 <= refresh_ahead_seconds <= 10 * 60:
            raise ValueError("managed session refresh window is invalid")
        if (
            not 1 <= request_timeout_seconds <= 120
            or not 10 <= claim_lease_seconds <= 300
        ):
            raise ValueError("managed session refresh timing is invalid")
        self.repository = ManagedSessionRefreshRepository(
            database, initialize=initialize
        )
        self.session = session
        self.broker = broker
        self.refresh_ahead_seconds = refresh_ahead_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.claim_lease_seconds = claim_lease_seconds
        self.clock = clock
        self._lock = asyncio.Lock()

    def converge_startup(self) -> None:
        self.repository.initialize()

    def recover(self) -> SessionRefreshProjection:
        try:
            self.session.recover()
            active = self.session.read_data_scope_snapshot()
            digest = active.lease_digest
        except SessionUnavailable:
            digest = None
        return self.repository.recover(active_lease_digest=digest, now=self._now())

    async def refresh_if_due(self, *, force: bool = False) -> SessionRefreshProjection:
        async with self._lock:
            context = await asyncio.to_thread(self.session.refresh_context)
            now = self._now()
            if not force and context.access_expires_at - now > timedelta(
                seconds=self.refresh_ahead_seconds
            ):
                return self.repository.projection()
            source_digest = context.lease.digest
            request_hash = hashlib.sha256(
                b"ecorex-session-refresh-request-v1\0" + source_digest.encode("ascii")
            ).hexdigest()
            projection, claim_token = self.repository.claim(
                source_lease_digest=source_digest,
                request_hash=request_hash,
                now=now,
                lease_seconds=self.claim_lease_seconds,
            )
            if projection.status == "reauthorization_required":
                raise SessionReauthorizationRequired(
                    "managed session requires device authorization"
                )
            if claim_token is None:
                return projection
            try:
                grant = await asyncio.wait_for(
                    self.broker.refresh(
                        lease_id=context.lease.claims.lease_id,
                        refresh_token=context.refresh_token,
                        idempotency_key=f"session-refresh:{source_digest}",
                    ),
                    timeout=self.request_timeout_seconds,
                )
                self._validate_grant(context, grant, now=now)
                snapshot = await asyncio.to_thread(
                    self.session.install,
                    grant.lease,
                    access_token=grant.access_token,
                    refresh_token=grant.refresh_token,
                    client_request_id=f"session-refresh:{source_digest}",
                )
                return self.repository.complete(
                    source_lease_digest=source_digest,
                    claim_token=claim_token,
                    active_lease_digest=snapshot.lease_digest,
                    now=self._now(),
                )
            except DeviceRefreshInvalidGrant:
                self.repository.require_reauthorization(
                    source_lease_digest=source_digest,
                    claim_token=claim_token,
                    now=self._now(),
                )
                self._audit_failure(context, "invalid_grant")
                raise SessionReauthorizationRequired(
                    "managed session requires device authorization"
                ) from None
            except SessionReauthorizationRequired:
                raise
            except Exception as error:
                attempt = max(1, projection.attempt)
                self.repository.retry(
                    source_lease_digest=source_digest,
                    claim_token=claim_token,
                    now=self._now(),
                    retry_seconds=min(300, 5 * (2 ** min(attempt - 1, 6))),
                    error_code=type(error).__name__.casefold()[:64],
                )
                raise SessionRefreshError(
                    f"managed session refresh failed safely: {type(error).__name__}"
                ) from None

    @staticmethod
    def _validate_grant(
        context: SessionRefreshContext,
        grant: BrokerDeviceGrant,
        *,
        now: datetime,
    ) -> None:
        old = context.lease.claims
        new = grant.lease.claims
        if (
            new.account_id != old.account_id
            or new.organization_id != old.organization_id
            or new.roles != old.roles
            or new.model_allowlist != old.model_allowlist
            or dict(new.quota) != dict(old.quota)
            or new.admin_denies != old.admin_denies
            or new.expires_at != old.expires_at
            or new.revision <= old.revision
            or new.issued_at < now - timedelta(minutes=2)
            or new.issued_at > now + timedelta(minutes=2)
        ):
            raise SessionRefreshError("managed session refresh policy changed")

    def _audit_failure(self, context: SessionRefreshContext, reason: str) -> None:
        try:
            self.session.repository.record_audit(
                event_type="session.refresh.reauthorization_required",
                outcome="failed",
                reason_code=reason,
                client_request_hash=None,
                lease=context.lease,
                generation=self.session.repository.state().generation,
                details={},
                now=_iso(self._now()),
            )
        except Exception:
            pass

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SessionRefreshError("managed session refresh clock is invalid")
        return value.astimezone(UTC)


class ManagedSessionRefreshSupervisor:
    def __init__(
        self,
        service: ManagedSessionRefreshService,
        *,
        poll_seconds: float = 30,
    ) -> None:
        if not 1 <= poll_seconds <= 300:
            raise ValueError("managed session refresh poll interval is invalid")
        self.service = service
        self.poll_seconds = poll_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._closed or self.running:
            return
        await asyncio.to_thread(self.service.recover)
        self._task = asyncio.create_task(self._run(), name="managed-session-refresh")

    def notify(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.service.refresh_if_due()
            except (
                SessionUnavailable,
                SessionRefreshError,
                SessionReauthorizationRequired,
            ):
                pass
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


def _require_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("managed session refresh digest is invalid")


def _iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SessionRefreshError("managed session refresh timestamp is invalid")
    return parsed.astimezone(UTC)


__all__ = [
    "ManagedSessionRefreshBroker",
    "ManagedSessionRefreshRepository",
    "ManagedSessionRefreshService",
    "ManagedSessionRefreshSupervisor",
    "SessionReauthorizationRequired",
    "SessionRefreshError",
    "SessionRefreshProjection",
]

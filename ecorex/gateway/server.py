"""Cloud-side managed Model Gateway with durable idempotent streaming.

The local Runtime never receives provider credentials.  A deployment injects
an authenticated provider adapter here; the adapter and its secrets stay in
the cloud process.  Request identity, quota admission and every emitted NDJSON
event are committed before bytes are sent to a client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .models import GatewayEvent, GatewayEventType, ModelGatewayRequest
from .schema import GatewaySchemaManager, GatewaySchemaReceipt


_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_EVENTS = 10_000
_ZERO_DIGEST = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(request: ModelGatewayRequest) -> str:
    return hashlib.sha256(
        b"ecorex-managed-gateway-request-v1\n"
        + _canonical(request.model_dump(mode="json"))
    ).hexdigest()


def _event_entry_digest(
    request_id: str,
    seq: int,
    payload_sha256: str,
    created_at: str,
    previous_digest: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (request_id, str(seq), payload_sha256, created_at, previous_digest)
        ).encode("utf-8")
    ).hexdigest()


def _fallback_response_id(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return "response_" + digest[:32]


def _bearer(value: str) -> str:
    scheme, separator, token = value.partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not 24 <= len(token) <= 4096
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise PermissionError("managed gateway authentication failed")
    return token


@dataclass(frozen=True, slots=True)
class GatewayPrincipal:
    subject: str
    account_id: str
    allowed_model_ids: frozenset[str]
    quota_period: str
    request_limit: int
    concurrent_request_limit: int = 4

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or any(character.isspace() or ord(character) < 32 for character in value)
            for value in (self.subject, self.account_id, self.quota_period)
        ):
            raise ValueError("gateway principal identity is incomplete")
        if not self.allowed_model_ids or any(
            not _SAFE_MODEL_ID.fullmatch(model_id)
            for model_id in self.allowed_model_ids
        ):
            raise ValueError("gateway principal model allowlist is invalid")
        if isinstance(self.request_limit, bool) or not 1 <= self.request_limit <= 1_000_000:
            raise ValueError("gateway principal request limit is invalid")
        if (
            isinstance(self.concurrent_request_limit, bool)
            or not 1 <= self.concurrent_request_limit <= 1_000
        ):
            raise ValueError("gateway principal concurrency limit is invalid")


class GatewayAuthenticator(Protocol):
    def authenticate(self, bearer_token: str) -> GatewayPrincipal:
        ...


class ManagedProviderAdapter(Protocol):
    """Cloud-only provider boundary; implementations own provider secrets."""

    def stream(
        self,
        request: ModelGatewayRequest,
        principal: GatewayPrincipal,
    ) -> AsyncIterator[GatewayEvent]:
        ...


class GatewayServiceLifecycle(Protocol):
    """Optional production lifecycle; tests and embedded use can omit it."""

    @property
    def accepting(self) -> bool: ...

    @property
    def live(self) -> bool: ...

    async def startup(self) -> None: ...

    async def readiness(self) -> bool: ...

    def admit_stream(self) -> bool: ...

    def release_stream(self) -> None: ...

    def begin_drain(self) -> None: ...

    async def shutdown(self) -> None: ...


class RejectingGatewayAuthenticator:
    def authenticate(self, bearer_token: str) -> GatewayPrincipal:
        del bearer_token
        raise PermissionError("managed gateway authentication is not configured")


class GatewayStoreError(RuntimeError):
    pass


class GatewayRequestConflict(GatewayStoreError):
    pass


class GatewayRequestActive(GatewayStoreError):
    pass


class GatewayQuotaExceeded(GatewayStoreError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayReservation:
    mode: str
    lease_token: str | None
    events: tuple[GatewayEvent, ...] = ()


class SQLiteGatewayStore:
    """Small cloud-side WAL store for quota and stream replay."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.schema_receipt: GatewaySchemaReceipt = GatewaySchemaManager(
            self.path
        ).validate()

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA recursive_triggers=ON")
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise GatewayStoreError("gateway durable state is unavailable") from None

    def reserve(
        self,
        request: ModelGatewayRequest,
        principal: GatewayPrincipal,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> GatewayReservation:
        now = now or _utcnow()
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("gateway store lease is invalid")
        if now.tzinfo is None:
            raise ValueError("gateway store time must be timezone-aware")
        now = now.astimezone(timezone.utc)
        fingerprint = _fingerprint(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["account_id"] != principal.account_id
                    or row["request_fingerprint"] != fingerprint
                ):
                    raise GatewayRequestConflict(
                        "gateway request identity was reused with different input"
                    )
                if row["status"] == "completed":
                    events = self._events(connection, request.request_id)
                    connection.commit()
                    return GatewayReservation("replay", None, events)
                try:
                    expiry = datetime.fromisoformat(str(row["lease_expires_at"]))
                except (TypeError, ValueError) as error:
                    raise GatewayStoreError(
                        "gateway request lease timestamp is invalid"
                    ) from error
                if expiry.tzinfo is None:
                    raise GatewayStoreError("gateway request lease timestamp is invalid")
                if expiry > now:
                    raise GatewayRequestActive("gateway request is already active")
                # A crashed/partitioned provider call is never invoked twice.  Its
                # durable partial stream converges to a terminal retryable fact.
                events = self._events(connection, request.request_id)
                if (
                    events
                    and events[-1].event_type
                    is GatewayEventType.TOOL_CALL_REQUESTED
                ):
                    # Compatibility recovery for a pre-handoff build that
                    # persisted the tool event but left its request active.
                    # No provider call and no extra quota admission occurs.
                    finalized = connection.execute(
                        "UPDATE gateway_requests SET status='completed', "
                        "lease_token=NULL, lease_expires_at=NULL, "
                        "terminal_event_type=?, updated_at=? "
                        "WHERE request_id=? AND status='active' AND lease_token=?",
                        (
                            GatewayEventType.TOOL_CALL_REQUESTED.value,
                            _iso(now),
                            request.request_id,
                            row["lease_token"],
                        ),
                    )
                    if finalized.rowcount != 1:
                        raise GatewayRequestConflict(
                            "gateway handoff recovery lease was lost"
                        )
                    connection.commit()
                    return GatewayReservation("replay", None, events)
                seq = len(events) + 1
                response_id = row["response_id"] or _fallback_response_id(
                    request.request_id
                )
                recovery_token = "gwrecovery_" + secrets.token_hex(24)
                renewed = connection.execute(
                    "UPDATE gateway_requests SET lease_token=?, lease_expires_at=?, updated_at=? "
                    "WHERE request_id=? AND status='active' AND lease_token=?",
                    (
                        recovery_token,
                        _iso(now + timedelta(seconds=lease_seconds)),
                        _iso(now),
                        request.request_id,
                        row["lease_token"],
                    ),
                )
                if renewed.rowcount != 1:
                    raise GatewayRequestConflict(
                        "gateway request recovery lease was lost"
                    )
                failure = GatewayEvent(
                    seq=seq,
                    event_type=GatewayEventType.RESPONSE_FAILED,
                    response_id=response_id,
                    error_code="gateway_execution_uncertain",
                    error_message="The prior managed model attempt could not be safely resumed.",
                    retryable=False,
                )
                self._append_in_transaction(
                    connection, request.request_id, recovery_token, failure, now
                )
                self._complete_in_transaction(
                    connection, request.request_id, recovery_token, failure, now
                )
                replay = self._events(connection, request.request_id)
                connection.commit()
                return GatewayReservation("replay", None, replay)

            count = connection.execute(
                "SELECT COUNT(*) AS count FROM gateway_requests "
                "WHERE account_id=? AND quota_period=?",
                (principal.account_id, principal.quota_period),
            ).fetchone()["count"]
            if int(count) >= principal.request_limit:
                raise GatewayQuotaExceeded("managed model request quota is exhausted")
            active_count = connection.execute(
                "SELECT COUNT(*) AS count FROM gateway_requests "
                "WHERE account_id=? AND status='active' AND lease_expires_at>?",
                (principal.account_id, _iso(now)),
            ).fetchone()["count"]
            if int(active_count) >= principal.concurrent_request_limit:
                raise GatewayQuotaExceeded(
                    "managed model concurrent request quota is exhausted"
                )
            lease_token = "gwlease_" + secrets.token_hex(24)
            connection.execute(
                "INSERT INTO gateway_requests("
                "request_id, account_id, quota_period, request_fingerprint, model_id, "
                "trace_id, status, lease_token, lease_expires_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    request.request_id,
                    principal.account_id,
                    principal.quota_period,
                    fingerprint,
                    request.model_id,
                    request.trace_id,
                    lease_token,
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    _iso(now),
                ),
            )
            connection.commit()
            return GatewayReservation("execute", lease_token)
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway durable state is unavailable") from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def append(
        self,
        request_id: str,
        lease_token: str,
        event: GatewayEvent,
    ) -> None:
        if event.event_type in {
            GatewayEventType.TOOL_CALL_REQUESTED,
            GatewayEventType.RESPONSE_COMPLETED,
            GatewayEventType.RESPONSE_FAILED,
        }:
            raise GatewayStoreError(
                "terminal or handoff gateway events require atomic append_terminal"
            )
        now = _utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._append_in_transaction(connection, request_id, lease_token, event, now)
            connection.commit()
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway durable state is unavailable") from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def append_terminal(
        self,
        request_id: str,
        lease_token: str,
        event: GatewayEvent,
    ) -> None:
        """Append the terminal fact and close the request in one transaction."""

        now = _utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._append_in_transaction(connection, request_id, lease_token, event, now)
            self._complete_in_transaction(connection, request_id, lease_token, event, now)
            connection.commit()
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway durable state is unavailable") from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        lease_token: str | None,
        event: GatewayEvent,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM gateway_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != "active"
            or not lease_token
            or not secrets.compare_digest(row["lease_token"] or "", lease_token)
        ):
            raise GatewayRequestConflict("gateway request lease was lost")
        try:
            lease_expires_at = datetime.fromisoformat(str(row["lease_expires_at"]))
        except (TypeError, ValueError) as error:
            raise GatewayStoreError("gateway request lease timestamp is invalid") from error
        if lease_expires_at.tzinfo is None or lease_expires_at <= now:
            raise GatewayRequestConflict("gateway request lease expired")
        expected = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM gateway_events "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()["seq"]
        )
        if event.seq != expected:
            raise GatewayStoreError("gateway event sequence is not contiguous")
        if row["response_id"] is not None and row["response_id"] != event.response_id:
            raise GatewayStoreError("gateway provider changed response identity")
        payload = _canonical(event.model_dump(mode="json"))
        if len(payload) > _MAX_EVENT_BYTES:
            raise GatewayStoreError("gateway event exceeds its size limit")
        totals = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))), 0) AS bytes, "
            "COUNT(*) AS count "
            "FROM gateway_events WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if (
            int(totals["count"]) >= _MAX_EVENTS
            or int(totals["bytes"]) + len(payload) > _MAX_RESPONSE_BYTES
        ):
            raise GatewayStoreError("gateway response exceeds its durable size limit")
        previous = connection.execute(
            "SELECT entry_digest FROM gateway_events WHERE request_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        previous_digest = previous["entry_digest"] if previous is not None else _ZERO_DIGEST
        if not isinstance(previous_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", previous_digest
        ):
            raise GatewayStoreError("gateway event chain is invalid")
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        created_at = _iso(now)
        entry_digest = _event_entry_digest(
            request_id,
            event.seq,
            payload_sha256,
            created_at,
            previous_digest,
        )
        connection.execute(
            "INSERT INTO gateway_events("
            "request_id, seq, payload_json, payload_sha256, previous_digest, "
            "entry_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                event.seq,
                payload.decode("utf-8"),
                payload_sha256,
                previous_digest,
                entry_digest,
                created_at,
            ),
        )
        connection.execute(
            "UPDATE gateway_requests SET response_id=COALESCE(response_id, ?), updated_at=? "
            "WHERE request_id=?",
            (event.response_id, _iso(now), request_id),
        )

    def _complete_in_transaction(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        lease_token: str | None,
        event: GatewayEvent,
        now: datetime,
    ) -> None:
        if event.event_type not in {
            GatewayEventType.TOOL_CALL_REQUESTED,
            GatewayEventType.RESPONSE_COMPLETED,
            GatewayEventType.RESPONSE_FAILED,
        }:
            raise GatewayStoreError(
                "only a terminal or tool handoff event can complete a request"
            )
        result = connection.execute(
            "UPDATE gateway_requests SET status='completed', lease_token=NULL, "
            "lease_expires_at=NULL, terminal_event_type=?, updated_at=? "
            "WHERE request_id=? AND status='active' AND lease_token=?",
            (event.event_type.value, _iso(now), request_id, lease_token),
        )
        if result.rowcount != 1:
            raise GatewayRequestConflict("gateway request completion lease was lost")

    def _events(
        self, connection: sqlite3.Connection, request_id: str
    ) -> tuple[GatewayEvent, ...]:
        rows = connection.execute(
            "SELECT * FROM gateway_events WHERE request_id=? ORDER BY seq",
            (request_id,),
        ).fetchall()
        events: list[GatewayEvent] = []
        previous_digest = _ZERO_DIGEST
        for expected, row in enumerate(rows, start=1):
            encoded = row["payload_json"].encode("utf-8")
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            expected_entry = _event_entry_digest(
                request_id,
                expected,
                payload_sha256,
                row["created_at"],
                previous_digest,
            )
            if (
                row["seq"] != expected
                or payload_sha256 != row["payload_sha256"]
                or row["previous_digest"] != previous_digest
                or row["entry_digest"] != expected_entry
            ):
                raise GatewayStoreError("durable gateway event integrity check failed")
            try:
                event = GatewayEvent.model_validate_json(encoded)
            except (ValidationError, ValueError, TypeError) as error:
                raise GatewayStoreError(
                    "durable gateway event contract is invalid"
                ) from error
            if _canonical(event.model_dump(mode="json")) != encoded:
                raise GatewayStoreError("durable gateway event encoding is non-canonical")
            events.append(event)
            previous_digest = expected_entry
        request_row = connection.execute(
            "SELECT status, response_id, terminal_event_type FROM gateway_requests "
            "WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if request_row is None:
            raise GatewayStoreError("gateway request record is missing")
        response_terminals = [
            event
            for event in events
            if event.event_type
            in {
                GatewayEventType.RESPONSE_COMPLETED,
                GatewayEventType.RESPONSE_FAILED,
            }
        ]
        handoffs = [
            event
            for event in events
            if event.event_type is GatewayEventType.TOOL_CALL_REQUESTED
        ]
        terminals = response_terminals + handoffs
        if any(event.response_id != request_row["response_id"] for event in events):
            raise GatewayStoreError("gateway response identity ledger is inconsistent")
        if request_row["status"] == "completed":
            if (
                len(terminals) != 1
                or not events
                or terminals[0] is not events[-1]
                or terminals[0].event_type.value != request_row["terminal_event_type"]
            ):
                raise GatewayStoreError("gateway terminal ledger is inconsistent")
        elif request_row["status"] == "active":
            legacy_handoff = (
                len(handoffs) == 1
                and not response_terminals
                and events
                and handoffs[0] is events[-1]
            )
            if (
                (terminals and not legacy_handoff)
                or request_row["terminal_event_type"] is not None
            ):
                raise GatewayStoreError("active gateway request contains a terminal fact")
        else:
            raise GatewayStoreError("gateway request status is invalid")
        return tuple(events)

    def events(self, request_id: str) -> tuple[GatewayEvent, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            events = self._events(connection, request_id)
            connection.commit()
            return events
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway durable state is unavailable") from None
        finally:
            connection.close()


def _ndjson(events: Iterable[GatewayEvent]) -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        for event in events:
            yield _canonical(event.model_dump(mode="json")) + b"\n"

    return generate()


@asynccontextmanager
async def _closing_provider_stream(events: AsyncIterator[GatewayEvent]):
    """Close provider resources without letting SDK finalizers leak secrets."""

    try:
        yield events
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            with suppress(Exception, asyncio.CancelledError):
                await close()


def create_managed_gateway_app(
    store: SQLiteGatewayStore,
    *,
    authenticator: GatewayAuthenticator,
    provider: ManagedProviderAdapter,
    allowed_model_ids: frozenset[str],
    lease_seconds: int = 180,
    service_lifecycle: GatewayServiceLifecycle | None = None,
) -> FastAPI:
    if not allowed_model_ids or any(
        not _SAFE_MODEL_ID.fullmatch(model_id) for model_id in allowed_model_ids
    ):
        raise ValueError("managed gateway model catalog is invalid")
    if not 30 <= lease_seconds <= 900:
        raise ValueError("managed gateway lease must be between 30 and 900 seconds")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if service_lifecycle is None:
            yield
            return
        started = False
        try:
            await service_lifecycle.startup()
            started = True
            yield
        finally:
            service_lifecycle.begin_drain()
            # shutdown is idempotent and also owns partial-start cleanup.
            await service_lifecycle.shutdown()

    app = FastAPI(
        title="EcoreX Managed Model Gateway",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.service_lifecycle = service_lifecycle

    def principal(request: Request) -> GatewayPrincipal:
        try:
            current = authenticator.authenticate(
                _bearer(request.headers.get("authorization", ""))
            )
            if not isinstance(current, GatewayPrincipal):
                raise PermissionError("managed gateway principal is invalid")
            return current
        except PermissionError as error:
            raise HTTPException(
                status_code=401, detail="managed gateway authentication failed"
            ) from None
        except Exception:
            raise HTTPException(
                status_code=503, detail="managed gateway authentication is unavailable"
            ) from None

    @app.middleware("http")
    async def secure_transport(request: Request, call_next):
        if (
            service_lifecycle is not None
            and not service_lifecycle.accepting
            and request.url.path not in {"/health/live", "/health/ready"}
        ):
            return JSONResponse(
                status_code=503,
                content={"status": "draining"},
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                parsed_length = int(content_length)
                if parsed_length < 0:
                    raise ValueError
                if parsed_length > _MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "managed gateway request is too large"},
                        headers={
                            "Cache-Control": "no-store",
                            "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "no-referrer",
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Content-Length is invalid"},
                    headers={
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Referrer-Policy": "no-referrer",
                    },
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    if service_lifecycle is not None:

        @app.get("/health/live", include_in_schema=False)
        async def health_live() -> JSONResponse:
            live = service_lifecycle.live
            return JSONResponse(
                status_code=200 if live else 503,
                content={"status": "live" if live else "stopped"},
            )

        @app.get("/health/ready", include_in_schema=False)
        async def health_ready() -> JSONResponse:
            try:
                ready = await service_lifecycle.readiness()
            except Exception:
                ready = False
            return JSONResponse(
                status_code=200 if ready else 503,
                content={"status": "ready" if ready else "unavailable"},
            )

    @app.get("/api/v1/models")
    def models(current: GatewayPrincipal = Depends(principal)) -> dict[str, object]:
        visible = sorted(allowed_model_ids & current.allowed_model_ids)
        return {"schema_version": 1, "models": visible}

    @app.post("/v1/responses", response_model=None, include_in_schema=False)
    @app.post("/api/v1/model/stream", response_model=None)
    async def stream_model(
        request: Request,
        current: GatewayPrincipal = Depends(principal),
    ):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if media_type != "application/json":
            raise HTTPException(
                status_code=415, detail="managed gateway requires application/json"
            )
        if request.headers.get("x-ecorex-protocol") != "1":
            raise HTTPException(status_code=400, detail="managed gateway protocol is required")
        encoded_request = bytearray()
        async for chunk in request.stream():
            encoded_request.extend(chunk)
            if len(encoded_request) > _MAX_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413, detail="managed gateway request is too large"
                )
        try:
            body = ModelGatewayRequest.model_validate_json(bytes(encoded_request))
        except (ValidationError, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=422, detail="managed gateway request is invalid"
            ) from None
        canonical_request = _canonical(body.model_dump(mode="json"))
        if len(canonical_request) > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="managed gateway request is too large")
        if (
            body.model_id not in allowed_model_ids
            or body.model_id not in current.allowed_model_ids
        ):
            raise HTTPException(status_code=403, detail="managed model is not allowed")
        admitted = False
        if service_lifecycle is not None:
            admitted = service_lifecycle.admit_stream()
            if not admitted:
                raise HTTPException(
                    status_code=503,
                    detail="managed gateway is draining",
                    headers={"Retry-After": "1"},
                )
        try:
            reservation = await asyncio.to_thread(
                store.reserve,
                body,
                current,
                lease_seconds=lease_seconds,
            )
        except GatewayRequestConflict as error:
            if admitted:
                service_lifecycle.release_stream()
            raise HTTPException(
                status_code=409, detail="gateway request identity conflict"
            ) from error
        except GatewayRequestActive as error:
            if admitted:
                service_lifecycle.release_stream()
            raise HTTPException(
                status_code=503,
                detail="gateway request is still active",
                headers={"Retry-After": "1"},
            ) from error
        except GatewayQuotaExceeded as error:
            if admitted:
                service_lifecycle.release_stream()
            raise HTTPException(
                status_code=429, detail="managed model quota is exhausted"
            ) from error
        except GatewayStoreError as error:
            if admitted:
                service_lifecycle.release_stream()
            raise HTTPException(
                status_code=503, detail="gateway durable state is unavailable"
            ) from error
        except BaseException:
            if admitted:
                service_lifecycle.release_stream()
            raise

        async def tracked(events: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            try:
                async for event in events:
                    yield event
            finally:
                if admitted:
                    service_lifecycle.release_stream()

        if reservation.mode == "replay":
            return StreamingResponse(
                tracked(_ndjson(reservation.events)),
                media_type="application/x-ndjson",
                headers={"X-EcoreX-Replay": "true"},
            )

        lease_token = reservation.lease_token
        assert lease_token is not None

        async def generate() -> AsyncIterator[bytes]:
            expected_seq = 1
            response_id: str | None = None
            terminal = False
            try:
                provider_events = provider.stream(body, current)
                async with _closing_provider_stream(provider_events):
                    async for event in provider_events:
                        event = GatewayEvent.model_validate(event)
                        if event.seq != expected_seq or terminal:
                            raise GatewayStoreError("provider stream sequence is invalid")
                        if response_id is not None and event.response_id != response_id:
                            raise GatewayStoreError("provider changed response identity")
                        response_id = response_id or event.response_id
                        if event.event_type is GatewayEventType.RESPONSE_FAILED:
                            # Provider SDK errors must never become an exfiltration
                            # channel for credentials or upstream diagnostics.
                            event = event.model_copy(
                                update={
                                    "error_code": "provider_response_failed",
                                    "error_message": (
                                        "The managed model provider rejected the request."
                                    ),
                                }
                            )
                        is_terminal = event.event_type in {
                            GatewayEventType.TOOL_CALL_REQUESTED,
                            GatewayEventType.RESPONSE_COMPLETED,
                            GatewayEventType.RESPONSE_FAILED,
                        }
                        if is_terminal:
                            await asyncio.to_thread(
                                store.append_terminal,
                                body.request_id,
                                lease_token,
                                event,
                            )
                        else:
                            await asyncio.to_thread(
                                store.append,
                                body.request_id,
                                lease_token,
                                event,
                            )
                        # The provider's terminal claim is not authoritative until
                        # the event and request state commit atomically.
                        terminal = is_terminal
                        expected_seq += 1
                        # Advance the durable cursor before yielding. A client can
                        # disconnect while this generator is suspended at yield;
                        # cancellation must then append the following sequence.
                        yield _canonical(event.model_dump(mode="json")) + b"\n"
                        if terminal:
                            return
                raise GatewayStoreError("provider stream ended before a terminal event")
            except (asyncio.CancelledError, GeneratorExit):
                if not terminal:
                    failure = GatewayEvent(
                        seq=expected_seq,
                        event_type=GatewayEventType.RESPONSE_FAILED,
                        response_id=response_id or _fallback_response_id(body.request_id),
                        error_code="gateway_cancelled",
                        error_message="The managed model attempt was cancelled.",
                        retryable=False,
                    )
                    try:
                        await asyncio.to_thread(
                            store.append_terminal,
                            body.request_id,
                            lease_token,
                            failure,
                        )
                    except GatewayStoreError:
                        pass
                raise
            except Exception as error:
                if terminal:
                    # A provider finalizer failed after the durable terminal fact.
                    # The client has a complete replayable response; never replace
                    # it with an upstream exception that may contain a secret.
                    return
                failure = GatewayEvent(
                    seq=expected_seq,
                    event_type=GatewayEventType.RESPONSE_FAILED,
                    response_id=response_id or _fallback_response_id(body.request_id),
                    error_code="provider_stream_failed",
                    error_message="The managed model attempt did not complete.",
                    retryable=bool(getattr(error, "retryable", True)),
                )
                try:
                    await asyncio.to_thread(
                        store.append_terminal,
                        body.request_id,
                        lease_token,
                        failure,
                    )
                except GatewayStoreError:
                    # A concurrent recovery may already have converged the same
                    # request.  Never expose its internal exception or a provider
                    # secret in the response body.
                    return
                yield _canonical(failure.model_dump(mode="json")) + b"\n"

        return StreamingResponse(
            tracked(generate()),
            media_type="application/x-ndjson",
        )

    return app

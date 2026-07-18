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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .models import (
    GatewayAccountUsageProjection,
    GatewayEvent,
    GatewayEventType,
    GatewayFunctionCallOutputInput,
    GatewayTokenUsageWindow,
    ModelGatewayRequest,
)
from .handoff import ChatModelRevision, DurableChatHandoff
from .schema import GatewaySchemaManager, GatewaySchemaReceipt


_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_EVENTS = 10_000
_MAX_ACTIVE_CHAT_HANDOFFS_PER_ACCOUNT = 256
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


@dataclass(frozen=True, slots=True)
class GatewayCompletedUsageFact:
    request_id: str
    account_id: str
    terminal_event_type: GatewayEventType
    input_tokens: int
    output_tokens: int
    total_tokens: int
    provider_created_at: datetime

    def __post_init__(self) -> None:
        if (
            not self.request_id
            or not self.account_id
            or self.terminal_event_type
            not in {
                GatewayEventType.RESPONSE_COMPLETED,
                GatewayEventType.TOOL_CALL_REQUESTED,
            }
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in (
                    self.input_tokens,
                    self.output_tokens,
                    self.total_tokens,
                )
            )
            or self.total_tokens < self.input_tokens + self.output_tokens
            or self.total_tokens <= 0
            or self.provider_created_at.tzinfo is None
        ):
            raise ValueError("gateway completed usage fact is invalid")


class GatewayUsageAccountant(Protocol):
    """Cross-database settlement boundary backed by an idempotent fact store."""

    def settle(self, fact: GatewayCompletedUsageFact) -> None: ...

    def reconcile(self, facts: Iterable[GatewayCompletedUsageFact]) -> None: ...

    def tokens_available(self, account_id: str) -> bool: ...

    def project(
        self,
        account_id: str,
        *,
        timezone_name: str,
    ) -> GatewayAccountUsageProjection: ...


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
    # Durable identity/lease/handoff failures are never an instruction to
    # submit another possibly billable provider POST.
    retryable = False


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
                    self._enqueue_usage_settlement_in_transaction(
                        connection, request.request_id, now
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

    def bind_chat_model_attempt(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        *,
        ttl_seconds: int,
    ) -> None:
        if not 300 <= ttl_seconds <= 86_400:
            raise ValueError("chat handoff TTL is invalid")
        if revision.provider_protocol != "openai_compatible_chat":
            raise GatewayStoreError("chat handoff protocol is invalid")
        now = _utcnow()
        values = (
            request.request_id,
            request.thread_id,
            request.turn_id,
            revision.config_id,
            revision.revision,
            revision.local_model_id,
            revision.upstream_model_id,
            revision.provider_protocol,
            revision.provider_origin_preset,
            _iso(now + timedelta(seconds=ttl_seconds)),
            _iso(now),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,model_id FROM gateway_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if row is None or row["status"] != "active" or row["model_id"] != request.model_id:
                raise GatewayRequestConflict("gateway model attempt is not active")
            existing = connection.execute(
                "SELECT * FROM gateway_model_attempts WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                identity = tuple(existing[key] for key in (
                    "request_id", "thread_id", "turn_id", "model_config_id",
                    "model_config_revision", "local_model_id", "upstream_model_id",
                    "provider_protocol", "provider_origin_preset",
                ))
                if identity != values[:9]:
                    raise GatewayRequestConflict("gateway model revision changed")
                connection.commit()
                return
            account = connection.execute(
                "SELECT account_id FROM gateway_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            active_handoffs = connection.execute(
                "SELECT COUNT(*) FROM gateway_chat_handoffs handoffs "
                "JOIN gateway_requests requests "
                "ON requests.request_id=handoffs.source_request_id "
                "WHERE requests.account_id=? AND handoffs.state IN ('pending','available') "
                "AND handoffs.expires_at>?",
                (account["account_id"], _iso(now)),
            ).fetchone()[0]
            if int(active_handoffs) >= _MAX_ACTIVE_CHAT_HANDOFFS_PER_ACCOUNT:
                raise GatewayQuotaExceeded("chat handoff quota is exhausted")
            connection.execute(
                "INSERT INTO gateway_model_attempts("
                "request_id,thread_id,turn_id,model_config_id,model_config_revision,"
                "local_model_id,upstream_model_id,provider_protocol,"
                "provider_origin_preset,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.commit()
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway handoff state is unavailable") from None
        finally:
            connection.close()

    def stage_chat_handoff(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        event: GatewayEvent,
        *,
        provider_tool_name: str,
        arguments_json: str,
    ) -> None:
        if event.event_type is not GatewayEventType.TOOL_CALL_REQUESTED:
            raise GatewayStoreError("chat handoff event is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider_tool_name):
            raise GatewayStoreError("chat handoff tool identity is invalid")
        try:
            arguments = json.loads(arguments_json)
        except (TypeError, json.JSONDecodeError):
            raise GatewayStoreError("chat handoff arguments are invalid") from None
        canonical = _canonical(arguments).decode("utf-8")
        if canonical != arguments_json or len(canonical.encode("utf-8")) > 1024 * 1024:
            raise GatewayStoreError("chat handoff arguments are invalid")
        now = _utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM gateway_model_attempts WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            self._validate_attempt(attempt, request, revision)
            existing = connection.execute(
                "SELECT * FROM gateway_chat_handoffs WHERE source_request_id=?",
                (request.request_id,),
            ).fetchone()
            identity = (
                event.response_id,
                event.tool_call_id,
                provider_tool_name,
                canonical,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
            if existing is not None:
                current = tuple(existing[key] for key in (
                    "response_id", "tool_call_id", "provider_tool_name",
                    "arguments_json", "arguments_sha256",
                ))
                if current != identity:
                    raise GatewayRequestConflict("chat handoff identity changed")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO gateway_chat_handoffs("
                "source_request_id,response_id,tool_call_id,provider_tool_name,"
                "arguments_json,arguments_sha256,state,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (
                    request.request_id,
                    *identity,
                    attempt["expires_at"],
                    _iso(now),
                ),
            )
            connection.commit()
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway handoff state is unavailable") from None
        finally:
            connection.close()

    def consume_chat_handoff(
        self,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
        *,
        now: datetime | None = None,
    ) -> DurableChatHandoff | None:
        outputs = [
            item
            for item in request.ordered_input_items()
            if isinstance(item, GatewayFunctionCallOutputInput)
        ]
        if not outputs:
            return None
        if request.previous_response_id is None or len(outputs) != 1:
            raise GatewayRequestConflict("chat handoff continuation is invalid")
        now = now or _utcnow()
        if now.tzinfo is None:
            raise ValueError("gateway handoff time must be timezone-aware")
        now = now.astimezone(timezone.utc)
        connection = self._connect()
        failure: GatewayStoreError | None = None
        result: DurableChatHandoff | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM gateway_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            row = connection.execute(
                "SELECT handoffs.*,attempts.thread_id,attempts.turn_id,"
                "attempts.model_config_id,attempts.model_config_revision,"
                "attempts.local_model_id,attempts.upstream_model_id,"
                "attempts.provider_protocol,attempts.provider_origin_preset,"
                "requests.account_id AS source_account_id "
                "FROM gateway_chat_handoffs handoffs "
                "JOIN gateway_model_attempts attempts "
                "ON attempts.request_id=handoffs.source_request_id "
                "JOIN gateway_requests requests "
                "ON requests.request_id=handoffs.source_request_id "
                "WHERE handoffs.response_id=?",
                (request.previous_response_id,),
            ).fetchone()
            if target is None or target["status"] != "active" or row is None:
                raise GatewayRequestConflict("chat handoff is unavailable")
            try:
                expiry = datetime.fromisoformat(str(row["expires_at"]))
            except (TypeError, ValueError):
                expiry = datetime.min.replace(tzinfo=timezone.utc)
                connection.execute(
                    "UPDATE gateway_chat_handoffs SET state='corrupt' "
                    "WHERE source_request_id=? AND state!='consumed'",
                    (row["source_request_id"],),
                )
                failure = GatewayStoreError("chat handoff is corrupt")
            if failure is None and (
                expiry.tzinfo is None or expiry <= now
            ):
                connection.execute(
                    "UPDATE gateway_chat_handoffs SET state='expired' "
                    "WHERE source_request_id=? AND state IN ('pending','available')",
                    (row["source_request_id"],),
                )
                failure = GatewayRequestConflict("chat handoff expired")
            digest = hashlib.sha256(str(row["arguments_json"]).encode("utf-8")).hexdigest()
            try:
                decoded = json.loads(str(row["arguments_json"]))
                canonical = _canonical(decoded).decode("utf-8")
            except (TypeError, json.JSONDecodeError):
                canonical = ""
            if failure is None and (
                digest != row["arguments_sha256"]
                or canonical != row["arguments_json"]
            ):
                connection.execute(
                    "UPDATE gateway_chat_handoffs SET state='corrupt' "
                    "WHERE source_request_id=? AND state!='consumed'",
                    (row["source_request_id"],),
                )
                failure = GatewayStoreError("chat handoff is corrupt")
            expected_identity = (
                request.thread_id,
                request.turn_id,
                revision.config_id,
                revision.revision,
                revision.local_model_id,
                revision.upstream_model_id,
                revision.provider_protocol,
                revision.provider_origin_preset,
                target["account_id"],
            )
            actual_identity = tuple(row[key] for key in (
                "thread_id", "turn_id", "model_config_id", "model_config_revision",
                "local_model_id", "upstream_model_id", "provider_protocol",
                "provider_origin_preset", "source_account_id",
            ))
            if failure is None and (
                actual_identity != expected_identity
                or target["model_id"] != revision.local_model_id
                or request.model_id != revision.local_model_id
            ):
                failure = GatewayRequestConflict("chat handoff configuration changed")
            if failure is None and (
                row["state"] != "available"
                or row["tool_call_id"] != outputs[0].tool_call_id
            ):
                failure = GatewayRequestConflict("chat handoff was already consumed")
            if failure is None:
                updated = connection.execute(
                    "UPDATE gateway_chat_handoffs SET state='consumed',"
                    "consumed_by_request_id=?,consumed_at=? "
                    "WHERE source_request_id=? AND state='available'",
                    (request.request_id, _iso(now), row["source_request_id"]),
                )
                if updated.rowcount != 1:
                    raise GatewayRequestConflict("chat handoff was already consumed")
                result = DurableChatHandoff(
                    response_id=str(row["response_id"]),
                    tool_call_id=str(row["tool_call_id"]),
                    provider_tool_name=str(row["provider_tool_name"]),
                    arguments_json=str(row["arguments_json"]),
                )
            connection.commit()
        except GatewayStoreError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection.in_transaction:
                connection.rollback()
            raise GatewayStoreError("gateway handoff state is unavailable") from None
        finally:
            connection.close()
        if failure is not None:
            raise failure
        return result

    @staticmethod
    def _validate_attempt(
        attempt: sqlite3.Row | None,
        request: ModelGatewayRequest,
        revision: ChatModelRevision,
    ) -> None:
        if attempt is None or tuple(attempt[key] for key in (
            "thread_id", "turn_id", "model_config_id", "model_config_revision",
            "local_model_id", "upstream_model_id", "provider_protocol",
            "provider_origin_preset",
        )) != (
            request.thread_id,
            request.turn_id,
            revision.config_id,
            revision.revision,
            revision.local_model_id,
            revision.upstream_model_id,
            revision.provider_protocol,
            revision.provider_origin_preset,
        ):
            raise GatewayRequestConflict("gateway model revision changed")

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
            if event.event_type is GatewayEventType.TOOL_CALL_REQUESTED:
                self._promote_chat_handoff(connection, request_id, event, now)
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

    @staticmethod
    def _promote_chat_handoff(
        connection: sqlite3.Connection,
        request_id: str,
        event: GatewayEvent,
        now: datetime,
    ) -> None:
        attempt = connection.execute(
            "SELECT provider_protocol FROM gateway_model_attempts WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if attempt is None:
            # Responses tool handoffs use provider-side previous_response_id and
            # do not require a Chat Completions reconstruction record.
            return
        if attempt["provider_protocol"] != "openai_compatible_chat":
            raise GatewayStoreError("gateway handoff protocol is invalid")
        row = connection.execute(
            "SELECT * FROM gateway_chat_handoffs WHERE source_request_id=?",
            (request_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "pending"
            or row["response_id"] != event.response_id
            or row["tool_call_id"] != event.tool_call_id
        ):
            raise GatewayStoreError("durable chat handoff is missing")
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]))
        except (TypeError, ValueError):
            raise GatewayStoreError("durable chat handoff is corrupt") from None
        if expiry.tzinfo is None or expiry <= now:
            raise GatewayStoreError("durable chat handoff expired before commit")
        updated = connection.execute(
            "UPDATE gateway_chat_handoffs SET state='available',available_at=? "
            "WHERE source_request_id=? AND state='pending'",
            (_iso(now), request_id),
        )
        if updated.rowcount != 1:
            raise GatewayRequestConflict("durable chat handoff changed")

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
        if event.event_type in {
            GatewayEventType.TOOL_CALL_REQUESTED,
            GatewayEventType.RESPONSE_COMPLETED,
        }:
            self._enqueue_usage_settlement_in_transaction(
                connection, request_id, now
            )

    @staticmethod
    def _enqueue_usage_settlement_in_transaction(
        connection: sqlite3.Connection,
        request_id: str,
        now: datetime,
    ) -> None:
        timestamp = _iso(now)
        connection.execute(
            "INSERT INTO gateway_usage_settlements("
            "request_id,state,attempt_count,next_attempt_at,created_at,"
            "updated_at,settled_at,last_error_code"
            ") VALUES(?,'pending',0,?,?,?,NULL,NULL) "
            "ON CONFLICT(request_id) DO NOTHING",
            (request_id, timestamp, timestamp, timestamp),
        )

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

    def account_usage(
        self,
        account_id: str,
        *,
        timezone_name: str,
        now: datetime | None = None,
    ) -> GatewayAccountUsageProjection:
        """Project exactly-once provider usage for one authenticated account."""

        if not isinstance(account_id, str) or not account_id:
            raise ValueError("gateway usage account identity is invalid")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("gateway usage timezone is required")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise ValueError("gateway usage timezone is invalid") from None
        calculated_at = now or _utcnow()
        if calculated_at.tzinfo is None:
            raise ValueError("gateway usage clock must be timezone-aware")
        calculated_at = calculated_at.astimezone(timezone.utc)
        local_now = calculated_at.astimezone(zone)
        day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start_local = day_start_local - timedelta(days=day_start_local.weekday())
        day_start = day_start_local.astimezone(timezone.utc)
        week_start = week_start_local.astimezone(timezone.utc)

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            coverage_row = connection.execute(
                """
                SELECT events.created_at
                FROM gateway_requests AS requests
                JOIN gateway_events AS events
                  ON events.request_id=requests.request_id
                 AND events.seq=(
                    SELECT MAX(candidate.seq)
                    FROM gateway_events AS candidate
                    WHERE candidate.request_id=requests.request_id
                 )
                WHERE requests.account_id=?
                  AND requests.status='completed'
                  AND requests.terminal_event_type IN (
                    'response.completed','tool_call.requested'
                  )
                ORDER BY datetime(events.created_at), requests.request_id
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT requests.request_id, events.created_at
                FROM gateway_requests AS requests
                JOIN gateway_events AS events
                  ON events.request_id=requests.request_id
                 AND events.seq=(
                    SELECT MAX(candidate.seq)
                    FROM gateway_events AS candidate
                    WHERE candidate.request_id=requests.request_id
                 )
                WHERE requests.account_id=?
                  AND requests.status='completed'
                  AND requests.terminal_event_type IN (
                    'response.completed','tool_call.requested'
                  )
                  AND datetime(events.created_at) >= datetime(?)
                ORDER BY datetime(events.created_at), requests.request_id
                """,
                (account_id, _iso(week_start)),
            ).fetchall()
            today = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            week = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            seen_request_ids: set[str] = set()
            for row in rows:
                request_id = str(row["request_id"])
                if request_id in seen_request_ids:
                    continue
                seen_request_ids.add(request_id)
                events = self._events(connection, request_id)
                if (
                    not events
                    or events[-1].event_type
                    not in {
                        GatewayEventType.RESPONSE_COMPLETED,
                        GatewayEventType.TOOL_CALL_REQUESTED,
                    }
                ):
                    raise GatewayStoreError("gateway usage terminal fact is inconsistent")
                usage = events[-1].usage or {}
                input_tokens = int(
                    usage.get("input_tokens", usage.get("prompt_tokens", 0))
                )
                output_tokens = int(
                    usage.get("output_tokens", usage.get("completion_tokens", 0))
                )
                total_tokens = max(
                    int(usage.get("total_tokens", 0)),
                    input_tokens + output_tokens,
                )
                values = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                }
                for key, value in values.items():
                    week[key] += value
                try:
                    completed_at = datetime.fromisoformat(str(row["created_at"]))
                except ValueError as error:
                    raise GatewayStoreError(
                        "gateway usage timestamp is invalid"
                    ) from error
                if completed_at.tzinfo is None:
                    raise GatewayStoreError("gateway usage timestamp is invalid")
                if completed_at.astimezone(timezone.utc) > calculated_at:
                    continue
                if completed_at.astimezone(timezone.utc) >= day_start:
                    for key, value in values.items():
                        today[key] += value
            coverage_started_at: datetime | None = None
            if coverage_row is not None:
                try:
                    coverage_started_at = datetime.fromisoformat(
                        str(coverage_row["created_at"])
                    )
                except ValueError as error:
                    raise GatewayStoreError(
                        "gateway usage coverage timestamp is invalid"
                    ) from error
                if coverage_started_at.tzinfo is None:
                    raise GatewayStoreError(
                        "gateway usage coverage timestamp is invalid"
                    )
                coverage_started_at = coverage_started_at.astimezone(timezone.utc)
            connection.commit()
            return GatewayAccountUsageProjection(
                timezone=timezone_name,
                today=GatewayTokenUsageWindow(**today),
                week=GatewayTokenUsageWindow(**week),
                week_started_at=week_start,
                coverage_started_at=coverage_started_at,
                calculated_at=calculated_at,
            )
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

    def completed_usage_facts(
        self,
        *,
        account_id: str | None = None,
        request_id: str | None = None,
        maximum: int = 100_000,
    ) -> tuple[GatewayCompletedUsageFact, ...]:
        """Read validated billable terminal facts from the durable event outbox."""

        if account_id is not None and (
            not isinstance(account_id, str) or not account_id
        ):
            raise ValueError("gateway usage account identity is invalid")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id
        ):
            raise ValueError("gateway usage request identity is invalid")
        if isinstance(maximum, bool) or not 1 <= maximum <= 1_000_000:
            raise ValueError("gateway usage fact limit is invalid")
        filters = [
            "requests.status='completed'",
            "requests.terminal_event_type IN "
            "('response.completed','tool_call.requested')",
        ]
        parameters: list[object] = []
        if account_id is not None:
            filters.append("requests.account_id=?")
            parameters.append(account_id)
        if request_id is not None:
            filters.append("requests.request_id=?")
            parameters.append(request_id)
        where = " AND ".join(filters)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM gateway_requests AS requests WHERE "
                    + where,
                    tuple(parameters),
                ).fetchone()[0]
            )
            if count > maximum:
                raise GatewayStoreError("gateway usage fact ledger is oversized")
            rows = connection.execute(
                "SELECT requests.request_id,requests.account_id,events.created_at "
                "FROM gateway_requests AS requests "
                "JOIN gateway_events AS events "
                "ON events.request_id=requests.request_id "
                "AND events.seq=("
                "SELECT MAX(candidate.seq) FROM gateway_events AS candidate "
                "WHERE candidate.request_id=requests.request_id"
                ") WHERE "
                + where
                + " ORDER BY datetime(events.created_at),requests.request_id",
                tuple(parameters),
            ).fetchall()
            facts: list[GatewayCompletedUsageFact] = []
            for row in rows:
                events = self._events(connection, str(row["request_id"]))
                if not events:
                    raise GatewayStoreError(
                        "gateway usage terminal fact is inconsistent"
                    )
                terminal = events[-1]
                if terminal.event_type not in {
                    GatewayEventType.RESPONSE_COMPLETED,
                    GatewayEventType.TOOL_CALL_REQUESTED,
                }:
                    raise GatewayStoreError(
                        "gateway usage terminal fact is inconsistent"
                    )
                usage = terminal.usage
                if not isinstance(usage, dict):
                    continue
                input_tokens = int(
                    usage.get("input_tokens", usage.get("prompt_tokens", 0))
                )
                output_tokens = int(
                    usage.get("output_tokens", usage.get("completion_tokens", 0))
                )
                total_tokens = max(
                    int(usage.get("total_tokens", 0)),
                    input_tokens + output_tokens,
                )
                if total_tokens <= 0:
                    continue
                try:
                    provider_created_at = datetime.fromisoformat(
                        str(row["created_at"])
                    )
                except ValueError as error:
                    raise GatewayStoreError(
                        "gateway usage timestamp is invalid"
                    ) from error
                facts.append(
                    GatewayCompletedUsageFact(
                        request_id=str(row["request_id"]),
                        account_id=str(row["account_id"]),
                        terminal_event_type=terminal.event_type,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                        provider_created_at=provider_created_at,
                    )
                )
            connection.commit()
            return tuple(facts)
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

    def pending_usage_facts(
        self,
        *,
        account_id: str | None = None,
        request_id: str | None = None,
        maximum: int = 64,
        now: datetime | None = None,
    ) -> tuple[GatewayCompletedUsageFact, ...]:
        """Claim a bounded view of the durable provider-usage outbox.

        Reading never scans the historical ledger. Terminal rows with missing
        provider usage are retained as ``usage_missing`` evidence instead of
        being retried forever or silently treated as zero.
        """

        if account_id is not None and (
            not isinstance(account_id, str) or not account_id
        ):
            raise ValueError("gateway usage account identity is invalid")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id
        ):
            raise ValueError("gateway usage request identity is invalid")
        if isinstance(maximum, bool) or not 1 <= maximum <= 1024:
            raise ValueError("gateway usage settlement batch is invalid")
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("gateway store time must be timezone-aware")
        current = current.astimezone(timezone.utc)
        filters = [
            "settlements.state='pending'",
            "settlements.next_attempt_at<=?",
        ]
        parameters: list[object] = [_iso(current)]
        if account_id is not None:
            filters.append("requests.account_id=?")
            parameters.append(account_id)
        if request_id is not None:
            filters.append("requests.request_id=?")
            parameters.append(request_id)
        parameters.append(maximum)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT requests.request_id,requests.account_id,events.created_at "
                "FROM gateway_usage_settlements AS settlements "
                "JOIN gateway_requests AS requests "
                "ON requests.request_id=settlements.request_id "
                "JOIN gateway_events AS events "
                "ON events.request_id=requests.request_id "
                "AND events.seq=("
                "SELECT MAX(candidate.seq) FROM gateway_events AS candidate "
                "WHERE candidate.request_id=requests.request_id"
                ") WHERE "
                + " AND ".join(filters)
                + " ORDER BY settlements.next_attempt_at,requests.request_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
            facts: list[GatewayCompletedUsageFact] = []
            for row in rows:
                fact = self._usage_fact_in_transaction(
                    connection,
                    request_id=str(row["request_id"]),
                    account_id=str(row["account_id"]),
                    provider_created_at=str(row["created_at"]),
                )
                if fact is None:
                    updated = connection.execute(
                        "UPDATE gateway_usage_settlements "
                        "SET state='usage_missing',attempt_count=attempt_count+1,"
                        "updated_at=?,settled_at=?,"
                        "last_error_code='provider_usage_missing' "
                        "WHERE request_id=? AND state='pending'",
                        (_iso(current), _iso(current), str(row["request_id"])),
                    )
                    if updated.rowcount != 1:
                        raise GatewayStoreError(
                            "gateway usage settlement changed"
                        )
                    continue
                facts.append(fact)
            connection.commit()
            return tuple(facts)
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

    def mark_usage_settled(
        self,
        fact: GatewayCompletedUsageFact,
        *,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(fact, GatewayCompletedUsageFact):
            raise ValueError("gateway usage settlement fact is invalid")
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("gateway store time must be timezone-aware")
        current = current.astimezone(timezone.utc)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT settlements.state,requests.account_id,events.created_at "
                "FROM gateway_usage_settlements AS settlements "
                "JOIN gateway_requests AS requests "
                "ON requests.request_id=settlements.request_id "
                "JOIN gateway_events AS events "
                "ON events.request_id=requests.request_id "
                "AND events.seq=("
                "SELECT MAX(candidate.seq) FROM gateway_events AS candidate "
                "WHERE candidate.request_id=requests.request_id"
                ") WHERE settlements.request_id=?",
                (fact.request_id,),
            ).fetchone()
            if row is None:
                raise GatewayStoreError("gateway usage settlement is missing")
            if row["state"] == "settled":
                connection.commit()
                return
            if row["state"] != "pending":
                raise GatewayStoreError("gateway usage settlement changed")
            durable = self._usage_fact_in_transaction(
                connection,
                request_id=fact.request_id,
                account_id=str(row["account_id"]),
                provider_created_at=str(row["created_at"]),
            )
            if durable != fact:
                raise GatewayStoreError("gateway usage settlement fact drifted")
            updated = connection.execute(
                "UPDATE gateway_usage_settlements "
                "SET state='settled',attempt_count=attempt_count+1,"
                "updated_at=?,settled_at=?,last_error_code=NULL "
                "WHERE request_id=? AND state='pending'",
                (_iso(current), _iso(current), fact.request_id),
            )
            if updated.rowcount != 1:
                raise GatewayStoreError("gateway usage settlement changed")
            connection.commit()
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

    def defer_usage_settlement(
        self,
        request_id: str,
        *,
        error_code: str = "control_plane_unavailable",
        now: datetime | None = None,
    ) -> None:
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(error_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{2,63}", error_code) is None
        ):
            raise ValueError("gateway usage settlement retry is invalid")
        current = now or _utcnow()
        if current.tzinfo is None:
            raise ValueError("gateway store time must be timezone-aware")
        current = current.astimezone(timezone.utc)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,attempt_count FROM gateway_usage_settlements "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise GatewayStoreError("gateway usage settlement is missing")
            if row["state"] != "pending":
                connection.commit()
                return
            attempt = int(row["attempt_count"]) + 1
            delay = min(300, 2 ** min(attempt, 8))
            updated = connection.execute(
                "UPDATE gateway_usage_settlements "
                "SET attempt_count=?,next_attempt_at=?,updated_at=?,last_error_code=? "
                "WHERE request_id=? AND state='pending'",
                (
                    attempt,
                    _iso(current + timedelta(seconds=delay)),
                    _iso(current),
                    error_code,
                    request_id,
                ),
            )
            if updated.rowcount != 1:
                raise GatewayStoreError("gateway usage settlement changed")
            connection.commit()
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

    def usage_settlement_counts(self) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT state,COUNT(*) AS count FROM gateway_usage_settlements "
                "GROUP BY state"
            ).fetchall()
            return {
                state: next(
                    (
                        int(row["count"])
                        for row in rows
                        if str(row["state"]) == state
                    ),
                    0,
                )
                for state in ("pending", "settled", "usage_missing")
            }
        except (OSError, sqlite3.Error):
            raise GatewayStoreError("gateway durable state is unavailable") from None
        finally:
            connection.close()

    def has_unsettled_usage(self, account_id: str) -> bool:
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("gateway usage account identity is invalid")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM gateway_usage_settlements AS settlements "
                "JOIN gateway_requests AS requests "
                "ON requests.request_id=settlements.request_id "
                "WHERE settlements.state IN ('pending','usage_missing') "
                "AND requests.account_id=? "
                "LIMIT 1",
                (account_id,),
            ).fetchone()
            return row is not None
        except (OSError, sqlite3.Error):
            raise GatewayStoreError("gateway durable state is unavailable") from None
        finally:
            connection.close()

    def _usage_fact_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        account_id: str,
        provider_created_at: str,
    ) -> GatewayCompletedUsageFact | None:
        events = self._events(connection, request_id)
        if not events:
            raise GatewayStoreError(
                "gateway usage terminal fact is inconsistent"
            )
        terminal = events[-1]
        if terminal.event_type not in {
            GatewayEventType.RESPONSE_COMPLETED,
            GatewayEventType.TOOL_CALL_REQUESTED,
        }:
            raise GatewayStoreError(
                "gateway usage terminal fact is inconsistent"
            )
        usage = terminal.usage
        if not isinstance(usage, dict):
            return None
        input_tokens = int(
            usage.get("input_tokens", usage.get("prompt_tokens", 0))
        )
        output_tokens = int(
            usage.get("output_tokens", usage.get("completion_tokens", 0))
        )
        total_tokens = max(
            int(usage.get("total_tokens", 0)),
            input_tokens + output_tokens,
        )
        if total_tokens <= 0:
            return None
        try:
            created = datetime.fromisoformat(provider_created_at)
        except ValueError as error:
            raise GatewayStoreError(
                "gateway usage timestamp is invalid"
            ) from error
        if created.tzinfo is None:
            raise GatewayStoreError("gateway usage timestamp is invalid")
        return GatewayCompletedUsageFact(
            request_id=request_id,
            account_id=account_id,
            terminal_event_type=terminal.event_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            provider_created_at=created,
        )


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
    dynamic_model_authority: bool = False,
    usage_accountant: GatewayUsageAccountant | None = None,
    lease_seconds: int = 180,
    service_lifecycle: GatewayServiceLifecycle | None = None,
) -> FastAPI:
    if not allowed_model_ids or any(
        not _SAFE_MODEL_ID.fullmatch(model_id) for model_id in allowed_model_ids
    ):
        raise ValueError("managed gateway model catalog is invalid")
    if not 30 <= lease_seconds <= 900:
        raise ValueError("managed gateway lease must be between 30 and 900 seconds")
    catalog_provider = getattr(provider, "public_catalog", None)
    if dynamic_model_authority and not callable(catalog_provider):
        raise ValueError("dynamic managed gateway catalog is unavailable")

    async def settle_usage_outbox(
        *,
        account_id: str | None = None,
        request_id: str | None = None,
        maximum: int = 64,
    ) -> int:
        if usage_accountant is None:
            return 0
        facts = await asyncio.to_thread(
            store.pending_usage_facts,
            account_id=account_id,
            request_id=request_id,
            maximum=maximum,
        )
        settled = 0
        for fact in facts:
            try:
                await asyncio.to_thread(usage_accountant.settle, fact)
                await asyncio.to_thread(store.mark_usage_settled, fact)
                settled += 1
            except Exception:
                with suppress(Exception):
                    await asyncio.to_thread(
                        store.defer_usage_settlement,
                        fact.request_id,
                    )
        return settled

    async def usage_settlement_worker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await settle_usage_outbox(maximum=128)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                continue

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if service_lifecycle is None and usage_accountant is None:
            yield
            return
        settlement_stop = asyncio.Event()
        settlement_task: asyncio.Task[None] | None = None
        try:
            if service_lifecycle is not None:
                await service_lifecycle.startup()
            if usage_accountant is not None:
                await settle_usage_outbox(maximum=256)
                settlement_task = asyncio.create_task(
                    usage_settlement_worker(settlement_stop),
                    name="ecorex-gateway-usage-settlement",
                )
            yield
        finally:
            settlement_stop.set()
            if settlement_task is not None:
                settlement_task.cancel()
                with suppress(asyncio.CancelledError):
                    await settlement_task
            if service_lifecycle is not None:
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
    app.state.usage_accountant = usage_accountant

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

    async def active_chat_catalog() -> tuple[list[dict[str, object]], frozenset[str]]:
        if not callable(catalog_provider):
            raise RuntimeError("managed gateway catalog is unavailable")
        projected = await catalog_provider()
        if not isinstance(projected, list) or len(projected) > 256:
            raise RuntimeError("managed gateway catalog is invalid")
        catalog: list[dict[str, object]] = []
        model_ids: set[str] = set()
        for item in projected:
            if not isinstance(item, dict):
                raise RuntimeError("managed gateway catalog is invalid")
            if "api_key" in item or "secret" in item:
                raise RuntimeError("managed gateway catalog contains secret material")
            if item.get("modality") != "chat":
                continue
            local_model_id = item.get("local_model_id")
            if (
                not isinstance(local_model_id, str)
                or _SAFE_MODEL_ID.fullmatch(local_model_id) is None
                or local_model_id in model_ids
            ):
                raise RuntimeError("managed gateway chat catalog is invalid")
            model_ids.add(local_model_id)
            catalog.append(dict(item))
        return catalog, frozenset(model_ids)

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
    async def models(current: GatewayPrincipal = Depends(principal)) -> dict[str, object]:
        visible = sorted(allowed_model_ids & current.allowed_model_ids)
        catalog: list[dict[str, object]] = []
        if dynamic_model_authority:
            try:
                active_catalog, active_ids = await active_chat_catalog()
                visible = sorted(active_ids & current.allowed_model_ids)
                catalog = [
                    item
                    for item in active_catalog
                    if item["local_model_id"] in visible
                ]
            except Exception:
                # Dynamic catalog is authoritative.  Stale bootstrap mappings
                # must never become an availability or security fallback.
                visible = []
                catalog = []
            return {
                "schema_version": 1,
                "models": visible,
                "catalog": catalog,
            }
        if callable(catalog_provider):
            try:
                projected = await catalog_provider()
                catalog = [
                    item
                    for item in projected
                    if isinstance(item, dict)
                    and item.get("local_model_id") in visible
                    and "api_key" not in item
                ]
                active_ids = {
                    str(item["local_model_id"])
                    for item in catalog
                    if isinstance(item.get("local_model_id"), str)
                }
                visible = [model_id for model_id in visible if model_id in active_ids]
            except Exception:
                # Model streaming remains fail-closed per request.  Catalog
                # refresh is informative and must never expose provider errors.
                catalog = []
                visible = []
        response: dict[str, object] = {"schema_version": 1, "models": visible}
        if callable(catalog_provider):
            response["catalog"] = catalog
        return response

    @app.get(
        "/api/v1/usage",
        response_model=GatewayAccountUsageProjection,
    )
    async def account_usage(
        timezone_name: str = Query(
            default="Asia/Shanghai",
            alias="timezone",
            min_length=1,
            max_length=64,
        ),
        current: GatewayPrincipal = Depends(principal),
    ) -> GatewayAccountUsageProjection:
        if usage_accountant is not None:
            try:
                await settle_usage_outbox(
                    account_id=current.account_id,
                    maximum=16,
                )
                if await asyncio.to_thread(
                    store.has_unsettled_usage,
                    current.account_id,
                ):
                    raise GatewayStoreError(
                        "managed account usage is incomplete"
                    )
                return await asyncio.to_thread(
                    usage_accountant.project,
                    current.account_id,
                    timezone_name=timezone_name,
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="managed account usage is unavailable",
                ) from None
        try:
            return store.account_usage(
                current.account_id,
                timezone_name=timezone_name,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except GatewayStoreError:
            raise HTTPException(
                status_code=503,
                detail="managed gateway usage is unavailable",
            ) from None

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
        if body.model_id not in current.allowed_model_ids:
            raise HTTPException(status_code=403, detail="managed model is not allowed")
        if dynamic_model_authority:
            try:
                _catalog, active_ids = await active_chat_catalog()
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="managed model catalog is unavailable",
                ) from None
            if body.model_id not in active_ids:
                raise HTTPException(status_code=403, detail="managed model is not allowed")
        elif body.model_id not in allowed_model_ids:
            raise HTTPException(status_code=403, detail="managed model is not allowed")
        if usage_accountant is not None:
            try:
                await settle_usage_outbox(
                    account_id=current.account_id,
                    maximum=64,
                )
                unsettled = await asyncio.to_thread(
                    store.has_unsettled_usage,
                    current.account_id,
                )
                available = await asyncio.to_thread(
                    usage_accountant.tokens_available,
                    current.account_id,
                )
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="managed usage settlement is unavailable",
                ) from None
            if unsettled:
                raise HTTPException(
                    status_code=503,
                    detail="managed usage settlement is pending",
                    headers={"Retry-After": "1"},
                )
            if not available:
                raise HTTPException(
                    status_code=429,
                    detail="managed token quota is exhausted",
                )
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
                        if (
                            is_terminal
                            and usage_accountant is not None
                            and event.event_type
                            in {
                                GatewayEventType.RESPONSE_COMPLETED,
                                GatewayEventType.TOOL_CALL_REQUESTED,
                            }
                        ):
                            try:
                                await settle_usage_outbox(
                                    request_id=body.request_id,
                                    maximum=1,
                                )
                            except Exception:
                                # The committed terminal remains a durable
                                # outbox and is retried by the bounded worker.
                                pass
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

"""Encrypted cloud audit ingestion, retention, and RBAC query service."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from ecorex.observability.audit import (
    AuditIntegrityError,
    AuditPayloadCipher,
    AuditRedactor,
    AuditRetentionPolicy,
)
from ecorex.protocol import AuditRecordProjection
from ecorex.runtime.database import json_dumps

from .audit_schema import CloudAuditSchemaManager, CloudAuditSchemaReceipt
from .models import ControlPrincipal


MAX_CLOUD_AUDIT_REQUEST_BYTES = 1024 * 1024
MAX_CLOUD_AUDIT_PAYLOAD_BYTES = 768 * 1024
_SAFE_EVENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_AUDIT_ID = re.compile(r"^audit_[0-9a-f]{64}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_ZERO_INTEGRITY_MAC = "0" * 64


class CloudAuditError(RuntimeError):
    pass


class CloudAuditConflict(CloudAuditError):
    pass


class CloudAuditRejected(CloudAuditError):
    pass


class CloudAuditIntegrityError(CloudAuditError):
    pass


class _CloudModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloudAuditReceipt(_CloudModel):
    audit_id: str
    created: bool
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CloudAuditMetadata(_CloudModel):
    audit_id: str
    source_event_id: str
    category: str
    event_type: str
    account_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary_included: bool = False
    created_at: datetime
    received_at: datetime


class CloudAuditDetail(CloudAuditMetadata):
    payload: dict[str, Any]


class CloudAuditListResponse(_CloudModel):
    records: list[CloudAuditMetadata]
    count: int = Field(ge=0)


class CloudAuditAggregate(_CloudModel):
    day_utc: str
    category: str
    event_type: str
    record_count: int = Field(ge=0)


class CloudAuditAggregateResponse(_CloudModel):
    records: list[CloudAuditAggregate]
    count: int = Field(ge=0)


class CloudAuditRetentionResult(_CloudModel):
    raw_deleted: int = Field(ge=0)
    aggregate_deleted: int = Field(ge=0)
    idempotency_deleted: int = Field(default=0, ge=0)
    raw_days: int = Field(ge=1)
    aggregate_days: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class CloudAuditIntegrityEntry:
    sequence: int
    actor_subject: str
    action: str
    target_id: str
    payload_sha256: str
    previous_mac: str
    entry_mac: str
    created_at: str


class CloudAuditBodyLimitMiddleware:
    """Bound audit ingestion before FastAPI parses potentially sensitive JSON."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = MAX_CLOUD_AUDIT_REQUEST_BYTES,
    ) -> None:
        if not 4096 <= max_bytes <= 8 * 1024 * 1024:
            raise ValueError("cloud audit request size limit is invalid")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/v1/audit/records"
        ):
            await self.app(scope, receive, send)
            return
        lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if lengths:
            raw = lengths[0]
            if (
                len(lengths) != 1
                or len(raw) > 20
                or not raw.isdigit()
                or int(raw) > self.max_bytes
            ):
                await self._reject(scope, receive, send)
                return
        total = 0
        buffered: list[dict[str, Any]] = []
        while True:
            message = await receive()
            buffered.append(message)
            if len(buffered) > 4096:
                await self._reject(scope, receive, send)
                return
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if message.get("more_body", False):
                    continue
            break
        index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        from fastapi.responses import JSONResponse

        async def empty_receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "code": "audit_payload_too_large",
                    "message": "audit record exceeds its size limit",
                }
            },
        )
        await response(scope, empty_receive, send)


def _validate_text(value: str | None, name: str, *, maximum: int = 512) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise CloudAuditRejected(f"{name} is invalid")


def _semantic_record(record: AuditRecordProjection) -> dict[str, Any]:
    # Attempts and local delivery timestamps are deliberately excluded.  A lost
    # 201 response must be replayable with the same immutable cloud identity.
    return {
        "audit_id": record.audit_id,
        "source_event_id": record.source_event_id,
        "category": record.category,
        "event_type": record.event_type,
        "account_id": record.account_id,
        "thread_id": record.thread_id,
        "turn_id": record.turn_id,
        "trace_id": record.trace_id,
        "payload_sha256": record.payload_sha256,
        "binary_included": record.binary_included,
        "created_at": record.created_at.astimezone(UTC).isoformat(),
    }


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


class CloudAuditRepository:
    """WAL repository with encrypted raw payloads and an HMAC access chain."""

    def __init__(
        self,
        path: str | Path,
        *,
        encryption_key: bytes,
        integrity_key: bytes,
        retention: AuditRetentionPolicy | None = None,
        redactor: AuditRedactor | None = None,
        max_payload_bytes: int = MAX_CLOUD_AUDIT_PAYLOAD_BYTES,
    ) -> None:
        material = bytes(integrity_key)
        if len(material) < 32:
            raise ValueError("cloud audit HMAC key must contain at least 32 bytes")
        if not 4096 <= max_payload_bytes <= 4 * 1024 * 1024:
            raise ValueError("cloud audit payload size limit is invalid")
        cipher = AuditPayloadCipher(encryption_key)
        self.path = Path(path).expanduser().resolve()
        self.schema_receipt: CloudAuditSchemaReceipt = CloudAuditSchemaManager(
            self.path
        ).validate()
        self.cipher = cipher
        self.integrity_key = material
        self.retention = retention or AuditRetentionPolicy()
        self.redactor = redactor or AuditRedactor(max_string_bytes=max_payload_bytes)
        self.max_payload_bytes = max_payload_bytes
        self._integrity_checkpoint_lock = threading.RLock()
        self._integrity_checkpoint = (0, _ZERO_INTEGRITY_MAC)
        self._integrity_checkpoint_fault: str | None = None
        self.verify_full_integrity()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
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

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._integrity_checkpoint_snapshot()
            candidate = self._verify_integrity_incremental(
                connection, checkpoint, recheck_tail=True
            )
            yield connection
            candidate = self._verify_integrity_incremental(
                connection, candidate, recheck_tail=False
            )
            connection.commit()
            committed = True
            self._advance_integrity_checkpoint(candidate)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            try:
                connection.close()
            except Exception:
                if not committed:
                    raise
                self._poison_integrity_checkpoint(
                    "cloud audit connection cleanup failed after commit; "
                    "explicit full verification is required"
                )

    def _mac(self, value: str) -> str:
        return hmac.new(
            self.integrity_key, value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _verify_integrity_row(
        self, row: sqlite3.Row, expected_sequence: int, previous: str
    ) -> str:
        if int(row["sequence"]) != expected_sequence or row["previous_mac"] != previous:
            raise CloudAuditIntegrityError("cloud audit integrity chain is invalid")
        material = "\0".join(
            (
                str(expected_sequence),
                previous,
                str(row["actor_subject"]),
                str(row["action"]),
                str(row["target_id"]),
                str(row["payload_sha256"]),
                str(row["created_at"]),
            )
        )
        expected = self._mac(material)
        if not hmac.compare_digest(str(row["entry_mac"]), expected):
            raise CloudAuditIntegrityError("cloud audit integrity chain is invalid")
        return expected

    def _verify_integrity_full_connection(
        self, connection: sqlite3.Connection
    ) -> tuple[int, str]:
        previous = _ZERO_INTEGRITY_MAC
        expected_sequence = 1
        for row in connection.execute(
            "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
            "previous_mac,entry_mac,created_at "
            "FROM cloud_audit_integrity ORDER BY sequence"
        ):
            previous = self._verify_integrity_row(
                row, expected_sequence, previous
            )
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _verify_integrity_incremental(
        self,
        connection: sqlite3.Connection,
        checkpoint: tuple[int, str],
        *,
        recheck_tail: bool,
    ) -> tuple[int, str]:
        checkpoint_sequence, checkpoint_mac = checkpoint
        if checkpoint_sequence < 0 or (
            checkpoint_sequence == 0 and checkpoint_mac != _ZERO_INTEGRITY_MAC
        ):
            raise CloudAuditIntegrityError(
                "cloud audit integrity checkpoint is invalid"
            )
        if recheck_tail and checkpoint_sequence:
            tail = connection.execute(
                "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
                "previous_mac,entry_mac,created_at "
                "FROM cloud_audit_integrity WHERE sequence=?",
                (checkpoint_sequence,),
            ).fetchone()
            if tail is None:
                raise CloudAuditIntegrityError(
                    "cloud audit integrity chain is invalid"
                )
            recomputed = self._verify_integrity_row(
                tail,
                checkpoint_sequence,
                str(tail["previous_mac"]),
            )
            if not hmac.compare_digest(recomputed, checkpoint_mac):
                raise CloudAuditIntegrityError(
                    "cloud audit integrity checkpoint is invalid"
                )

        expected_sequence = checkpoint_sequence + 1
        previous = checkpoint_mac
        for row in connection.execute(
            "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
            "previous_mac,entry_mac,created_at "
            "FROM cloud_audit_integrity WHERE sequence>? ORDER BY sequence",
            (checkpoint_sequence,),
        ):
            previous = self._verify_integrity_row(
                row, expected_sequence, previous
            )
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _integrity_checkpoint_snapshot(self) -> tuple[int, str]:
        with self._integrity_checkpoint_lock:
            if self._integrity_checkpoint_fault is not None:
                raise CloudAuditIntegrityError(self._integrity_checkpoint_fault)
            return self._integrity_checkpoint

    def _advance_integrity_checkpoint(self, candidate: tuple[int, str]) -> None:
        """Merge a committed checkpoint without turning success into failure."""

        with self._integrity_checkpoint_lock:
            current = self._integrity_checkpoint
            if candidate[0] > current[0]:
                self._integrity_checkpoint = candidate
            elif candidate[0] == current[0] and not hmac.compare_digest(
                candidate[1], current[1]
            ):
                self._integrity_checkpoint_fault = (
                    "cloud audit integrity checkpoint is inconsistent; "
                    "explicit full verification is required"
                )

    def _poison_integrity_checkpoint(self, message: str) -> None:
        with self._integrity_checkpoint_lock:
            self._integrity_checkpoint_fault = message

    def verify_full_integrity(self) -> int:
        """Verify the complete HMAC chain and reset the process checkpoint."""

        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            with self._integrity_checkpoint_lock:
                checkpoint = self._verify_integrity_full_connection(connection)
                connection.commit()
                self._integrity_checkpoint = checkpoint
                self._integrity_checkpoint_fault = None
            return checkpoint[0]
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, (CloudAuditIntegrityError, sqlite3.DatabaseError)):
                self._poison_integrity_checkpoint(
                    "cloud audit integrity failed full verification"
                )
            raise
        finally:
            connection.close()

    def _append_integrity(
        self,
        connection: sqlite3.Connection,
        *,
        actor_subject: str,
        action: str,
        target_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = connection.execute(
            "SELECT sequence, entry_mac FROM cloud_audit_integrity "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(previous["sequence"]) + 1 if previous is not None else 1
        previous_mac = str(previous["entry_mac"]) if previous is not None else "0" * 64
        payload_sha256 = _sha256_json(dict(payload))
        created_at = datetime.now(UTC).isoformat()
        entry_mac = self._mac(
            "\0".join(
                (
                    str(sequence),
                    previous_mac,
                    actor_subject,
                    action,
                    target_id,
                    payload_sha256,
                    created_at,
                )
            )
        )
        connection.execute(
            "INSERT INTO cloud_audit_integrity("
            "actor_subject, action, target_id, payload_sha256, previous_mac, "
            "entry_mac, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                actor_subject,
                action,
                target_id,
                payload_sha256,
                previous_mac,
                entry_mac,
                created_at,
            ),
        )

    @staticmethod
    def _associated_data(record: AuditRecordProjection, fingerprint: str) -> str:
        return "\x1f".join(
            (record.audit_id, record.account_id, record.payload_sha256, fingerprint)
        )

    def _validated_payload(self, record: AuditRecordProjection) -> str:
        _validate_text(record.audit_id, "audit_id", maximum=128)
        _validate_text(record.source_event_id, "source_event_id", maximum=512)
        _validate_text(record.account_id, "account_id", maximum=256)
        _validate_text(record.thread_id, "thread_id", maximum=256)
        _validate_text(record.turn_id, "turn_id", maximum=256)
        if not _AUDIT_ID.fullmatch(record.audit_id):
            raise CloudAuditRejected("audit_id is invalid")
        for name, value in (
            ("source_event_id", record.source_event_id),
            ("account_id", record.account_id),
            ("thread_id", record.thread_id),
            ("turn_id", record.turn_id),
        ):
            if value is not None and not _SAFE_IDENTITY.fullmatch(value):
                raise CloudAuditRejected(f"{name} is invalid")
        if not _SAFE_EVENT_TYPE.fullmatch(record.event_type):
            raise CloudAuditRejected("event_type is invalid")
        if record.trace_id is not None and not _HEX_32.fullmatch(record.trace_id):
            raise CloudAuditRejected("trace_id is invalid")
        if not _HEX_64.fullmatch(record.payload_sha256):
            raise CloudAuditRejected("payload digest is invalid")
        try:
            plaintext = json_dumps(record.payload)
        except (TypeError, ValueError):
            raise CloudAuditRejected("audit payload encoding is invalid") from None
        if len(plaintext.encode("utf-8")) > self.max_payload_bytes:
            raise CloudAuditRejected("audit payload exceeds its size limit")
        digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, record.payload_sha256):
            raise CloudAuditRejected("audit payload digest does not match")
        secondary = self.redactor.redact(record.payload)
        if json_dumps(secondary) != plaintext:
            raise CloudAuditRejected("audit payload failed cloud safety validation")
        if record.binary_included is not False:
            raise CloudAuditRejected("binary audit payloads are forbidden")
        return plaintext

    def ingest(
        self,
        principal: ControlPrincipal,
        record: AuditRecordProjection,
        *,
        idempotency_key: str,
    ) -> CloudAuditReceipt:
        if principal.account_id != record.account_id:
            raise CloudAuditRejected("principal account does not match audit account")
        if idempotency_key != record.audit_id:
            raise CloudAuditRejected("audit idempotency key is invalid")
        plaintext = self._validated_payload(record)
        semantic = _semantic_record(record)
        fingerprint = _sha256_json(semantic)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT record_fingerprint, payload_sha256 FROM cloud_audit_idempotency "
                "WHERE audit_id = ?",
                (record.audit_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(
                    str(existing["record_fingerprint"]), fingerprint
                ):
                    raise CloudAuditConflict(
                        "audit identity was reused with different content"
                    )
                self._append_integrity(
                    connection,
                    actor_subject=principal.subject,
                    action="audit.ingest.replayed",
                    target_id=record.audit_id,
                    payload={"account_id": record.account_id, "fingerprint": fingerprint},
                )
                return CloudAuditReceipt(
                    audit_id=record.audit_id,
                    created=False,
                    payload_sha256=str(existing["payload_sha256"]),
                )
            duplicate = connection.execute(
                "SELECT audit_id FROM cloud_audit_idempotency WHERE account_id = ? "
                "AND source_event_id = ? AND category = ? AND event_type = ?",
                (
                    record.account_id,
                    record.source_event_id,
                    record.category,
                    record.event_type,
                ),
            ).fetchone()
            if duplicate is not None:
                raise CloudAuditConflict("audit source identity is already bound")
            envelope = self.cipher.encrypt(
                plaintext,
                associated_data=self._associated_data(record, fingerprint),
            )
            received_at = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO cloud_audit_idempotency("
                "audit_id, source_event_id, category, event_type, account_id, "
                "record_fingerprint, payload_sha256, source_created_at, "
                "first_received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.audit_id,
                    record.source_event_id,
                    record.category,
                    record.event_type,
                    record.account_id,
                    fingerprint,
                    record.payload_sha256,
                    record.created_at.astimezone(UTC).isoformat(),
                    received_at,
                ),
            )
            connection.execute(
                "INSERT INTO cloud_audit_records("
                "audit_id, source_event_id, category, event_type, account_id, "
                "thread_id, turn_id, trace_id, payload_envelope, payload_format, "
                "payload_sha256, record_fingerprint, binary_included, created_at, "
                "received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    record.audit_id,
                    record.source_event_id,
                    record.category,
                    record.event_type,
                    record.account_id,
                    record.thread_id,
                    record.turn_id,
                    record.trace_id,
                    envelope,
                    AuditPayloadCipher.FORMAT,
                    record.payload_sha256,
                    fingerprint,
                    record.created_at.astimezone(UTC).isoformat(),
                    received_at,
                ),
            )
            connection.execute(
                "INSERT INTO cloud_audit_daily(day_utc, category, event_type, record_count) "
                "VALUES (?, ?, ?, 1) ON CONFLICT(day_utc, category, event_type) "
                "DO UPDATE SET record_count = record_count + 1",
                (
                    record.created_at.astimezone(UTC).date().isoformat(),
                    record.category,
                    record.event_type,
                ),
            )
            self._append_integrity(
                connection,
                actor_subject=principal.subject,
                action="audit.ingest.created",
                target_id=record.audit_id,
                payload={"account_id": record.account_id, "fingerprint": fingerprint},
            )
            return CloudAuditReceipt(
                audit_id=record.audit_id,
                created=True,
                payload_sha256=record.payload_sha256,
            )

    @staticmethod
    def _verify_row_fingerprint(row: sqlite3.Row) -> None:
        semantic = {
            "audit_id": str(row["audit_id"]),
            "source_event_id": str(row["source_event_id"]),
            "category": str(row["category"]),
            "event_type": str(row["event_type"]),
            "account_id": str(row["account_id"]),
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "trace_id": row["trace_id"],
            "payload_sha256": str(row["payload_sha256"]),
            "binary_included": False,
            "created_at": datetime.fromisoformat(str(row["created_at"]))
            .astimezone(UTC)
            .isoformat(),
        }
        expected = _sha256_json(semantic)
        if not hmac.compare_digest(str(row["record_fingerprint"]), expected):
            raise CloudAuditIntegrityError("cloud audit metadata fingerprint is invalid")

    @classmethod
    def _metadata(cls, row: sqlite3.Row) -> CloudAuditMetadata:
        cls._verify_row_fingerprint(row)
        return CloudAuditMetadata(
            audit_id=str(row["audit_id"]),
            source_event_id=str(row["source_event_id"]),
            category=str(row["category"]),
            event_type=str(row["event_type"]),
            account_id=str(row["account_id"]),
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            trace_id=row["trace_id"],
            payload_sha256=str(row["payload_sha256"]),
            binary_included=False,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            received_at=datetime.fromisoformat(str(row["received_at"])),
        )

    def list_metadata(
        self,
        actor: ControlPrincipal,
        *,
        account_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> tuple[CloudAuditMetadata, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("cloud audit query limit is invalid")
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("account_id", account_id),
            ("category", category),
            ("event_type", event_type),
        ):
            if value is not None:
                _validate_text(value, column, maximum=256)
                clauses.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(limit)
        sql = "SELECT * FROM cloud_audit_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, audit_id DESC LIMIT ?"
        with self._transaction() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            result_digest = _sha256_json(
                [
                    {
                        "audit_id": row["audit_id"],
                        "payload_sha256": row["payload_sha256"],
                        "record_fingerprint": row["record_fingerprint"],
                    }
                    for row in rows
                ]
            )
            self._append_integrity(
                connection,
                actor_subject=actor.subject,
                action="audit.metadata.queried",
                target_id="audit-list",
                payload={
                    "account_id": account_id,
                    "category": category,
                    "event_type": event_type,
                    "limit": limit,
                    "result_count": len(rows),
                    "result_sha256": result_digest,
                },
            )
            return tuple(self._metadata(row) for row in rows)

    def get_detail(
        self, actor: ControlPrincipal, audit_id: str
    ) -> CloudAuditDetail:
        _validate_text(audit_id, "audit_id", maximum=128)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM cloud_audit_records WHERE audit_id = ?", (audit_id,)
            ).fetchone()
            if row is None:
                raise KeyError(audit_id)
            self._verify_row_fingerprint(row)
            fingerprint = str(row["record_fingerprint"])
            associated_data = "\x1f".join(
                (
                    str(row["audit_id"]),
                    str(row["account_id"]),
                    str(row["payload_sha256"]),
                    fingerprint,
                )
            )
            try:
                plaintext = self.cipher.decrypt(
                    str(row["payload_envelope"]), associated_data=associated_data
                )
            except AuditIntegrityError:
                raise CloudAuditIntegrityError(
                    "cloud audit payload authentication failed"
                ) from None
            if hashlib.sha256(plaintext.encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise CloudAuditIntegrityError("cloud audit payload digest is invalid")
            try:
                payload = json.loads(plaintext)
            except json.JSONDecodeError:
                raise CloudAuditIntegrityError("cloud audit payload encoding is invalid") from None
            if not isinstance(payload, dict) or json_dumps(self.redactor.redact(payload)) != plaintext:
                raise CloudAuditIntegrityError("cloud audit payload safety proof is invalid")
            self._append_integrity(
                connection,
                actor_subject=actor.subject,
                action="audit.payload.accessed",
                target_id=audit_id,
                payload={"account_id": row["account_id"], "payload_sha256": row["payload_sha256"]},
            )
            metadata = self._metadata(row)
            return CloudAuditDetail(**metadata.model_dump(), payload=payload)

    def list_aggregates(
        self, actor: ControlPrincipal, *, limit: int = 1000
    ) -> tuple[CloudAuditAggregate, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("cloud audit aggregate limit is invalid")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM cloud_audit_daily ORDER BY day_utc DESC, category, "
                "event_type LIMIT ?",
                (limit,),
            ).fetchall()
            result_digest = _sha256_json([dict(row) for row in rows])
            self._append_integrity(
                connection,
                actor_subject=actor.subject,
                action="audit.aggregate.queried",
                target_id="audit-aggregate",
                payload={
                    "limit": limit,
                    "result_count": len(rows),
                    "result_sha256": result_digest,
                },
            )
            return tuple(
                CloudAuditAggregate(
                    day_utc=str(row["day_utc"]),
                    category=str(row["category"]),
                    event_type=str(row["event_type"]),
                    record_count=int(row["record_count"]),
                )
                for row in rows
            )

    def enforce_retention(
        self,
        actor: ControlPrincipal,
        *,
        now: datetime | None = None,
    ) -> CloudAuditRetentionResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        raw_cutoff = (current - timedelta(days=self.retention.raw_days)).isoformat()
        aggregate_cutoff = (
            current - timedelta(days=self.retention.aggregate_days)
        ).date().isoformat()
        idempotency_cutoff = (
            current - timedelta(days=self.retention.aggregate_days)
        ).isoformat()
        with self._transaction() as connection:
            raw_deleted = connection.execute(
                "DELETE FROM cloud_audit_records WHERE created_at < ?", (raw_cutoff,)
            ).rowcount
            aggregate_deleted = connection.execute(
                "DELETE FROM cloud_audit_daily WHERE day_utc < ?", (aggregate_cutoff,)
            ).rowcount
            idempotency_deleted = connection.execute(
                "DELETE FROM cloud_audit_idempotency WHERE source_created_at < ?",
                (idempotency_cutoff,),
            ).rowcount
            self._append_integrity(
                connection,
                actor_subject=actor.subject,
                action="audit.retention.enforced",
                target_id="audit-retention",
                payload={
                    "raw_deleted": max(0, raw_deleted),
                    "aggregate_deleted": max(0, aggregate_deleted),
                    "idempotency_deleted": max(0, idempotency_deleted),
                    "raw_days": self.retention.raw_days,
                    "aggregate_days": self.retention.aggregate_days,
                },
            )
            return CloudAuditRetentionResult(
                raw_deleted=max(0, raw_deleted),
                aggregate_deleted=max(0, aggregate_deleted),
                idempotency_deleted=max(0, idempotency_deleted),
                raw_days=self.retention.raw_days,
                aggregate_days=self.retention.aggregate_days,
            )

    def integrity_entries(self) -> tuple[CloudAuditIntegrityEntry, ...]:
        connection = self._connect()
        entries: list[CloudAuditIntegrityEntry] = []
        try:
            with self._integrity_checkpoint_lock:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT sequence,actor_subject,action,target_id,payload_sha256,"
                    "previous_mac,entry_mac,created_at "
                    "FROM cloud_audit_integrity ORDER BY sequence"
                )
                previous = _ZERO_INTEGRITY_MAC
                expected_sequence = 1
                for row in rows:
                    previous = self._verify_integrity_row(
                        row, expected_sequence, previous
                    )
                    entries.append(CloudAuditIntegrityEntry(**dict(row)))
                    expected_sequence += 1
                checkpoint = (expected_sequence - 1, previous)
                connection.commit()
                self._integrity_checkpoint = checkpoint
                self._integrity_checkpoint_fault = None
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, (CloudAuditIntegrityError, sqlite3.DatabaseError)):
                self._poison_integrity_checkpoint(
                    "cloud audit integrity failed full verification"
                )
            raise
        finally:
            connection.close()
        return tuple(entries)


def create_cloud_audit_router(
    repository: CloudAuditRepository,
    *,
    principal_dependency: Callable[..., ControlPrincipal],
    admin_dependency: Callable[..., ControlPrincipal],
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/api/v1/audit/records",
        response_model=CloudAuditReceipt,
        status_code=201,
    )
    def ingest(
        record: AuditRecordProjection,
        response: Response,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        current: ControlPrincipal = Depends(principal_dependency),
    ) -> CloudAuditReceipt:
        try:
            receipt = repository.ingest(
                current, record, idempotency_key=idempotency_key
            )
        except CloudAuditConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "audit_conflict",
                    "message": "audit identity conflicts with existing state",
                },
            ) from error
        except CloudAuditRejected as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "audit_rejected",
                    "message": "audit record failed safety validation",
                },
            ) from error
        if not receipt.created:
            response.status_code = 200
        return receipt

    @router.get(
        "/api/v1/admin/audit/records",
        response_model=CloudAuditListResponse,
    )
    def list_records(
        account_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        current: ControlPrincipal = Depends(admin_dependency),
    ) -> CloudAuditListResponse:
        records = repository.list_metadata(
            current,
            account_id=account_id,
            category=category,
            event_type=event_type,
            limit=limit,
        )
        return CloudAuditListResponse(records=list(records), count=len(records))

    @router.get(
        "/api/v1/admin/audit/records/{audit_id}",
        response_model=CloudAuditDetail,
    )
    def detail(
        audit_id: str,
        current: ControlPrincipal = Depends(admin_dependency),
    ) -> CloudAuditDetail:
        try:
            return repository.get_detail(current, audit_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="audit record was not found") from error

    @router.get(
        "/api/v1/admin/audit/aggregates",
        response_model=CloudAuditAggregateResponse,
    )
    def aggregates(
        limit: int = Query(default=1000, ge=1, le=5000),
        current: ControlPrincipal = Depends(admin_dependency),
    ) -> CloudAuditAggregateResponse:
        records = repository.list_aggregates(current, limit=limit)
        return CloudAuditAggregateResponse(records=list(records), count=len(records))

    @router.post(
        "/api/v1/admin/audit/retention:enforce",
        response_model=CloudAuditRetentionResult,
    )
    def retention(
        current: ControlPrincipal = Depends(admin_dependency),
    ) -> CloudAuditRetentionResult:
        return repository.enforce_retention(current)

    return router


__all__ = [
    "CloudAuditAggregate",
    "CloudAuditAggregateResponse",
    "CloudAuditBodyLimitMiddleware",
    "CloudAuditConflict",
    "CloudAuditDetail",
    "CloudAuditError",
    "CloudAuditIntegrityEntry",
    "CloudAuditIntegrityError",
    "CloudAuditListResponse",
    "CloudAuditMetadata",
    "CloudAuditReceipt",
    "CloudAuditRejected",
    "CloudAuditRepository",
    "CloudAuditRetentionResult",
    "create_cloud_audit_router",
]

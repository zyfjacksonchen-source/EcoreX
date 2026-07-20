"""Encrypted, redacted transactional audit outbox with at-least-once delivery."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import os
import re
import sqlite3
import threading
from typing import Any, Awaitable, Mapping, Protocol
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ecorex.protocol import (
    AuditDrainResponse,
    AuditRecordProjection,
    AuditRetentionResponse,
    EventEnvelope,
)
from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|authorization|cookie|session|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_BINARY_KEY = re.compile(
    r"(?:binary|blob|bytes|file_content|image_data|audio_data|video_data|base64)",
    re.IGNORECASE,
)
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]{512,}={0,2}$")
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}")
_EMBEDDED_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"']+|/(?:Users|home|tmp|var|etc)/[^\s\"']+)"
)
_AUDIT_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}$")
_AUDIT_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.:]{0,127}$")
_AUDIT_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_DISCOVERY = re.compile(
    r"^connector:[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}@"
    r"[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}/"
    r"[A-Za-z0-9][-A-Za-z0-9_.:]{0,255}@[0-9a-f]{64}$"
)
_CONNECTOR_AUDIT_IDENTITIES = frozenset(
    {"connector_id", "action_id", "instance_id", "invocation_id"}
)
_CONNECTOR_AUDIT_TOKENS = frozenset(
    {
        "status",
        "delivery",
        "outcome",
        "resolution",
        "health",
        "error_code",
        "reason",
        "stage_status",
        "completion_path",
    }
)
_CONNECTOR_AUDIT_DIGESTS = frozenset(
    {
        "input_sha256",
        "idempotency_key_sha256",
        "admission_policy_sha256",
        "result_envelope_sha256",
        "result_sha256",
    }
)
_MODEL_RECOVERY_AUDIT_TOKENS = frozenset({"action", "trigger_code", "resolved_by"})
_MODEL_RECOVERY_AUDIT_DIGESTS = frozenset({"tool_output_sha256"})


def _is_secret_key(value: str) -> bool:
    # Normalize camelCase/PascalCase as well as snake/kebab keys. Audit input
    # is supplied by tools and connectors, so relying on one naming style can
    # turn a harmless schema change into credential disclosure.
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").casefold()
    compact = normalized.replace("_", "")
    exact = {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "token",
        "secret",
        "clientsecret",
        "appsecret",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "session",
        "sessionid",
        "privatekey",
    }
    return bool(
        _SECRET_KEY.search(value)
        or compact in exact
        or normalized.endswith(
            (
                "_api_key",
                "_access_token",
                "_refresh_token",
                "_auth_token",
                "_secret",
                "_password",
                "_authorization",
                "_private_key",
            )
        )
    )


class AuditError(RuntimeError):
    pass


class AuditIntegrityError(AuditError):
    pass


class AuditPayloadCipher:
    """AES-GCM envelope for redacted audit payloads persisted in SQLite."""

    FORMAT = "aesgcm-v1"

    def __init__(self, key: bytes) -> None:
        material = bytes(key)
        if len(material) != 32:
            raise ValueError("audit encryption key must contain 32 bytes")
        self._cipher = AESGCM(material)

    def encrypt(self, plaintext: str, *, associated_data: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext.encode("utf-8"),
            associated_data.encode("utf-8"),
        )
        return json_dumps(
            {
                "version": 1,
                "algorithm": "AES-256-GCM",
                "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            }
        )

    def decrypt(self, envelope_json: str, *, associated_data: str) -> str:
        try:
            envelope = json.loads(envelope_json)
            if (
                not isinstance(envelope, dict)
                or envelope.get("version") != 1
                or envelope.get("algorithm") != "AES-256-GCM"
            ):
                raise ValueError("invalid envelope")
            nonce = base64.b64decode(
                str(envelope["nonce"]), altchars=b"-_", validate=True
            )
            ciphertext = base64.b64decode(
                str(envelope["ciphertext"]), altchars=b"-_", validate=True
            )
            if len(nonce) != 12 or len(ciphertext) < 16:
                raise ValueError("invalid envelope")
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                associated_data.encode("utf-8"),
            )
            return plaintext.decode("utf-8")
        except (
            InvalidTag,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            raise AuditIntegrityError(
                "stored audit payload authentication failed"
            ) from None


class AuditPublisher(Protocol):
    def publish(self, record: AuditRecordProjection) -> Awaitable[None] | None: ...


@dataclass(frozen=True, slots=True)
class AuditRetentionPolicy:
    raw_days: int = 30
    aggregate_days: int = 180

    def __post_init__(self) -> None:
        if not 1 <= self.raw_days <= 3650:
            raise ValueError("audit raw retention must be between 1 and 3650 days")
        if not self.raw_days <= self.aggregate_days <= 3650:
            raise ValueError("audit aggregate retention must cover raw retention")


class AuditRedactor:
    """Remove credentials, local paths, and binary bodies before persistence."""

    def __init__(self, *, max_depth: int = 16, max_string_bytes: int = 1_048_576):
        self.max_depth = max_depth
        self.max_string_bytes = max_string_bytes

    def redact(self, value: Any, *, key: str = "", depth: int = 0) -> Any:
        if depth > self.max_depth:
            return "[REDACTED:MAX_DEPTH]"
        if _is_secret_key(key):
            return "[REDACTED:SECRET]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            return self._binary_omission(value)
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            if (
                _BINARY_KEY.search(key)
                or value.startswith("data:")
                or _BASE64.fullmatch(value) is not None
            ):
                return self._binary_omission(encoded)
            if self._is_local_path(value):
                return (
                    "[REDACTED:PATH:" + hashlib.sha256(encoded).hexdigest()[:12] + "]"
                )
            value = _INLINE_SECRET.sub(
                lambda match: f"{match.group(1)}=[REDACTED:SECRET]", value
            )
            value = _BEARER_SECRET.sub("Bearer [REDACTED:SECRET]", value)
            value = _EMBEDDED_PATH.sub(self._embedded_path_replacement, value)
            encoded = value.encode("utf-8", errors="replace")
            if len(encoded) > self.max_string_bytes:
                prefix = encoded[: self.max_string_bytes].decode(
                    "utf-8", errors="ignore"
                )
                return {
                    "text": prefix,
                    "truncated": True,
                    "original_size_bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            return value
        if isinstance(value, Mapping):
            return {
                str(child_key): self.redact(
                    child_value,
                    key=str(child_key),
                    depth=depth + 1,
                )
                for child_key, child_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(child, key=key, depth=depth + 1) for child in value]
        return str(value)

    @staticmethod
    def _embedded_path_replacement(match: re.Match[str]) -> str:
        digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:12]
        return f"[REDACTED:PATH:{digest}]"

    @staticmethod
    def _is_local_path(value: str) -> bool:
        if value.startswith(("http://", "https://", "blob:")):
            return False
        return bool(_WINDOWS_PATH.match(value) or value.startswith("/"))

    @staticmethod
    def _binary_omission(value: bytes) -> dict[str, Any]:
        return {
            "omitted": "binary",
            "size_bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }


class AuditOutbox:
    def __init__(
        self,
        database: SQLiteDatabase | str,
        *,
        account_id: str,
        cipher: AuditPayloadCipher,
        publisher: AuditPublisher | None = None,
        retention: AuditRetentionPolicy | None = None,
        lease_seconds: int = 30,
        redactor: AuditRedactor | None = None,
        initialize: bool = True,
    ) -> None:
        if not account_id:
            raise ValueError("audit account_id is required")
        if not 1 <= lease_seconds <= 300:
            raise ValueError("audit lease must be between 1 and 300 seconds")
        self.database = (
            database
            if isinstance(database, SQLiteDatabase)
            else SQLiteDatabase(database)
        )
        self.account_id = account_id
        self.publisher = publisher
        self.retention = retention or AuditRetentionPolicy()
        self.lease_seconds = lease_seconds
        self.redactor = redactor or AuditRedactor()
        self.cipher = cipher
        self._startup_lock = threading.Lock()
        self._startup_converged = False
        if initialize:
            self.initialize()
        else:
            self.validate()

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def validate(self) -> None:
        """Validate the encrypted product schema without creating facts."""

        # DDL and legacy plaintext conversion belong to the signed migration
        # boundary. Runtime startup validates the final encrypted shape only.
        with self.database.reader() as connection:
            validate_product_schema(connection)
            legacy = connection.execute(
                "SELECT 1 FROM observability_audit_outbox "
                "WHERE payload_format != ? LIMIT 1",
                (AuditPayloadCipher.FORMAT,),
            ).fetchone()
            if legacy is not None:
                raise AuditIntegrityError(
                    "audit payload storage requires a signed encryption migration"
                )

    def initialize(self) -> None:
        """Initialize the durable cursor during healthy startup convergence."""

        with self._startup_lock:
            if self._startup_converged:
                return
            self.validate()
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO observability_audit_cursors(account_id) VALUES (?) "
                    "ON CONFLICT(account_id) DO NOTHING",
                    (self.account_id,),
                )
            self._startup_converged = True

    def converge_startup(self) -> None:
        """Idempotently enable writes after healthy startup convergence."""

        self.initialize()

    def _require_converged(self) -> None:
        if not self._startup_converged:
            raise AuditIntegrityError("audit outbox startup has not converged")

    @staticmethod
    def _associated_data(row: Mapping[str, Any]) -> str:
        return "\x1f".join(
            str(row[name])
            for name in (
                "audit_id",
                "source_event_id",
                "category",
                "event_type",
                "account_id",
            )
        )

    def _plaintext_from_row(self, row: sqlite3.Row) -> str:
        if row["payload_format"] != AuditPayloadCipher.FORMAT:
            raise AuditIntegrityError("stored audit payload format is invalid")
        return self.cipher.decrypt(
            str(row["payload_json"]),
            associated_data=self._associated_data(row),
        )

    def record_in_transaction(
        self, connection: sqlite3.Connection, event: EventEnvelope
    ) -> None:
        self._require_converged()
        if not connection.in_transaction:
            raise RuntimeError("audit event recording requires an active transaction")
        for category, payload in self._audit_views(event):
            self._persist_view_in_transaction(
                connection,
                source_event_id=event.event_id,
                category=category,
                event_type=event.event_type,
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                trace_id=event.trace_id,
                payload=payload,
                created_at=event.created_at,
            )
        source = connection.execute(
            "SELECT rowid AS source_rowid FROM events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if source is not None:
            self._advance_cursor_in_transaction(
                connection,
                column="last_event_rowid",
                rowid=int(source["source_rowid"]),
            )

    def _advance_cursor_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        column: str,
        rowid: int,
    ) -> None:
        self._require_converged()
        if column not in {"last_event_rowid", "last_permission_rowid"}:
            raise ValueError("audit cursor column is invalid")
        connection.execute(
            f"UPDATE observability_audit_cursors SET {column} = MAX({column}, ?) "
            "WHERE account_id = ?",
            (rowid, self.account_id),
        )

    def backfill_events(self) -> int:
        self._require_converged()
        inserted_before = self.count()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "SELECT last_event_rowid FROM observability_audit_cursors "
                "WHERE account_id = ?",
                (self.account_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT rowid AS source_rowid, * FROM events WHERE rowid > ? "
                "ORDER BY rowid",
                (int(cursor["last_event_rowid"]),),
            ).fetchall()
            from ecorex.runtime.event_store import EventStore

            for row in rows:
                self.record_in_transaction(connection, EventStore._from_row(row))
        return self.count() - inserted_before

    def backfill_permissions(self) -> int:
        self._require_converged()
        inserted_before = self.count()
        with self.database.transaction() as connection:
            self.backfill_permissions_in_transaction(connection)
        return self.count() - inserted_before

    def backfill_permissions_in_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> int:
        """Materialize permission audit facts in the authority transaction."""

        self._require_converged()
        if not connection.in_transaction:
            raise RuntimeError(
                "permission audit recording requires an active transaction"
            )
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'permission_change_requests'"
        ).fetchone()
        if table is None:
            return 0
        cursor = connection.execute(
            "SELECT last_permission_rowid FROM observability_audit_cursors "
            "WHERE account_id = ?",
            (self.account_id,),
        ).fetchone()
        if cursor is None:
            raise AuditIntegrityError("permission audit cursor is unavailable")
        rows = connection.execute(
            "SELECT rowid AS source_rowid, * FROM permission_change_requests "
            "WHERE account_id = ? AND rowid > ? ORDER BY rowid",
            (self.account_id, int(cursor["last_permission_rowid"])),
        ).fetchall()
        for row in rows:
            response = json_loads(row["response_json"], {})
            self._persist_view_in_transaction(
                connection,
                source_event_id=(
                    f"permission:{self.account_id}:{row['client_request_id']}"
                ),
                category="permission",
                event_type="permission.settings_changed",
                thread_id=None,
                turn_id=None,
                trace_id=None,
                payload={
                    "client_request_id": row["client_request_id"],
                    "expected_revision": row["expected_revision"],
                    "resulting_revision": row["resulting_revision"],
                    "permissions": response,
                },
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            self._advance_cursor_in_transaction(
                connection,
                column="last_permission_rowid",
                rowid=int(row["source_rowid"]),
            )
        return len(rows)

    def _persist_view_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        source_event_id: str,
        category: str,
        event_type: str,
        thread_id: str | None,
        turn_id: str | None,
        trace_id: str | None,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        self._require_converged()
        redacted = self.redactor.redact(dict(payload))
        plaintext_json = json_dumps(redacted)
        digest = hashlib.sha256(plaintext_json.encode("utf-8")).hexdigest()
        identity = f"{source_event_id}:{category}:{event_type}"
        audit_id = "audit_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT * FROM observability_audit_outbox WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
        if existing is not None:
            if (
                self._plaintext_from_row(existing) != plaintext_json
                or existing["payload_sha256"] != digest
            ):
                raise AuditIntegrityError(
                    "audit identity was reused with different content"
                )
            return
        timestamp = created_at.astimezone(UTC).isoformat()
        identity_values = {
            "audit_id": audit_id,
            "source_event_id": source_event_id,
            "category": category,
            "event_type": event_type,
            "account_id": self.account_id,
        }
        encrypted_payload = self.cipher.encrypt(
            plaintext_json,
            associated_data=self._associated_data(identity_values),
        )
        connection.execute(
            "INSERT INTO observability_audit_outbox("
            "audit_id, source_event_id, category, event_type, account_id, "
            "thread_id, turn_id, trace_id, payload_json, payload_format, "
            "payload_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                source_event_id,
                category,
                event_type,
                self.account_id,
                thread_id,
                turn_id,
                trace_id,
                encrypted_payload,
                AuditPayloadCipher.FORMAT,
                digest,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO observability_audit_daily("
            "day_utc, category, event_type, record_count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(day_utc, category, event_type) DO UPDATE SET "
            "record_count = record_count + 1",
            (timestamp[:10], category, event_type),
        )

    def _audit_views(
        self, event: EventEnvelope
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        payload = event.payload
        common = {
            "event_seq": event.seq,
            "config_snapshot_id": event.config_snapshot_id,
            "capability_snapshot_id": event.capability_snapshot_id,
            "permission_snapshot_id": event.permission_snapshot_id,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
        }
        views: list[tuple[str, Mapping[str, Any]]] = []
        if event.event_type == "turn.accepted":
            views.extend(
                (
                    (
                        "prompt",
                        {
                            **common,
                            "input": payload.get("input"),
                            "agent_model_id": payload.get("agent_model_id"),
                            "metadata": payload.get("metadata", {}),
                        },
                    ),
                    (
                        "permission",
                        {
                            **common,
                            "permission_snapshot_id": event.permission_snapshot_id,
                        },
                    ),
                    ("task", {**common, "status": "accepted"}),
                )
            )
        elif event.event_type == "turn.steered":
            views.append(("prompt", {**common, **payload}))
        elif event.event_type.startswith("model.continuation_recovery"):
            views.append(
                (
                    "task",
                    self._model_recovery_audit_view(event, common),
                )
            )
        elif event.event_type in {"item.delta", "model.response_completed"}:
            views.append(("response", {**common, **payload}))
        elif event.event_type.startswith("connector."):
            views.append(
                (
                    "connector",
                    self._connector_audit_view(event, common),
                )
            )
        elif event.event_type.startswith("tool."):
            views.append(("tool", {**common, **payload}))
        elif event.event_type.startswith("artifact."):
            views.append(("artifact", {**common, **payload}))
        elif event.event_type.startswith("interaction."):
            views.append(("human", {**common, **payload}))
            if payload.get("kind") == "permission_approval":
                views.append(("permission", {**common, **payload}))
        elif event.event_type.startswith(("turn.", "job.")):
            views.append(("task", {**common, **payload}))
        return tuple(views)

    @staticmethod
    def _connector_audit_view(
        event: EventEnvelope,
        common: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return a narrow Connector audit contract, never an arbitrary payload.

        Connector result events are also written directly by the Runtime result
        coordinator, so this projection independently validates every value
        instead of relying on the Connector outbox bridge's sanitization.
        """

        payload = event.payload
        result: dict[str, Any] = dict(common)
        for key in _CONNECTOR_AUDIT_IDENTITIES:
            value = payload.get(key)
            if (
                isinstance(value, str)
                and _AUDIT_SAFE_IDENTITY.fullmatch(value) is not None
            ):
                result[key] = value
        invocation_alias = payload.get("connector_invocation_id")
        if (
            "invocation_id" not in result
            and isinstance(invocation_alias, str)
            and _AUDIT_SAFE_IDENTITY.fullmatch(invocation_alias) is not None
        ):
            result["invocation_id"] = invocation_alias
        discovery_id = payload.get("discovery_id")
        if (
            isinstance(discovery_id, str)
            and _AUDIT_DISCOVERY.fullmatch(discovery_id) is not None
        ):
            result["discovery_id"] = discovery_id
        for key in _CONNECTOR_AUDIT_TOKENS:
            value = payload.get(key)
            if (
                isinstance(value, str)
                and _AUDIT_SAFE_TOKEN.fullmatch(value) is not None
            ):
                result[key] = value
        for key in _CONNECTOR_AUDIT_DIGESTS:
            value = payload.get(key)
            if isinstance(value, str) and _AUDIT_SHA256.fullmatch(value) is not None:
                result[key] = value
        result.setdefault("outcome", event.event_type.rsplit(".", 1)[-1])
        return result

    @staticmethod
    def _model_recovery_audit_view(
        event: EventEnvelope,
        common: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Expose recovery health without retaining provider or Tool payloads."""

        payload = event.payload
        result: dict[str, Any] = dict(common)
        for key in _MODEL_RECOVERY_AUDIT_TOKENS:
            value = payload.get(key)
            if (
                isinstance(value, str)
                and _AUDIT_SAFE_TOKEN.fullmatch(value) is not None
            ):
                result[key] = value
        for key in _MODEL_RECOVERY_AUDIT_DIGESTS:
            value = payload.get(key)
            if isinstance(value, str) and _AUDIT_SHA256.fullmatch(value) is not None:
                result[key] = value
        for key in ("from_round", "next_round", "round"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        result["status"] = event.event_type.rsplit("_", 1)[-1]
        return result

    def get(self, audit_id: str) -> AuditRecordProjection:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM observability_audit_outbox WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        if row is None:
            raise KeyError(audit_id)
        return self._from_row(row)

    def list(
        self,
        *,
        thread_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[AuditRecordProjection, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("audit limit must be between 1 and 1000")
        if status not in {None, "pending", "retry_wait", "published", "rejected"}:
            raise ValueError("audit delivery status is invalid")
        clauses: list[str] = ["account_id = ?"]
        parameters: list[Any] = [self.account_id]
        if thread_id is not None:
            clauses.append("thread_id = ?")
            parameters.append(thread_id)
        if status == "published":
            clauses.append("published_at IS NOT NULL")
        elif status == "rejected":
            clauses.append("rejected_at IS NOT NULL")
        elif status == "retry_wait":
            clauses.append(
                "published_at IS NULL AND rejected_at IS NULL "
                "AND next_attempt_at IS NOT NULL"
            )
        elif status == "pending":
            clauses.append(
                "published_at IS NULL AND rejected_at IS NULL "
                "AND next_attempt_at IS NULL"
            )
        parameters.append(limit)
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM observability_audit_outbox WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, audit_id LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def count(self, *, pending_only: bool = False) -> int:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM observability_audit_outbox "
                "WHERE account_id = ?"
                + (
                    " AND published_at IS NULL AND rejected_at IS NULL"
                    if pending_only
                    else ""
                ),
                (self.account_id,),
            ).fetchone()
        return int(row["count"])

    def _from_row(self, row: sqlite3.Row) -> AuditRecordProjection:
        plaintext_json = self._plaintext_from_row(row)
        digest = hashlib.sha256(plaintext_json.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise AuditIntegrityError("stored audit payload digest is invalid")
        payload = json.loads(plaintext_json)
        if not isinstance(payload, dict):
            raise AuditIntegrityError("stored audit payload is not an object")
        status = (
            "published"
            if row["published_at"]
            else "rejected"
            if row["rejected_at"]
            else "retry_wait"
            if row["next_attempt_at"]
            else "pending"
        )
        return AuditRecordProjection(
            audit_id=str(row["audit_id"]),
            source_event_id=str(row["source_event_id"]),
            category=str(row["category"]),
            event_type=str(row["event_type"]),
            account_id=str(row["account_id"]),
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            trace_id=row["trace_id"],
            payload=payload,
            payload_sha256=digest,
            binary_included=False,
            delivery_status=status,
            attempts=int(row["attempts"]),
            next_attempt_at=(
                datetime.fromisoformat(row["next_attempt_at"])
                if row["next_attempt_at"]
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            published_at=(
                datetime.fromisoformat(row["published_at"])
                if row["published_at"]
                else None
            ),
            rejected_at=(
                datetime.fromisoformat(row["rejected_at"])
                if row["rejected_at"]
                else None
            ),
            last_error_code=row["last_error_code"],
        )

    async def drain(self, *, limit: int = 100) -> AuditDrainResponse:
        self._require_converged()
        if self.publisher is None:
            raise AuditError("audit publisher is not configured")
        if not 1 <= limit <= 1000:
            raise ValueError("audit drain limit must be between 1 and 1000")
        attempted = published = retry_scheduled = rejected = 0
        for _index in range(limit):
            claimed = await asyncio.to_thread(self._claim_next)
            if claimed is None:
                break
            attempted += 1
            record, lease_token = claimed
            try:
                operation = self.publisher.publish
                if inspect.iscoroutinefunction(operation):
                    result = operation(record)
                else:
                    result = await asyncio.to_thread(operation, record)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                error_code = getattr(error, "code", type(error).__name__)
                if getattr(error, "retryable", True) is False:
                    rejected += 1
                    await asyncio.to_thread(
                        self._mark_rejected,
                        record.audit_id,
                        lease_token,
                        str(error_code),
                    )
                else:
                    retry_scheduled += 1
                    await asyncio.to_thread(
                        self._mark_retry,
                        record.audit_id,
                        lease_token,
                        str(error_code),
                    )
            else:
                published += 1
                await asyncio.to_thread(
                    self._mark_published, record.audit_id, lease_token
                )
        return AuditDrainResponse(
            attempted=attempted,
            published=published,
            retry_scheduled=retry_scheduled,
            rejected=rejected,
            pending=self.count(pending_only=True),
        )

    def _claim_next(self) -> tuple[AuditRecordProjection, str] | None:
        self._require_converged()
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM observability_audit_outbox "
                "WHERE published_at IS NULL "
                "AND rejected_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at, audit_id LIMIT 1",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE observability_audit_outbox SET lease_token = ?, "
                "lease_expires_at = ?, attempts = attempts + 1, next_attempt_at = NULL "
                "WHERE audit_id = ? AND published_at IS NULL AND rejected_at IS NULL",
                (
                    token,
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    row["audit_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM observability_audit_outbox WHERE audit_id = ?",
                (row["audit_id"],),
            ).fetchone()
            return self._from_row(updated), token

    def _mark_published(self, audit_id: str, lease_token: str) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE observability_audit_outbox SET published_at = ?, "
                "lease_token = NULL, lease_expires_at = NULL, next_attempt_at = NULL, "
                "last_error_code = NULL WHERE audit_id = ? AND lease_token = ? "
                "AND published_at IS NULL AND rejected_at IS NULL",
                (datetime.now(UTC).isoformat(), audit_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise AuditIntegrityError("audit publish lease was lost")

    def _mark_retry(self, audit_id: str, lease_token: str, error_code: str) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT attempts FROM observability_audit_outbox "
                "WHERE audit_id = ? AND lease_token = ? AND published_at IS NULL "
                "AND rejected_at IS NULL",
                (audit_id, lease_token),
            ).fetchone()
            if row is None:
                raise AuditIntegrityError("audit retry lease was lost")
            delay = min(300, 2 ** min(int(row["attempts"]), 8))
            connection.execute(
                "UPDATE observability_audit_outbox SET lease_token = NULL, "
                "lease_expires_at = NULL, next_attempt_at = ?, last_error_code = ? "
                "WHERE audit_id = ? AND lease_token = ? AND rejected_at IS NULL",
                (
                    (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                    error_code[:128],
                    audit_id,
                    lease_token,
                ),
            )

    def _mark_rejected(self, audit_id: str, lease_token: str, error_code: str) -> None:
        self._require_converged()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE observability_audit_outbox SET rejected_at = ?, "
                "lease_token = NULL, lease_expires_at = NULL, next_attempt_at = NULL, "
                "last_error_code = ? WHERE audit_id = ? AND lease_token = ? "
                "AND published_at IS NULL AND rejected_at IS NULL",
                (
                    datetime.now(UTC).isoformat(),
                    error_code[:128],
                    audit_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise AuditIntegrityError("audit reject lease was lost")

    def enforce_retention(
        self, *, now: datetime | None = None
    ) -> AuditRetentionResponse:
        self._require_converged()
        now = (now or datetime.now(UTC)).astimezone(UTC)
        raw_cutoff = (now - timedelta(days=self.retention.raw_days)).isoformat()
        aggregate_cutoff = (
            (now - timedelta(days=self.retention.aggregate_days)).date().isoformat()
        )
        with self.database.transaction() as connection:
            raw = connection.execute(
                "DELETE FROM observability_audit_outbox "
                "WHERE (published_at IS NOT NULL AND published_at < ?) "
                "OR (rejected_at IS NOT NULL AND rejected_at < ?)",
                (raw_cutoff, raw_cutoff),
            ).rowcount
            aggregate = connection.execute(
                "DELETE FROM observability_audit_daily WHERE day_utc < ?",
                (aggregate_cutoff,),
            ).rowcount
        return AuditRetentionResponse(
            raw_deleted=max(0, raw), aggregate_deleted=max(0, aggregate)
        )


class AuditDispatcher:
    """Small background retry loop; unchanged connectivity is not an error."""

    def __init__(
        self,
        outbox: AuditOutbox,
        *,
        poll_seconds: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        if not 0.1 <= poll_seconds <= 300:
            raise ValueError("audit poll interval is invalid")
        self.outbox = outbox
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error_code: str | None = None
        self.last_error_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None:
            return
        self.outbox._require_converged()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ecorex-audit-dispatcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        self.outbox._require_converged()
        last_retention_day: str | None = None
        while not self._stop.is_set():
            try:
                if self.outbox.publisher is not None:
                    await self.outbox.drain(limit=self.batch_size)
                today = datetime.now(UTC).date().isoformat()
                if today != last_retention_day:
                    await asyncio.to_thread(self.outbox.enforce_retention)
                    last_retention_day = today
                self.last_error_code = None
                self.last_error_at = None
            except Exception as error:
                # The outbox remains durable and the next loop retries. Keep a
                # bounded health signal instead of allowing a background task
                # to die silently on an unexpected storage/publisher failure.
                self.last_error_code = type(error).__name__[:128]
                self.last_error_at = datetime.now(UTC)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

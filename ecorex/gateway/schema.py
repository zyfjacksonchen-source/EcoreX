"""Explicit SQLite schema authority for the cloud Model Gateway.

Gateway API/worker processes only validate this contract.  DDL and the bounded
legacy event-chain transform run solely through :class:`GatewaySchemaManager`.
This service-side lifecycle is intentionally independent from the local
Runtime's signed candidate storage manifest.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping, Sequence


CURRENT_GATEWAY_SCHEMA_VERSION = 2
GATEWAY_SCHEMA_RECEIPT_VERSION = 1
MAX_LEGACY_EVENTS = 100_000
MAX_LEGACY_EVENT_BYTES = 64 * 1024 * 1024
_ZERO_DIGEST = "0" * 64
_HEX_DIGEST = frozenset("0123456789abcdef")


class GatewaySchemaError(RuntimeError):
    """Gateway schema is absent, unknown, drifted, corrupt, or too new."""


GATEWAY_SCHEMA_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS gateway_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    transformed_rows INTEGER NOT NULL CHECK(transformed_rows >= 0),
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS gateway_schema_migrations_no_update
BEFORE UPDATE ON gateway_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'gateway schema history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS gateway_schema_migrations_no_delete
BEFORE DELETE ON gateway_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'gateway schema history is immutable');
END;
"""


GATEWAY_REQUEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_requests (
    request_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    quota_period TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    model_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'completed')),
    lease_token TEXT,
    lease_expires_at TEXT,
    response_id TEXT,
    terminal_event_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS gateway_requests_quota
ON gateway_requests(account_id, quota_period, created_at);

CREATE TRIGGER IF NOT EXISTS gateway_requests_identity_immutable
BEFORE UPDATE ON gateway_requests
WHEN NEW.request_id != OLD.request_id
  OR NEW.account_id != OLD.account_id
  OR NEW.quota_period != OLD.quota_period
  OR NEW.request_fingerprint != OLD.request_fingerprint
  OR NEW.model_id != OLD.model_id
  OR NEW.trace_id != OLD.trace_id
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'gateway request identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS gateway_requests_completed_immutable
BEFORE UPDATE ON gateway_requests
WHEN OLD.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'completed gateway requests are immutable');
END;

CREATE TRIGGER gateway_requests_response_immutable
BEFORE UPDATE ON gateway_requests
WHEN OLD.response_id IS NOT NULL
  AND NEW.response_id IS NOT OLD.response_id
BEGIN
    SELECT RAISE(ABORT, 'gateway response identity is immutable');
END;
"""


GATEWAY_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_events (
    request_id TEXT NOT NULL REFERENCES gateway_requests(request_id),
    seq INTEGER NOT NULL CHECK(seq > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(request_id, seq)
);

CREATE TRIGGER IF NOT EXISTS gateway_events_no_update
BEFORE UPDATE ON gateway_events BEGIN
    SELECT RAISE(ABORT, 'gateway events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS gateway_events_no_delete
BEFORE DELETE ON gateway_events BEGIN
    SELECT RAISE(ABORT, 'gateway events are append-only');
END;
"""


LEGACY_GATEWAY_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_events (
    request_id TEXT NOT NULL REFERENCES gateway_requests(request_id),
    seq INTEGER NOT NULL CHECK(seq > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(request_id, seq)
);

CREATE TRIGGER IF NOT EXISTS gateway_events_no_update
BEFORE UPDATE ON gateway_events BEGIN
    SELECT RAISE(ABORT, 'gateway events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS gateway_events_no_delete
BEFORE DELETE ON gateway_events BEGIN
    SELECT RAISE(ABORT, 'gateway events are append-only');
END;
"""


GATEWAY_SCHEMA_V1_SQL = (
    GATEWAY_SCHEMA_HISTORY_SQL + GATEWAY_REQUEST_SCHEMA_SQL + GATEWAY_EVENT_SCHEMA_SQL
)

GATEWAY_CHAT_HANDOFF_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_model_attempts (
    request_id TEXT PRIMARY KEY REFERENCES gateway_requests(request_id),
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    model_config_id TEXT NOT NULL,
    model_config_revision INTEGER NOT NULL CHECK(model_config_revision > 0),
    local_model_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    provider_protocol TEXT NOT NULL CHECK(provider_protocol IN (
        'responses','openai_compatible_chat'
    )),
    provider_origin_preset TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS gateway_model_attempts_immutable
BEFORE UPDATE ON gateway_model_attempts BEGIN
    SELECT RAISE(ABORT, 'gateway model attempt identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS gateway_model_attempts_no_delete
BEFORE DELETE ON gateway_model_attempts BEGIN
    SELECT RAISE(ABORT, 'gateway model attempt identity is immutable');
END;

CREATE TABLE IF NOT EXISTS gateway_chat_handoffs (
    source_request_id TEXT PRIMARY KEY REFERENCES gateway_model_attempts(request_id),
    response_id TEXT NOT NULL UNIQUE,
    tool_call_id TEXT NOT NULL,
    provider_tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','available','consumed','expired','corrupt'
    )),
    consumed_by_request_id TEXT UNIQUE REFERENCES gateway_requests(request_id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    available_at TEXT,
    consumed_at TEXT,
    CHECK(
        (state='pending' AND available_at IS NULL AND consumed_by_request_id IS NULL AND consumed_at IS NULL)
        OR (state='available' AND available_at IS NOT NULL AND consumed_by_request_id IS NULL AND consumed_at IS NULL)
        OR (state='consumed' AND available_at IS NOT NULL AND consumed_by_request_id IS NOT NULL AND consumed_at IS NOT NULL)
        OR (state IN ('expired','corrupt'))
    )
);

CREATE INDEX IF NOT EXISTS gateway_chat_handoffs_expiry
ON gateway_chat_handoffs(state, expires_at);

CREATE TRIGGER IF NOT EXISTS gateway_chat_handoffs_identity_immutable
BEFORE UPDATE ON gateway_chat_handoffs
WHEN NEW.source_request_id != OLD.source_request_id
  OR NEW.response_id != OLD.response_id
  OR NEW.tool_call_id != OLD.tool_call_id
  OR NEW.provider_tool_name != OLD.provider_tool_name
  OR NEW.arguments_json != OLD.arguments_json
  OR NEW.arguments_sha256 != OLD.arguments_sha256
  OR NEW.expires_at != OLD.expires_at
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'gateway chat handoff identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS gateway_chat_handoffs_no_delete
BEFORE DELETE ON gateway_chat_handoffs BEGIN
    SELECT RAISE(ABORT, 'gateway chat handoffs are retained');
END;
"""

GATEWAY_SCHEMA_SQL = GATEWAY_SCHEMA_V1_SQL + GATEWAY_CHAT_HANDOFF_SCHEMA_SQL
PRE_AUTHORITY_GATEWAY_SCHEMA_SQL = GATEWAY_REQUEST_SCHEMA_SQL + GATEWAY_EVENT_SCHEMA_SQL
LEGACY_GATEWAY_SCHEMA_SQL = (
    GATEWAY_REQUEST_SCHEMA_SQL + LEGACY_GATEWAY_EVENT_SCHEMA_SQL
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _event_entry_digest(
    request_id: str,
    seq: int,
    payload_sha256: str,
    created_at: str,
    previous_digest: str,
) -> str:
    return _digest(
        "\0".join(
            (request_id, str(seq), payload_sha256, created_at, previous_digest)
        ).encode("utf-8")
    )


def _schema_records(connection: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type,name"
    ).fetchall()
    return tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in rows
    )


def _schema_digest(connection: sqlite3.Connection) -> str:
    return _digest(_canonical(_schema_records(connection)))


def _compiled_schema_digest(sql: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(sql)
        return _schema_digest(connection)
    finally:
        connection.close()


def _execute_sql(connection: sqlite3.Connection, sql: str) -> None:
    """Execute complete statements without ``executescript`` auto-commit."""

    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                connection.execute(statement)
    if pending.strip():
        raise GatewaySchemaError("gateway schema migration SQL is incomplete")


EMPTY_GATEWAY_SCHEMA_SHA256 = _digest(_canonical(()))
LEGACY_GATEWAY_SCHEMA_SHA256 = _compiled_schema_digest(LEGACY_GATEWAY_SCHEMA_SQL)
PRE_AUTHORITY_GATEWAY_SCHEMA_SHA256 = _compiled_schema_digest(
    PRE_AUTHORITY_GATEWAY_SCHEMA_SQL
)
GATEWAY_SCHEMA_V1_SHA256 = _compiled_schema_digest(GATEWAY_SCHEMA_V1_SQL)
GATEWAY_SCHEMA_SHA256 = _compiled_schema_digest(GATEWAY_SCHEMA_SQL)
MIGRATION_001_NAME = "initial-versioned-gateway-ledger"
MIGRATION_001_CHECKSUM = _digest(
    b"ecorex-gateway-schema-migration-v1\0"
    + GATEWAY_SCHEMA_V1_SQL.encode("utf-8")
    + b"\0legacy-event-chain-rebuild-v1"
)
MIGRATION_002_NAME = "durable-chat-tool-handoffs"
MIGRATION_002_CHECKSUM = _digest(
    b"ecorex-gateway-schema-migration-v2\0"
    + GATEWAY_CHAT_HANDOFF_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class GatewaySchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    transformed_rows: int
    event_chain_sha256: str
    installed_at: str

    def __post_init__(self) -> None:
        expected = {
            1: (MIGRATION_001_NAME, MIGRATION_001_CHECKSUM, GATEWAY_SCHEMA_V1_SHA256),
            2: (MIGRATION_002_NAME, MIGRATION_002_CHECKSUM, GATEWAY_SCHEMA_SHA256),
        }.get(self.migration_version)
        if (
            self.schema_version != GATEWAY_SCHEMA_RECEIPT_VERSION
            or expected is None
            or self.migration_name != expected[0]
            or self.migration_checksum != expected[1]
            or self.target_schema_sha256 != expected[2]
            or not 0 <= self.transformed_rows <= MAX_LEGACY_EVENTS
        ):
            raise GatewaySchemaError("gateway schema migration receipt is invalid")
        for value in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
            self.event_chain_sha256,
        ):
            if not _is_digest(value):
                raise GatewaySchemaError("gateway schema migration receipt is invalid")
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except ValueError as error:
            raise GatewaySchemaError("gateway schema migration receipt is invalid") from error
        if installed.tzinfo is None:
            raise GatewaySchemaError("gateway schema migration receipt is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_checksum": self.migration_checksum,
            "source_schema_sha256": self.source_schema_sha256,
            "target_schema_sha256": self.target_schema_sha256,
            "transformed_rows": self.transformed_rows,
            "event_chain_sha256": self.event_chain_sha256,
            "installed_at": self.installed_at,
        }


class GatewaySchemaManager:
    """Operator-owned migrator and process-owned read-only validator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(
        self, *, target_version: int = CURRENT_GATEWAY_SCHEMA_VERSION
    ) -> GatewaySchemaReceipt:
        if target_version != CURRENT_GATEWAY_SCHEMA_VERSION:
            raise ValueError("gateway schema migration target is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_database_or_absent(self.path)
        connection = self._connect(read_only=False)
        try:
            # BEGIN EXCLUSIVE is the cross-process migration lease. Shape is
            # re-read only after the lease, before any DDL or data mutation.
            connection.execute("BEGIN EXCLUSIVE")
            source_digest = _schema_digest(connection)
            names = {record["name"] for record in _schema_records(connection)}
            if "gateway_schema_migrations" in names:
                if source_digest == GATEWAY_SCHEMA_SHA256:
                    receipt = self._validate_connection(connection)
                    connection.commit()
                    self._activate_wal(connection)
                    return receipt
                if source_digest != GATEWAY_SCHEMA_V1_SHA256:
                    raise GatewaySchemaError("gateway schema source shape is unknown")
                rows = connection.execute(
                    "SELECT * FROM gateway_schema_migrations ORDER BY version"
                ).fetchall()
                if len(rows) != 1 or int(rows[0]["version"]) != 1:
                    raise GatewaySchemaError(
                        "gateway schema migration history is invalid"
                    )
                prior = self._validate_receipt_row(rows[0], version=1)
                transformed_rows = prior.transformed_rows
                event_chain_digest = prior.event_chain_sha256
            else:
                initial_source = source_digest
                if source_digest == EMPTY_GATEWAY_SCHEMA_SHA256:
                    _execute_sql(connection, GATEWAY_SCHEMA_V1_SQL)
                    transformed_rows = 0
                    event_chain_digest = _digest(_canonical([]))
                elif source_digest == PRE_AUTHORITY_GATEWAY_SCHEMA_SHA256:
                    _execute_sql(connection, GATEWAY_SCHEMA_HISTORY_SQL)
                    transformed_rows = 0
                    event_chain_digest = self._existing_chain_digest(connection)
                elif source_digest == LEGACY_GATEWAY_SCHEMA_SHA256:
                    transformed_rows, event_chain_digest = self._migrate_legacy_events(
                        connection
                    )
                    _execute_sql(connection, GATEWAY_SCHEMA_HISTORY_SQL)
                else:
                    raise GatewaySchemaError("gateway schema source shape is unknown")
                if _schema_digest(connection) != GATEWAY_SCHEMA_V1_SHA256:
                    raise GatewaySchemaError("gateway schema migration target drifted")
                receipt_v1 = GatewaySchemaReceipt(
                    schema_version=GATEWAY_SCHEMA_RECEIPT_VERSION,
                    migration_version=1,
                    migration_name=MIGRATION_001_NAME,
                    migration_checksum=MIGRATION_001_CHECKSUM,
                    source_schema_sha256=initial_source,
                    target_schema_sha256=GATEWAY_SCHEMA_V1_SHA256,
                    transformed_rows=transformed_rows,
                    event_chain_sha256=event_chain_digest,
                    installed_at=datetime.now(UTC).isoformat(),
                )
                self._insert_receipt(connection, receipt_v1)

            _execute_sql(connection, GATEWAY_CHAT_HANDOFF_SCHEMA_SQL)
            target_digest = _schema_digest(connection)
            if target_digest != GATEWAY_SCHEMA_SHA256:
                raise GatewaySchemaError("gateway schema migration target drifted")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise GatewaySchemaError("gateway schema migration violated foreign keys")
            quick = connection.execute("PRAGMA quick_check").fetchone()
            if quick is None or str(quick[0]).casefold() != "ok":
                raise GatewaySchemaError("gateway schema migration integrity check failed")
            installed_at = datetime.now(UTC).isoformat()
            receipt = GatewaySchemaReceipt(
                schema_version=GATEWAY_SCHEMA_RECEIPT_VERSION,
                migration_version=CURRENT_GATEWAY_SCHEMA_VERSION,
                migration_name=MIGRATION_002_NAME,
                migration_checksum=MIGRATION_002_CHECKSUM,
                source_schema_sha256=GATEWAY_SCHEMA_V1_SHA256,
                target_schema_sha256=target_digest,
                transformed_rows=transformed_rows,
                event_chain_sha256=event_chain_digest,
                installed_at=installed_at,
            )
            self._insert_receipt(connection, receipt)
            self._validate_connection(connection)
            connection.commit()
            # WAL is a deployment topology choice committed only after a known,
            # verified shape has completed migration.
            self._activate_wal(connection)
            return receipt
        except GatewaySchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise GatewaySchemaError("gateway schema migration failed") from None
        finally:
            connection.close()

    def validate(self) -> GatewaySchemaReceipt:
        _require_regular_database(self.path)
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except GatewaySchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise GatewaySchemaError("gateway schema validation failed") from None
        finally:
            connection.close()

    def _validate_connection(self, connection: sqlite3.Connection) -> GatewaySchemaReceipt:
        if _schema_digest(connection) != GATEWAY_SCHEMA_SHA256:
            raise GatewaySchemaError("gateway schema object fingerprint is incompatible")
        rows = connection.execute(
            "SELECT * FROM gateway_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise GatewaySchemaError("gateway schema migration history is missing")
        versions = [int(row["version"]) for row in rows]
        if any(version > CURRENT_GATEWAY_SCHEMA_VERSION for version in versions):
            raise GatewaySchemaError("gateway schema is newer than this process")
        if versions != list(range(1, CURRENT_GATEWAY_SCHEMA_VERSION + 1)):
            raise GatewaySchemaError("gateway schema migration history is incomplete")
        for expected_version, history_row in enumerate(rows, start=1):
            self._validate_receipt_row(history_row, version=expected_version)
        row = rows[-1]
        receipt = self._validate_receipt_row(
            row, version=CURRENT_GATEWAY_SCHEMA_VERSION
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise GatewaySchemaError("gateway schema foreign keys are invalid")
        return receipt

    @staticmethod
    def _validate_receipt_row(
        row: sqlite3.Row, *, version: int
    ) -> GatewaySchemaReceipt:
        receipt_json = str(row["receipt_json"])
        source_schema_sha256 = str(row["source_schema_sha256"])
        expected = {
            1: (
                MIGRATION_001_NAME,
                MIGRATION_001_CHECKSUM,
                GATEWAY_SCHEMA_V1_SHA256,
                {
                    EMPTY_GATEWAY_SCHEMA_SHA256,
                    LEGACY_GATEWAY_SCHEMA_SHA256,
                    PRE_AUTHORITY_GATEWAY_SCHEMA_SHA256,
                },
            ),
            2: (
                MIGRATION_002_NAME,
                MIGRATION_002_CHECKSUM,
                GATEWAY_SCHEMA_SHA256,
                {GATEWAY_SCHEMA_V1_SHA256},
            ),
        }.get(version)
        if (
            expected is None
            or int(row["version"]) != version
            or row["migration_name"] != expected[0]
            or row["migration_checksum"] != expected[1]
            or row["target_schema_sha256"] != expected[2]
            or source_schema_sha256 not in expected[3]
            or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
        ):
            raise GatewaySchemaError("gateway schema migration history is invalid")
        raw = json.loads(receipt_json)
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "migration_version",
            "migration_name",
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "transformed_rows",
            "event_chain_sha256",
            "installed_at",
        }:
            raise GatewaySchemaError("gateway schema migration receipt is invalid")
        receipt = GatewaySchemaReceipt(**dict(raw))
        if (
            receipt.migration_version != int(row["version"])
            or receipt.migration_name != str(row["migration_name"])
            or receipt.migration_checksum != str(row["migration_checksum"])
            or receipt.source_schema_sha256 != source_schema_sha256
            or receipt.target_schema_sha256 != str(row["target_schema_sha256"])
            or receipt.transformed_rows != int(row["transformed_rows"])
            or receipt.installed_at != str(row["installed_at"])
        ):
            raise GatewaySchemaError("gateway schema migration receipt is inconsistent")
        return receipt

    @staticmethod
    def _insert_receipt(
        connection: sqlite3.Connection, receipt: GatewaySchemaReceipt
    ) -> None:
        receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
        connection.execute(
            "INSERT INTO gateway_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,transformed_rows,receipt_json,receipt_sha256,"
            "installed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt.migration_version,
                receipt.migration_name,
                receipt.migration_checksum,
                receipt.source_schema_sha256,
                receipt.target_schema_sha256,
                receipt.transformed_rows,
                receipt_json,
                _digest(receipt_json.encode("utf-8")),
                receipt.installed_at,
            ),
        )

    def _migrate_legacy_events(
        self, connection: sqlite3.Connection
    ) -> tuple[int, str]:
        totals = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) "
            "FROM gateway_events"
        ).fetchone()
        count = int(totals[0])
        size = int(totals[1])
        if count > MAX_LEGACY_EVENTS or size > MAX_LEGACY_EVENT_BYTES:
            raise GatewaySchemaError("legacy gateway event ledger exceeds migration bounds")
        rows = connection.execute(
            "SELECT request_id,seq,payload_json,payload_sha256,created_at "
            "FROM gateway_events ORDER BY request_id,seq"
        ).fetchall()
        migrated: list[tuple[str, int, str, str, str, str, str]] = []
        expected_seq: dict[str, int] = {}
        previous_by_request: dict[str, str] = {}
        chain_material: list[dict[str, Any]] = []
        for row in rows:
            request_id = str(row["request_id"])
            sequence = expected_seq.get(request_id, 1)
            encoded = str(row["payload_json"]).encode("utf-8")
            payload_sha256 = _digest(encoded)
            if int(row["seq"]) != sequence or row["payload_sha256"] != payload_sha256:
                raise GatewaySchemaError(
                    "legacy gateway event ledger failed migration integrity"
                )
            previous = previous_by_request.get(request_id, _ZERO_DIGEST)
            created_at = str(row["created_at"])
            entry = _event_entry_digest(
                request_id, sequence, payload_sha256, created_at, previous
            )
            migrated.append(
                (
                    request_id,
                    sequence,
                    str(row["payload_json"]),
                    payload_sha256,
                    previous,
                    entry,
                    created_at,
                )
            )
            chain_material.append(
                {"request_id": request_id, "seq": sequence, "entry_digest": entry}
            )
            previous_by_request[request_id] = entry
            expected_seq[request_id] = sequence + 1

        connection.execute("DROP TRIGGER gateway_events_no_update")
        connection.execute("DROP TRIGGER gateway_events_no_delete")
        connection.execute("DROP TABLE gateway_events")
        _execute_sql(connection, GATEWAY_EVENT_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO gateway_events("
            "request_id,seq,payload_json,payload_sha256,previous_digest,entry_digest,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            migrated,
        )
        return count, _digest(_canonical(chain_material))

    @staticmethod
    def _existing_chain_digest(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT request_id,seq,payload_json,payload_sha256,previous_digest,"
            "entry_digest,created_at FROM gateway_events "
            "ORDER BY request_id,seq"
        ).fetchall()
        if len(rows) > MAX_LEGACY_EVENTS:
            raise GatewaySchemaError("gateway event ledger exceeds validation bounds")
        total_bytes = 0
        expected_seq: dict[str, int] = {}
        previous_by_request: dict[str, str] = {}
        material: list[dict[str, Any]] = []
        for row in rows:
            request_id = str(row["request_id"])
            sequence = expected_seq.get(request_id, 1)
            encoded = str(row["payload_json"]).encode("utf-8")
            total_bytes += len(encoded)
            if total_bytes > MAX_LEGACY_EVENT_BYTES:
                raise GatewaySchemaError("gateway event ledger exceeds validation bounds")
            payload_sha256 = _digest(encoded)
            previous = previous_by_request.get(request_id, _ZERO_DIGEST)
            entry = _event_entry_digest(
                request_id,
                sequence,
                payload_sha256,
                str(row["created_at"]),
                previous,
            )
            if (
                int(row["seq"]) != sequence
                or str(row["payload_sha256"]) != payload_sha256
                or str(row["previous_digest"]) != previous
                or str(row["entry_digest"]) != entry
            ):
                raise GatewaySchemaError("gateway event ledger integrity is invalid")
            material.append(
                {"request_id": request_id, "seq": sequence, "entry_digest": entry}
            )
            previous_by_request[request_id] = entry
            expected_seq[request_id] = sequence + 1
        return _digest(_canonical(material))

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        target = (
            f"file:{self.path.as_posix()}?mode=ro" if read_only else str(self.path)
        )
        connection = sqlite3.connect(
            target,
            uri=read_only,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        if not read_only:
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _activate_wal(connection: sqlite3.Connection) -> None:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).casefold() != "wal":
            raise GatewaySchemaError("gateway schema WAL activation failed")


def validate_gateway_wal_health(path: str | Path) -> None:
    """Bounded read-only WAL/readability probe for production readiness."""

    resolved = Path(path).expanduser().resolve()
    _require_regular_database(resolved)
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        mode = connection.execute("PRAGMA journal_mode").fetchone()
        quick = connection.execute("PRAGMA quick_check(1)").fetchone()
        if (
            mode is None
            or str(mode[0]).casefold() != "wal"
            or quick is None
            or str(quick[0]).casefold() != "ok"
        ):
            raise GatewaySchemaError("gateway SQLite WAL health check failed")
    except GatewaySchemaError:
        raise
    except (OSError, sqlite3.Error):
        raise GatewaySchemaError("gateway SQLite WAL health check failed") from None
    finally:
        connection.close()


def _is_digest(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _HEX_DIGEST
    )


def _require_regular_database_or_absent(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _require_regular_database(path)


def _require_regular_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise GatewaySchemaError("gateway schema database is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise GatewaySchemaError("gateway schema database must be a regular file")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecorex-gateway-schema")
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    manager = GatewaySchemaManager(args.database)
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment CLI
    raise SystemExit(main())


__all__ = [
    "CURRENT_GATEWAY_SCHEMA_VERSION",
    "GATEWAY_SCHEMA_SHA256",
    "GatewaySchemaError",
    "GatewaySchemaManager",
    "GatewaySchemaReceipt",
    "validate_gateway_wal_health",
]

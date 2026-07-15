"""Explicit, co-locatable SQLite schema authority for Cloud Audit.

The release Control Plane, Cloud Audit, and Cloud Share may share one database
file, but they do not share migration authority.  This module fingerprints only
``cloud_audit_*`` and ``idx_cloud_audit_*`` objects.  Runtime repositories only
validate; all DDL is reserved for :class:`CloudAuditSchemaManager`.
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


CURRENT_CLOUD_AUDIT_SCHEMA_VERSION = 1
CLOUD_AUDIT_SCHEMA_RECEIPT_VERSION = 1
_HEX_DIGEST = frozenset("0123456789abcdef")
_MANAGED_NAME_PREFIXES = ("cloud_audit_", "idx_cloud_audit_")
_MANAGED_TABLE_NAMES = (
    "cloud_audit_schema_migrations",
    "cloud_audit_records",
    "cloud_audit_idempotency",
    "cloud_audit_daily",
    "cloud_audit_integrity",
)


class CloudAuditSchemaError(RuntimeError):
    """The Cloud Audit schema is absent, unknown, drifted, or too new."""


CLOUD_AUDIT_SCHEMA_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS cloud_audit_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS cloud_audit_schema_migrations_no_update
BEFORE UPDATE ON cloud_audit_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud audit schema history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS cloud_audit_schema_migrations_no_delete
BEFORE DELETE ON cloud_audit_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud audit schema history is immutable');
END;
"""


CLOUD_AUDIT_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cloud_audit_records (
    audit_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    trace_id TEXT,
    payload_envelope TEXT NOT NULL,
    payload_format TEXT NOT NULL CHECK(payload_format = 'aesgcm-v1'),
    payload_sha256 TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    binary_included INTEGER NOT NULL CHECK(binary_included = 0),
    created_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(account_id, source_event_id, category, event_type)
);
CREATE INDEX IF NOT EXISTS idx_cloud_audit_account_time
    ON cloud_audit_records(account_id, created_at, audit_id);
CREATE INDEX IF NOT EXISTS idx_cloud_audit_thread_time
    ON cloud_audit_records(thread_id, created_at, audit_id);
CREATE INDEX IF NOT EXISTS idx_cloud_audit_category_time
    ON cloud_audit_records(category, event_type, created_at, audit_id);
CREATE TRIGGER IF NOT EXISTS cloud_audit_records_no_update
BEFORE UPDATE ON cloud_audit_records BEGIN
    SELECT RAISE(ABORT, 'cloud audit records are immutable');
END;

CREATE TABLE IF NOT EXISTS cloud_audit_idempotency (
    audit_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    first_received_at TEXT NOT NULL,
    UNIQUE(account_id, source_event_id, category, event_type)
);
CREATE INDEX IF NOT EXISTS idx_cloud_audit_idempotency_retention
    ON cloud_audit_idempotency(source_created_at, audit_id);
CREATE TRIGGER IF NOT EXISTS cloud_audit_idempotency_no_update
BEFORE UPDATE ON cloud_audit_idempotency BEGIN
    SELECT RAISE(ABORT, 'cloud audit idempotency is immutable');
END;

CREATE TABLE IF NOT EXISTS cloud_audit_daily (
    day_utc TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    PRIMARY KEY(day_utc, category, event_type)
);

CREATE TABLE IF NOT EXISTS cloud_audit_integrity (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_mac TEXT NOT NULL,
    entry_mac TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS cloud_audit_integrity_no_update
BEFORE UPDATE ON cloud_audit_integrity BEGIN
    SELECT RAISE(ABORT, 'cloud audit integrity log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS cloud_audit_integrity_no_delete
BEFORE DELETE ON cloud_audit_integrity BEGIN
    SELECT RAISE(ABORT, 'cloud audit integrity log is append-only');
END;
"""


PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SQL = CLOUD_AUDIT_CORE_SCHEMA_SQL
CLOUD_AUDIT_SCHEMA_SQL = CLOUD_AUDIT_SCHEMA_HISTORY_SQL + CLOUD_AUDIT_CORE_SCHEMA_SQL


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


def _managed_schema_records(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], ...]:
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
        if str(row[1]).startswith(_MANAGED_NAME_PREFIXES)
    )


def _schema_digest(connection: sqlite3.Connection) -> str:
    return _digest(_canonical(_managed_schema_records(connection)))


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
        raise CloudAuditSchemaError("cloud audit schema migration SQL is incomplete")


EMPTY_CLOUD_AUDIT_SCHEMA_SHA256 = _digest(_canonical(()))
PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SHA256 = _compiled_schema_digest(
    PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SQL
)
CLOUD_AUDIT_SCHEMA_SHA256 = _compiled_schema_digest(CLOUD_AUDIT_SCHEMA_SQL)
MIGRATION_001_NAME = "initial-versioned-cloud-audit"
MIGRATION_001_CHECKSUM = _digest(
    b"ecorex-cloud-audit-schema-migration-v1\0" + CLOUD_AUDIT_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class CloudAuditSchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != CLOUD_AUDIT_SCHEMA_RECEIPT_VERSION
            or self.migration_version != CURRENT_CLOUD_AUDIT_SCHEMA_VERSION
            or self.migration_name != MIGRATION_001_NAME
            or self.migration_checksum != MIGRATION_001_CHECKSUM
            or self.source_schema_sha256
            not in {
                EMPTY_CLOUD_AUDIT_SCHEMA_SHA256,
                PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SHA256,
            }
            or self.target_schema_sha256 != CLOUD_AUDIT_SCHEMA_SHA256
        ):
            raise CloudAuditSchemaError(
                "cloud audit schema migration receipt is invalid"
            )
        for value in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
        ):
            if not _is_digest(value):
                raise CloudAuditSchemaError(
                    "cloud audit schema migration receipt is invalid"
                )
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except ValueError as error:
            raise CloudAuditSchemaError(
                "cloud audit schema migration receipt is invalid"
            ) from error
        if installed.tzinfo is None:
            raise CloudAuditSchemaError(
                "cloud audit schema migration receipt is invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_checksum": self.migration_checksum,
            "source_schema_sha256": self.source_schema_sha256,
            "target_schema_sha256": self.target_schema_sha256,
            "installed_at": self.installed_at,
        }


class CloudAuditSchemaManager:
    """Operator-owned migrator and process-owned read-only validator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(
        self, *, target_version: int = CURRENT_CLOUD_AUDIT_SCHEMA_VERSION
    ) -> CloudAuditSchemaReceipt:
        if target_version != CURRENT_CLOUD_AUDIT_SCHEMA_VERSION:
            raise ValueError("cloud audit schema migration target is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_database_or_absent(self.path)
        connection = self._connect(read_only=False)
        try:
            # Cross-process migration lease.  Re-read shape only after the
            # exclusive lock and before any schema or data mutation.
            connection.execute("BEGIN EXCLUSIVE")
            source_digest = _schema_digest(connection)
            names = {record["name"] for record in _managed_schema_records(connection)}
            if "cloud_audit_schema_migrations" in names:
                receipt = self._validate_connection(connection)
                connection.commit()
                return receipt
            if source_digest == EMPTY_CLOUD_AUDIT_SCHEMA_SHA256:
                _execute_sql(connection, CLOUD_AUDIT_SCHEMA_SQL)
            elif source_digest == PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SHA256:
                _execute_sql(connection, CLOUD_AUDIT_SCHEMA_HISTORY_SQL)
            else:
                raise CloudAuditSchemaError(
                    "cloud audit schema source shape is unknown"
                )

            target_digest = _schema_digest(connection)
            if target_digest != CLOUD_AUDIT_SCHEMA_SHA256:
                raise CloudAuditSchemaError(
                    "cloud audit schema migration target drifted"
                )
            self._check_database(connection, during_migration=True)
            installed_at = datetime.now(UTC).isoformat()
            receipt = CloudAuditSchemaReceipt(
                schema_version=CLOUD_AUDIT_SCHEMA_RECEIPT_VERSION,
                migration_version=CURRENT_CLOUD_AUDIT_SCHEMA_VERSION,
                migration_name=MIGRATION_001_NAME,
                migration_checksum=MIGRATION_001_CHECKSUM,
                source_schema_sha256=source_digest,
                target_schema_sha256=target_digest,
                installed_at=installed_at,
            )
            receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
            connection.execute(
                "INSERT INTO cloud_audit_schema_migrations("
                "version,migration_name,migration_checksum,source_schema_sha256,"
                "target_schema_sha256,receipt_json,receipt_sha256,installed_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    receipt.migration_version,
                    receipt.migration_name,
                    receipt.migration_checksum,
                    receipt.source_schema_sha256,
                    receipt.target_schema_sha256,
                    receipt_json,
                    _digest(receipt_json.encode("utf-8")),
                    receipt.installed_at,
                ),
            )
            self._validate_connection(connection)
            connection.commit()
            connection.execute("PRAGMA journal_mode=WAL")
            return receipt
        except CloudAuditSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise CloudAuditSchemaError("cloud audit schema migration failed") from None
        finally:
            connection.close()

    def validate(self) -> CloudAuditSchemaReceipt:
        _require_regular_database(self.path)
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except CloudAuditSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise CloudAuditSchemaError(
                "cloud audit schema validation failed"
            ) from None
        finally:
            connection.close()

    def _validate_connection(
        self, connection: sqlite3.Connection
    ) -> CloudAuditSchemaReceipt:
        names = {record["name"] for record in _managed_schema_records(connection)}
        if "cloud_audit_schema_migrations" not in names:
            raise CloudAuditSchemaError(
                "cloud audit schema migration history is missing"
            )
        rows = connection.execute(
            "SELECT * FROM cloud_audit_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise CloudAuditSchemaError(
                "cloud audit schema migration history is missing"
            )
        versions = [int(row["version"]) for row in rows]
        if any(version > CURRENT_CLOUD_AUDIT_SCHEMA_VERSION for version in versions):
            raise CloudAuditSchemaError("cloud audit schema is newer than this process")
        if versions != list(range(1, CURRENT_CLOUD_AUDIT_SCHEMA_VERSION + 1)):
            raise CloudAuditSchemaError(
                "cloud audit schema migration history is incomplete"
            )
        if _schema_digest(connection) != CLOUD_AUDIT_SCHEMA_SHA256:
            raise CloudAuditSchemaError(
                "cloud audit schema object fingerprint is incompatible"
            )
        row = rows[-1]
        receipt_json = str(row["receipt_json"])
        if (
            row["migration_name"] != MIGRATION_001_NAME
            or row["migration_checksum"] != MIGRATION_001_CHECKSUM
            or row["source_schema_sha256"]
            not in {
                EMPTY_CLOUD_AUDIT_SCHEMA_SHA256,
                PRE_AUTHORITY_CLOUD_AUDIT_SCHEMA_SHA256,
            }
            or row["target_schema_sha256"] != CLOUD_AUDIT_SCHEMA_SHA256
            or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
        ):
            raise CloudAuditSchemaError(
                "cloud audit schema migration history is invalid"
            )
        raw = json.loads(receipt_json)
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "migration_version",
            "migration_name",
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "installed_at",
        }:
            raise CloudAuditSchemaError(
                "cloud audit schema migration receipt is invalid"
            )
        receipt = CloudAuditSchemaReceipt(**dict(raw))
        if (
            receipt.migration_version != int(row["version"])
            or receipt.migration_name != str(row["migration_name"])
            or receipt.migration_checksum != str(row["migration_checksum"])
            or receipt.source_schema_sha256 != str(row["source_schema_sha256"])
            or receipt.target_schema_sha256 != str(row["target_schema_sha256"])
            or receipt.installed_at != str(row["installed_at"])
        ):
            raise CloudAuditSchemaError(
                "cloud audit schema migration receipt is inconsistent"
            )
        self._check_database(connection, during_migration=False)
        return receipt

    @staticmethod
    def _check_database(
        connection: sqlite3.Connection, *, during_migration: bool
    ) -> None:
        for table_name in _MANAGED_TABLE_NAMES:
            violations = connection.execute(
                f"PRAGMA foreign_key_check({table_name})"
            ).fetchall()
            if violations:
                raise CloudAuditSchemaError(
                    "cloud audit schema foreign keys are invalid"
                )
            quick = connection.execute(f"PRAGMA quick_check({table_name})").fetchone()
            if quick is None or str(quick[0]).casefold() != "ok":
                suffix = (
                    "migration integrity check failed"
                    if during_migration
                    else "integrity check failed"
                )
                raise CloudAuditSchemaError(f"cloud audit schema {suffix}")

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        target = f"file:{self.path.as_posix()}?mode=ro" if read_only else str(self.path)
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


def migrate_cloud_audit_database(
    path: str | Path,
    *,
    target_version: int = CURRENT_CLOUD_AUDIT_SCHEMA_VERSION,
) -> CloudAuditSchemaReceipt:
    """Explicit deployment/test composition function for Cloud Audit."""

    return CloudAuditSchemaManager(path).migrate(target_version=target_version)


def validate_cloud_audit_database(path: str | Path) -> CloudAuditSchemaReceipt:
    """Validate Cloud Audit without creating, repairing, or mutating it."""

    return CloudAuditSchemaManager(path).validate()


def _is_digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGEST


def _require_regular_database_or_absent(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CloudAuditSchemaError(
            "cloud audit schema database is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise CloudAuditSchemaError(
            "cloud audit schema database must be a regular file"
        )


def _require_regular_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CloudAuditSchemaError(
            "cloud audit schema database is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise CloudAuditSchemaError(
            "cloud audit schema database must be a regular file"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ecorex.control_plane.audit_schema")
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    manager = CloudAuditSchemaManager(args.database)
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment CLI
    raise SystemExit(main())


__all__ = [
    "CLOUD_AUDIT_SCHEMA_SHA256",
    "CURRENT_CLOUD_AUDIT_SCHEMA_VERSION",
    "CloudAuditSchemaError",
    "CloudAuditSchemaManager",
    "CloudAuditSchemaReceipt",
    "migrate_cloud_audit_database",
    "validate_cloud_audit_database",
]

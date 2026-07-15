"""Explicit SQLite schema authority for local image orchestration.

``SQLiteImageJobStore`` is a runtime repository and never creates or repairs
storage.  Operators and tests invoke :class:`SQLiteImageSchemaManager` before
composition.  The manager accepts only an empty database, the one exact
pre-authority v1 schema, or the already-versioned current schema.
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


CURRENT_SQLITE_IMAGE_SCHEMA_VERSION = 1
SQLITE_IMAGE_SCHEMA_RECEIPT_VERSION = 1
_HEX_DIGEST = frozenset("0123456789abcdef")


class SQLiteImageSchemaError(RuntimeError):
    """The SQLite image schema is absent, unknown, drifted, or too new."""


SQLITE_IMAGE_SCHEMA_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS image_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS image_schema_migrations_no_update
BEFORE UPDATE ON image_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'image schema history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS image_schema_migrations_no_delete
BEFORE DELETE ON image_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'image schema history is immutable');
END;
"""


# This is the complete physical contract that the pre-authority Store used to
# create in its constructor.  Constraints are intentionally kept in the table
# SQL because the canonical sqlite_schema digest binds them as well as every
# explicit index and trigger.
SQLITE_IMAGE_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    model_id TEXT NOT NULL,
    size_class TEXT NOT NULL,
    weight INTEGER NOT NULL CHECK(weight > 0),
    priority INTEGER NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    fair_finish REAL NOT NULL,
    available_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    provider_idempotency_key TEXT NOT NULL UNIQUE,
    provider_request_id TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, client_request_id)
);
CREATE INDEX IF NOT EXISTS image_jobs_schedulable
    ON image_jobs(status, available_at, fair_finish, priority, created_at);
CREATE INDEX IF NOT EXISTS image_jobs_account_status
    ON image_jobs(account_id, status, created_at);
CREATE INDEX IF NOT EXISTS image_jobs_model_status
    ON image_jobs(model_id, status);

CREATE TABLE IF NOT EXISTS image_scheduler_accounts (
    account_id TEXT PRIMARY KEY,
    last_finish REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS image_inputs (
    account_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    mime_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,sha256)
);
CREATE TRIGGER IF NOT EXISTS image_inputs_no_update
BEFORE UPDATE ON image_inputs BEGIN
    SELECT RAISE(ABORT, 'image input ownership is immutable');
END;
CREATE TRIGGER IF NOT EXISTS image_inputs_no_delete
BEFORE DELETE ON image_inputs BEGIN
    SELECT RAISE(ABORT, 'image input ownership is immutable');
END;

CREATE TABLE IF NOT EXISTS image_results (
    job_id TEXT PRIMARY KEY REFERENCES image_jobs(job_id),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS image_results_no_update
BEFORE UPDATE ON image_results BEGIN
    SELECT RAISE(ABORT, 'image results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS image_results_no_delete
BEFORE DELETE ON image_results BEGIN
    SELECT RAISE(ABORT, 'image results are immutable');
END;

CREATE TABLE IF NOT EXISTS image_usage (
    job_id TEXT PRIMARY KEY REFERENCES image_jobs(job_id),
    usage_json TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS image_usage_no_update
BEFORE UPDATE ON image_usage BEGIN
    SELECT RAISE(ABORT, 'image usage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS image_usage_no_delete
BEFORE DELETE ON image_usage BEGIN
    SELECT RAISE(ABORT, 'image usage is immutable');
END;

CREATE TABLE IF NOT EXISTS image_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES image_jobs(job_id),
    account_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS image_events_job_seq ON image_events(job_id, seq);
CREATE TRIGGER IF NOT EXISTS image_events_no_update
BEFORE UPDATE ON image_events BEGIN
    SELECT RAISE(ABORT, 'image events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS image_events_no_delete
BEFORE DELETE ON image_events BEGIN
    SELECT RAISE(ABORT, 'image events are append-only');
END;

CREATE TABLE IF NOT EXISTS image_breakers (
    scope TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL,
    open_until TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_recovery_requests (
    account_id TEXT NOT NULL,
    recovery_request_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES image_jobs(job_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,recovery_request_id)
);
CREATE TRIGGER IF NOT EXISTS image_recovery_no_update
BEFORE UPDATE ON image_recovery_requests BEGIN
    SELECT RAISE(ABORT, 'image recovery requests are immutable');
END;
CREATE TRIGGER IF NOT EXISTS image_recovery_no_delete
BEFORE DELETE ON image_recovery_requests BEGIN
    SELECT RAISE(ABORT, 'image recovery requests are immutable');
END;
"""


PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SQL = SQLITE_IMAGE_CORE_SCHEMA_SQL
SQLITE_IMAGE_SCHEMA_SQL = (
    SQLITE_IMAGE_SCHEMA_HISTORY_SQL + SQLITE_IMAGE_CORE_SCHEMA_SQL
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


def _schema_records(
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
        raise SQLiteImageSchemaError("SQLite image schema migration SQL is incomplete")


EMPTY_SQLITE_IMAGE_SCHEMA_SHA256 = _digest(_canonical(()))
PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SHA256 = _compiled_schema_digest(
    PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SQL
)
SQLITE_IMAGE_SCHEMA_SHA256 = _compiled_schema_digest(SQLITE_IMAGE_SCHEMA_SQL)
MIGRATION_001_NAME = "initial-versioned-sqlite-image-orchestration"
MIGRATION_001_CHECKSUM = _digest(
    b"ecorex-sqlite-image-schema-migration-v1\0"
    + SQLITE_IMAGE_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class SQLiteImageSchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != SQLITE_IMAGE_SCHEMA_RECEIPT_VERSION
            or self.migration_version != CURRENT_SQLITE_IMAGE_SCHEMA_VERSION
            or self.migration_name != MIGRATION_001_NAME
            or self.migration_checksum != MIGRATION_001_CHECKSUM
            or self.source_schema_sha256
            not in {
                EMPTY_SQLITE_IMAGE_SCHEMA_SHA256,
                PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SHA256,
            }
            or self.target_schema_sha256 != SQLITE_IMAGE_SCHEMA_SHA256
        ):
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is invalid"
            )
        for value in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
        ):
            if not _is_digest(value):
                raise SQLiteImageSchemaError(
                    "SQLite image schema migration receipt is invalid"
                )
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except ValueError as error:
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is invalid"
            ) from error
        if installed.tzinfo is None or installed.utcoffset() is None:
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is invalid"
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


class SQLiteImageSchemaManager:
    """Operator-owned migrator and runtime-owned read-only validator."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(os.path.abspath(Path(path).expanduser()))

    def migrate(
        self, *, target_version: int = CURRENT_SQLITE_IMAGE_SCHEMA_VERSION
    ) -> SQLiteImageSchemaReceipt:
        if target_version != CURRENT_SQLITE_IMAGE_SCHEMA_VERSION:
            raise ValueError("SQLite image schema migration target is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_database_or_absent(self.path)
        connection = self._connect(read_only=False)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            source_digest = _schema_digest(connection)
            names = {record["name"] for record in _schema_records(connection)}
            if "image_schema_migrations" in names:
                receipt = self._validate_connection(connection)
                connection.commit()
            else:
                if source_digest == EMPTY_SQLITE_IMAGE_SCHEMA_SHA256:
                    _execute_sql(connection, SQLITE_IMAGE_SCHEMA_SQL)
                elif source_digest == PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SHA256:
                    _execute_sql(connection, SQLITE_IMAGE_SCHEMA_HISTORY_SQL)
                else:
                    raise SQLiteImageSchemaError(
                        "SQLite image schema source shape is unknown"
                    )

                target_digest = _schema_digest(connection)
                if target_digest != SQLITE_IMAGE_SCHEMA_SHA256:
                    raise SQLiteImageSchemaError(
                        "SQLite image schema migration target drifted"
                    )
                self._check_database(connection, during_migration=True)
                receipt = SQLiteImageSchemaReceipt(
                    schema_version=SQLITE_IMAGE_SCHEMA_RECEIPT_VERSION,
                    migration_version=CURRENT_SQLITE_IMAGE_SCHEMA_VERSION,
                    migration_name=MIGRATION_001_NAME,
                    migration_checksum=MIGRATION_001_CHECKSUM,
                    source_schema_sha256=source_digest,
                    target_schema_sha256=target_digest,
                    installed_at=datetime.now(UTC).isoformat(),
                )
                receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
                connection.execute(
                    "INSERT INTO image_schema_migrations("
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

            # ``journal_mode`` cannot change inside the schema transaction.
            # Always reach this point, including for an already-versioned
            # database, so a crash/failure after the schema commit can be
            # repaired by rerunning the explicit deployment migration.
            self._activate_wal(connection)
            return receipt
        except SQLiteImageSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise SQLiteImageSchemaError(
                "SQLite image schema migration failed"
            ) from None
        finally:
            connection.close()

    def validate(self) -> SQLiteImageSchemaReceipt:
        _require_regular_database(self.path)
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except SQLiteImageSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise SQLiteImageSchemaError(
                "SQLite image schema validation failed"
            ) from None
        finally:
            connection.close()

    def _validate_connection(
        self, connection: sqlite3.Connection
    ) -> SQLiteImageSchemaReceipt:
        names = {record["name"] for record in _schema_records(connection)}
        if "image_schema_migrations" not in names:
            raise SQLiteImageSchemaError(
                "SQLite image schema migration history is missing"
            )
        rows = connection.execute(
            "SELECT * FROM image_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise SQLiteImageSchemaError(
                "SQLite image schema migration history is missing"
            )
        versions = [int(row["version"]) for row in rows]
        if any(version > CURRENT_SQLITE_IMAGE_SCHEMA_VERSION for version in versions):
            raise SQLiteImageSchemaError(
                "SQLite image schema is newer than this process"
            )
        if versions != list(range(1, CURRENT_SQLITE_IMAGE_SCHEMA_VERSION + 1)):
            raise SQLiteImageSchemaError(
                "SQLite image schema migration history is incomplete"
            )
        if _schema_digest(connection) != SQLITE_IMAGE_SCHEMA_SHA256:
            raise SQLiteImageSchemaError(
                "SQLite image schema object fingerprint is incompatible"
            )
        row = rows[-1]
        receipt_json = str(row["receipt_json"])
        source_digest = str(row["source_schema_sha256"])
        if (
            row["migration_name"] != MIGRATION_001_NAME
            or row["migration_checksum"] != MIGRATION_001_CHECKSUM
            or source_digest
            not in {
                EMPTY_SQLITE_IMAGE_SCHEMA_SHA256,
                PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SHA256,
            }
            or row["target_schema_sha256"] != SQLITE_IMAGE_SCHEMA_SHA256
            or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
        ):
            raise SQLiteImageSchemaError(
                "SQLite image schema migration history is invalid"
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
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is invalid"
            )
        receipt = SQLiteImageSchemaReceipt(**dict(raw))
        if receipt_json.encode("utf-8") != _canonical(receipt.to_dict()):
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is non-canonical"
            )
        if (
            receipt.migration_version != int(row["version"])
            or receipt.migration_name != str(row["migration_name"])
            or receipt.migration_checksum != str(row["migration_checksum"])
            or receipt.source_schema_sha256 != source_digest
            or receipt.target_schema_sha256 != str(row["target_schema_sha256"])
            or receipt.installed_at != str(row["installed_at"])
        ):
            raise SQLiteImageSchemaError(
                "SQLite image schema migration receipt is inconsistent"
            )
        self._check_database(connection, during_migration=False)
        return receipt

    @staticmethod
    def _check_database(
        connection: sqlite3.Connection, *, during_migration: bool
    ) -> None:
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SQLiteImageSchemaError(
                "SQLite image schema foreign keys are invalid"
            )
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            suffix = (
                "migration integrity check failed"
                if during_migration
                else "integrity check failed"
            )
            raise SQLiteImageSchemaError(f"SQLite image schema {suffix}")

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
            raise SQLiteImageSchemaError("SQLite image schema WAL activation failed")


def migrate_sqlite_image_database(
    path: str | os.PathLike[str],
    *,
    target_version: int = CURRENT_SQLITE_IMAGE_SCHEMA_VERSION,
) -> SQLiteImageSchemaReceipt:
    """Explicit deployment/test composition function for SQLite image data."""

    return SQLiteImageSchemaManager(path).migrate(target_version=target_version)


def validate_sqlite_image_database(
    path: str | os.PathLike[str],
) -> SQLiteImageSchemaReceipt:
    """Validate without creating, repairing, or otherwise mutating storage."""

    return SQLiteImageSchemaManager(path).validate()


def _is_digest(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _HEX_DIGEST
    )


def _require_regular_database_or_absent(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _require_regular_database(path, allow_empty=True)


def _require_regular_database(path: Path, *, allow_empty: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SQLiteImageSchemaError(
            "SQLite image schema database is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_size <= 0 and not allow_empty)
    ):
        raise SQLiteImageSchemaError(
            "SQLite image schema database must be a regular file"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ecorex.image_orchestrator.sqlite_schema"
    )
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    manager = SQLiteImageSchemaManager(args.database)
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment CLI
    raise SystemExit(main())


__all__ = [
    "CURRENT_SQLITE_IMAGE_SCHEMA_VERSION",
    "PRE_AUTHORITY_SQLITE_IMAGE_SCHEMA_SHA256",
    "SQLITE_IMAGE_CORE_SCHEMA_SQL",
    "SQLITE_IMAGE_SCHEMA_SHA256",
    "SQLiteImageSchemaError",
    "SQLiteImageSchemaManager",
    "SQLiteImageSchemaReceipt",
    "migrate_sqlite_image_database",
    "validate_sqlite_image_database",
]

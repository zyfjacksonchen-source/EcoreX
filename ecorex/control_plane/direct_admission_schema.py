"""Versioned append-only storage for direct-release admissions and waivers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION = 1
DIRECT_ADMISSION_MIGRATION_NAME = "direct-release-admission-v1"

DIRECT_ADMISSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS direct_admission_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version = 1),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS direct_admission_schema_migrations_no_update
BEFORE UPDATE ON direct_admission_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'direct admission schema history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS direct_admission_schema_migrations_no_delete
BEFORE DELETE ON direct_admission_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'direct admission schema history is immutable');
END;

CREATE TABLE IF NOT EXISTS direct_release_admissions (
    attestation_sha256 TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    phase TEXT NOT NULL CHECK(phase IN ('prepare','finalize')),
    operator_instruction_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    candidate_receipt_sha256 TEXT NOT NULL,
    operator_waiver_sha256 TEXT NOT NULL,
    publication_receipt_sha256 TEXT NOT NULL,
    release_key_id TEXT NOT NULL,
    publication_key_id TEXT NOT NULL,
    attestation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(release_id, phase),
    CHECK(release_key_id <> publication_key_id)
);
CREATE TRIGGER IF NOT EXISTS direct_release_admissions_no_update
BEFORE UPDATE ON direct_release_admissions BEGIN
    SELECT RAISE(ABORT, 'direct release admissions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS direct_release_admissions_no_delete
BEFORE DELETE ON direct_release_admissions BEGIN
    SELECT RAISE(ABORT, 'direct release admissions are immutable');
END;

CREATE TABLE IF NOT EXISTS direct_release_gate_waivers (
    release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    gate_name TEXT NOT NULL CHECK(
        gate_name IN ('live-model','live-image','cdp-acceptance')
    ),
    status TEXT NOT NULL CHECK(status = 'waived'),
    evidence TEXT NOT NULL,
    attestation_sha256 TEXT NOT NULL
        REFERENCES direct_release_admissions(attestation_sha256),
    created_at TEXT NOT NULL,
    PRIMARY KEY(release_id, gate_name)
);
CREATE TRIGGER IF NOT EXISTS direct_release_gate_waivers_no_update
BEFORE UPDATE ON direct_release_gate_waivers BEGIN
    SELECT RAISE(ABORT, 'direct release gate waivers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS direct_release_gate_waivers_no_delete
BEFORE DELETE ON direct_release_gate_waivers BEGIN
    SELECT RAISE(ABORT, 'direct release gate waivers are immutable');
END;
CREATE INDEX IF NOT EXISTS direct_release_admissions_release_phase
    ON direct_release_admissions(release_id, phase);
"""

DIRECT_ADMISSION_MIGRATION_CHECKSUM = hashlib.sha256(
    b"ecorex-direct-admission-schema-v1\0"
    + DIRECT_ADMISSION_SCHEMA_SQL.encode("utf-8")
).hexdigest()


class DirectAdmissionSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DirectAdmissionSchemaReceipt:
    schema_version: int
    migration_name: str
    migration_checksum: str
    installed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_name": self.migration_name,
            "migration_checksum": self.migration_checksum,
            "installed_at": self.installed_at,
        }


class DirectAdmissionSchemaManager:
    """Operator migrator and process-owned strict validator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(self) -> DirectAdmissionSchemaReceipt:
        connection = self._connect(read_only=False)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            _execute_sql(connection, DIRECT_ADMISSION_SCHEMA_SQL)
            row = connection.execute(
                "SELECT * FROM direct_admission_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO direct_admission_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at) "
                    "VALUES(1,?,?,?)",
                    (
                        DIRECT_ADMISSION_MIGRATION_NAME,
                        DIRECT_ADMISSION_MIGRATION_CHECKSUM,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except DirectAdmissionSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if connection.in_transaction:
                connection.rollback()
            raise DirectAdmissionSchemaError(
                "direct admission schema migration failed"
            ) from None
        finally:
            connection.close()

    def validate(self) -> DirectAdmissionSchemaReceipt:
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except DirectAdmissionSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if connection.in_transaction:
                connection.rollback()
            raise DirectAdmissionSchemaError(
                "direct admission schema validation failed"
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _validate_connection(
        connection: sqlite3.Connection,
    ) -> DirectAdmissionSchemaReceipt:
        expected = _compiled_objects()
        observed = _managed_objects(connection)
        if observed != expected:
            raise DirectAdmissionSchemaError(
                "direct admission schema object fingerprint is incompatible"
            )
        rows = connection.execute(
            "SELECT * FROM direct_admission_schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != 1:
            raise DirectAdmissionSchemaError(
                "direct admission schema history is incomplete"
            )
        row = rows[0]
        if (
            int(row["version"]) != CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION
            or row["migration_name"] != DIRECT_ADMISSION_MIGRATION_NAME
            or row["migration_checksum"] != DIRECT_ADMISSION_MIGRATION_CHECKSUM
        ):
            raise DirectAdmissionSchemaError(
                "direct admission schema history is invalid"
            )
        try:
            installed = datetime.fromisoformat(str(row["installed_at"]))
        except ValueError:
            raise DirectAdmissionSchemaError(
                "direct admission schema history is invalid"
            ) from None
        if installed.tzinfo is None:
            raise DirectAdmissionSchemaError(
                "direct admission schema history is invalid"
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise DirectAdmissionSchemaError(
                "direct admission schema foreign keys are invalid"
            )
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            raise DirectAdmissionSchemaError(
                "direct admission schema database integrity is invalid"
            )
        return DirectAdmissionSchemaReceipt(
            schema_version=1,
            migration_name=str(row["migration_name"]),
            migration_checksum=str(row["migration_checksum"]),
            installed_at=str(row["installed_at"]),
        )

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
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if not read_only:
            connection.execute("PRAGMA synchronous=FULL")
        return connection


def _managed_objects(connection: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name LIKE 'direct_%' AND sql IS NOT NULL ORDER BY type,name"
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


def _compiled_objects() -> tuple[dict[str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "CREATE TABLE control_releases(release_id TEXT PRIMARY KEY)"
        )
        connection.executescript(DIRECT_ADMISSION_SCHEMA_SQL)
        return _managed_objects(connection)
    finally:
        connection.close()


def _execute_sql(connection: sqlite3.Connection, sql: str) -> None:
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                connection.execute(statement)
    if pending.strip():
        raise DirectAdmissionSchemaError(
            "direct admission schema migration SQL is incomplete"
        )


__all__ = [
    "CURRENT_DIRECT_ADMISSION_SCHEMA_VERSION",
    "DIRECT_ADMISSION_MIGRATION_CHECKSUM",
    "DIRECT_ADMISSION_MIGRATION_NAME",
    "DirectAdmissionSchemaError",
    "DirectAdmissionSchemaManager",
    "DirectAdmissionSchemaReceipt",
]

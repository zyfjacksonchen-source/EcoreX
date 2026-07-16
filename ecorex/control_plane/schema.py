"""Explicit SQLite schema authority for the EcoreX Control Plane core.

Release, rollout, client distribution, and durable update-signal processes only
validate this contract.  Schema mutation is reserved for
``ControlPlaneSchemaManager.migrate`` and its deployment CLI.  Cloud Share and
Cloud Audit deliberately remain outside this manager's namespace and lifecycle.
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

from ecorex.update.locking import LockUnavailable, ProductFileLock

from .bootstrap_index_schema import (
    BootstrapIndexSchemaError,
    BootstrapIndexSchemaManager,
)
from .direct_admission_schema import (
    DirectAdmissionSchemaError,
    DirectAdmissionSchemaManager,
)


CURRENT_CONTROL_PLANE_SCHEMA_VERSION = 1
CONTROL_PLANE_SCHEMA_RECEIPT_VERSION = 1
_HEX_DIGEST = frozenset("0123456789abcdef")
_MANAGED_NAME_PREFIXES = ("control_", "idx_control_")


class ControlPlaneSchemaError(RuntimeError):
    """The managed Control Plane schema is absent, unknown, or incompatible."""


CONTROL_PLANE_SCHEMA_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS control_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS control_schema_migrations_no_update
BEFORE UPDATE ON control_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'control plane schema history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS control_schema_migrations_no_delete
BEFORE DELETE ON control_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'control plane schema history is immutable');
END;
"""


CONTROL_PLANE_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS control_releases (
    release_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    build_digest TEXT NOT NULL,
    channel TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    manifest_file_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate','published','withdrawn')),
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS control_release_gates (
    release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    gate_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed','failed')),
    evidence TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(release_id, gate_name)
);
CREATE TABLE IF NOT EXISTS control_release_gate_attestations (
    attestation_sha256 TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    phase TEXT NOT NULL CHECK (phase IN ('prepare','finalize')),
    attestation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(release_id, phase)
);
CREATE TRIGGER IF NOT EXISTS control_release_gate_attestations_no_update
BEFORE UPDATE ON control_release_gate_attestations BEGIN
    SELECT RAISE(ABORT, 'release gate attestations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS control_release_gate_attestations_no_delete
BEFORE DELETE ON control_release_gate_attestations BEGIN
    SELECT RAISE(ABORT, 'release gate attestations are immutable');
END;
CREATE TABLE IF NOT EXISTS control_rollouts (
    rollout_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','active','paused','halted','completed')),
    percentage INTEGER NOT NULL CHECK (percentage BETWEEN 1 AND 100),
    target_organizations_json TEXT NOT NULL,
    target_accounts_json TEXT NOT NULL,
    minimum_compatible_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_rollouts_active
    ON control_rollouts(channel, status, created_at);
CREATE TABLE IF NOT EXISTS control_release_rollbacks (
    rollback_id TEXT PRIMARY KEY REFERENCES control_rollouts(rollout_id),
    source_release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    target_release_id TEXT NOT NULL REFERENCES control_releases(release_id),
    authorization_ttl_seconds INTEGER NOT NULL
        CHECK (authorization_ttl_seconds BETWEEN 60 AND 900),
    created_at TEXT NOT NULL,
    CHECK (source_release_id <> target_release_id)
);
CREATE INDEX IF NOT EXISTS idx_control_release_rollbacks_source
    ON control_release_rollbacks(source_release_id, created_at DESC);
CREATE TABLE IF NOT EXISTS control_channel_state (
    channel TEXT PRIMARY KEY CHECK (channel IN ('canary','stable')),
    kill_switch_active INTEGER NOT NULL
        CHECK (kill_switch_active IN (0,1)),
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO control_channel_state VALUES
    ('canary',0,'1970-01-01T00:00:00+00:00'),
    ('stable',0,'1970-01-01T00:00:00+00:00');
CREATE TABLE IF NOT EXISTS control_clients (
    client_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    organization_id TEXT,
    platform TEXT NOT NULL,
    architecture TEXT NOT NULL,
    current_version TEXT NOT NULL,
    update_state TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_idempotency (
    actor_subject TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_subject, client_request_id)
);
CREATE TRIGGER IF NOT EXISTS control_idempotency_no_update
BEFORE UPDATE ON control_idempotency BEGIN
    SELECT RAISE(ABORT, 'control idempotency is append-only');
END;
CREATE TRIGGER IF NOT EXISTS control_idempotency_no_delete
BEFORE DELETE ON control_idempotency BEGIN
    SELECT RAISE(ABORT, 'control idempotency is append-only');
END;
CREATE TABLE IF NOT EXISTS control_admin_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS control_admin_audit_no_update
BEFORE UPDATE ON control_admin_audit BEGIN
    SELECT RAISE(ABORT, 'control audit is append-only');
END;
CREATE TRIGGER IF NOT EXISTS control_admin_audit_no_delete
BEFORE DELETE ON control_admin_audit BEGIN
    SELECT RAISE(ABORT, 'control audit is append-only');
END;
CREATE TABLE IF NOT EXISTS control_update_signals (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    dedupe_key TEXT NOT NULL UNIQUE,
    signal_type TEXT NOT NULL CHECK (signal_type IN (
        'rollout.activated','rollout.paused','rollout.halted',
        'channel.killed','channel.kill_cleared'
    )),
    channel TEXT NOT NULL CHECK (channel IN ('canary','stable')),
    rollout_id TEXT REFERENCES control_rollouts(rollout_id),
    release_id TEXT REFERENCES control_releases(release_id),
    created_at TEXT NOT NULL,
    CHECK (
        (signal_type LIKE 'rollout.%' AND rollout_id IS NOT NULL
            AND release_id IS NOT NULL)
        OR
        (signal_type LIKE 'channel.%' AND rollout_id IS NULL
            AND release_id IS NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS control_update_signals_no_update
BEFORE UPDATE ON control_update_signals BEGIN
    SELECT RAISE(ABORT, 'control update signals are append-only');
END;
CREATE INDEX IF NOT EXISTS idx_control_update_signals_created
    ON control_update_signals(created_at, sequence);
CREATE TABLE IF NOT EXISTS control_update_signal_consumers (
    consumer_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_releases_resume_order
    ON control_releases(created_at DESC, release_id DESC);
CREATE INDEX IF NOT EXISTS idx_control_rollouts_resume_order
    ON control_rollouts(created_at DESC, rollout_id DESC);
CREATE INDEX IF NOT EXISTS idx_control_audit_creation_order
    ON control_admin_audit(action, target_id, sequence);
"""


PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SQL = CONTROL_PLANE_CORE_SCHEMA_SQL
CONTROL_PLANE_SCHEMA_SQL = (
    CONTROL_PLANE_SCHEMA_HISTORY_SQL + CONTROL_PLANE_CORE_SCHEMA_SQL
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


def _managed_schema_records(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], ...]:
    """Return every managed object; Cloud Share/Audit objects are out of scope."""

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
        raise ControlPlaneSchemaError(
            "control plane schema migration SQL is incomplete"
        )


EMPTY_CONTROL_PLANE_SCHEMA_SHA256 = _digest(_canonical(()))
PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SHA256 = _compiled_schema_digest(
    PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SQL
)
CONTROL_PLANE_SCHEMA_SHA256 = _compiled_schema_digest(CONTROL_PLANE_SCHEMA_SQL)
MIGRATION_001_NAME = "initial-versioned-control-plane-core"
MIGRATION_001_CHECKSUM = _digest(
    b"ecorex-control-plane-schema-migration-v1\0"
    + CONTROL_PLANE_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class ControlPlaneSchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != CONTROL_PLANE_SCHEMA_RECEIPT_VERSION
            or self.migration_version != CURRENT_CONTROL_PLANE_SCHEMA_VERSION
            or self.migration_name != MIGRATION_001_NAME
            or self.migration_checksum != MIGRATION_001_CHECKSUM
            or self.source_schema_sha256
            not in {
                EMPTY_CONTROL_PLANE_SCHEMA_SHA256,
                PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SHA256,
            }
            or self.target_schema_sha256 != CONTROL_PLANE_SCHEMA_SHA256
        ):
            raise ControlPlaneSchemaError(
                "control plane schema migration receipt is invalid"
            )
        for value in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
        ):
            if not _is_digest(value):
                raise ControlPlaneSchemaError(
                    "control plane schema migration receipt is invalid"
                )
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except ValueError as error:
            raise ControlPlaneSchemaError(
                "control plane schema migration receipt is invalid"
            ) from error
        if installed.tzinfo is None:
            raise ControlPlaneSchemaError(
                "control plane schema migration receipt is invalid"
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


class ControlPlaneSchemaManager:
    """Operator-owned migrator and process-owned read-only validator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(
        self, *, target_version: int = CURRENT_CONTROL_PLANE_SCHEMA_VERSION
    ) -> ControlPlaneSchemaReceipt:
        lock_path = self.path.with_name(self.path.name + ".schema-migrate.lock")
        try:
            with ProductFileLock(lock_path, timeout=60):
                return self._migrate_locked(target_version=target_version)
        except LockUnavailable:
            raise ControlPlaneSchemaError(
                "control plane schema migration lease is unavailable"
            ) from None

    def _migrate_locked(
        self, *, target_version: int
    ) -> ControlPlaneSchemaReceipt:
        if target_version != CURRENT_CONTROL_PLANE_SCHEMA_VERSION:
            raise ValueError("control plane schema migration target is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_database_or_absent(self.path)
        connection = self._connect(read_only=False)
        try:
            # Cross-process migration lease.  The shape is re-read only after
            # this lock has been acquired, before any schema or data mutation.
            connection.execute("BEGIN EXCLUSIVE")
            source_digest = _schema_digest(connection)
            names = {record["name"] for record in _managed_schema_records(connection)}
            if "control_schema_migrations" in names:
                receipt = self._validate_connection(connection)
                connection.commit()
                BootstrapIndexSchemaManager(self.path).migrate()
                DirectAdmissionSchemaManager(self.path).migrate()
                return receipt
            if source_digest == EMPTY_CONTROL_PLANE_SCHEMA_SHA256:
                _execute_sql(connection, CONTROL_PLANE_SCHEMA_SQL)
            elif source_digest == PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SHA256:
                _execute_sql(connection, CONTROL_PLANE_SCHEMA_HISTORY_SQL)
            else:
                raise ControlPlaneSchemaError(
                    "control plane schema source shape is unknown"
                )

            target_digest = _schema_digest(connection)
            if target_digest != CONTROL_PLANE_SCHEMA_SHA256:
                raise ControlPlaneSchemaError(
                    "control plane schema migration target drifted"
                )
            self._check_database(connection, during_migration=True)
            installed_at = datetime.now(UTC).isoformat()
            receipt = ControlPlaneSchemaReceipt(
                schema_version=CONTROL_PLANE_SCHEMA_RECEIPT_VERSION,
                migration_version=CURRENT_CONTROL_PLANE_SCHEMA_VERSION,
                migration_name=MIGRATION_001_NAME,
                migration_checksum=MIGRATION_001_CHECKSUM,
                source_schema_sha256=source_digest,
                target_schema_sha256=target_digest,
                installed_at=installed_at,
            )
            receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
            connection.execute(
                "INSERT INTO control_schema_migrations("
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
            BootstrapIndexSchemaManager(self.path).migrate()
            DirectAdmissionSchemaManager(self.path).migrate()
            return receipt
        except (BootstrapIndexSchemaError, DirectAdmissionSchemaError):
            if connection.in_transaction:
                connection.rollback()
            raise ControlPlaneSchemaError(
                "control plane extension schema migration failed"
            ) from None
        except ControlPlaneSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise ControlPlaneSchemaError(
                "control plane schema migration failed"
            ) from None
        finally:
            connection.close()

    def validate(self) -> ControlPlaneSchemaReceipt:
        _require_regular_database(self.path)
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            BootstrapIndexSchemaManager(self.path).validate()
            DirectAdmissionSchemaManager(self.path).validate()
            return receipt
        except ControlPlaneSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except Exception as error:
            if isinstance(
                error, (BootstrapIndexSchemaError, DirectAdmissionSchemaError)
            ):
                if connection.in_transaction:
                    connection.rollback()
                raise ControlPlaneSchemaError(
                    "control plane extension schema is incompatible"
                ) from None
            if connection.in_transaction:
                connection.rollback()
            raise ControlPlaneSchemaError(
                "control plane schema validation failed"
            ) from None
        finally:
            connection.close()

    def _validate_connection(
        self, connection: sqlite3.Connection
    ) -> ControlPlaneSchemaReceipt:
        names = {record["name"] for record in _managed_schema_records(connection)}
        if "control_schema_migrations" not in names:
            raise ControlPlaneSchemaError(
                "control plane schema migration history is missing"
            )
        rows = connection.execute(
            "SELECT * FROM control_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise ControlPlaneSchemaError(
                "control plane schema migration history is missing"
            )
        versions = [int(row["version"]) for row in rows]
        if any(version > CURRENT_CONTROL_PLANE_SCHEMA_VERSION for version in versions):
            raise ControlPlaneSchemaError(
                "control plane schema is newer than this process"
            )
        if versions != list(range(1, CURRENT_CONTROL_PLANE_SCHEMA_VERSION + 1)):
            raise ControlPlaneSchemaError(
                "control plane schema migration history is incomplete"
            )
        if _schema_digest(connection) != CONTROL_PLANE_SCHEMA_SHA256:
            raise ControlPlaneSchemaError(
                "control plane schema object fingerprint is incompatible"
            )
        row = rows[-1]
        receipt_json = str(row["receipt_json"])
        if (
            row["migration_name"] != MIGRATION_001_NAME
            or row["migration_checksum"] != MIGRATION_001_CHECKSUM
            or row["source_schema_sha256"]
            not in {
                EMPTY_CONTROL_PLANE_SCHEMA_SHA256,
                PRE_AUTHORITY_CONTROL_PLANE_SCHEMA_SHA256,
            }
            or row["target_schema_sha256"] != CONTROL_PLANE_SCHEMA_SHA256
            or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
        ):
            raise ControlPlaneSchemaError(
                "control plane schema migration history is invalid"
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
            raise ControlPlaneSchemaError(
                "control plane schema migration receipt is invalid"
            )
        receipt = ControlPlaneSchemaReceipt(**dict(raw))
        if (
            receipt.migration_version != int(row["version"])
            or receipt.migration_name != str(row["migration_name"])
            or receipt.migration_checksum != str(row["migration_checksum"])
            or receipt.source_schema_sha256 != str(row["source_schema_sha256"])
            or receipt.target_schema_sha256 != str(row["target_schema_sha256"])
            or receipt.installed_at != str(row["installed_at"])
        ):
            raise ControlPlaneSchemaError(
                "control plane schema migration receipt is inconsistent"
            )
        self._check_database(connection, during_migration=False)
        return receipt

    @staticmethod
    def _check_database(
        connection: sqlite3.Connection, *, during_migration: bool
    ) -> None:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ControlPlaneSchemaError(
                "control plane schema foreign keys are invalid"
            )
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            suffix = (
                "migration integrity check failed"
                if during_migration
                else "integrity check failed"
            )
            raise ControlPlaneSchemaError(f"control plane schema {suffix}")

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


def migrate_control_plane_database(
    path: str | Path,
    *,
    target_version: int = CURRENT_CONTROL_PLANE_SCHEMA_VERSION,
) -> ControlPlaneSchemaReceipt:
    """Explicit deployment/test composition function for the managed schema."""

    return ControlPlaneSchemaManager(path).migrate(target_version=target_version)


def validate_control_plane_database(path: str | Path) -> ControlPlaneSchemaReceipt:
    """Validate without creating, repairing, or otherwise mutating the database."""

    return ControlPlaneSchemaManager(path).validate()


def validate_control_plane_wal_health(path: str | Path) -> None:
    """Lightweight production readiness check without schema mutation.

    Full object/catalog validation remains :meth:`ControlPlaneSchemaManager.validate`.
    This bounded probe only verifies that the already-validated shared database
    is still a readable WAL database with a healthy first integrity page.
    """

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
            raise ControlPlaneSchemaError(
                "control plane SQLite WAL health check failed"
            )
    except ControlPlaneSchemaError:
        raise
    except (OSError, sqlite3.Error):
        raise ControlPlaneSchemaError(
            "control plane SQLite WAL health check failed"
        ) from None
    finally:
        connection.close()


def _is_digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGEST


def _require_regular_database_or_absent(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ControlPlaneSchemaError(
            "control plane schema database is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ControlPlaneSchemaError(
            "control plane schema database must be a regular file"
        )


def _require_regular_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ControlPlaneSchemaError(
            "control plane schema database is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise ControlPlaneSchemaError(
            "control plane schema database must be a regular file"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ecorex.control_plane.schema")
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    manager = ControlPlaneSchemaManager(args.database)
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment CLI
    raise SystemExit(main())


__all__ = [
    "CONTROL_PLANE_SCHEMA_SHA256",
    "CURRENT_CONTROL_PLANE_SCHEMA_VERSION",
    "ControlPlaneSchemaError",
    "ControlPlaneSchemaManager",
    "ControlPlaneSchemaReceipt",
    "migrate_control_plane_database",
    "validate_control_plane_database",
    "validate_control_plane_wal_health",
]

"""Durable schema authority for the managed device identity broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sqlite3


CURRENT_DEVICE_IDENTITY_SCHEMA_VERSION = 1
DEVICE_IDENTITY_MIGRATION_NAME = "initial-managed-device-identity"


DEVICE_IDENTITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS device_identity_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS device_identity_schema_migrations_no_update
BEFORE UPDATE ON device_identity_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'device identity schema history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS device_identity_schema_migrations_no_delete
BEFORE DELETE ON device_identity_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'device identity schema history is immutable');
END;

CREATE TABLE IF NOT EXISTS device_identity_account_revisions (
    account_id TEXT PRIMARY KEY,
    high_water_revision INTEGER NOT NULL CHECK(high_water_revision > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_identity_flows (
    flow_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    begin_request_hash TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    device_code_digest TEXT NOT NULL UNIQUE,
    user_code_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','authorized','denied','expired')),
    account_id TEXT,
    lease_revision INTEGER,
    poll_attempts INTEGER NOT NULL DEFAULT 0 CHECK(poll_attempts >= 0),
    last_polled_at TEXT,
    failed_verification_attempts INTEGER NOT NULL DEFAULT 0
        CHECK(failed_verification_attempts BETWEEN 0 AND 10),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    authorized_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK((status='authorized') = (account_id IS NOT NULL)),
    CHECK((status='authorized') = (lease_revision IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_device_identity_flows_due
    ON device_identity_flows(status, expires_at, created_at);
CREATE TRIGGER IF NOT EXISTS device_identity_flow_identity_immutable
BEFORE UPDATE OF flow_id,client_id,begin_request_hash,request_digest,
    device_code_digest,user_code_digest,created_at,expires_at
ON device_identity_flows BEGIN
    SELECT RAISE(ABORT, 'device identity flow identity is immutable');
END;

CREATE TABLE IF NOT EXISTS device_identity_grants (
    flow_id TEXT PRIMARY KEY REFERENCES device_identity_flows(flow_id),
    lease_id TEXT NOT NULL UNIQUE,
    lease_json TEXT NOT NULL,
    access_jti TEXT NOT NULL UNIQUE,
    refresh_jti TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    access_expires_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS device_identity_grants_no_update
BEFORE UPDATE ON device_identity_grants BEGIN
    SELECT RAISE(ABORT, 'device identity grants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS device_identity_grants_no_delete
BEFORE DELETE ON device_identity_grants BEGIN
    SELECT RAISE(ABORT, 'device identity grants are immutable');
END;

CREATE TABLE IF NOT EXISTS device_identity_refresh_grants (
    source_lease_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    lease_id TEXT NOT NULL UNIQUE,
    lease_json TEXT NOT NULL,
    access_jti TEXT NOT NULL UNIQUE,
    refresh_jti TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    access_expires_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS device_identity_refresh_grants_no_update
BEFORE UPDATE ON device_identity_refresh_grants BEGIN
    SELECT RAISE(ABORT, 'device identity refresh grants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS device_identity_refresh_grants_no_delete
BEFORE DELETE ON device_identity_refresh_grants BEGIN
    SELECT RAISE(ABORT, 'device identity refresh grants are immutable');
END;

CREATE TABLE IF NOT EXISTS device_identity_legacy_credentials (
    credential_digest TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_version TEXT NOT NULL CHECK(source_version='0.2.9.2'),
    state TEXT NOT NULL CHECK(state IN ('active','revoked')),
    imported_at TEXT NOT NULL,
    source_record_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_device_identity_legacy_account
    ON device_identity_legacy_credentials(account_id, state);
CREATE TRIGGER IF NOT EXISTS device_identity_legacy_credentials_no_update
BEFORE UPDATE ON device_identity_legacy_credentials BEGIN
    SELECT RAISE(ABORT, 'legacy credential mappings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS device_identity_legacy_credentials_no_delete
BEFORE DELETE ON device_identity_legacy_credentials BEGIN
    SELECT RAISE(ABORT, 'legacy credential mappings are immutable');
END;

CREATE TABLE IF NOT EXISTS device_identity_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    flow_hash TEXT,
    account_hash TEXT,
    details_json TEXT NOT NULL,
    previous_digest TEXT,
    entry_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS device_identity_audit_no_update
BEFORE UPDATE ON device_identity_audit BEGIN
    SELECT RAISE(ABORT, 'device identity audit is immutable');
END;
CREATE TRIGGER IF NOT EXISTS device_identity_audit_no_delete
BEFORE DELETE ON device_identity_audit BEGIN
    SELECT RAISE(ABORT, 'device identity audit is immutable');
END;
"""


DEVICE_IDENTITY_SCHEMA_SHA256 = hashlib.sha256(
    DEVICE_IDENTITY_SCHEMA_SQL.encode("utf-8")
).hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name LIKE 'device_identity_%' AND sql IS NOT NULL "
        "ORDER BY type,name"
    ).fetchall()
    payload = "\n".join("\x1f".join(str(value) for value in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_schema_fingerprint() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DEVICE_IDENTITY_SCHEMA_SQL)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


DEVICE_IDENTITY_OBJECTS_SHA256 = _expected_schema_fingerprint()


class DeviceIdentitySchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceIdentitySchemaReceipt:
    migration_version: int
    schema_sha256: str


class DeviceIdentitySchemaManager:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).resolve()

    def migrate(self) -> DeviceIdentitySchemaReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + DEVICE_IDENTITY_SCHEMA_SQL)
            row = connection.execute(
                "SELECT migration_name,migration_checksum "
                "FROM device_identity_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO device_identity_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at) "
                    "VALUES(1,?,?,?)",
                    (
                        DEVICE_IDENTITY_MIGRATION_NAME,
                        DEVICE_IDENTITY_SCHEMA_SHA256,
                        datetime.now(UTC).replace(microsecond=0).isoformat(),
                    ),
                )
            elif tuple(row) != (
                DEVICE_IDENTITY_MIGRATION_NAME,
                DEVICE_IDENTITY_SCHEMA_SHA256,
            ):
                raise DeviceIdentitySchemaError(
                    "device identity migration receipt is incompatible"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.validate()

    def validate(self) -> DeviceIdentitySchemaReceipt:
        if not self.path.is_file():
            raise DeviceIdentitySchemaError("device identity database is unavailable")
        connection = self._connect()
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DeviceIdentitySchemaError("device identity database is corrupt")
            row = connection.execute(
                "SELECT migration_name,migration_checksum "
                "FROM device_identity_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None or tuple(row) != (
                DEVICE_IDENTITY_MIGRATION_NAME,
                DEVICE_IDENTITY_SCHEMA_SHA256,
            ):
                raise DeviceIdentitySchemaError(
                    "device identity migration receipt is incompatible"
                )
            required = {
                "device_identity_account_revisions",
                "device_identity_flows",
                "device_identity_grants",
                "device_identity_refresh_grants",
                "device_identity_legacy_credentials",
                "device_identity_audit",
            }
            observed = {
                str(item[0])
                for item in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name LIKE 'device_identity_%'"
                ).fetchall()
            }
            if not required <= observed:
                raise DeviceIdentitySchemaError("device identity schema is incomplete")
            if _schema_fingerprint(connection) != DEVICE_IDENTITY_OBJECTS_SHA256:
                raise DeviceIdentitySchemaError("device identity schema drifted")
        except sqlite3.Error as error:
            raise DeviceIdentitySchemaError(
                "device identity schema validation failed"
            ) from error
        finally:
            connection.close()
        return DeviceIdentitySchemaReceipt(
            migration_version=CURRENT_DEVICE_IDENTITY_SCHEMA_VERSION,
            schema_sha256=DEVICE_IDENTITY_SCHEMA_SHA256,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


__all__ = [
    "CURRENT_DEVICE_IDENTITY_SCHEMA_VERSION",
    "DEVICE_IDENTITY_SCHEMA_SHA256",
    "DEVICE_IDENTITY_OBJECTS_SHA256",
    "DeviceIdentitySchemaError",
    "DeviceIdentitySchemaManager",
    "DeviceIdentitySchemaReceipt",
]

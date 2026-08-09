"""Additive SQLite authority for the managed Feishu connector gateway."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3


CURRENT_CONNECTOR_GATEWAY_SCHEMA_VERSION = 1
CONNECTOR_GATEWAY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS connector_gateway_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS connector_gateway_schema_migrations_no_update
BEFORE UPDATE ON connector_gateway_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'connector gateway schema history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS connector_gateway_schema_migrations_no_delete
BEFORE DELETE ON connector_gateway_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'connector gateway schema history is immutable');
END;

CREATE TABLE IF NOT EXISTS connector_gateway_flows (
    flow_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL CHECK(connector_id='feishu'),
    account_id TEXT NOT NULL,
    organization_id TEXT,
    return_uri TEXT NOT NULL,
    state_sha256 TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    challenge_json TEXT NOT NULL,
    result_envelope_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','consumed')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_connector_gateway_flows_expiry
ON connector_gateway_flows(status, expires_at);

CREATE TRIGGER IF NOT EXISTS connector_gateway_flows_identity_immutable
BEFORE UPDATE ON connector_gateway_flows
WHEN NEW.flow_id != OLD.flow_id
  OR NEW.connector_id != OLD.connector_id
  OR NEW.account_id != OLD.account_id
  OR NEW.organization_id IS NOT OLD.organization_id
  OR NEW.return_uri != OLD.return_uri
  OR NEW.state_sha256 != OLD.state_sha256
  OR NEW.code_challenge != OLD.code_challenge
  OR NEW.challenge_json != OLD.challenge_json
  OR NEW.expires_at != OLD.expires_at
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'connector gateway flow identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS connector_gateway_flows_result_transition
BEFORE UPDATE ON connector_gateway_flows
WHEN (OLD.status='consumed' AND (
        NEW.status!='consumed'
        OR NEW.result_envelope_json IS NOT OLD.result_envelope_json
        OR NEW.consumed_at IS NOT OLD.consumed_at
     ))
  OR (OLD.status='active' AND NEW.status='active' AND (
        NEW.result_envelope_json IS NOT NULL OR NEW.consumed_at IS NOT NULL
     ))
  OR (OLD.status='active' AND NEW.status='consumed' AND (
        NEW.result_envelope_json IS NULL OR NEW.consumed_at IS NULL
     ))
BEGIN
    SELECT RAISE(ABORT, 'connector gateway flow result transition is invalid');
END;

CREATE TABLE IF NOT EXISTS connector_gateway_grants (
    grant_sha256 TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL CHECK(connector_id='feishu'),
    account_id TEXT NOT NULL,
    organization_id TEXT,
    account_subject TEXT NOT NULL,
    account_display_name TEXT NOT NULL,
    granted_scopes_json TEXT NOT NULL,
    token_envelope_json TEXT NOT NULL,
    access_expires_at TEXT NOT NULL,
    refresh_expires_at TEXT,
    revision INTEGER NOT NULL CHECK(revision > 0),
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_connector_gateway_grants_account
ON connector_gateway_grants(account_id, connector_id, revoked);

CREATE TRIGGER IF NOT EXISTS connector_gateway_grants_identity_immutable
BEFORE UPDATE ON connector_gateway_grants
WHEN NEW.grant_sha256 != OLD.grant_sha256
  OR NEW.connector_id != OLD.connector_id
  OR NEW.account_id != OLD.account_id
  OR NEW.organization_id IS NOT OLD.organization_id
  OR NEW.account_subject != OLD.account_subject
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'connector gateway grant identity is immutable');
END;

CREATE TABLE IF NOT EXISTS connector_gateway_idempotency (
    account_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','completed','failed')),
    response_envelope_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, organization_id, idempotency_key)
);

CREATE TRIGGER IF NOT EXISTS connector_gateway_idempotency_identity_immutable
BEFORE UPDATE ON connector_gateway_idempotency
WHEN NEW.account_id != OLD.account_id
  OR NEW.organization_id != OLD.organization_id
  OR NEW.idempotency_key != OLD.idempotency_key
  OR NEW.operation != OLD.operation
  OR NEW.request_sha256 != OLD.request_sha256
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'connector gateway idempotency identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS connector_gateway_idempotency_result_transition
BEFORE UPDATE ON connector_gateway_idempotency
WHEN OLD.status='completed'
  OR (NEW.status='completed' AND NEW.response_envelope_json IS NULL)
  OR (NEW.status!='completed' AND NEW.response_envelope_json IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'connector gateway idempotency result transition is invalid');
END;
"""

CONNECTOR_GATEWAY_SCHEMA_SHA256 = hashlib.sha256(
    CONNECTOR_GATEWAY_SCHEMA_SQL.encode("utf-8")
).hexdigest()
MIGRATION_001_NAME = "managed-feishu-connector-gateway"
MIGRATION_001_CHECKSUM = hashlib.sha256(
    b"emate-connector-gateway-schema-v1\0"
    + CONNECTOR_GATEWAY_SCHEMA_SQL.encode("utf-8")
).hexdigest()


class ConnectorGatewaySchemaError(RuntimeError):
    pass


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
        raise ConnectorGatewaySchemaError(
            "connector gateway schema migration SQL is incomplete"
        )


@dataclass(frozen=True, slots=True)
class ConnectorGatewaySchemaReceipt:
    migration_version: int
    schema_sha256: str


class ConnectorGatewaySchemaManager:
    """Install once during the existing Control Plane migration, validate on serve."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(self) -> ConnectorGatewaySchemaReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            _execute_sql(connection, CONNECTOR_GATEWAY_SCHEMA_SQL)
            row = connection.execute(
                "SELECT migration_name,migration_checksum FROM "
                "connector_gateway_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO connector_gateway_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at) "
                    "VALUES(1,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (MIGRATION_001_NAME, MIGRATION_001_CHECKSUM),
                )
            elif tuple(row) != (MIGRATION_001_NAME, MIGRATION_001_CHECKSUM):
                raise ConnectorGatewaySchemaError(
                    "connector gateway schema history is incompatible"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.validate()

    def validate(self) -> ConnectorGatewaySchemaReceipt:
        if not self.path.is_file():
            raise ConnectorGatewaySchemaError(
                "connector gateway schema database is unavailable"
            )
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        try:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE "
                    "name LIKE 'connector_gateway_%' OR "
                    "name LIKE 'idx_connector_gateway_%'"
                )
            }
            required = {
                "connector_gateway_schema_migrations",
                "connector_gateway_schema_migrations_no_update",
                "connector_gateway_schema_migrations_no_delete",
                "connector_gateway_flows",
                "idx_connector_gateway_flows_expiry",
                "connector_gateway_flows_identity_immutable",
                "connector_gateway_flows_result_transition",
                "connector_gateway_grants",
                "idx_connector_gateway_grants_account",
                "connector_gateway_grants_identity_immutable",
                "connector_gateway_idempotency",
                "connector_gateway_idempotency_identity_immutable",
                "connector_gateway_idempotency_result_transition",
            }
            if names != required:
                raise ConnectorGatewaySchemaError(
                    "connector gateway schema objects are incompatible"
                )
            rows = connection.execute(
                "SELECT version,migration_name,migration_checksum FROM "
                "connector_gateway_schema_migrations ORDER BY version"
            ).fetchall()
            if rows != [(1, MIGRATION_001_NAME, MIGRATION_001_CHECKSUM)]:
                raise ConnectorGatewaySchemaError(
                    "connector gateway schema history is incomplete"
                )
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ConnectorGatewaySchemaError(
                    "connector gateway schema integrity is unavailable"
                )
        except sqlite3.DatabaseError:
            raise ConnectorGatewaySchemaError(
                "connector gateway schema validation failed"
            ) from None
        finally:
            connection.close()
        return ConnectorGatewaySchemaReceipt(
            migration_version=CURRENT_CONNECTOR_GATEWAY_SCHEMA_VERSION,
            schema_sha256=CONNECTOR_GATEWAY_SCHEMA_SHA256,
        )


__all__ = [
    "CONNECTOR_GATEWAY_SCHEMA_SHA256",
    "CURRENT_CONNECTOR_GATEWAY_SCHEMA_VERSION",
    "ConnectorGatewaySchemaError",
    "ConnectorGatewaySchemaManager",
    "ConnectorGatewaySchemaReceipt",
]

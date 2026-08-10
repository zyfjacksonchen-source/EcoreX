"""Additive SQLite authority for managed WeChat callback channels."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3


CURRENT_WECHAT_CALLBACK_SCHEMA_VERSION = 2
WECHAT_CALLBACK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wechat_callback_schema_migrations(
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wechat_callback_bindings(
    binding_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL CHECK(channel_id IN (
        'wechatcom_app','wechat_kf','wechatmp','wechatmp_service'
    )),
    account_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    app_id_sha256 TEXT NOT NULL,
    credential_envelope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('enabled','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, organization_id, channel_id, app_id_sha256)
);
CREATE INDEX IF NOT EXISTS idx_wechat_callback_bindings_owner
ON wechat_callback_bindings(account_id, organization_id, status, channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wechat_callback_mp_mode
ON wechat_callback_bindings(app_id_sha256)
WHERE status='enabled' AND channel_id IN ('wechatmp','wechatmp_service');

CREATE TRIGGER IF NOT EXISTS wechat_callback_bindings_identity_immutable
BEFORE UPDATE ON wechat_callback_bindings
WHEN NEW.binding_id != OLD.binding_id
  OR NEW.channel_id != OLD.channel_id
  OR NEW.account_id != OLD.account_id
  OR NEW.organization_id != OLD.organization_id
  OR NEW.app_id_sha256 != OLD.app_id_sha256
  OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'wechat callback binding identity is immutable');
END;

CREATE TABLE IF NOT EXISTS wechat_callback_inbox(
    event_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES wechat_callback_bindings(binding_id),
    provider_message_sha256 TEXT NOT NULL,
    payload_envelope_json TEXT,
    reply_envelope_json TEXT,
    state TEXT NOT NULL CHECK(state IN ('ready','leased','acknowledged')),
    lease_id TEXT,
    lease_expires_at TEXT,
    passive_deadline_at TEXT,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE(binding_id, provider_message_sha256),
    CHECK((state='leased') = (lease_id IS NOT NULL)),
    CHECK((state='leased') = (lease_expires_at IS NOT NULL)),
    CHECK((state='acknowledged') = (payload_envelope_json IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_wechat_callback_inbox_pull
ON wechat_callback_inbox(binding_id, state, created_at, event_id);

CREATE TABLE IF NOT EXISTS wechat_callback_deliveries(
    binding_id TEXT NOT NULL REFERENCES wechat_callback_bindings(binding_id),
    idempotency_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    payload_envelope_json TEXT,
    state TEXT NOT NULL CHECK(state IN ('active','ready','sent','failed','uncertain')),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(binding_id, idempotency_key),
    CHECK((state IN ('active','ready')) = (payload_envelope_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_wechat_callback_deliveries_event
ON wechat_callback_deliveries(binding_id, event_id, state);

CREATE TABLE IF NOT EXISTS wechat_callback_kf_state(
    binding_id TEXT PRIMARY KEY REFERENCES wechat_callback_bindings(binding_id),
    cursor_envelope_json TEXT,
    sync_token_envelope_json TEXT,
    dirty INTEGER NOT NULL DEFAULT 0 CHECK(dirty IN (0,1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wechat_callback_audit_outbox(
    audit_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wechat_callback_audit_pending
ON wechat_callback_audit_outbox(delivered_at, created_at, audit_id);
"""

WECHAT_CALLBACK_SCHEMA_V2_SQL = """
ALTER TABLE wechat_callback_inbox
ADD COLUMN conversation_sha256 TEXT;
ALTER TABLE wechat_callback_inbox
ADD COLUMN passive_attempts INTEGER NOT NULL DEFAULT 0
CHECK(passive_attempts BETWEEN 0 AND 3);
ALTER TABLE wechat_callback_inbox
ADD COLUMN passive_hard_deadline_at TEXT;
ALTER TABLE wechat_callback_inbox
ADD COLUMN passive_hint_sent INTEGER NOT NULL DEFAULT 0
CHECK(passive_hint_sent IN (0,1));
ALTER TABLE wechat_callback_inbox
ADD COLUMN passive_original_replied INTEGER NOT NULL DEFAULT 0
CHECK(passive_original_replied IN (0,1));
"""

WECHAT_CALLBACK_SCHEMA_SHA256 = hashlib.sha256(
    (WECHAT_CALLBACK_SCHEMA_SQL + WECHAT_CALLBACK_SCHEMA_V2_SQL).encode("utf-8")
).hexdigest()
_MIGRATION_NAME = "managed-wechat-callback-ingress"
_MIGRATION_CHECKSUM = hashlib.sha256(
    b"emate-wechat-callback-schema-v1\0"
    + WECHAT_CALLBACK_SCHEMA_SQL.encode("utf-8")
).hexdigest()
_MIGRATION_V2_NAME = "managed-wechat-passive-replies"
_MIGRATION_V2_CHECKSUM = hashlib.sha256(
    b"emate-wechat-callback-schema-v2\0"
    + WECHAT_CALLBACK_SCHEMA_V2_SQL.encode("utf-8")
).hexdigest()


class WechatCallbackSchemaError(RuntimeError):
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
        raise WechatCallbackSchemaError("wechat callback schema SQL is incomplete")


@dataclass(frozen=True, slots=True)
class WechatCallbackSchemaReceipt:
    migration_version: int
    schema_sha256: str


class WechatCallbackSchemaManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(self) -> WechatCallbackSchemaReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            _execute_sql(connection, WECHAT_CALLBACK_SCHEMA_SQL)
            row = connection.execute(
                "SELECT migration_name,migration_checksum FROM "
                "wechat_callback_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO wechat_callback_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at) "
                    "VALUES(1,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (_MIGRATION_NAME, _MIGRATION_CHECKSUM),
                )
            elif tuple(row) != (_MIGRATION_NAME, _MIGRATION_CHECKSUM):
                raise WechatCallbackSchemaError(
                    "wechat callback schema history is incompatible"
                )
            row = connection.execute(
                "SELECT migration_name,migration_checksum FROM "
                "wechat_callback_schema_migrations WHERE version=2"
            ).fetchone()
            if row is None:
                _execute_sql(connection, WECHAT_CALLBACK_SCHEMA_V2_SQL)
                connection.execute(
                    "INSERT INTO wechat_callback_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at) "
                    "VALUES(2,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (_MIGRATION_V2_NAME, _MIGRATION_V2_CHECKSUM),
                )
            elif tuple(row) != (_MIGRATION_V2_NAME, _MIGRATION_V2_CHECKSUM):
                raise WechatCallbackSchemaError(
                    "wechat callback schema history is incompatible"
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return self.validate()

    def validate(self) -> WechatCallbackSchemaReceipt:
        if not self.path.is_file():
            raise WechatCallbackSchemaError(
                "wechat callback schema database is unavailable"
            )
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        try:
            required = {
                "wechat_callback_schema_migrations",
                "wechat_callback_bindings",
                "idx_wechat_callback_bindings_owner",
                "idx_wechat_callback_mp_mode",
                "wechat_callback_bindings_identity_immutable",
                "wechat_callback_inbox",
                "idx_wechat_callback_inbox_pull",
                "wechat_callback_deliveries",
                "idx_wechat_callback_deliveries_event",
                "wechat_callback_kf_state",
                "wechat_callback_audit_outbox",
                "idx_wechat_callback_audit_pending",
            }
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE "
                    "name LIKE 'wechat_callback_%' OR "
                    "name LIKE 'idx_wechat_callback_%'"
                )
            }
            if names != required:
                raise WechatCallbackSchemaError(
                    "wechat callback schema objects are incompatible"
                )
            row = connection.execute(
                "SELECT migration_name,migration_checksum FROM "
                "wechat_callback_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None or tuple(row) != (_MIGRATION_NAME, _MIGRATION_CHECKSUM):
                raise WechatCallbackSchemaError(
                    "wechat callback schema history is incompatible"
                )
            row = connection.execute(
                "SELECT migration_name,migration_checksum FROM "
                "wechat_callback_schema_migrations WHERE version=2"
            ).fetchone()
            if row is None or tuple(row) != (
                _MIGRATION_V2_NAME,
                _MIGRATION_V2_CHECKSUM,
            ):
                raise WechatCallbackSchemaError(
                    "wechat callback schema history is incompatible"
                )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(wechat_callback_inbox)"
                )
            }
            if not {
                "conversation_sha256",
                "passive_attempts",
                "passive_hard_deadline_at",
                "passive_hint_sent",
                "passive_original_replied",
            }.issubset(columns):
                raise WechatCallbackSchemaError(
                    "wechat callback schema columns are incompatible"
                )
        finally:
            connection.close()
        return WechatCallbackSchemaReceipt(
            migration_version=CURRENT_WECHAT_CALLBACK_SCHEMA_VERSION,
            schema_sha256=WECHAT_CALLBACK_SCHEMA_SHA256,
        )


__all__ = [
    "CURRENT_WECHAT_CALLBACK_SCHEMA_VERSION",
    "WECHAT_CALLBACK_SCHEMA_SHA256",
    "WechatCallbackSchemaError",
    "WechatCallbackSchemaManager",
    "WechatCallbackSchemaReceipt",
]

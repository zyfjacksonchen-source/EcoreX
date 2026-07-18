"""Explicit schema authority for users, quotas and managed model configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3


CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION = 3
ADMIN_MANAGEMENT_MIGRATION_NAME = "provider-usage-facts"

_INITIAL_MIGRATION_NAME = "initial-admin-management"
_INITIAL_MIGRATION_CHECKSUM = (
    "ceeb871fe920bc47afe58032a461b464220f707a56633deed1ce8b4e45afc72d"
)
_MIGRATION_V2_NAME = "managed-model-origin-presets"


ADMIN_MANAGEMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_ops_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS admin_ops_schema_migrations_no_update
BEFORE UPDATE ON admin_ops_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'admin management schema history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS admin_ops_schema_migrations_no_delete
BEFORE DELETE ON admin_ops_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'admin management schema history is immutable');
END;

CREATE TABLE IF NOT EXISTS admin_ops_users (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT,
    organization_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','suspended')),
    token_limit INTEGER NOT NULL CHECK(token_limit >= 0),
    tokens_used INTEGER NOT NULL CHECK(tokens_used >= 0),
    image_limit INTEGER NOT NULL CHECK(image_limit >= 0),
    images_used INTEGER NOT NULL CHECK(images_used >= 0),
    revision INTEGER NOT NULL CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_ops_users_filter
    ON admin_ops_users(status, organization_id, updated_at DESC, account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_ops_users_email
    ON admin_ops_users(email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS admin_ops_usage_ledger (
    adjustment_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES admin_ops_users(account_id),
    token_delta INTEGER NOT NULL,
    image_delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    resulting_user_revision INTEGER NOT NULL CHECK(resulting_user_revision > 0),
    created_at TEXT NOT NULL,
    CHECK(token_delta <> 0 OR image_delta <> 0)
);
CREATE TRIGGER IF NOT EXISTS admin_ops_usage_ledger_no_update
BEFORE UPDATE ON admin_ops_usage_ledger BEGIN
    SELECT RAISE(ABORT, 'usage ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS admin_ops_usage_ledger_no_delete
BEFORE DELETE ON admin_ops_usage_ledger BEGIN
    SELECT RAISE(ABORT, 'usage ledger is immutable');
END;
CREATE INDEX IF NOT EXISTS idx_admin_ops_usage_account
    ON admin_ops_usage_ledger(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS admin_ops_model_configs (
    config_id TEXT PRIMARY KEY,
    local_model_id TEXT NOT NULL UNIQUE,
    modality TEXT NOT NULL CHECK(modality IN ('chat','image_generation','image_edit')),
    active_revision INTEGER,
    draft_revision INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(active_revision IS NULL OR active_revision > 0),
    CHECK(draft_revision IS NULL OR draft_revision > 0)
);
CREATE INDEX IF NOT EXISTS idx_admin_ops_model_configs_modality
    ON admin_ops_model_configs(modality, local_model_id);

CREATE TABLE IF NOT EXISTS admin_ops_model_revisions (
    config_id TEXT NOT NULL REFERENCES admin_ops_model_configs(config_id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    display_name TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    provider_preset TEXT NOT NULL CHECK(provider_preset IN (
        'responses','openai_compatible_chat','openai_compatible_image'
    )),
    is_default INTEGER NOT NULL CHECK(is_default IN (0,1)),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    status TEXT NOT NULL CHECK(status IN (
        'draft','testing','active','rejected','superseded'
    )),
    secret_id TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    test_id TEXT,
    test_status TEXT NOT NULL CHECK(test_status IN (
        'not_tested','running','passed','failed'
    )),
    test_error_code TEXT,
    tested_at TEXT,
    actor_subject TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(config_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_admin_ops_model_revisions_status
    ON admin_ops_model_revisions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS admin_ops_model_defaults (
    modality TEXT PRIMARY KEY CHECK(modality IN ('chat','image_generation','image_edit')),
    config_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(config_id, revision)
        REFERENCES admin_ops_model_revisions(config_id, revision)
);

CREATE TABLE IF NOT EXISTS admin_ops_secrets (
    secret_id TEXT PRIMARY KEY,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS admin_ops_secrets_no_update
BEFORE UPDATE ON admin_ops_secrets BEGIN
    SELECT RAISE(ABORT, 'managed model secrets are immutable');
END;

CREATE TABLE IF NOT EXISTS admin_ops_model_tests (
    test_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','passed','failed','superseded')),
    error_code TEXT,
    actor_subject TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(config_id, revision)
        REFERENCES admin_ops_model_revisions(config_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_ops_model_tests_request
    ON admin_ops_model_tests(actor_subject, client_request_id);
CREATE INDEX IF NOT EXISTS idx_admin_ops_model_tests_config
    ON admin_ops_model_tests(config_id, started_at DESC);

CREATE TABLE IF NOT EXISTS admin_ops_idempotency (
    actor_subject TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_subject, client_request_id)
);
CREATE TRIGGER IF NOT EXISTS admin_ops_idempotency_no_update
BEFORE UPDATE ON admin_ops_idempotency BEGIN
    SELECT RAISE(ABORT, 'admin management idempotency is immutable');
END;
CREATE TRIGGER IF NOT EXISTS admin_ops_idempotency_no_delete
BEFORE DELETE ON admin_ops_idempotency BEGIN
    SELECT RAISE(ABORT, 'admin management idempotency is immutable');
END;

CREATE TABLE IF NOT EXISTS admin_ops_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS admin_ops_audit_no_update
BEFORE UPDATE ON admin_ops_audit BEGIN
    SELECT RAISE(ABORT, 'admin management audit is immutable');
END;
CREATE TRIGGER IF NOT EXISTS admin_ops_audit_no_delete
BEFORE DELETE ON admin_ops_audit BEGIN
    SELECT RAISE(ABORT, 'admin management audit is immutable');
END;
"""


_MIGRATION_V2_CHECKSUM = hashlib.sha256(
    b"ecorex-admin-management-schema-v2\0managed-model-origin-presets"
).hexdigest()

ADMIN_MANAGEMENT_SCHEMA_V2_SQL = """
ALTER TABLE admin_ops_model_revisions
ADD COLUMN provider_origin_preset TEXT NOT NULL DEFAULT 'ecorex_chat'
CHECK(provider_origin_preset IN (
    'ecorex_chat','deepseek_chat','gemini_chat','doubao_chat','ecorex_image'
));
UPDATE admin_ops_model_revisions
SET provider_origin_preset = CASE (
    SELECT local_model_id FROM admin_ops_model_configs configs
    WHERE configs.config_id=admin_ops_model_revisions.config_id
)
    WHEN 'ecorex-chat' THEN 'ecorex_chat'
    WHEN 'ecorex-deepseek-v4-pro' THEN 'deepseek_chat'
    WHEN 'ecorex-gemini-3.1-pro' THEN 'gemini_chat'
    WHEN 'ecorex-doubao-seed-2.0-pro' THEN 'doubao_chat'
    ELSE 'ecorex_image'
END;
"""

ADMIN_MANAGEMENT_SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS admin_ops_provider_usage_facts (
    fact_id TEXT PRIMARY KEY,
    source_service TEXT NOT NULL CHECK(source_service IN (
        'legacy_baseline','managed_gateway','image_service'
    )),
    source_id TEXT NOT NULL,
    usage_kind TEXT NOT NULL CHECK(usage_kind IN ('baseline','chat','image')),
    account_id TEXT NOT NULL REFERENCES admin_ops_users(account_id),
    input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK(
        total_tokens >= 0 AND total_tokens >= input_tokens + output_tokens
    ),
    image_count INTEGER NOT NULL CHECK(image_count >= 0),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    provider_created_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK(total_tokens > 0 OR image_count > 0),
    CHECK(
        (usage_kind='chat' AND total_tokens > 0 AND image_count=0)
        OR (usage_kind='image' AND total_tokens=0 AND image_count > 0)
        OR usage_kind='baseline'
    ),
    UNIQUE(source_service, source_id, usage_kind)
);
CREATE INDEX IF NOT EXISTS idx_admin_ops_provider_usage_account
    ON admin_ops_provider_usage_facts(account_id, provider_created_at, fact_id);
CREATE TRIGGER IF NOT EXISTS admin_ops_provider_usage_facts_no_update
BEFORE UPDATE ON admin_ops_provider_usage_facts BEGIN
    SELECT RAISE(ABORT, 'provider usage facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS admin_ops_provider_usage_facts_no_delete
BEFORE DELETE ON admin_ops_provider_usage_facts BEGIN
    SELECT RAISE(ABORT, 'provider usage facts are immutable');
END;
"""

ADMIN_MANAGEMENT_MIGRATION_CHECKSUM = hashlib.sha256(
    b"ecorex-admin-management-schema-v3\0provider-usage-facts"
).hexdigest()


class AdminManagementSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdminManagementSchemaReceipt:
    migration_version: int
    migration_name: str
    migration_checksum: str
    installed_at: str


def _managed_shape(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name LIKE 'admin_ops_%' AND sql IS NOT NULL "
        "ORDER BY type,name"
    ).fetchall()
    payload = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expected_shape() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(ADMIN_MANAGEMENT_SCHEMA_SQL)
        connection.executescript(ADMIN_MANAGEMENT_SCHEMA_V2_SQL)
        connection.executescript(ADMIN_MANAGEMENT_SCHEMA_V3_SQL)
        return _managed_shape(connection)
    finally:
        connection.close()


ADMIN_MANAGEMENT_SCHEMA_SHA256 = _expected_shape()


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
        raise AdminManagementSchemaError("admin management schema SQL is incomplete")


class AdminManagementSchemaManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(self) -> AdminManagementSchemaReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN EXCLUSIVE")
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name LIKE 'admin_ops_%'"
                )
            }
            rows: list[sqlite3.Row] | list[tuple[object, ...]]
            if names:
                rows = connection.execute(
                    "SELECT version,migration_name,migration_checksum "
                    "FROM admin_ops_schema_migrations ORDER BY version"
                ).fetchall()
                if len(rows) == CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION:
                    receipt = self._validate_connection(connection)
                    connection.commit()
                    return receipt
                if not 1 <= len(rows) < CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION:
                    raise AdminManagementSchemaError(
                        "admin management schema history is invalid"
                    )
                expected_prefix = (
                    (
                        1,
                        _INITIAL_MIGRATION_NAME,
                        _INITIAL_MIGRATION_CHECKSUM,
                    ),
                    (2, _MIGRATION_V2_NAME, _MIGRATION_V2_CHECKSUM),
                )
                if any(
                    tuple(row) != expected_prefix[index]
                    for index, row in enumerate(rows)
                ):
                    raise AdminManagementSchemaError(
                        "admin management schema history is invalid"
                    )
            else:
                _execute_sql(connection, ADMIN_MANAGEMENT_SCHEMA_SQL)
                initial_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO admin_ops_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at"
                    ") VALUES(1,?,?,?)",
                    (_INITIAL_MIGRATION_NAME, _INITIAL_MIGRATION_CHECKSUM, initial_at),
                )
            rows = connection.execute(
                "SELECT version,migration_name,migration_checksum "
                "FROM admin_ops_schema_migrations ORDER BY version"
            ).fetchall()
            if len(rows) == 1:
                unknown_slots = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM admin_ops_model_configs WHERE local_model_id "
                        "NOT IN ('ecorex-chat','ecorex-deepseek-v4-pro',"
                        "'ecorex-gemini-3.1-pro','ecorex-doubao-seed-2.0-pro',"
                        "'gpt-image-2','gpt-image-2-edit')"
                    ).fetchone()[0]
                )
                if unknown_slots:
                    raise AdminManagementSchemaError(
                        "admin management contains an unknown managed model slot"
                    )
                _execute_sql(connection, ADMIN_MANAGEMENT_SCHEMA_V2_SQL)
                installed_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO admin_ops_schema_migrations("
                    "version,migration_name,migration_checksum,installed_at"
                    ") VALUES(2,?,?,?)",
                    (_MIGRATION_V2_NAME, _MIGRATION_V2_CHECKSUM, installed_at),
                )
            _execute_sql(connection, ADMIN_MANAGEMENT_SCHEMA_V3_SQL)
            installed_at = datetime.now(UTC).isoformat()
            for row in connection.execute(
                "SELECT account_id,tokens_used,images_used,updated_at "
                "FROM admin_ops_users WHERE tokens_used > 0 OR images_used > 0 "
                "ORDER BY account_id"
            ).fetchall():
                account_id = str(row[0])
                input_tokens = 0
                output_tokens = 0
                total_tokens = int(row[1])
                image_count = int(row[2])
                material = {
                    "source_service": "legacy_baseline",
                    "source_id": account_id,
                    "usage_kind": "baseline",
                    "account_id": account_id,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "image_count": image_count,
                    "provider_created_at": str(row[3]),
                }
                payload = json.dumps(
                    material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                fact_id = "usagefact_" + hashlib.sha256(
                    b"ecorex-provider-usage-fact-v1\0"
                    + b"legacy_baseline\0"
                    + account_id.encode("utf-8")
                    + b"\0baseline"
                ).hexdigest()
                connection.execute(
                    "INSERT INTO admin_ops_provider_usage_facts("
                    "fact_id,source_service,source_id,usage_kind,account_id,"
                    "input_tokens,output_tokens,total_tokens,image_count,payload_sha256,"
                    "provider_created_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fact_id,
                        "legacy_baseline",
                        account_id,
                        "baseline",
                        account_id,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        image_count,
                        hashlib.sha256(payload).hexdigest(),
                        str(row[3]),
                        installed_at,
                    ),
                )
            connection.execute(
                "INSERT INTO admin_ops_schema_migrations("
                "version,migration_name,migration_checksum,installed_at"
                ") VALUES(?,?,?,?)",
                (
                    CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION,
                    ADMIN_MANAGEMENT_MIGRATION_NAME,
                    ADMIN_MANAGEMENT_MIGRATION_CHECKSUM,
                    installed_at,
                ),
            )
            if _managed_shape(connection) != ADMIN_MANAGEMENT_SCHEMA_SHA256:
                raise AdminManagementSchemaError(
                    "admin management schema migration target drifted"
                )
            connection.commit()
            return AdminManagementSchemaReceipt(
                migration_version=CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION,
                migration_name=ADMIN_MANAGEMENT_MIGRATION_NAME,
                migration_checksum=ADMIN_MANAGEMENT_MIGRATION_CHECKSUM,
                installed_at=installed_at,
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def validate(self) -> AdminManagementSchemaReceipt:
        if not self.path.is_file():
            raise AdminManagementSchemaError("admin management schema is missing")
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=30
        )
        try:
            return self._validate_connection(connection)
        finally:
            connection.close()

    @staticmethod
    def _validate_connection(
        connection: sqlite3.Connection,
    ) -> AdminManagementSchemaReceipt:
        if _managed_shape(connection) != ADMIN_MANAGEMENT_SCHEMA_SHA256:
            raise AdminManagementSchemaError("admin management schema drifted")
        row = connection.execute(
            "SELECT version,migration_name,migration_checksum,installed_at "
            "FROM admin_ops_schema_migrations ORDER BY version"
        ).fetchall()
        if len(row) != CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION:
            raise AdminManagementSchemaError(
                "admin management schema history is invalid"
            )
        initial, migration_v2, value = row
        if (
            int(initial[0]) != 1
            or str(initial[1]) != _INITIAL_MIGRATION_NAME
            or str(initial[2]) != _INITIAL_MIGRATION_CHECKSUM
            or int(migration_v2[0]) != 2
            or str(migration_v2[1]) != _MIGRATION_V2_NAME
            or str(migration_v2[2]) != _MIGRATION_V2_CHECKSUM
            or
            int(value[0]) != CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION
            or str(value[1]) != ADMIN_MANAGEMENT_MIGRATION_NAME
            or str(value[2]) != ADMIN_MANAGEMENT_MIGRATION_CHECKSUM
        ):
            raise AdminManagementSchemaError(
                "admin management schema receipt is invalid"
            )
        return AdminManagementSchemaReceipt(
            migration_version=int(value[0]),
            migration_name=str(value[1]),
            migration_checksum=str(value[2]),
            installed_at=str(value[3]),
        )


__all__ = [
    "ADMIN_MANAGEMENT_MIGRATION_CHECKSUM",
    "ADMIN_MANAGEMENT_MIGRATION_NAME",
    "ADMIN_MANAGEMENT_SCHEMA_SHA256",
    "ADMIN_MANAGEMENT_SCHEMA_V2_SQL",
    "ADMIN_MANAGEMENT_SCHEMA_V3_SQL",
    "AdminManagementSchemaError",
    "AdminManagementSchemaManager",
    "AdminManagementSchemaReceipt",
    "CURRENT_ADMIN_MANAGEMENT_SCHEMA_VERSION",
]

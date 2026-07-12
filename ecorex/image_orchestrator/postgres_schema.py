"""Explicit physical PostgreSQL schema authority for image orchestration.

API and worker processes only run the read-only validator.  The deployment
manager accepts an empty managed namespace, the one frozen pre-authority
schema, or the exact current schema; every other shape fails before DDL.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
import re
from typing import Any


ConnectionFactory = Callable[[], Any]
CURRENT_IMAGE_SCHEMA_VERSION = 1
POSTGRES_IMAGE_SCHEMA_RECEIPT_VERSION = 1
_MIGRATION_LOCK_ID = 0x45434F524558494D  # "ECOREXIM", signed int64-safe.
_HEX_DIGEST = frozenset("0123456789abcdef")


# Frozen schema emitted by the pre-authority Store.  It is never executed by
# current Runtime code; its object fingerprint and exact history row are the
# sole legacy source accepted by the deployment migrator.
PRE_AUTHORITY_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_scheduler_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    touched_at TIMESTAMPTZ NOT NULL
);
INSERT INTO image_scheduler_control(singleton,touched_at)
VALUES(TRUE,now()) ON CONFLICT(singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS image_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('generate','retouch')),
    model_id TEXT NOT NULL,
    size_class TEXT NOT NULL,
    weight INTEGER NOT NULL CHECK(weight > 0),
    priority INTEGER NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_json JSONB NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    fair_finish DOUBLE PRECISION NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_generation BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    provider_idempotency_key TEXT NOT NULL UNIQUE,
    provider_request_id TEXT,
    checkpoint_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(account_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS image_jobs_schedulable
ON image_jobs(status,available_at,fair_finish,priority DESC,created_at);
CREATE INDEX IF NOT EXISTS image_jobs_account_status
ON image_jobs(account_id,status,created_at);
CREATE INDEX IF NOT EXISTS image_jobs_model_status
ON image_jobs(model_id,status);

CREATE TABLE IF NOT EXISTS image_scheduler_accounts (
    account_id TEXT PRIMARY KEY,
    last_finish DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS image_inputs (
    account_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
    mime_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(account_id,sha256)
);
CREATE TABLE IF NOT EXISTS image_results (
    job_id TEXT PRIMARY KEY REFERENCES image_jobs(job_id),
    sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime_type TEXT NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS image_usage (
    job_id TEXT PRIMARY KEY REFERENCES image_jobs(job_id),
    usage_json JSONB NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS image_events (
    seq BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES image_jobs(job_id),
    account_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS image_events_job_seq ON image_events(job_id,seq);
CREATE TABLE IF NOT EXISTS image_breakers (
    scope TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL,
    open_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS image_recovery_requests (
    account_id TEXT NOT NULL,
    recovery_request_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES image_jobs(job_id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(account_id,recovery_request_id)
);

CREATE OR REPLACE FUNCTION ecorex_image_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'image ledger rows are immutable';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS image_results_immutable ON image_results;
CREATE TRIGGER image_results_immutable BEFORE UPDATE OR DELETE ON image_results
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
DROP TRIGGER IF EXISTS image_inputs_immutable ON image_inputs;
CREATE TRIGGER image_inputs_immutable BEFORE UPDATE OR DELETE ON image_inputs
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
DROP TRIGGER IF EXISTS image_usage_immutable ON image_usage;
CREATE TRIGGER image_usage_immutable BEFORE UPDATE OR DELETE ON image_usage
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
DROP TRIGGER IF EXISTS image_events_immutable ON image_events;
CREATE TRIGGER image_events_immutable BEFORE UPDATE OR DELETE ON image_events
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
DROP TRIGGER IF EXISTS image_recovery_immutable ON image_recovery_requests;
CREATE TRIGGER image_recovery_immutable BEFORE UPDATE OR DELETE ON image_recovery_requests
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
"""

PRE_AUTHORITY_SCHEMA_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS ecorex_image_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK(checksum ~ '^[0-9a-f]{64}$'),
    installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Fresh creation is permitted only after an empty catalog fingerprint, so the
# current migration deliberately uses non-repairing CREATE statements.
MIGRATION_001_SQL = PRE_AUTHORITY_CORE_SCHEMA_SQL.replace(
    "CREATE TABLE IF NOT EXISTS", "CREATE TABLE"
).replace("CREATE INDEX IF NOT EXISTS", "CREATE INDEX")

SCHEMA_HISTORY_SQL = """
CREATE TABLE ecorex_image_schema_migrations (
    version INTEGER NOT NULL,
    migration_name TEXT NOT NULL,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ecorex_image_schema_migrations_pkey PRIMARY KEY(version),
    CONSTRAINT ecorex_image_schema_migrations_migration_name_key UNIQUE(migration_name),
    CONSTRAINT ecorex_image_schema_migrations_version_check CHECK(version > 0),
    CONSTRAINT ecorex_image_schema_migrations_migration_checksum_check
        CHECK(migration_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ecorex_image_schema_migrations_source_schema_sha256_check
        CHECK(source_schema_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ecorex_image_schema_migrations_target_schema_sha256_check
        CHECK(target_schema_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ecorex_image_schema_migrations_receipt_sha256_check
        CHECK(receipt_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TRIGGER ecorex_image_schema_migrations_immutable
BEFORE UPDATE OR DELETE ON ecorex_image_schema_migrations
FOR EACH ROW EXECUTE FUNCTION ecorex_image_immutable();
"""

PRE_AUTHORITY_ADOPTION_SQL = """
DROP TABLE ecorex_image_schema_migrations;
""" + SCHEMA_HISTORY_SQL

SCHEMA_PROBE_SQL = """
SELECT singleton,touched_at FROM image_scheduler_control WHERE FALSE;
SELECT job_id,account_id,operation,model_id,size_class,weight,priority,
       client_request_id,request_fingerprint,request_json,status,attempt,
       max_attempts,fair_finish,available_at,deadline,lease_owner,lease_token,
       lease_generation,lease_expires_at,heartbeat_at,provider_idempotency_key,
       provider_request_id,checkpoint_json,cancellation_requested,
       last_error_code,created_at,updated_at
FROM image_jobs WHERE FALSE;
SELECT account_id,last_finish FROM image_scheduler_accounts WHERE FALSE;
SELECT account_id,sha256,size_bytes,mime_type,created_at FROM image_inputs WHERE FALSE;
SELECT job_id,sha256,size_bytes,mime_type,committed_at FROM image_results WHERE FALSE;
SELECT job_id,usage_json,committed_at FROM image_usage WHERE FALSE;
SELECT seq,event_id,job_id,account_id,event_type,payload_json,created_at
FROM image_events WHERE FALSE;
SELECT scope,failure_count,open_until,updated_at FROM image_breakers WHERE FALSE;
SELECT account_id,recovery_request_id,job_id,created_at
FROM image_recovery_requests WHERE FALSE;
"""


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


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return " ".join(str(value).split())


def _body_digest(value: str) -> str:
    return _digest(_normalize(value).encode("utf-8"))


def _is_digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGEST


@dataclass(frozen=True, slots=True)
class ImageSchemaMigration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return _digest(
            b"ecorex-postgres-image-schema-migration\0"
            + str(self.version).encode("ascii")
            + b"\0"
            + self.name.encode("utf-8")
            + b"\0"
            + self.sql.encode("utf-8")
            + b"\0adopt\0"
            + PRE_AUTHORITY_ADOPTION_SQL.encode("utf-8")
        )


IMAGE_SCHEMA_MIGRATIONS = (
    ImageSchemaMigration(
        1,
        "initial-authoritative-image-orchestration",
        MIGRATION_001_SQL + SCHEMA_HISTORY_SQL,
    ),
)
MIGRATION_001_NAME = IMAGE_SCHEMA_MIGRATIONS[0].name
MIGRATION_001_CHECKSUM = IMAGE_SCHEMA_MIGRATIONS[0].checksum
PRE_AUTHORITY_MIGRATION_NAME = "initial-image-orchestration"
PRE_AUTHORITY_MIGRATION_CHECKSUM = _digest(
    PRE_AUTHORITY_CORE_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class PostgresImageSchemaCatalog:
    tables: tuple[tuple[str, ...], ...] = ()
    columns: tuple[tuple[str, ...], ...] = ()
    constraints: tuple[tuple[str, ...], ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()
    triggers: tuple[tuple[str, ...], ...] = ()
    functions: tuple[tuple[str, ...], ...] = ()
    sequences: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, tuple[tuple[str, ...], ...]]:
        return {
            "tables": self.tables,
            "columns": self.columns,
            "constraints": self.constraints,
            "indexes": self.indexes,
            "triggers": self.triggers,
            "functions": self.functions,
            "sequences": self.sequences,
        }

    @property
    def sha256(self) -> str:
        return _digest(_canonical(self.to_dict()))


_TABLE_NAMES = (
    "ecorex_image_schema_migrations",
    "image_breakers",
    "image_events",
    "image_inputs",
    "image_jobs",
    "image_recovery_requests",
    "image_results",
    "image_scheduler_accounts",
    "image_scheduler_control",
    "image_usage",
)
_TABLES = tuple(
    (name, "r", "p", "d", "false", "false", "false") for name in _TABLE_NAMES
)


def _column_records(
    table: str,
    definitions: Sequence[tuple[str, str, bool, str, str, str, str]],
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            table,
            str(ordinal),
            name,
            data_type,
            "true" if not_null else "false",
            default,
            identity,
            generated,
            collation,
        )
        for ordinal, (
            name,
            data_type,
            not_null,
            default,
            identity,
            generated,
            collation,
        ) in enumerate(definitions, 1)
    )


_TEXT = "pg_catalog.default"
_CURRENT_HISTORY_COLUMNS = _column_records(
    "ecorex_image_schema_migrations",
    (
        ("version", "integer", True, "", "", "", ""),
        ("migration_name", "text", True, "", "", "", _TEXT),
        ("migration_checksum", "text", True, "", "", "", _TEXT),
        ("source_schema_sha256", "text", True, "", "", "", _TEXT),
        ("target_schema_sha256", "text", True, "", "", "", _TEXT),
        ("receipt_json", "text", True, "", "", "", _TEXT),
        ("receipt_sha256", "text", True, "", "", "", _TEXT),
        ("installed_at", "timestamp with time zone", True, "", "", "", ""),
    ),
)
_PRE_AUTHORITY_HISTORY_COLUMNS = _column_records(
    "ecorex_image_schema_migrations",
    (
        ("version", "integer", True, "", "", "", ""),
        ("migration_name", "text", True, "", "", "", _TEXT),
        ("checksum", "text", True, "", "", "", _TEXT),
        ("installed_at", "timestamp with time zone", True, "now()", "", "", ""),
    ),
)
_DOMAIN_COLUMNS = (
    *_column_records(
        "image_scheduler_control",
        (
            ("singleton", "boolean", True, "true", "", "", ""),
            ("touched_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_jobs",
        (
            ("job_id", "text", True, "", "", "", _TEXT),
            ("account_id", "text", True, "", "", "", _TEXT),
            ("operation", "text", True, "", "", "", _TEXT),
            ("model_id", "text", True, "", "", "", _TEXT),
            ("size_class", "text", True, "", "", "", _TEXT),
            ("weight", "integer", True, "", "", "", ""),
            ("priority", "integer", True, "", "", "", ""),
            ("client_request_id", "text", True, "", "", "", _TEXT),
            ("request_fingerprint", "text", True, "", "", "", _TEXT),
            ("request_json", "jsonb", True, "", "", "", ""),
            ("status", "text", True, "", "", "", _TEXT),
            ("attempt", "integer", True, "0", "", "", ""),
            ("max_attempts", "integer", True, "", "", "", ""),
            ("fair_finish", "double precision", True, "", "", "", ""),
            ("available_at", "timestamp with time zone", True, "", "", "", ""),
            ("deadline", "timestamp with time zone", True, "", "", "", ""),
            ("lease_owner", "text", False, "", "", "", _TEXT),
            ("lease_token", "text", False, "", "", "", _TEXT),
            ("lease_generation", "bigint", True, "0", "", "", ""),
            ("lease_expires_at", "timestamp with time zone", False, "", "", "", ""),
            ("heartbeat_at", "timestamp with time zone", False, "", "", "", ""),
            ("provider_idempotency_key", "text", True, "", "", "", _TEXT),
            ("provider_request_id", "text", False, "", "", "", _TEXT),
            ("checkpoint_json", "jsonb", True, "'{}'::jsonb", "", "", ""),
            ("cancellation_requested", "boolean", True, "false", "", "", ""),
            ("last_error_code", "text", False, "", "", "", _TEXT),
            ("created_at", "timestamp with time zone", True, "", "", "", ""),
            ("updated_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_scheduler_accounts",
        (
            ("account_id", "text", True, "", "", "", _TEXT),
            ("last_finish", "double precision", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_inputs",
        (
            ("account_id", "text", True, "", "", "", _TEXT),
            ("sha256", "text", True, "", "", "", _TEXT),
            ("size_bytes", "bigint", True, "", "", "", ""),
            ("mime_type", "text", True, "", "", "", _TEXT),
            ("created_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_results",
        (
            ("job_id", "text", True, "", "", "", _TEXT),
            ("sha256", "text", True, "", "", "", _TEXT),
            ("size_bytes", "bigint", True, "", "", "", ""),
            ("mime_type", "text", True, "", "", "", _TEXT),
            ("committed_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_usage",
        (
            ("job_id", "text", True, "", "", "", _TEXT),
            ("usage_json", "jsonb", True, "", "", "", ""),
            ("committed_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_events",
        (
            (
                "seq",
                "bigint",
                True,
                "nextval('image_events_seq_seq'::regclass)",
                "",
                "",
                "",
            ),
            ("event_id", "text", True, "", "", "", _TEXT),
            ("job_id", "text", True, "", "", "", _TEXT),
            ("account_id", "text", True, "", "", "", _TEXT),
            ("event_type", "text", True, "", "", "", _TEXT),
            ("payload_json", "jsonb", True, "", "", "", ""),
            ("created_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_breakers",
        (
            ("scope", "text", True, "", "", "", _TEXT),
            ("failure_count", "integer", True, "", "", "", ""),
            ("open_until", "timestamp with time zone", False, "", "", "", ""),
            ("updated_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
    *_column_records(
        "image_recovery_requests",
        (
            ("account_id", "text", True, "", "", "", _TEXT),
            ("recovery_request_id", "text", True, "", "", "", _TEXT),
            ("job_id", "text", True, "", "", "", _TEXT),
            ("created_at", "timestamp with time zone", True, "", "", "", ""),
        ),
    ),
)


def _constraint(
    name: str,
    kind: str,
    table: str,
    *,
    columns: str = "",
    reference_table: str = "",
    reference_columns: str = "",
    expression: str = "",
) -> tuple[str, ...]:
    foreign = kind == "f"
    return (
        name,
        kind,
        table,
        reference_table,
        columns,
        reference_columns,
        "false",
        "false",
        "true",
        expression,
        "a" if foreign else "",
        "a" if foreign else "",
        "s" if foreign else "",
    )


_DOMAIN_CONSTRAINTS = (
    _constraint("image_scheduler_control_pkey", "p", "image_scheduler_control", columns="singleton"),
    _constraint(
        "image_scheduler_control_singleton_check",
        "c",
        "image_scheduler_control",
        columns="singleton",
        expression="singleton",
    ),
    _constraint("image_jobs_pkey", "p", "image_jobs", columns="job_id"),
    _constraint(
        "image_jobs_operation_check",
        "c",
        "image_jobs",
        columns="operation",
        expression="operation = ANY (ARRAY['generate'::text, 'retouch'::text])",
    ),
    _constraint(
        "image_jobs_weight_check",
        "c",
        "image_jobs",
        columns="weight",
        expression="weight > 0",
    ),
    _constraint(
        "image_jobs_provider_idempotency_key_key",
        "u",
        "image_jobs",
        columns="provider_idempotency_key",
    ),
    _constraint(
        "image_jobs_account_id_client_request_id_key",
        "u",
        "image_jobs",
        columns="account_id,client_request_id",
    ),
    _constraint("image_scheduler_accounts_pkey", "p", "image_scheduler_accounts", columns="account_id"),
    _constraint("image_inputs_pkey", "p", "image_inputs", columns="account_id,sha256"),
    _constraint(
        "image_inputs_size_bytes_check",
        "c",
        "image_inputs",
        columns="size_bytes",
        expression="size_bytes > 0",
    ),
    _constraint("image_results_pkey", "p", "image_results", columns="job_id"),
    _constraint(
        "image_results_job_id_fkey",
        "f",
        "image_results",
        columns="job_id",
        reference_table="image_jobs",
        reference_columns="job_id",
    ),
    _constraint("image_usage_pkey", "p", "image_usage", columns="job_id"),
    _constraint(
        "image_usage_job_id_fkey",
        "f",
        "image_usage",
        columns="job_id",
        reference_table="image_jobs",
        reference_columns="job_id",
    ),
    _constraint("image_events_pkey", "p", "image_events", columns="seq"),
    _constraint("image_events_event_id_key", "u", "image_events", columns="event_id"),
    _constraint(
        "image_events_job_id_fkey",
        "f",
        "image_events",
        columns="job_id",
        reference_table="image_jobs",
        reference_columns="job_id",
    ),
    _constraint("image_breakers_pkey", "p", "image_breakers", columns="scope"),
    _constraint(
        "image_recovery_requests_pkey",
        "p",
        "image_recovery_requests",
        columns="account_id,recovery_request_id",
    ),
    _constraint(
        "image_recovery_requests_job_id_fkey",
        "f",
        "image_recovery_requests",
        columns="job_id",
        reference_table="image_jobs",
        reference_columns="job_id",
    ),
)
_CURRENT_HISTORY_CONSTRAINTS = (
    _constraint("ecorex_image_schema_migrations_pkey", "p", "ecorex_image_schema_migrations", columns="version"),
    _constraint(
        "ecorex_image_schema_migrations_migration_name_key",
        "u",
        "ecorex_image_schema_migrations",
        columns="migration_name",
    ),
    _constraint(
        "ecorex_image_schema_migrations_version_check",
        "c",
        "ecorex_image_schema_migrations",
        columns="version",
        expression="version > 0",
    ),
    *(
        _constraint(
            f"ecorex_image_schema_migrations_{column}_check",
            "c",
            "ecorex_image_schema_migrations",
            columns=column,
            expression=f"{column} ~ '^[0-9a-f]{{64}}$'::text",
        )
        for column in (
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "receipt_sha256",
        )
    ),
)
_PRE_AUTHORITY_HISTORY_CONSTRAINTS = (
    _constraint("ecorex_image_schema_migrations_pkey", "p", "ecorex_image_schema_migrations", columns="version"),
    _constraint(
        "ecorex_image_schema_migrations_migration_name_key",
        "u",
        "ecorex_image_schema_migrations",
        columns="migration_name",
    ),
    _constraint(
        "ecorex_image_schema_migrations_version_check",
        "c",
        "ecorex_image_schema_migrations",
        columns="version",
        expression="version > 0",
    ),
    _constraint(
        "ecorex_image_schema_migrations_checksum_check",
        "c",
        "ecorex_image_schema_migrations",
        columns="checksum",
        expression="checksum ~ '^[0-9a-f]{64}$'::text",
    ),
)


_COLUMN_TYPES = {
    (record[0], record[2]): record[3] for record in (*_CURRENT_HISTORY_COLUMNS, *_DOMAIN_COLUMNS)
}
_OPCLASS = {
    "bigint": "pg_catalog.int8_ops",
    "boolean": "pg_catalog.bool_ops",
    "double precision": "pg_catalog.float8_ops",
    "integer": "pg_catalog.int4_ops",
    "text": "pg_catalog.text_ops",
    "timestamp with time zone": "pg_catalog.timestamptz_ops",
}


def _index_records(
    definitions: Sequence[
        tuple[str, str, bool, bool, Sequence[tuple[str, bool, bool]]]
    ],
) -> tuple[tuple[str, ...], ...]:
    records: list[tuple[str, ...]] = []
    for name, table, unique, primary, keys in definitions:
        for position, (column, descending, nulls_first) in enumerate(keys, 1):
            data_type = _COLUMN_TYPES[(table, column)]
            records.append(
                (
                    name,
                    table,
                    "btree",
                    "true" if unique else "false",
                    "true" if primary else "false",
                    str(position),
                    "false",
                    column,
                    _OPCLASS[data_type],
                    _TEXT if data_type == "text" else "",
                    "true" if descending else "false",
                    "true" if nulls_first else "false",
                    "",
                )
            )
    return tuple(records)


_INDEX_DEFINITIONS = (
    ("ecorex_image_schema_migrations_pkey", "ecorex_image_schema_migrations", True, True, (("version", False, False),)),
    ("ecorex_image_schema_migrations_migration_name_key", "ecorex_image_schema_migrations", True, False, (("migration_name", False, False),)),
    ("image_scheduler_control_pkey", "image_scheduler_control", True, True, (("singleton", False, False),)),
    ("image_jobs_pkey", "image_jobs", True, True, (("job_id", False, False),)),
    ("image_jobs_provider_idempotency_key_key", "image_jobs", True, False, (("provider_idempotency_key", False, False),)),
    ("image_jobs_account_id_client_request_id_key", "image_jobs", True, False, (("account_id", False, False), ("client_request_id", False, False))),
    ("image_jobs_schedulable", "image_jobs", False, False, (("status", False, False), ("available_at", False, False), ("fair_finish", False, False), ("priority", True, True), ("created_at", False, False))),
    ("image_jobs_account_status", "image_jobs", False, False, (("account_id", False, False), ("status", False, False), ("created_at", False, False))),
    ("image_jobs_model_status", "image_jobs", False, False, (("model_id", False, False), ("status", False, False))),
    ("image_scheduler_accounts_pkey", "image_scheduler_accounts", True, True, (("account_id", False, False),)),
    ("image_inputs_pkey", "image_inputs", True, True, (("account_id", False, False), ("sha256", False, False))),
    ("image_results_pkey", "image_results", True, True, (("job_id", False, False),)),
    ("image_usage_pkey", "image_usage", True, True, (("job_id", False, False),)),
    ("image_events_pkey", "image_events", True, True, (("seq", False, False),)),
    ("image_events_event_id_key", "image_events", True, False, (("event_id", False, False),)),
    ("image_events_job_seq", "image_events", False, False, (("job_id", False, False), ("seq", False, False))),
    ("image_breakers_pkey", "image_breakers", True, True, (("scope", False, False),)),
    ("image_recovery_requests_pkey", "image_recovery_requests", True, True, (("account_id", False, False), ("recovery_request_id", False, False))),
)
_INDEXES = _index_records(_INDEX_DEFINITIONS)

_FUNCTION_BODY = "BEGIN RAISE EXCEPTION 'image ledger rows are immutable'; END;"
_FUNCTIONS = (
    (
        "ecorex_image_immutable",
        "",
        "trigger",
        "plpgsql",
        "v",
        "false",
        "false",
        "false",
        "u",
        "",
        _body_digest(_FUNCTION_BODY),
    ),
)
_DOMAIN_TRIGGERS = tuple(
    (
        name,
        table,
        "O",
        "27",
        "",
        "ecorex_image_immutable()",
        "false",
        "false",
        "0",
        "",
        "",
        "",
        "",
        "",
    )
    for name, table in (
        ("image_events_immutable", "image_events"),
        ("image_inputs_immutable", "image_inputs"),
        ("image_recovery_immutable", "image_recovery_requests"),
        ("image_results_immutable", "image_results"),
        ("image_usage_immutable", "image_usage"),
    )
)
_CURRENT_HISTORY_TRIGGER = (
    (
        "ecorex_image_schema_migrations_immutable",
        "ecorex_image_schema_migrations",
        "O",
        "27",
        "",
        "ecorex_image_immutable()",
        "false",
        "false",
        "0",
        "",
        "",
        "",
        "",
        "",
    ),
)
_SEQUENCES = (
    (
        "image_events_seq_seq",
        "bigint",
        "1",
        "1",
        "1",
        "9223372036854775807",
        "1",
        "false",
        "image_events",
        "seq",
    ),
)

EXPECTED_IMAGE_SCHEMA_CATALOG = PostgresImageSchemaCatalog(
    tables=_TABLES,
    columns=tuple(sorted((*_CURRENT_HISTORY_COLUMNS, *_DOMAIN_COLUMNS))),
    constraints=tuple(sorted((*_CURRENT_HISTORY_CONSTRAINTS, *_DOMAIN_CONSTRAINTS))),
    indexes=tuple(sorted(_INDEXES)),
    triggers=tuple(sorted((*_CURRENT_HISTORY_TRIGGER, *_DOMAIN_TRIGGERS))),
    functions=_FUNCTIONS,
    sequences=_SEQUENCES,
)
PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG = PostgresImageSchemaCatalog(
    tables=_TABLES,
    columns=tuple(sorted((*_PRE_AUTHORITY_HISTORY_COLUMNS, *_DOMAIN_COLUMNS))),
    constraints=tuple(sorted((*_PRE_AUTHORITY_HISTORY_CONSTRAINTS, *_DOMAIN_CONSTRAINTS))),
    indexes=tuple(sorted(_INDEXES)),
    triggers=tuple(sorted(_DOMAIN_TRIGGERS)),
    functions=_FUNCTIONS,
    sequences=_SEQUENCES,
)
EMPTY_IMAGE_SCHEMA_CATALOG = PostgresImageSchemaCatalog()
POSTGRES_IMAGE_SCHEMA_SHA256 = EXPECTED_IMAGE_SCHEMA_CATALOG.sha256
PRE_AUTHORITY_IMAGE_SCHEMA_SHA256 = PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG.sha256
EMPTY_IMAGE_SCHEMA_SHA256 = EMPTY_IMAGE_SCHEMA_CATALOG.sha256

# Compatibility projections retained for callers that display the contract.
EXPECTED_IMAGE_SCHEMA_COLUMNS = tuple(
    (table, column, data_type, "NO" if not_null == "true" else "YES")
    for table, _ordinal, column, data_type, not_null, *_rest in EXPECTED_IMAGE_SCHEMA_CATALOG.columns
)
EXPECTED_IMAGE_SCHEMA_INDEXES = frozenset(record[0] for record in _INDEXES)
EXPECTED_IMAGE_SCHEMA_INDEX_COLUMNS = tuple(
    (name, table, key, position, descending.title(), nulls_first.title())
    for (
        name,
        table,
        _method,
        _unique,
        _primary,
        position,
        _include,
        key,
        _opclass,
        _collation,
        descending,
        nulls_first,
        _predicate,
    ) in _INDEXES
    if name in {
        "image_events_job_seq",
        "image_jobs_account_status",
        "image_jobs_model_status",
        "image_jobs_schedulable",
    }
)
EXPECTED_IMAGE_SCHEMA_TRIGGERS = frozenset(
    (name, table, function.removesuffix("()"))
    for name, table, _enabled, _type, _when, function, *_rest in EXPECTED_IMAGE_SCHEMA_CATALOG.triggers
)


@dataclass(frozen=True, slots=True)
class PostgresImageSchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != POSTGRES_IMAGE_SCHEMA_RECEIPT_VERSION
            or self.migration_version != CURRENT_IMAGE_SCHEMA_VERSION
            or self.migration_name != MIGRATION_001_NAME
            or self.migration_checksum != MIGRATION_001_CHECKSUM
            or self.source_schema_sha256
            not in {EMPTY_IMAGE_SCHEMA_SHA256, PRE_AUTHORITY_IMAGE_SCHEMA_SHA256}
            or self.target_schema_sha256 != POSTGRES_IMAGE_SCHEMA_SHA256
        ):
            raise ImageSchemaError("PostgreSQL image schema receipt is invalid")
        for value in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
        ):
            if not _is_digest(value):
                raise ImageSchemaError("PostgreSQL image schema receipt is invalid")
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except ValueError as error:
            raise ImageSchemaError("PostgreSQL image schema receipt is invalid") from error
        if installed.tzinfo is None or installed.utcoffset() is None:
            raise ImageSchemaError("PostgreSQL image schema receipt is invalid")

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


class ImageSchemaError(RuntimeError):
    """Schema is absent, incompatible, corrupt or ahead of this binary."""


_CATALOG_TABLES_SQL = """
/* ecorex:image-schema:tables */
SELECT c.relname AS table_name,c.relkind AS relation_kind,
       c.relpersistence AS persistence,c.relreplident AS replica_identity,
       c.relrowsecurity::text AS row_security,
       c.relforcerowsecurity::text AS force_row_security,
       c.relispartition::text AS is_partition
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid=c.relnamespace
WHERE n.nspname=%s AND c.relkind IN ('r','p')
  AND (left(c.relname,6)='image_'
       OR left(c.relname,13)='ecorex_image_')
ORDER BY c.relname
"""
_CATALOG_COLUMNS_SQL = """
/* ecorex:image-schema:columns */
SELECT c.relname AS table_name,a.attnum::text AS ordinal_position,
       a.attname AS column_name,
       format_type(a.atttypid,a.atttypmod) AS formatted_type,
       a.attnotnull::text AS is_not_null,
       COALESCE(pg_get_expr(d.adbin,d.adrelid,true),'') AS default_expression,
       a.attidentity AS identity_kind,a.attgenerated AS generated_kind,
       COALESCE(cn.nspname || '.' || coll.collname,'') AS collation_identity
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid=c.relnamespace
JOIN pg_attribute AS a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef AS d ON d.adrelid=c.oid AND d.adnum=a.attnum
LEFT JOIN pg_collation AS coll ON coll.oid=a.attcollation
LEFT JOIN pg_namespace AS cn ON cn.oid=coll.collnamespace
WHERE n.nspname=%s AND c.relkind IN ('r','p')
  AND (left(c.relname,6)='image_'
       OR left(c.relname,13)='ecorex_image_')
ORDER BY c.relname,a.attnum
"""
_CATALOG_CONSTRAINTS_SQL = """
/* ecorex:image-schema:constraints */
SELECT con.conname AS constraint_name,con.contype AS constraint_type,
       c.relname AS table_name,COALESCE(rc.relname,'') AS reference_table,
       COALESCE((SELECT string_agg(a.attname,',' ORDER BY keys.ordinality)
                 FROM unnest(con.conkey) WITH ORDINALITY AS keys(attnum,ordinality)
                 JOIN pg_attribute AS a ON a.attrelid=con.conrelid AND a.attnum=keys.attnum),'') AS columns,
       COALESCE((SELECT string_agg(a.attname,',' ORDER BY keys.ordinality)
                 FROM unnest(con.confkey) WITH ORDINALITY AS keys(attnum,ordinality)
                 JOIN pg_attribute AS a ON a.attrelid=con.confrelid AND a.attnum=keys.attnum),'') AS reference_columns,
       con.condeferrable::text AS is_deferrable,
       con.condeferred::text AS is_deferred,
       con.convalidated::text AS is_validated,
       CASE WHEN con.contype='c' THEN pg_get_expr(con.conbin,con.conrelid,true) ELSE '' END AS check_expression,
       CASE WHEN con.contype='f' THEN con.confupdtype::text ELSE '' END AS update_action,
       CASE WHEN con.contype='f' THEN con.confdeltype::text ELSE '' END AS delete_action,
       CASE WHEN con.contype='f' THEN con.confmatchtype::text ELSE '' END AS match_type
FROM pg_constraint AS con
JOIN pg_class AS c ON c.oid=con.conrelid
JOIN pg_namespace AS n ON n.oid=c.relnamespace
LEFT JOIN pg_class AS rc ON rc.oid=con.confrelid
WHERE n.nspname=%s
  AND (left(c.relname,6)='image_'
       OR left(c.relname,13)='ecorex_image_')
ORDER BY con.conname
"""
_CATALOG_INDEXES_SQL = """
/* ecorex:image-schema:indexes */
SELECT i.relname AS index_name,t.relname AS table_name,am.amname AS access_method,
       ix.indisunique::text AS is_unique,ix.indisprimary::text AS is_primary,
       keys.ordinality::text AS key_position,
       (keys.ordinality>ix.indnkeyatts)::text AS is_include,
       CASE WHEN keys.attnum=0 THEN pg_get_indexdef(ix.indexrelid,keys.ordinality::integer,true)
            ELSE a.attname END AS key_expression,
       COALESCE(opn.nspname || '.' || op.opcname,'') AS operator_class,
       COALESCE(cn.nspname || '.' || coll.collname,'') AS collation_identity,
       ((keys.option & 1)=1)::text AS is_descending,
       ((keys.option & 2)=2)::text AS is_nulls_first,
       COALESCE(pg_get_expr(ix.indpred,ix.indrelid,true),'') AS predicate
FROM pg_index AS ix
JOIN pg_class AS i ON i.oid=ix.indexrelid
JOIN pg_class AS t ON t.oid=ix.indrelid
JOIN pg_namespace AS n ON n.oid=t.relnamespace
JOIN pg_am AS am ON am.oid=i.relam
CROSS JOIN LATERAL unnest(
    ix.indkey::smallint[],ix.indclass::oid[],ix.indcollation::oid[],ix.indoption::smallint[]
) WITH ORDINALITY AS keys(attnum,opclass,collation_oid,option,ordinality)
LEFT JOIN pg_attribute AS a ON a.attrelid=t.oid AND a.attnum=keys.attnum
LEFT JOIN pg_opclass AS op ON op.oid=keys.opclass
LEFT JOIN pg_namespace AS opn ON opn.oid=op.opcnamespace
LEFT JOIN pg_collation AS coll ON coll.oid=keys.collation_oid
LEFT JOIN pg_namespace AS cn ON cn.oid=coll.collnamespace
WHERE n.nspname=%s AND ix.indisvalid AND ix.indisready
  AND (left(t.relname,6)='image_'
       OR left(t.relname,13)='ecorex_image_')
ORDER BY i.relname,keys.ordinality
"""
_CATALOG_TRIGGERS_SQL = """
/* ecorex:image-schema:triggers */
SELECT t.tgname AS trigger_name,c.relname AS table_name,
       t.tgenabled AS enabled_mode,t.tgtype::text AS trigger_type,
       COALESCE(pg_get_expr(t.tgqual,t.tgrelid,true),'') AS when_expression,
       p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS function_identity,
       t.tgdeferrable::text AS is_deferrable,
       t.tginitdeferred::text AS is_initially_deferred,
       t.tgnargs::text AS argument_count,
       CASE WHEN t.tgattr::text='' THEN '' ELSE t.tgattr::text END AS attribute_numbers,
       encode(t.tgargs,'hex') AS argument_bytes,
       COALESCE(t.tgoldtable,'') AS old_transition_table,
       COALESCE(t.tgnewtable,'') AS new_transition_table,
       COALESCE(con.conname,'') AS constraint_name
FROM pg_trigger AS t
JOIN pg_class AS c ON c.oid=t.tgrelid
JOIN pg_namespace AS n ON n.oid=c.relnamespace
JOIN pg_proc AS p ON p.oid=t.tgfoid
JOIN pg_namespace AS pn ON pn.oid=p.pronamespace
LEFT JOIN pg_constraint AS con ON con.oid=t.tgconstraint
WHERE NOT t.tgisinternal AND n.nspname=%s
  AND pn.nspname=n.nspname
  AND (left(c.relname,6)='image_'
       OR left(c.relname,13)='ecorex_image_')
ORDER BY t.tgname
"""
_CATALOG_FUNCTIONS_SQL = """
/* ecorex:image-schema:functions */
SELECT p.proname AS function_name,
       pg_get_function_identity_arguments(p.oid) AS identity_arguments,
       pg_get_function_result(p.oid) AS result_type,l.lanname AS language_name,
       p.provolatile AS volatility,p.prosecdef::text AS is_security_definer,
       p.proleakproof::text AS is_leakproof,p.proisstrict::text AS is_strict,
       p.proparallel AS parallel_mode,
       COALESCE(array_to_string(p.proconfig,','),'') AS runtime_config,
       p.prosrc AS function_source
FROM pg_proc AS p
JOIN pg_namespace AS n ON n.oid=p.pronamespace
JOIN pg_language AS l ON l.oid=p.prolang
WHERE n.nspname=%s
  AND (left(p.proname,6)='image_'
       OR left(p.proname,13)='ecorex_image_')
ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)
"""
_CATALOG_SEQUENCES_SQL = """
/* ecorex:image-schema:sequences */
SELECT c.relname AS sequence_name,format_type(s.seqtypid,NULL) AS data_type,
       s.seqstart::text AS start_value,s.seqincrement::text AS increment_value,
       s.seqmin::text AS minimum_value,s.seqmax::text AS maximum_value,
       s.seqcache::text AS cache_size,s.seqcycle::text AS is_cycling,
       COALESCE(tc.relname,'') AS owner_table,
       COALESCE(a.attname,'') AS owner_column
FROM pg_sequence AS s
JOIN pg_class AS c ON c.oid=s.seqrelid
JOIN pg_namespace AS n ON n.oid=c.relnamespace
LEFT JOIN pg_depend AS d ON d.objid=c.oid AND d.classid='pg_class'::regclass
                             AND d.refclassid='pg_class'::regclass
                             AND d.deptype IN ('a','i')
LEFT JOIN pg_class AS tc ON tc.oid=d.refobjid
LEFT JOIN pg_attribute AS a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
WHERE n.nspname=%s
  AND (left(c.relname,6)='image_'
       OR left(c.relname,13)='ecorex_image_')
ORDER BY c.relname
"""

_CATALOG_QUERIES = (
    ("tables", _CATALOG_TABLES_SQL, ("table_name", "relation_kind", "persistence", "replica_identity", "row_security", "force_row_security", "is_partition")),
    ("columns", _CATALOG_COLUMNS_SQL, ("table_name", "ordinal_position", "column_name", "formatted_type", "is_not_null", "default_expression", "identity_kind", "generated_kind", "collation_identity")),
    ("constraints", _CATALOG_CONSTRAINTS_SQL, ("constraint_name", "constraint_type", "table_name", "reference_table", "columns", "reference_columns", "is_deferrable", "is_deferred", "is_validated", "check_expression", "update_action", "delete_action", "match_type")),
    ("indexes", _CATALOG_INDEXES_SQL, ("index_name", "table_name", "access_method", "is_unique", "is_primary", "key_position", "is_include", "key_expression", "operator_class", "collation_identity", "is_descending", "is_nulls_first", "predicate")),
    ("triggers", _CATALOG_TRIGGERS_SQL, ("trigger_name", "table_name", "enabled_mode", "trigger_type", "when_expression", "function_identity", "is_deferrable", "is_initially_deferred", "argument_count", "attribute_numbers", "argument_bytes", "old_transition_table", "new_transition_table", "constraint_name")),
    ("functions", _CATALOG_FUNCTIONS_SQL, ("function_name", "identity_arguments", "result_type", "language_name", "volatility", "is_security_definer", "is_leakproof", "is_strict", "parallel_mode", "runtime_config", "function_source")),
    ("sequences", _CATALOG_SEQUENCES_SQL, ("sequence_name", "data_type", "start_value", "increment_value", "minimum_value", "maximum_value", "cache_size", "is_cycling", "owner_table", "owner_column")),
)


class PostgresImageSchemaManager:
    """Operator-owned migrator and Runtime-owned read-only validator."""

    def __init__(self, dsn: str, *, connection_factory: ConnectionFactory | None = None) -> None:
        if not isinstance(dsn, str) or (not dsn.strip() and connection_factory is None):
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self._connection_factory = connection_factory

    def migrate(
        self, *, target_version: int = CURRENT_IMAGE_SCHEMA_VERSION
    ) -> PostgresImageSchemaReceipt:
        if target_version != CURRENT_IMAGE_SCHEMA_VERSION:
            raise ValueError("image schema migration target is invalid")
        try:
            with self._transaction() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
                schema_name = self._server_and_schema(connection)
                source = self._read_catalog(connection, schema_name)
                if source.sha256 == POSTGRES_IMAGE_SCHEMA_SHA256:
                    return self._validate_connection(connection, schema_name=schema_name)
                if source.sha256 == EMPTY_IMAGE_SCHEMA_SHA256:
                    connection.execute(MIGRATION_001_SQL)
                    connection.execute(SCHEMA_HISTORY_SQL)
                elif source.sha256 == PRE_AUTHORITY_IMAGE_SCHEMA_SHA256:
                    self._validate_pre_authority_history(connection)
                    connection.execute(PRE_AUTHORITY_ADOPTION_SQL)
                else:
                    raise ImageSchemaError("PostgreSQL image schema source shape is unknown")

                target = self._read_catalog(connection, schema_name)
                self._require_catalog(target)
                receipt = PostgresImageSchemaReceipt(
                    schema_version=POSTGRES_IMAGE_SCHEMA_RECEIPT_VERSION,
                    migration_version=CURRENT_IMAGE_SCHEMA_VERSION,
                    migration_name=MIGRATION_001_NAME,
                    migration_checksum=MIGRATION_001_CHECKSUM,
                    source_schema_sha256=source.sha256,
                    target_schema_sha256=target.sha256,
                    installed_at=datetime.now(UTC).isoformat(),
                )
                receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
                connection.execute(
                    """
                    INSERT INTO ecorex_image_schema_migrations(
                        version,migration_name,migration_checksum,
                        source_schema_sha256,target_schema_sha256,
                        receipt_json,receipt_sha256,installed_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
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
                return self._validate_connection(connection, schema_name=schema_name)
        except ImageSchemaError:
            raise
        except Exception:
            raise ImageSchemaError("PostgreSQL image schema migration failed") from None

    def validate(self) -> PostgresImageSchemaReceipt:
        """Validate under a server-enforced read-only transaction."""

        try:
            with self._read() as connection:
                schema_name = self._server_and_schema(connection)
                return self._validate_connection(connection, schema_name=schema_name)
        except ImageSchemaError:
            raise
        except Exception:
            raise ImageSchemaError(
                "PostgreSQL image schema is unavailable; run the explicit image migration"
            ) from None

    def _validate_connection(
        self, connection: Any, *, schema_name: str
    ) -> PostgresImageSchemaReceipt:
        catalog = self._read_catalog(connection, schema_name)
        self._require_catalog(catalog)
        receipt = self._read_receipt(connection)
        if receipt.target_schema_sha256 != catalog.sha256:
            raise ImageSchemaError("PostgreSQL image schema receipt target is incompatible")
        for statement in SCHEMA_PROBE_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        return receipt

    @staticmethod
    def _require_catalog(catalog: PostgresImageSchemaCatalog) -> None:
        if catalog == EXPECTED_IMAGE_SCHEMA_CATALOG:
            return
        for dimension in EXPECTED_IMAGE_SCHEMA_CATALOG.to_dict():
            if getattr(catalog, dimension) != getattr(EXPECTED_IMAGE_SCHEMA_CATALOG, dimension):
                raise ImageSchemaError(
                    f"PostgreSQL image schema {dimension} fingerprint is incompatible"
                )
        raise ImageSchemaError("PostgreSQL image schema fingerprint is incompatible")

    def _server_and_schema(self, connection: Any) -> str:
        server_version = self._first_value(connection.execute("SHOW server_version_num").fetchone())
        if int(server_version) < 150000:
            raise ImageSchemaError("PostgreSQL 15 or newer is required")
        schema_name = self._first_value(
            connection.execute("SELECT current_schema() AS schema_name").fetchone()
        )
        if not isinstance(schema_name, str) or not schema_name or not re.fullmatch(r"[^\x00]+", schema_name):
            raise ImageSchemaError("PostgreSQL image schema search path is invalid")
        return schema_name

    def _read_catalog(self, connection: Any, schema_name: str) -> PostgresImageSchemaCatalog:
        dimensions: dict[str, tuple[tuple[str, ...], ...]] = {}
        for dimension, sql, fields in _CATALOG_QUERIES:
            cursor = connection.execute(sql, (schema_name,))
            records: list[tuple[str, ...]] = []
            for row in cursor.fetchall():
                values = self._catalog_row(row, fields)
                if dimension == "functions":
                    values = (*values[:-1], _body_digest(values[-1]))
                records.append(values)
            dimensions[dimension] = tuple(sorted(records))
        return PostgresImageSchemaCatalog(**dimensions)

    @staticmethod
    def _catalog_row(row: Any, fields: Sequence[str]) -> tuple[str, ...]:
        if isinstance(row, Mapping):
            try:
                values = tuple(row[field] for field in fields)
            except KeyError as error:
                raise ImageSchemaError("PostgreSQL image catalog row is invalid") from error
        elif isinstance(row, (tuple, list)):
            values = tuple(row)
        else:
            raise ImageSchemaError("PostgreSQL image catalog row is invalid")
        if len(values) != len(fields):
            raise ImageSchemaError("PostgreSQL image catalog row is invalid")
        return tuple(_normalize(value) for value in values)

    @staticmethod
    def _first_value(row: Any) -> Any:
        if isinstance(row, Mapping):
            if not row:
                raise ImageSchemaError("PostgreSQL image schema query returned no value")
            return next(iter(row.values()))
        if isinstance(row, (tuple, list)) and row:
            return row[0]
        raise ImageSchemaError("PostgreSQL image schema query returned no value")

    @staticmethod
    def _validate_pre_authority_history(connection: Any) -> None:
        rows = connection.execute(
            """
            /* ecorex:image-schema:legacy-history */
            SELECT version,migration_name,checksum
            FROM ecorex_image_schema_migrations ORDER BY version
            """
        ).fetchall()
        if len(rows) != 1:
            raise ImageSchemaError("PostgreSQL pre-authority history is invalid")
        row = rows[0]
        values = (
            tuple(row[field] for field in ("version", "migration_name", "checksum"))
            if isinstance(row, Mapping)
            else tuple(row)
        )
        if values != (
            CURRENT_IMAGE_SCHEMA_VERSION,
            PRE_AUTHORITY_MIGRATION_NAME,
            PRE_AUTHORITY_MIGRATION_CHECKSUM,
        ):
            raise ImageSchemaError("PostgreSQL pre-authority history is invalid")

    @staticmethod
    def _read_receipt(connection: Any) -> PostgresImageSchemaReceipt:
        rows = connection.execute(
            """
            /* ecorex:image-schema:history */
            SELECT version,migration_name,migration_checksum,
                   source_schema_sha256,target_schema_sha256,
                   receipt_json,receipt_sha256,installed_at
            FROM ecorex_image_schema_migrations ORDER BY version
            """
        ).fetchall()
        if not rows:
            raise ImageSchemaError("PostgreSQL image schema migration history is missing")
        versions: list[int] = []
        parsed: list[tuple[Any, ...]] = []
        fields = (
            "version",
            "migration_name",
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "receipt_json",
            "receipt_sha256",
            "installed_at",
        )
        for row in rows:
            values = tuple(row[field] for field in fields) if isinstance(row, Mapping) else tuple(row)
            if len(values) != len(fields):
                raise ImageSchemaError("PostgreSQL image schema migration history is invalid")
            versions.append(int(values[0]))
            parsed.append(values)
        if any(version > CURRENT_IMAGE_SCHEMA_VERSION for version in versions):
            raise ImageSchemaError("PostgreSQL image schema is newer than this runtime")
        if versions != list(range(1, CURRENT_IMAGE_SCHEMA_VERSION + 1)):
            raise ImageSchemaError("PostgreSQL image schema migration history is incomplete")
        values = parsed[-1]
        receipt_json = str(values[5])
        if str(values[6]) != _digest(receipt_json.encode("utf-8")):
            raise ImageSchemaError("PostgreSQL image schema migration history is invalid")
        try:
            raw = json.loads(receipt_json)
        except json.JSONDecodeError as error:
            raise ImageSchemaError("PostgreSQL image schema receipt is invalid") from error
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "migration_version",
            "migration_name",
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "installed_at",
        }:
            raise ImageSchemaError("PostgreSQL image schema receipt is invalid")
        receipt = PostgresImageSchemaReceipt(**dict(raw))
        if receipt_json.encode("utf-8") != _canonical(receipt.to_dict()):
            raise ImageSchemaError("PostgreSQL image schema receipt is non-canonical")
        installed_at = values[7]
        installed_text = (
            installed_at.isoformat() if isinstance(installed_at, datetime) else str(installed_at)
        )
        if (
            int(values[0]) != receipt.migration_version
            or str(values[1]) != receipt.migration_name
            or str(values[2]) != receipt.migration_checksum
            or str(values[3]) != receipt.source_schema_sha256
            or str(values[4]) != receipt.target_schema_sha256
            or installed_text != receipt.installed_at
        ):
            raise ImageSchemaError("PostgreSQL image schema receipt is inconsistent")
        return receipt

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError(
                "PostgreSQL image orchestration requires the optional psycopg package"
            ) from None
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.transaction():
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                yield connection
        finally:
            connection.close()


def migrate_postgres_image_database(
    dsn: str,
    *,
    target_version: int = CURRENT_IMAGE_SCHEMA_VERSION,
    connection_factory: ConnectionFactory | None = None,
) -> PostgresImageSchemaReceipt:
    """Explicit deployment API; Runtime composition must call validate only."""

    return PostgresImageSchemaManager(
        dsn, connection_factory=connection_factory
    ).migrate(target_version=target_version)


def validate_postgres_image_database(
    dsn: str, *, connection_factory: ConnectionFactory | None = None
) -> PostgresImageSchemaReceipt:
    return PostgresImageSchemaManager(dsn, connection_factory=connection_factory).validate()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ecorex.image_orchestrator.postgres_schema"
    )
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument(
        "--dsn-env",
        default="ECOREX_IMAGE_POSTGRES_DSN",
        help="environment variable containing the PostgreSQL DSN",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env, "")
    if not dsn:
        parser.error(f"PostgreSQL DSN environment variable {args.dsn_env!r} is empty")
    manager = PostgresImageSchemaManager(dsn)
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment CLI
    raise SystemExit(main())


__all__ = [
    "CURRENT_IMAGE_SCHEMA_VERSION",
    "EMPTY_IMAGE_SCHEMA_SHA256",
    "EXPECTED_IMAGE_SCHEMA_CATALOG",
    "EXPECTED_IMAGE_SCHEMA_COLUMNS",
    "EXPECTED_IMAGE_SCHEMA_INDEXES",
    "EXPECTED_IMAGE_SCHEMA_INDEX_COLUMNS",
    "EXPECTED_IMAGE_SCHEMA_TRIGGERS",
    "IMAGE_SCHEMA_MIGRATIONS",
    "ImageSchemaError",
    "ImageSchemaMigration",
    "MIGRATION_001_SQL",
    "POSTGRES_IMAGE_SCHEMA_SHA256",
    "PRE_AUTHORITY_CORE_SCHEMA_SQL",
    "PRE_AUTHORITY_IMAGE_SCHEMA_CATALOG",
    "PRE_AUTHORITY_IMAGE_SCHEMA_SHA256",
    "PRE_AUTHORITY_MIGRATION_CHECKSUM",
    "PRE_AUTHORITY_MIGRATION_NAME",
    "PRE_AUTHORITY_SCHEMA_HISTORY_SQL",
    "PostgresImageSchemaCatalog",
    "PostgresImageSchemaManager",
    "PostgresImageSchemaReceipt",
    "SCHEMA_HISTORY_SQL",
    "SCHEMA_PROBE_SQL",
    "main",
    "migrate_postgres_image_database",
    "validate_postgres_image_database",
]

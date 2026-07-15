"""Compiled local Runtime schema for encrypted audit delivery."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


AUDIT_OUTBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observability_audit_outbox (
    audit_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN (
            'prompt', 'response', 'tool', 'permission',
            'task', 'artifact', 'human', 'connector'
        )
    ),
    event_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    trace_id TEXT,
    payload_json TEXT NOT NULL,
    payload_format TEXT NOT NULL DEFAULT 'aesgcm-v1',
    payload_sha256 TEXT NOT NULL,
    binary_included INTEGER NOT NULL DEFAULT 0 CHECK(binary_included = 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    published_at TEXT,
    rejected_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_event_id, category, event_type)
);

CREATE INDEX IF NOT EXISTS idx_observability_audit_pending
ON observability_audit_outbox(
    published_at, next_attempt_at, created_at, audit_id
) WHERE published_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_observability_audit_thread
ON observability_audit_outbox(thread_id, created_at, audit_id);

CREATE INDEX IF NOT EXISTS idx_observability_audit_pending_v2
ON observability_audit_outbox(
    published_at, rejected_at, next_attempt_at, created_at, audit_id
) WHERE published_at IS NULL AND rejected_at IS NULL;

CREATE TABLE IF NOT EXISTS observability_audit_daily (
    day_utc TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    PRIMARY KEY(day_utc, category, event_type)
);

CREATE TABLE IF NOT EXISTS observability_audit_cursors (
    account_id TEXT PRIMARY KEY,
    last_event_rowid INTEGER NOT NULL DEFAULT 0 CHECK(last_event_rowid >= 0),
    last_permission_rowid INTEGER NOT NULL DEFAULT 0 CHECK(last_permission_rowid >= 0)
);
"""


AUDIT_OUTBOX_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="audit_outbox",
    sql=AUDIT_OUTBOX_SCHEMA_SQL,
    object_names=(
        "observability_audit_outbox",
        "idx_observability_audit_pending",
        "idx_observability_audit_thread",
        "idx_observability_audit_pending_v2",
        "observability_audit_daily",
        "observability_audit_cursors",
    ),
)


__all__ = ["AUDIT_OUTBOX_SCHEMA_FRAGMENT", "AUDIT_OUTBOX_SCHEMA_SQL"]

"""Compiled local Runtime schema for encrypted OTLP trace delivery."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


TRACE_OUTBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observability_trace_segments (
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    segment_kind TEXT NOT NULL CHECK(segment_kind IN ('turn','thread')),
    segment_id TEXT NOT NULL,
    target_seq INTEGER NOT NULL CHECK(target_seq > 0),
    created_at TEXT NOT NULL,
    rejected_at TEXT,
    last_error_code TEXT,
    PRIMARY KEY(account_id, thread_id, segment_kind, segment_id)
);

CREATE TABLE IF NOT EXISTS observability_trace_outbox (
    batch_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    segment_kind TEXT NOT NULL CHECK(segment_kind IN ('turn','thread')),
    segment_id TEXT NOT NULL,
    through_seq INTEGER NOT NULL CHECK(through_seq > 0),
    event_digest TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    chunk_count INTEGER NOT NULL CHECK(chunk_count > 0),
    span_count INTEGER NOT NULL CHECK(span_count > 0),
    payload_json TEXT NOT NULL,
    payload_format TEXT NOT NULL DEFAULT 'aesgcm-v1',
    payload_sha256 TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    published_at TEXT,
    rejected_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(
        account_id, thread_id, segment_kind, segment_id,
        through_seq, event_digest, chunk_index
    )
);

CREATE INDEX IF NOT EXISTS idx_observability_trace_pending
ON observability_trace_outbox(
    published_at, rejected_at, next_attempt_at, created_at, batch_id
) WHERE published_at IS NULL AND rejected_at IS NULL;

CREATE TABLE IF NOT EXISTS observability_trace_cursors (
    account_id TEXT PRIMARY KEY,
    last_event_rowid INTEGER NOT NULL DEFAULT 0 CHECK(last_event_rowid >= 0)
);
"""


TRACE_OUTBOX_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="trace_outbox",
    sql=TRACE_OUTBOX_SCHEMA_SQL,
    object_names=(
        "observability_trace_segments",
        "observability_trace_outbox",
        "idx_observability_trace_pending",
        "observability_trace_cursors",
    ),
)


__all__ = ["TRACE_OUTBOX_SCHEMA_FRAGMENT", "TRACE_OUTBOX_SCHEMA_SQL"]

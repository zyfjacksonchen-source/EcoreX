"""Compiled local Runtime schema for durable online-update state."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


UPDATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_update_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL,
    target_version TEXT,
    release_id TEXT,
    build_digest TEXT,
    transaction_id TEXT,
    requires_refresh INTEGER NOT NULL CHECK (requires_refresh IN (0, 1)),
    error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_update_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS runtime_update_events_no_update
BEFORE UPDATE ON runtime_update_events
BEGIN
    SELECT RAISE(ABORT, 'update events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS runtime_update_events_no_delete
BEFORE DELETE ON runtime_update_events
BEGIN
    SELECT RAISE(ABORT, 'update events are append-only');
END;

CREATE TABLE IF NOT EXISTS runtime_update_signals (
    event_id TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_update_activation_requests (
    client_request_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS runtime_update_activation_no_update
BEFORE UPDATE ON runtime_update_activation_requests
BEGIN
    SELECT RAISE(ABORT, 'update activation audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS runtime_update_activation_no_delete
BEFORE DELETE ON runtime_update_activation_requests
BEGIN
    SELECT RAISE(ABORT, 'update activation audit is append-only');
END;
"""


UPDATE_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="update",
    sql=UPDATE_SCHEMA_SQL,
    object_names=(
        "runtime_update_state",
        "runtime_update_events",
        "runtime_update_events_no_update",
        "runtime_update_events_no_delete",
        "runtime_update_signals",
        "runtime_update_activation_requests",
        "runtime_update_activation_no_update",
        "runtime_update_activation_no_delete",
    ),
)


__all__ = ["UPDATE_SCHEMA_FRAGMENT", "UPDATE_SCHEMA_SQL"]

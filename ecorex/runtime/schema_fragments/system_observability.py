"""Compiled local Runtime schema for system health observations."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


SYSTEM_OBSERVABILITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_metric_samples (
    sample_id TEXT PRIMARY KEY,
    overall TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS system_metric_samples_created
ON system_metric_samples(created_at,sample_id);

CREATE TABLE IF NOT EXISTS system_health_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    overall TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_health_events (
    event_id TEXT PRIMARY KEY,
    from_status TEXT,
    to_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS system_health_events_no_update
BEFORE UPDATE ON system_health_events BEGIN
    SELECT RAISE(ABORT, 'system health events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS system_health_events_no_delete
BEFORE DELETE ON system_health_events BEGIN
    SELECT RAISE(ABORT, 'system health events are append-only');
END;
"""


SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="system_observability",
    sql=SYSTEM_OBSERVABILITY_SCHEMA_SQL,
    object_names=(
        "system_metric_samples",
        "system_metric_samples_created",
        "system_health_state",
        "system_health_events",
        "system_health_events_no_update",
        "system_health_events_no_delete",
    ),
)


__all__ = [
    "SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT",
    "SYSTEM_OBSERVABILITY_SCHEMA_SQL",
]

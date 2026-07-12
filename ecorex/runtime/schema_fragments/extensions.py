"""Compiled local Runtime schema for the Extension Registry."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


EXTENSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS extension_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_revisions (
    revision_id TEXT PRIMARY KEY,
    extension_id TEXT NOT NULL,
    version TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    source TEXT NOT NULL,
    trust TEXT NOT NULL,
    signature_key_id TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    UNIQUE(extension_id, version, artifact_sha256),
    UNIQUE(extension_id, manifest_sha256)
);

CREATE INDEX IF NOT EXISTS idx_extension_revisions_identity
    ON extension_revisions(extension_id, installed_at, revision_id);

CREATE TABLE IF NOT EXISTS extension_states (
    extension_id TEXT PRIMARY KEY,
    active_revision_id TEXT REFERENCES extension_revisions(revision_id),
    staged_revision_id TEXT REFERENCES extension_revisions(revision_id),
    prior_known_good_revision_id TEXT REFERENCES extension_revisions(revision_id),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    health TEXT NOT NULL CHECK(
        health IN ('unknown','healthy','degraded','unhealthy','circuit_open')
    ),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
    restart_attempts INTEGER NOT NULL DEFAULT 0 CHECK(restart_attempts >= 0),
    restart_window_started_at TEXT,
    circuit_open_until TEXT,
    negotiated_protocol_version TEXT,
    catalog_digest TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_signature_evidence (
    evidence_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES extension_revisions(revision_id),
    manifest_sha256 TEXT NOT NULL,
    signature_key_id TEXT NOT NULL,
    signature_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    UNIQUE(revision_id, manifest_sha256)
);

CREATE TABLE IF NOT EXISTS extension_quarantines (
    revision_id TEXT PRIMARY KEY REFERENCES extension_revisions(revision_id),
    extension_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_requests (
    client_request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    extension_id TEXT NOT NULL,
    revision_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extension_events_extension_seq
    ON extension_events(extension_id, seq);

CREATE TABLE IF NOT EXISTS extension_catalog_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS extension_revisions_no_update
BEFORE UPDATE ON extension_revisions BEGIN
    SELECT RAISE(ABORT, 'extension revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_revisions_no_delete
BEFORE DELETE ON extension_revisions BEGIN
    SELECT RAISE(ABORT, 'extension revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_signature_evidence_no_update
BEFORE UPDATE ON extension_signature_evidence BEGIN
    SELECT RAISE(ABORT, 'extension signature evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_signature_evidence_no_delete
BEFORE DELETE ON extension_signature_evidence BEGIN
    SELECT RAISE(ABORT, 'extension signature evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_quarantines_no_update
BEFORE UPDATE ON extension_quarantines BEGIN
    SELECT RAISE(ABORT, 'extension quarantines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_quarantines_no_delete
BEFORE DELETE ON extension_quarantines BEGIN
    SELECT RAISE(ABORT, 'extension quarantines are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_requests_no_update
BEFORE UPDATE ON extension_requests BEGIN
    SELECT RAISE(ABORT, 'extension requests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_requests_no_delete
BEFORE DELETE ON extension_requests BEGIN
    SELECT RAISE(ABORT, 'extension requests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_events_no_update
BEFORE UPDATE ON extension_events BEGIN
    SELECT RAISE(ABORT, 'extension events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS extension_events_no_delete
BEFORE DELETE ON extension_events BEGIN
    SELECT RAISE(ABORT, 'extension events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS extension_catalog_snapshots_no_update
BEFORE UPDATE ON extension_catalog_snapshots BEGIN
    SELECT RAISE(ABORT, 'extension snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS extension_catalog_snapshots_no_delete
BEFORE DELETE ON extension_catalog_snapshots BEGIN
    SELECT RAISE(ABORT, 'extension snapshots are immutable');
END;
"""


EXTENSIONS_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="extensions",
    sql=EXTENSIONS_SCHEMA_SQL,
    object_names=(
        "extension_meta",
        "extension_revisions",
        "idx_extension_revisions_identity",
        "extension_states",
        "extension_signature_evidence",
        "extension_quarantines",
        "extension_requests",
        "extension_events",
        "idx_extension_events_extension_seq",
        "extension_catalog_snapshots",
        "extension_revisions_no_update",
        "extension_revisions_no_delete",
        "extension_signature_evidence_no_update",
        "extension_signature_evidence_no_delete",
        "extension_quarantines_no_update",
        "extension_quarantines_no_delete",
        "extension_requests_no_update",
        "extension_requests_no_delete",
        "extension_events_no_update",
        "extension_events_no_delete",
        "extension_catalog_snapshots_no_update",
        "extension_catalog_snapshots_no_delete",
    ),
)


__all__ = ["EXTENSIONS_SCHEMA_FRAGMENT", "EXTENSIONS_SCHEMA_SQL"]

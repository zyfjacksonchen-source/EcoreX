"""Compiled Output-domain schema for the shared local product database."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


OUTPUT_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="output",
    sql="""
CREATE TABLE IF NOT EXISTS output_policy_snapshots (
    output_policy_snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    preference_revision INTEGER NOT NULL CHECK(preference_revision >= 1),
    location_alias TEXT NOT NULL CHECK(location_alias IN ('documents', 'downloads', 'workspace')),
    root_path TEXT NOT NULL,
    root_device INTEGER NOT NULL,
    root_inode INTEGER NOT NULL,
    root_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, preference_revision)
);

CREATE TRIGGER IF NOT EXISTS output_policy_snapshots_no_update
BEFORE UPDATE ON output_policy_snapshots BEGIN
    SELECT RAISE(ABORT, 'output policy snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS output_policy_snapshots_no_delete
BEFORE DELETE ON output_policy_snapshots BEGIN
    SELECT RAISE(ABORT, 'output policy snapshots are immutable');
END;

CREATE TABLE IF NOT EXISTS output_preferences (
    account_id TEXT PRIMARY KEY,
    location_alias TEXT NOT NULL CHECK(location_alias IN ('documents', 'downloads', 'workspace')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    output_policy_snapshot_id TEXT NOT NULL REFERENCES output_policy_snapshots(output_policy_snapshot_id),
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS output_preferences_revision_fence
BEFORE UPDATE ON output_preferences
WHEN NEW.revision != OLD.revision + 1 BEGIN
    SELECT RAISE(ABORT, 'output preference revision must advance exactly once');
END;

CREATE TABLE IF NOT EXISTS output_preference_history (
    account_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    location_alias TEXT NOT NULL,
    output_policy_snapshot_id TEXT NOT NULL,
    client_request_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, revision),
    FOREIGN KEY(output_policy_snapshot_id) REFERENCES output_policy_snapshots(output_policy_snapshot_id)
);

CREATE TRIGGER IF NOT EXISTS output_preference_history_no_update
BEFORE UPDATE ON output_preference_history BEGIN
    SELECT RAISE(ABORT, 'output preference history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS output_preference_history_no_delete
BEFORE DELETE ON output_preference_history BEGIN
    SELECT RAISE(ABORT, 'output preference history is immutable');
END;

CREATE TABLE IF NOT EXISTS output_materializations (
    materialization_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    output_policy_snapshot_id TEXT NOT NULL REFERENCES output_policy_snapshots(output_policy_snapshot_id),
    location_alias TEXT NOT NULL,
    display_name TEXT NOT NULL,
    display_name_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    status TEXT NOT NULL CHECK(status IN ('preparing', 'published', 'completed')),
    reused_existing INTEGER NOT NULL DEFAULT 0 CHECK(reused_existing IN (0, 1)),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    created_at TEXT NOT NULL,
    published_at TEXT,
    completed_at TEXT,
    UNIQUE(account_id, artifact_id, revision_id, output_policy_snapshot_id)
);

CREATE TRIGGER IF NOT EXISTS output_materialization_identity_immutable
BEFORE UPDATE OF account_id, artifact_id, revision_id, output_policy_snapshot_id,
    location_alias, sha256, size_bytes, created_at
ON output_materializations BEGIN
    SELECT RAISE(ABORT, 'output materialization identity is immutable');
END;

CREATE TABLE IF NOT EXISTS output_name_claims (
    output_policy_snapshot_id TEXT NOT NULL,
    display_name_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(output_policy_snapshot_id, display_name_key),
    FOREIGN KEY(output_policy_snapshot_id) REFERENCES output_policy_snapshots(output_policy_snapshot_id)
);

CREATE TABLE IF NOT EXISTS output_name_collisions (
    collision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_policy_snapshot_id TEXT NOT NULL,
    display_name_key TEXT NOT NULL,
    observed_sha256 TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(output_policy_snapshot_id, display_name_key, observed_sha256)
);

CREATE TABLE IF NOT EXISTS output_idempotency (
    account_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    result_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, client_request_id)
);

CREATE TRIGGER IF NOT EXISTS output_idempotency_no_update
BEFORE UPDATE ON output_idempotency BEGIN
    SELECT RAISE(ABORT, 'output idempotency facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS output_idempotency_no_delete
BEFORE DELETE ON output_idempotency BEGIN
    SELECT RAISE(ABORT, 'output idempotency facts are immutable');
END;

CREATE TABLE IF NOT EXISTS output_audit (
    audit_order INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS output_audit_no_update
BEFORE UPDATE ON output_audit BEGIN
    SELECT RAISE(ABORT, 'output audit is append-only');
END;
CREATE TRIGGER IF NOT EXISTS output_audit_no_delete
BEFORE DELETE ON output_audit BEGIN
    SELECT RAISE(ABORT, 'output audit is append-only');
END;
""",
    object_names=(
        "output_policy_snapshots",
        "output_policy_snapshots_no_update",
        "output_policy_snapshots_no_delete",
        "output_preferences",
        "output_preferences_revision_fence",
        "output_preference_history",
        "output_preference_history_no_update",
        "output_preference_history_no_delete",
        "output_materializations",
        "output_materialization_identity_immutable",
        "output_name_claims",
        "output_name_collisions",
        "output_idempotency",
        "output_idempotency_no_update",
        "output_idempotency_no_delete",
        "output_audit",
        "output_audit_no_update",
        "output_audit_no_delete",
    ),
)


__all__ = ["OUTPUT_SCHEMA_FRAGMENT"]

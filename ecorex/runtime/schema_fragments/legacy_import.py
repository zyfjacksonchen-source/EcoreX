"""Canonical tables retained for an audited v0.3 copy-on-write import."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


LEGACY_IMPORT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS migration_runs (
    migration_id TEXT PRIMARY KEY,
    source_version TEXT NOT NULL,
    target_version TEXT NOT NULL,
    source_inventory_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS legacy_id_map (
    entity_kind TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    legacy_parent_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(entity_kind, legacy_id, legacy_parent_id),
    UNIQUE(entity_kind, target_id)
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    legacy_project_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    project_path TEXT NOT NULL DEFAULT '',
    memory_path TEXT NOT NULL DEFAULT '',
    dreams_path TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS project_thread_bindings (
    thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    source TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS connector_instances (
    instance_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL,
    legacy_enabled INTEGER NOT NULL CHECK(legacy_enabled IN (0, 1)),
    activation_status TEXT NOT NULL CHECK(
        activation_status IN ('requires_reauth', 'pending_validation', 'disabled')
    ),
    credential_quarantined INTEGER NOT NULL CHECK(credential_quarantined IN (0, 1)),
    source TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS skill_states (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    source TEXT NOT NULL,
    activation_status TEXT NOT NULL DEFAULT 'pending_contract_validation',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS legacy_run_records (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT REFERENCES threads(thread_id),
    turn_id TEXT REFERENCES turns(turn_id),
    run_type TEXT NOT NULL,
    source_status TEXT NOT NULL,
    imported_status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    recovery_status TEXT NOT NULL CHECK(recovery_status IN (
        'historical', 'requires_user_confirmation', 'diagnostic_only'
    )),
    terminal_reason TEXT,
    error_code TEXT,
    model TEXT,
    provider TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_row_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_run_event_records (
    request_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq > 0),
    source_event_id INTEGER NOT NULL CHECK(source_event_id > 0),
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'runtime',
    idempotency_key TEXT NOT NULL,
    orphaned INTEGER NOT NULL DEFAULT 0 CHECK(orphaned IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(request_id, event_seq),
    UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS legacy_pending_work (
    request_id TEXT PRIMARY KEY REFERENCES legacy_run_records(request_id),
    thread_id TEXT REFERENCES threads(thread_id),
    turn_id TEXT REFERENCES turns(turn_id),
    visible_input TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    source_payload_digest TEXT,
    recovery_status TEXT NOT NULL DEFAULT 'requires_user_confirmation'
        CHECK(recovery_status = 'requires_user_confirmation'),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_scheduler_tasks (
    task_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    legacy_enabled INTEGER NOT NULL CHECK(legacy_enabled IN (0, 1)),
    activation_status TEXT NOT NULL CHECK(activation_status IN (
        'requires_user_confirmation', 'unsupported_action'
    )),
    schedule_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    source_row_digest TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS legacy_permission_preferences (
    preference_id TEXT PRIMARY KEY CHECK(preference_id = 'legacy-default'),
    source_mode TEXT NOT NULL,
    target_profile TEXT NOT NULL CHECK(target_profile IN ('default', 'full_access')),
    activation_status TEXT NOT NULL CHECK(
        activation_status = 'staged_for_account_binding'
    ),
    remembered_grants_json TEXT NOT NULL DEFAULT '[]',
    filesystem_policy_present INTEGER NOT NULL CHECK(filesystem_policy_present IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_source_evidence (
    evidence_id TEXT PRIMARY KEY CHECK(evidence_id = 'v030-source'),
    evidence_level TEXT NOT NULL,
    marker_label TEXT,
    marker_sha256 TEXT,
    declared_version TEXT,
    declared_commit TEXT,
    package_sha256 TEXT,
    schema_fingerprint TEXT NOT NULL,
    schema_tables_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_artifact_links (
    item_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    legacy_message_id TEXT NOT NULL,
    legacy_relative_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_memory_blob_links (
    path TEXT PRIMARY KEY,
    blob_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_warnings (
    warning_index INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


LEGACY_IMPORT_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="legacy-import-v3",
    sql=LEGACY_IMPORT_SCHEMA_SQL,
    object_names=(
        "migration_runs",
        "legacy_id_map",
        "projects",
        "project_thread_bindings",
        "connector_instances",
        "skill_states",
        "legacy_run_records",
        "legacy_run_event_records",
        "legacy_pending_work",
        "legacy_scheduler_tasks",
        "legacy_permission_preferences",
        "migration_source_evidence",
        "migration_artifact_links",
        "migration_memory_blob_links",
        "migration_warnings",
        "migration_meta",
    ),
)


__all__ = ["LEGACY_IMPORT_SCHEMA_FRAGMENT", "LEGACY_IMPORT_SCHEMA_SQL"]

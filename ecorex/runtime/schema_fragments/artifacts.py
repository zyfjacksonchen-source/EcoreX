"""Compiled Artifact-domain schema for the shared local product database."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


ARTIFACT_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="artifacts",
    sql="""
CREATE TABLE IF NOT EXISTS artifact_entities (
    created_order INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    family TEXT NOT NULL,
    role TEXT NOT NULL,
    visibility TEXT NOT NULL,
    status TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    classification_reasons_json TEXT NOT NULL,
    current_revision_id TEXT,
    owner_account_id TEXT NOT NULL DEFAULT 'local-user',
    thread_id TEXT,
    turn_id TEXT,
    created_by_tool_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_entities_visibility_order
    ON artifact_entities(visibility, created_order);

CREATE INDEX IF NOT EXISTS idx_artifact_entities_owner_visibility_order
    ON artifact_entities(owner_account_id, visibility, created_order);

CREATE TABLE IF NOT EXISTS input_attachment_uploads (
    client_request_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    original_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(revision_id) REFERENCES artifact_revisions(revision_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_input_attachment_uploads_account_created
    ON input_attachment_uploads(account_id, created_at);

CREATE TRIGGER IF NOT EXISTS artifact_entity_scope_immutable
BEFORE UPDATE OF owner_account_id, thread_id, turn_id, created_by_tool_id
ON artifact_entities
BEGIN
    SELECT RAISE(ABORT, 'artifact scope is immutable');
END;

CREATE TABLE IF NOT EXISTS artifact_display_name_claims (
    claim_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_revisions (
    revision_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    requested_name TEXT NOT NULL,
    display_name TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256 TEXT NOT NULL,
    source_artifact_ids_json TEXT NOT NULL,
    supersedes_revision_id TEXT,
    quality_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(supersedes_revision_id) REFERENCES artifact_revisions(revision_id),
    UNIQUE(artifact_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_artifact_revisions_artifact
    ON artifact_revisions(artifact_id, revision_number);

CREATE TABLE IF NOT EXISTS artifact_lineage_sources (
    revision_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    PRIMARY KEY(revision_id, source_order),
    UNIQUE(revision_id, source_artifact_id),
    FOREIGN KEY(revision_id) REFERENCES artifact_revisions(revision_id) ON DELETE CASCADE,
    FOREIGN KEY(source_artifact_id) REFERENCES artifact_entities(artifact_id)
);

CREATE TABLE IF NOT EXISTS artifact_renditions (
    parent_revision_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    rendition_artifact_id TEXT NOT NULL,
    rendition_revision_id TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    PRIMARY KEY(parent_revision_id, kind),
    FOREIGN KEY(parent_revision_id) REFERENCES artifact_revisions(revision_id) ON DELETE CASCADE,
    FOREIGN KEY(rendition_artifact_id) REFERENCES artifact_entities(artifact_id),
    FOREIGN KEY(rendition_revision_id) REFERENCES artifact_revisions(revision_id)
);

CREATE TABLE IF NOT EXISTS artifact_feedback (
    feedback_order INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id TEXT NOT NULL UNIQUE,
    artifact_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(revision_id) REFERENCES artifact_revisions(revision_id),
    UNIQUE(artifact_id, client_request_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_feedback_current
    ON artifact_feedback(artifact_id, revision_id, feedback_order);

CREATE TABLE IF NOT EXISTS artifact_external_actions (
    action_order INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('open', 'reveal')),
    client_request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared', 'launching', 'completed', 'failed')),
    failure_code TEXT,
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(revision_id) REFERENCES artifact_revisions(revision_id),
    UNIQUE(artifact_id, client_request_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_external_actions_status
    ON artifact_external_actions(status, action_order);

CREATE TABLE IF NOT EXISTS artifact_retouch_jobs (
    job_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    base_revision_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    annotation_layer_artifact_id TEXT NOT NULL,
    annotation_layer_revision_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_revision_id TEXT,
    result_sha256 TEXT,
    completion_digest TEXT,
    change_summary TEXT,
    inspection_regions_json TEXT NOT NULL DEFAULT '[]',
    failure_reason TEXT,
    durable_job_id TEXT,
    execution_thread_id TEXT,
    execution_turn_id TEXT,
    external_idempotency_key TEXT,
    staged_result_json TEXT,
    input_revisions_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id),
    FOREIGN KEY(base_revision_id) REFERENCES artifact_revisions(revision_id),
    FOREIGN KEY(annotation_layer_artifact_id) REFERENCES artifact_entities(artifact_id),
    FOREIGN KEY(annotation_layer_revision_id) REFERENCES artifact_revisions(revision_id),
    FOREIGN KEY(result_revision_id) REFERENCES artifact_revisions(revision_id),
    UNIQUE(artifact_id, client_request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_retouch_durable_job
    ON artifact_retouch_jobs(durable_job_id) WHERE durable_job_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_retouch_external_key
    ON artifact_retouch_jobs(external_idempotency_key)
    WHERE external_idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS artifact_retouch_workspaces (
    workspace_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    base_revision_id TEXT NOT NULL,
    owner_account_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('editing', 'submitting', 'submitted')),
    edit_surface_json TEXT NOT NULL,
    annotations_json TEXT NOT NULL DEFAULT '[]',
    references_json TEXT NOT NULL DEFAULT '[]',
    global_instruction TEXT NOT NULL DEFAULT '',
    view_state_json TEXT NOT NULL DEFAULT '{}',
    mask_metadata_json TEXT,
    last_client_request_id TEXT,
    last_request_digest TEXT,
    submit_client_request_id TEXT,
    submitted_job_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifact_entities(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(base_revision_id) REFERENCES artifact_revisions(revision_id),
    FOREIGN KEY(submitted_job_id) REFERENCES artifact_retouch_jobs(job_id),
    UNIQUE(owner_account_id, artifact_id, base_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_retouch_workspaces_owner
    ON artifact_retouch_workspaces(owner_account_id, updated_at);
""",
    object_names=(
        "artifact_entities",
        "idx_artifact_entities_visibility_order",
        "idx_artifact_entities_owner_visibility_order",
        "input_attachment_uploads",
        "idx_input_attachment_uploads_account_created",
        "artifact_entity_scope_immutable",
        "artifact_display_name_claims",
        "artifact_revisions",
        "idx_artifact_revisions_artifact",
        "artifact_lineage_sources",
        "artifact_renditions",
        "artifact_feedback",
        "idx_artifact_feedback_current",
        "artifact_external_actions",
        "idx_artifact_external_actions_status",
        "artifact_retouch_jobs",
        "idx_artifact_retouch_durable_job",
        "idx_artifact_retouch_external_key",
        "artifact_retouch_workspaces",
        "idx_artifact_retouch_workspaces_owner",
    ),
)


__all__ = ["ARTIFACT_SCHEMA_FRAGMENT"]

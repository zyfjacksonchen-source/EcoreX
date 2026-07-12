"""Compiled local Integration schema for outboxes and managed image journals."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


INTEGRATION_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="integration",
    sql="""
CREATE TABLE IF NOT EXISTS artifact_event_outbox (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT 'local-user',
    thread_id TEXT,
    turn_id TEXT,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_token TEXT,
    lease_expires_at TEXT,
    published_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS artifact_event_outbox_pending
ON artifact_event_outbox(published_at, created_at)
WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS image_tool_publications (
    publication_key TEXT PRIMARY KEY,
    marker TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_token TEXT,
    lease_expires_at TEXT,
    cloud_job_id TEXT,
    result_sha256 TEXT,
    artifact_id TEXT,
    revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS image_tool_publication_cloud_result_unique
ON image_tool_publications(account_id,cloud_job_id,result_sha256)
WHERE cloud_job_id IS NOT NULL AND result_sha256 IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS image_tool_publication_identity_immutable
BEFORE UPDATE OF publication_key,marker,account_id,request_sha256
ON image_tool_publications BEGIN
    SELECT RAISE(ABORT, 'image publication identity is immutable');
END;

CREATE UNIQUE INDEX IF NOT EXISTS artifact_image_publication_marker_unique
ON artifact_entities(owner_account_id,created_by_tool_id)
WHERE created_by_tool_id GLOB 'image-publication:*';

CREATE TABLE IF NOT EXISTS managed_image_job_journal (
    account_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id,client_request_id),
    UNIQUE(account_id,job_id)
);

CREATE TRIGGER IF NOT EXISTS managed_image_journal_identity_immutable
BEFORE UPDATE OF account_id,client_request_id,request_fingerprint,job_id
ON managed_image_job_journal BEGIN
    SELECT RAISE(ABORT, 'managed image journal identity is immutable');
END;
""",
    object_names=(
        "artifact_event_outbox",
        "artifact_event_outbox_pending",
        "image_tool_publications",
        "image_tool_publication_cloud_result_unique",
        "image_tool_publication_identity_immutable",
        "artifact_image_publication_marker_unique",
        "managed_image_job_journal",
        "managed_image_journal_identity_immutable",
    ),
)


__all__ = ["INTEGRATION_SCHEMA_FRAGMENT"]

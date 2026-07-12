"""Compiled local Runtime schema for public-share and diagnostic state."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


SHARING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS share_snapshots (
    share_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'publishing','published','failed','revoking','revoked','expired'
    )),
    remote_snapshot_id TEXT,
    public_url TEXT,
    expires_at TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(account_id, client_request_id)
);
CREATE INDEX IF NOT EXISTS share_snapshots_thread
    ON share_snapshots(account_id, thread_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS share_snapshots_remote_identity
    ON share_snapshots(remote_snapshot_id)
    WHERE remote_snapshot_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS share_snapshots_public_url
    ON share_snapshots(public_url)
    WHERE public_url IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS share_snapshot_identity_immutable
BEFORE UPDATE OF account_id, thread_id, source_watermark,
    payload_json, payload_sha256, client_request_id,
    request_fingerprint, expires_at, created_at
ON share_snapshots BEGIN
    SELECT RAISE(ABORT, 'share snapshot identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS share_snapshots_no_delete
BEFORE DELETE ON share_snapshots BEGIN
    SELECT RAISE(ABORT, 'share snapshots cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS share_operations (
    operation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    share_id TEXT NOT NULL,
    action TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, client_request_id)
);
CREATE TRIGGER IF NOT EXISTS share_operations_no_update
BEFORE UPDATE ON share_operations BEGIN
    SELECT RAISE(ABORT, 'share operations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS share_operations_no_delete
BEFORE DELETE ON share_operations BEGIN
    SELECT RAISE(ABORT, 'share operations are append-only');
END;

CREATE TABLE IF NOT EXISTS share_job_bindings (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    share_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('publish','revoke')),
    client_request_id TEXT NOT NULL,
    external_idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id),
    FOREIGN KEY(share_id) REFERENCES share_snapshots(share_id),
    UNIQUE(account_id, share_id, action, client_request_id)
);
CREATE INDEX IF NOT EXISTS share_job_bindings_share
    ON share_job_bindings(account_id, share_id, created_at);
CREATE TRIGGER IF NOT EXISTS share_job_bindings_no_update
BEFORE UPDATE ON share_job_bindings BEGIN
    SELECT RAISE(ABORT, 'share job bindings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS share_job_bindings_no_delete
BEFORE DELETE ON share_job_bindings BEGIN
    SELECT RAISE(ABORT, 'share job bindings are immutable');
END;

CREATE TABLE IF NOT EXISTS diagnostic_snapshots (
    diagnostic_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
    reason_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, client_request_id)
);
CREATE TRIGGER IF NOT EXISTS diagnostic_snapshots_no_update
BEFORE UPDATE ON diagnostic_snapshots BEGIN
    SELECT RAISE(ABORT, 'diagnostic snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS diagnostic_snapshots_no_delete
BEFORE DELETE ON diagnostic_snapshots BEGIN
    SELECT RAISE(ABORT, 'diagnostic snapshots are immutable');
END;
"""


SHARING_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="sharing",
    sql=SHARING_SCHEMA_SQL,
    object_names=(
        "share_snapshots",
        "share_snapshots_thread",
        "share_snapshots_remote_identity",
        "share_snapshots_public_url",
        "share_snapshot_identity_immutable",
        "share_snapshots_no_delete",
        "share_operations",
        "share_operations_no_update",
        "share_operations_no_delete",
        "share_job_bindings",
        "share_job_bindings_share",
        "share_job_bindings_no_update",
        "share_job_bindings_no_delete",
        "diagnostic_snapshots",
        "diagnostic_snapshots_no_update",
        "diagnostic_snapshots_no_delete",
    ),
)


__all__ = ["SHARING_SCHEMA_FRAGMENT", "SHARING_SCHEMA_SQL"]

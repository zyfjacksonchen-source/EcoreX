"""Compiled local Runtime schema for managed account/session state."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


MANAGED_SESSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS managed_session_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    high_water_revision INTEGER NOT NULL CHECK(high_water_revision >= 0),
    active_intent_id TEXT,
    pending_intent_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_session_installs (
    intent_id TEXT PRIMARY KEY,
    client_request_hash TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('staged','vault_written','committed','superseded','aborted')
    ),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    base_generation INTEGER NOT NULL CHECK(base_generation >= 0),
    target_revision INTEGER NOT NULL CHECK(target_revision > 0),
    lease_json TEXT NOT NULL,
    lease_digest TEXT NOT NULL,
    credential_ref TEXT NOT NULL UNIQUE,
    previous_credential_ref TEXT,
    failure_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_managed_session_installs_status
    ON managed_session_installs(status, updated_at, intent_id);

CREATE TRIGGER IF NOT EXISTS managed_session_install_identity_immutable
BEFORE UPDATE ON managed_session_installs
WHEN NEW.intent_id IS NOT OLD.intent_id
  OR NEW.client_request_hash IS NOT OLD.client_request_hash
  OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
  OR NEW.target_revision IS NOT OLD.target_revision
  OR NEW.lease_json IS NOT OLD.lease_json
  OR NEW.lease_digest IS NOT OLD.lease_digest
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'managed session install identity is immutable');
END;

CREATE TABLE IF NOT EXISTS managed_session_logouts (
    client_request_hash TEXT PRIMARY KEY,
    expected_lease_digest TEXT,
    result_generation INTEGER NOT NULL CHECK(result_generation >= 0),
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS managed_session_logouts_no_update
BEFORE UPDATE ON managed_session_logouts
BEGIN
    SELECT RAISE(ABORT, 'managed session logout requests are append-only');
END;

CREATE TRIGGER IF NOT EXISTS managed_session_logouts_no_delete
BEFORE DELETE ON managed_session_logouts
BEGIN
    SELECT RAISE(ABORT, 'managed session logout requests are append-only');
END;

CREATE TABLE IF NOT EXISTS managed_session_credential_cleanup (
    credential_ref TEXT PRIMARY KEY,
    reason_code TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','done')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_managed_session_cleanup_pending
    ON managed_session_credential_cleanup(state, created_at, credential_ref)
    WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS managed_session_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    client_request_hash TEXT,
    account_hash TEXT,
    organization_hash TEXT,
    lease_digest TEXT,
    revision INTEGER,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS managed_session_audit_no_update
BEFORE UPDATE ON managed_session_audit
BEGIN
    SELECT RAISE(ABORT, 'managed session audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS managed_session_audit_no_delete
BEFORE DELETE ON managed_session_audit
BEGIN
    SELECT RAISE(ABORT, 'managed session audit is append-only');
END;
"""


MANAGED_SESSION_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="managed_session",
    sql=MANAGED_SESSION_SCHEMA_SQL,
    object_names=(
        "managed_session_state",
        "managed_session_installs",
        "idx_managed_session_installs_status",
        "managed_session_install_identity_immutable",
        "managed_session_logouts",
        "managed_session_logouts_no_update",
        "managed_session_logouts_no_delete",
        "managed_session_credential_cleanup",
        "idx_managed_session_cleanup_pending",
        "managed_session_audit",
        "managed_session_audit_no_update",
        "managed_session_audit_no_delete",
    ),
)


__all__ = ["MANAGED_SESSION_SCHEMA_FRAGMENT", "MANAGED_SESSION_SCHEMA_SQL"]

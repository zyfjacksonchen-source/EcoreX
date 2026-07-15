"""Compiled local Runtime schema for managed device authorization."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


DEVICE_AUTHORIZATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS managed_device_flows (
    flow_id TEXT PRIMARY KEY,
    client_request_hash TEXT NOT NULL UNIQUE,
    provider_flow_id TEXT NOT NULL UNIQUE,
    credential_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN (
        'pending','authorized','denied','expired','failed'
    )),
    user_code TEXT NOT NULL,
    verification_url TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    poll_interval_seconds INTEGER NOT NULL,
    next_poll_at TEXT NOT NULL,
    poll_attempt INTEGER NOT NULL DEFAULT 0,
    poll_lease_token TEXT,
    poll_lease_expires_at TEXT,
    session_generation INTEGER,
    lease_digest TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS managed_device_identity_immutable
BEFORE UPDATE OF flow_id,client_request_hash,provider_flow_id,
    credential_ref,user_code,verification_url,expires_at,created_at
ON managed_device_flows BEGIN
    SELECT RAISE(ABORT, 'managed device identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS managed_device_no_delete
BEFORE DELETE ON managed_device_flows BEGIN
    SELECT RAISE(ABORT, 'managed device flows are durable');
END;

CREATE TABLE IF NOT EXISTS managed_device_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    poll_attempt INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS managed_device_audit_no_update
BEFORE UPDATE ON managed_device_audit BEGIN
    SELECT RAISE(ABORT, 'managed device audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS managed_device_audit_no_delete
BEFORE DELETE ON managed_device_audit BEGIN
    SELECT RAISE(ABORT, 'managed device audit is append-only');
END;
"""


DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="device_authorization",
    sql=DEVICE_AUTHORIZATION_SCHEMA_SQL,
    object_names=(
        "managed_device_flows",
        "managed_device_identity_immutable",
        "managed_device_no_delete",
        "managed_device_audit",
        "managed_device_audit_no_update",
        "managed_device_audit_no_delete",
    ),
)


__all__ = [
    "DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT",
    "DEVICE_AUTHORIZATION_SCHEMA_SQL",
]

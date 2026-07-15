"""Compiled schema for the durable Runtime permission authority."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


PERMISSIONS_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="runtime-permissions",
    sql="""
    CREATE TABLE runtime_permission_state (
        account_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL CHECK (profile IN ('default', 'full_access')),
        revision INTEGER NOT NULL CHECK (revision > 0),
        updated_at TEXT NOT NULL,
        state_digest TEXT
    );

    CREATE TABLE permission_change_requests (
        account_id TEXT NOT NULL,
        client_request_id TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        response_json TEXT NOT NULL,
        expected_revision INTEGER,
        resulting_revision INTEGER,
        state_digest TEXT,
        audit_digest TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, client_request_id)
    );

    CREATE TABLE permission_state_ledger (
        account_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        profile TEXT NOT NULL CHECK (profile IN ('default', 'full_access')),
        previous_digest TEXT,
        state_digest TEXT NOT NULL,
        client_request_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, revision),
        UNIQUE (account_id, state_digest)
    );

    CREATE TRIGGER permission_state_ledger_no_update
    BEFORE UPDATE ON permission_state_ledger
    BEGIN
        SELECT RAISE(ABORT, 'permission ledger is append-only');
    END;

    CREATE TRIGGER permission_state_ledger_no_delete
    BEFORE DELETE ON permission_state_ledger
    BEGIN
        SELECT RAISE(ABORT, 'permission ledger is append-only');
    END;

    CREATE TRIGGER permission_state_ledger_chain_insert
    BEFORE INSERT ON permission_state_ledger
    WHEN (
        EXISTS (
            SELECT 1 FROM permission_state_ledger
            WHERE account_id = NEW.account_id
        )
        AND (
            NEW.revision != (
                SELECT MAX(revision) + 1 FROM permission_state_ledger
                WHERE account_id = NEW.account_id
            )
            OR NEW.previous_digest != (
                SELECT state_digest FROM permission_state_ledger
                WHERE account_id = NEW.account_id
                ORDER BY revision DESC LIMIT 1
            )
        )
    ) OR (
        NOT EXISTS (
            SELECT 1 FROM permission_state_ledger
            WHERE account_id = NEW.account_id
        )
        AND NEW.previous_digest IS NOT NULL
    )
    BEGIN
        SELECT RAISE(ABORT, 'permission ledger chain is invalid');
    END;

    CREATE TRIGGER runtime_permission_state_guard_update
    BEFORE UPDATE ON runtime_permission_state
    WHEN NEW.account_id != OLD.account_id
        OR NEW.revision != OLD.revision + 1
        OR NEW.profile = OLD.profile
        OR NEW.updated_at <= OLD.updated_at
        OR NOT EXISTS (
            SELECT 1 FROM permission_state_ledger AS event
            WHERE event.account_id = NEW.account_id
              AND event.revision = NEW.revision
              AND event.profile = NEW.profile
              AND event.state_digest = NEW.state_digest
              AND event.created_at = NEW.updated_at
        )
    BEGIN
        SELECT RAISE(ABORT, 'permission state update is not ledger-backed');
    END;

    CREATE TRIGGER runtime_permission_state_no_delete
    BEFORE DELETE ON runtime_permission_state
    BEGIN
        SELECT RAISE(ABORT, 'permission state cannot be deleted');
    END;

    CREATE TRIGGER permission_change_requests_no_update
    BEFORE UPDATE ON permission_change_requests
    BEGIN
        SELECT RAISE(ABORT, 'permission audit is append-only');
    END;

    CREATE TRIGGER permission_change_requests_no_delete
    BEFORE DELETE ON permission_change_requests
    BEGIN
        SELECT RAISE(ABORT, 'permission audit is append-only');
    END;
    """,
    object_names=(
        "runtime_permission_state",
        "permission_change_requests",
        "permission_state_ledger",
        "permission_state_ledger_no_update",
        "permission_state_ledger_no_delete",
        "permission_state_ledger_chain_insert",
        "runtime_permission_state_guard_update",
        "runtime_permission_state_no_delete",
        "permission_change_requests_no_update",
        "permission_change_requests_no_delete",
    ),
)


__all__ = ["PERMISSIONS_SCHEMA_FRAGMENT"]

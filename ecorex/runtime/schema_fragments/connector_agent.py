"""Durable model-originated Connector login bindings and auth completions."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


CONNECTOR_AGENT_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="connector-agent-runtime",
    sql="""
    CREATE TABLE connector_auth_completions (
        flow_id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        target_instance_id TEXT,
        completed_instance_id TEXT NOT NULL,
        completed_at TEXT NOT NULL
    );

    CREATE INDEX idx_connector_auth_completions_instance
        ON connector_auth_completions(completed_instance_id, completed_at);

    CREATE TABLE connector_interaction_logins (
        interaction_id TEXT NOT NULL REFERENCES interactions(interaction_id),
        connector_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('connect', 'reauthorize')),
        target_instance_id TEXT,
        generation INTEGER NOT NULL CHECK(generation >= 0),
        status TEXT NOT NULL CHECK(
            status IN (
                'starting', 'awaiting_callback', 'completing', 'failed',
                'completed', 'cancelled', 'reauthorization_required'
                , 'authorization_required'
            )
        ),
        lifecycle_request_id TEXT NOT NULL UNIQUE,
        flow_id TEXT UNIQUE,
        completed_instance_id TEXT,
        expires_at TEXT,
        operation_token TEXT,
        operation_lease_expires_at TEXT,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(interaction_id, generation),
        CHECK(
            (mode='connect' AND target_instance_id IS NULL)
            OR (mode='reauthorize' AND target_instance_id IS NOT NULL)
        )
    );

    CREATE UNIQUE INDEX idx_connector_interaction_logins_flow
        ON connector_interaction_logins(flow_id)
        WHERE flow_id IS NOT NULL;

    CREATE INDEX idx_connector_interaction_logins_current
        ON connector_interaction_logins(interaction_id, generation DESC, status);
    """,
    object_names=(
        "connector_auth_completions",
        "idx_connector_auth_completions_instance",
        "connector_interaction_logins",
        "idx_connector_interaction_logins_flow",
        "idx_connector_interaction_logins_current",
    ),
)


__all__ = ["CONNECTOR_AGENT_SCHEMA_FRAGMENT"]

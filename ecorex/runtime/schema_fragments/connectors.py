"""Compiled v6 schema for local Connector lifecycle and execution facts."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


CONNECTORS_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="connectors-v6",
    sql="""
    CREATE TABLE connector_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL
    );

    CREATE TABLE connector_definitions (
        connector_id TEXT PRIMARY KEY,
        contract_version TEXT NOT NULL,
        definition_json TEXT NOT NULL,
        definition_sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE connector_auth_flows (
        flow_id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        auth_kind TEXT NOT NULL,
        state_sha256 TEXT NOT NULL UNIQUE,
        private_ref TEXT,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('preparing', 'active', 'consumed', 'expired')
        ),
        operation_token TEXT,
        operation_lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        consumed_at TEXT,
        reauthorize_instance_id TEXT
    );

    CREATE INDEX idx_connector_flows_recovery
        ON connector_auth_flows(status, expires_at);

    CREATE UNIQUE INDEX uq_connector_active_reauthorization
        ON connector_auth_flows(reauthorize_instance_id)
        WHERE reauthorize_instance_id IS NOT NULL
          AND status IN ('preparing', 'active');

    CREATE TABLE connector_runtime_instances (
        instance_id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        account_subject TEXT NOT NULL,
        account_display_name TEXT NOT NULL,
        credential_ref TEXT NOT NULL UNIQUE,
        granted_scopes_json TEXT NOT NULL,
        health TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        lifecycle TEXT NOT NULL CHECK (
            lifecycle IN (
                'pending', 'active', 'draining', 'revoking', 'disconnecting'
            )
        ),
        transition_token TEXT,
        transition_lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_error_code TEXT
    );

    CREATE UNIQUE INDEX uq_connector_account_active
        ON connector_runtime_instances(connector_id, account_subject);

    CREATE INDEX idx_connector_instances_catalog
        ON connector_runtime_instances(connector_id, lifecycle);

    CREATE TABLE connector_operation_leases (
        operation_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        lease_token TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        uncertainty_policy TEXT NOT NULL DEFAULT 'auto_release' CHECK (
            uncertainty_policy IN ('auto_release', 'manual_reconcile')
        ),
        status TEXT NOT NULL DEFAULT 'active' CHECK (
            status IN ('active', 'outcome_unknown')
        ),
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(instance_id) REFERENCES connector_runtime_instances(instance_id)
            ON DELETE CASCADE
    );

    CREATE INDEX idx_connector_operation_leases_instance
        ON connector_operation_leases(instance_id, expires_at);

    CREATE TABLE connector_invocations (
        invocation_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        connector_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        idempotency_key_sha256 TEXT,
        admission_policy_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'completed', 'outcome_unknown')
        ),
        result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX idx_connector_invocations_instance
        ON connector_invocations(instance_id, created_at);

    CREATE UNIQUE INDEX idx_connector_invocations_operation
        ON connector_invocations(operation_id);

    CREATE TABLE connector_idempotency (
        instance_id TEXT NOT NULL,
        account_scope_sha256 TEXT NOT NULL,
        action_id TEXT NOT NULL,
        idempotency_key_sha256 TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        invocation_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'completed', 'outcome_unknown')
        ),
        result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(account_scope_sha256, action_id, idempotency_key_sha256),
        FOREIGN KEY(invocation_id) REFERENCES connector_invocations(invocation_id)
    );

    CREATE TABLE connector_result_staging (
        invocation_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        lease_token_sha256 TEXT NOT NULL CHECK (
            length(lease_token_sha256) = 64
            AND lease_token_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        result_sha256 TEXT NOT NULL CHECK (
            length(result_sha256) = 64
            AND result_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        size_bytes INTEGER NOT NULL CHECK (
            size_bytes BETWEEN 0 AND 8388608
        ),
        delivery_hint TEXT NOT NULL CHECK (
            delivery_hint IN ('inline', 'artifact', 'unavailable')
        ),
        inline_data_json TEXT,
        discovery_id TEXT NOT NULL,
        requested_name TEXT NOT NULL,
        owner_account_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        created_by_tool_id TEXT NOT NULL CHECK (
            created_by_tool_id IN ('connector_read', 'connector_write')
        ),
        runtime_context_json TEXT NOT NULL CHECK (
            json_valid(runtime_context_json)
            AND json_type(runtime_context_json) = 'object'
        ),
        completion_path TEXT NOT NULL CHECK (
            completion_path IN ('provider_result', 'late_provider_result')
        ),
        status TEXT NOT NULL CHECK (status IN ('staged', 'finalized')),
        artifact_id TEXT,
        revision_id TEXT,
        result_json TEXT CHECK (
            result_json IS NULL
            OR (json_valid(result_json) AND json_type(result_json) = 'object')
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(invocation_id) REFERENCES connector_invocations(invocation_id),
        CHECK (
            (delivery_hint IN ('inline', 'unavailable')
             AND inline_data_json IS NOT NULL
             AND json_valid(inline_data_json)
             AND length(CAST(inline_data_json AS BLOB)) <= 524288)
            OR (delivery_hint = 'artifact' AND inline_data_json IS NULL)
        ),
        CHECK (
            (status = 'staged'
             AND artifact_id IS NULL
             AND revision_id IS NULL
             AND result_json IS NULL)
            OR
            (status = 'finalized'
             AND result_json IS NOT NULL
             AND (
                 (delivery_hint IN ('inline', 'unavailable')
                  AND artifact_id IS NULL
                  AND revision_id IS NULL)
                 OR
                 (delivery_hint = 'artifact'
                  AND artifact_id IS NOT NULL
                  AND revision_id IS NOT NULL)
             ))
        )
    );

    CREATE INDEX idx_connector_result_staging_status
        ON connector_result_staging(status, created_at, invocation_id);

    CREATE TABLE connector_lifecycle_requests (
        client_request_id TEXT PRIMARY KEY,
        operation_kind TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'completed', 'failed')
        ),
        result_json TEXT,
        error_code TEXT,
        lease_token TEXT,
        lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX idx_connector_lifecycle_requests_status
        ON connector_lifecycle_requests(status, lease_expires_at);

    CREATE TABLE connector_vault_transitions (
        transition_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL UNIQUE,
        old_credential_ref TEXT NOT NULL,
        new_credential_ref TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('preparing', 'swapped')),
        operation_token TEXT NOT NULL,
        operation_lease_expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(instance_id) REFERENCES connector_runtime_instances(instance_id)
    );

    CREATE INDEX idx_connector_vault_transitions_recovery
        ON connector_vault_transitions(status, operation_lease_expires_at);

    CREATE TABLE connector_outbox (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        aggregate_seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        published_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_expires_at TEXT,
        next_attempt_at TEXT,
        dead_lettered_at TEXT
    );

    CREATE INDEX idx_connector_outbox_pending
        ON connector_outbox(
            published_at, dead_lettered_at, next_attempt_at,
            lease_expires_at, created_at
        );

    CREATE UNIQUE INDEX uq_connector_outbox_aggregate_seq
        ON connector_outbox(aggregate_id, aggregate_seq);
    """,
    object_names=(
        "connector_schema",
        "connector_definitions",
        "connector_auth_flows",
        "idx_connector_flows_recovery",
        "uq_connector_active_reauthorization",
        "connector_runtime_instances",
        "uq_connector_account_active",
        "idx_connector_instances_catalog",
        "connector_operation_leases",
        "idx_connector_operation_leases_instance",
        "connector_invocations",
        "idx_connector_invocations_instance",
        "idx_connector_invocations_operation",
        "connector_idempotency",
        "connector_result_staging",
        "idx_connector_result_staging_status",
        "connector_lifecycle_requests",
        "idx_connector_lifecycle_requests_status",
        "connector_vault_transitions",
        "idx_connector_vault_transitions_recovery",
        "connector_outbox",
        "idx_connector_outbox_pending",
        "uq_connector_outbox_aggregate_seq",
    ),
)


__all__ = ["CONNECTORS_SCHEMA_FRAGMENT"]

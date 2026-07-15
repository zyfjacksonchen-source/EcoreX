"""Compiled schema for governed execution facts and immutable snapshots."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


CAPABILITY_SNAPSHOTS_FRAGMENT = SchemaFragment(
    fragment_id="capability-snapshots",
    sql="""
    CREATE TABLE capability_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        policy_snapshot_id TEXT NOT NULL,
        intent TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TRIGGER capability_snapshots_no_update
    BEFORE UPDATE ON capability_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'capability snapshots are immutable');
    END;

    CREATE TRIGGER capability_snapshots_no_delete
    BEFORE DELETE ON capability_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'capability snapshots are immutable');
    END;
    """,
    object_names=(
        "capability_snapshots",
        "capability_snapshots_no_update",
        "capability_snapshots_no_delete",
    ),
)


RUNTIME_SNAPSHOTS_FRAGMENT = SchemaFragment(
    fragment_id="runtime-snapshots",
    sql="""
    CREATE TABLE runtime_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE INDEX idx_runtime_snapshots_kind_created
        ON runtime_snapshots(kind, created_at, snapshot_id);

    CREATE TRIGGER runtime_snapshots_no_update
    BEFORE UPDATE ON runtime_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'runtime snapshots are immutable');
    END;

    CREATE TRIGGER runtime_snapshots_no_delete
    BEFORE DELETE ON runtime_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'runtime snapshots are immutable');
    END;
    """,
    object_names=(
        "runtime_snapshots",
        "idx_runtime_snapshots_kind_created",
        "runtime_snapshots_no_update",
        "runtime_snapshots_no_delete",
    ),
)


TOOL_EXECUTIONS_FRAGMENT = SchemaFragment(
    fragment_id="tool-executions",
    sql="""
    CREATE TABLE tool_executions (
        tool_call_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        turn_id TEXT NOT NULL REFERENCES turns(turn_id),
        execution_batch_id TEXT NOT NULL
            REFERENCES turn_execution_batches(batch_id),
        capability_snapshot_id TEXT NOT NULL,
        policy_snapshot_id TEXT NOT NULL,
        tool_id TEXT NOT NULL,
        arguments_json TEXT NOT NULL,
        arguments_sha256 TEXT NOT NULL,
        idempotency_key TEXT,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL CHECK(attempt > 0),
        result_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX idx_tool_executions_job
        ON tool_executions(
            job_id, execution_batch_id, created_at, tool_call_id
        );

    CREATE TRIGGER tool_executions_identity_immutable
    BEFORE UPDATE OF job_id, turn_id, execution_batch_id, capability_snapshot_id,
        policy_snapshot_id, tool_id, arguments_json,
        arguments_sha256, idempotency_key
    ON tool_executions
    BEGIN
        SELECT RAISE(ABORT, 'tool execution identity is immutable');
    END;

    CREATE TABLE invocation_admissions (
        tool_call_id TEXT PRIMARY KEY
            REFERENCES tool_executions(tool_call_id),
        permit_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        turn_id TEXT NOT NULL REFERENCES turns(turn_id),
        execution_batch_id TEXT NOT NULL,
        capability_snapshot_id TEXT NOT NULL,
        permission_account_id TEXT NOT NULL,
        frozen_permission_snapshot_id TEXT NOT NULL,
        current_permission_snapshot_id TEXT NOT NULL,
        current_permission_state_digest TEXT NOT NULL,
        current_availability_digest TEXT,
        tool_id TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        arguments_sha256 TEXT NOT NULL,
        idempotency_key TEXT,
        approved INTEGER NOT NULL CHECK(approved IN (0, 1)),
        approval_interaction_id TEXT,
        effective_sandbox TEXT NOT NULL CHECK(
            effective_sandbox IN (
                'read-only', 'workspace-write', 'danger-full-access'
            )
        ),
        admitted_at TEXT NOT NULL,
        permit_digest TEXT NOT NULL
    );

    CREATE INDEX idx_invocation_admissions_job
        ON invocation_admissions(job_id, admitted_at, tool_call_id);

    CREATE TRIGGER invocation_admissions_no_update
    BEFORE UPDATE ON invocation_admissions
    BEGIN
        SELECT RAISE(ABORT, 'invocation admissions are append-only');
    END;

    CREATE TRIGGER invocation_admissions_no_delete
    BEFORE DELETE ON invocation_admissions
    BEGIN
        SELECT RAISE(ABORT, 'invocation admissions are append-only');
    END;
    """,
    object_names=(
        "tool_executions",
        "idx_tool_executions_job",
        "tool_executions_identity_immutable",
        "invocation_admissions",
        "idx_invocation_admissions_job",
        "invocation_admissions_no_update",
        "invocation_admissions_no_delete",
    ),
)


EXECUTION_SCHEMA_FRAGMENTS = (
    CAPABILITY_SNAPSHOTS_FRAGMENT,
    RUNTIME_SNAPSHOTS_FRAGMENT,
    TOOL_EXECUTIONS_FRAGMENT,
)


__all__ = [
    "CAPABILITY_SNAPSHOTS_FRAGMENT",
    "EXECUTION_SCHEMA_FRAGMENTS",
    "RUNTIME_SNAPSHOTS_FRAGMENT",
    "TOOL_EXECUTIONS_FRAGMENT",
]

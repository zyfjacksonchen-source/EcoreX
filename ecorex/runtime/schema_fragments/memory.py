"""Compiled schema for local learned-memory reset and audit facts."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


MEMORY_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="local-memory",
    sql=(
        """
    CREATE TABLE memory_canonical_records (
        record_id TEXT PRIMARY KEY,
        legacy_chunk_id TEXT NOT NULL UNIQUE,
        user_id TEXT,
        scope TEXT NOT NULL,
        source TEXT NOT NULL,
        path TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        text TEXT NOT NULL,
        legacy_hash TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        embedding_state TEXT NOT NULL DEFAULT 'rebuild_required',
        created_at INTEGER,
        updated_at INTEGER,
        memory_origin TEXT NOT NULL DEFAULT 'learned'
            CHECK(memory_origin IN ('factory','learned','imported')),
        memory_state TEXT NOT NULL DEFAULT 'active'
            CHECK(memory_state IN ('active','tombstoned')),
        reset_id TEXT,
        tombstoned_at TEXT
    );

    CREATE TABLE memory_files (
        path TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        legacy_hash TEXT NOT NULL,
        mtime INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL,
        updated_at INTEGER,
        blob_sha256 TEXT,
        availability TEXT NOT NULL CHECK(availability IN ('stored','missing','unsafe')),
        memory_origin TEXT NOT NULL DEFAULT 'learned'
            CHECK(memory_origin IN ('factory','learned','imported')),
        memory_state TEXT NOT NULL DEFAULT 'active'
            CHECK(memory_state IN ('active','tombstoned')),
        reset_id TEXT,
        tombstoned_at TEXT
    );

    CREATE TABLE memory_reset_batches (
        reset_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('active','undone','purged')),
        affected_records INTEGER NOT NULL CHECK(affected_records >= 0),
        affected_files INTEGER NOT NULL CHECK(affected_files >= 0),
        created_at TEXT NOT NULL,
        undo_until TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE memory_mutation_requests (
        client_request_id TEXT PRIMARY KEY,
        operation TEXT NOT NULL CHECK(operation IN ('reset','undo')),
        target_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE memory_audit_events (
        audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        reset_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE memory_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    """
        '    CREATE TABLE "knowledge_mutation_requests" ('
        '"client_request_id" TEXT NOT NULL PRIMARY KEY, '
        '"operation" TEXT NOT NULL DEFAULT \'\', '
        '"request_sha256" TEXT NOT NULL DEFAULT \'\', '
        '"status" TEXT NOT NULL DEFAULT \'pending\', '
        '"plan_json" TEXT NOT NULL DEFAULT \'\', '
        '"created_at" TEXT NOT NULL DEFAULT \'\', '
        '"updated_at" TEXT NOT NULL DEFAULT \'\');\n\n'
        """
    CREATE INDEX memory_batches_expiry
        ON memory_reset_batches(status,undo_until);

    CREATE INDEX memory_records_resettable
        ON memory_canonical_records(memory_state,memory_origin,record_id);

    CREATE INDEX memory_files_resettable
        ON memory_files(memory_state,memory_origin,path);

    CREATE TRIGGER memory_audit_no_update
    BEFORE UPDATE ON memory_audit_events BEGIN
        SELECT RAISE(ABORT, 'memory audit events are append-only');
    END;

    CREATE TRIGGER memory_audit_no_delete
    BEFORE DELETE ON memory_audit_events BEGIN
        SELECT RAISE(ABORT, 'memory audit events are append-only');
    END;

    CREATE TRIGGER memory_requests_no_update
    BEFORE UPDATE ON memory_mutation_requests BEGIN
        SELECT RAISE(ABORT, 'memory mutation requests are immutable');
    END;

    CREATE TRIGGER memory_requests_no_delete
    BEFORE DELETE ON memory_mutation_requests BEGIN
        SELECT RAISE(ABORT, 'memory mutation requests are immutable');
    END;

    CREATE TRIGGER memory_batch_identity_immutable
    BEFORE UPDATE OF reset_id,created_at,undo_until,affected_records,affected_files
    ON memory_reset_batches BEGIN
        SELECT RAISE(ABORT, 'memory reset identity is immutable');
    END;
    """
    ),
    object_names=(
        "memory_canonical_records",
        "memory_files",
        "memory_reset_batches",
        "memory_mutation_requests",
        "memory_audit_events",
        "memory_meta",
        "knowledge_mutation_requests",
        "memory_batches_expiry",
        "memory_records_resettable",
        "memory_files_resettable",
        "memory_audit_no_update",
        "memory_audit_no_delete",
        "memory_requests_no_update",
        "memory_requests_no_delete",
        "memory_batch_identity_immutable",
    ),
)


__all__ = ["MEMORY_SCHEMA_FRAGMENT"]

"""Compiled schema for immutable Turn input and execution-boundary facts."""

from __future__ import annotations

from ..schema_catalog import SchemaFragment


TURN_EXECUTION_SCHEMA_FRAGMENT = SchemaFragment(
    fragment_id="turn-execution-inputs",
    sql="""
    CREATE TABLE turn_input_revisions (
        revision_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        turn_id TEXT NOT NULL REFERENCES turns(turn_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        source TEXT NOT NULL CHECK (
            (ordinal = 0 AND source = 'initial')
            OR (ordinal > 0 AND source IN ('steer', 'authority_refresh'))
        ),
        input_text TEXT NOT NULL CHECK (length(input_text) > 0),
        agent_model_id TEXT NOT NULL CHECK (length(agent_model_id) > 0),
        image_model_id TEXT CHECK (
            image_model_id IS NULL OR length(image_model_id) > 0
        ),
        explicit_tool_ids_json TEXT NOT NULL CHECK (
            json_valid(explicit_tool_ids_json)
            AND json_type(explicit_tool_ids_json) = 'array'
        ),
        metadata_json TEXT NOT NULL CHECK (
            json_valid(metadata_json) AND json_type(metadata_json) = 'object'
        ),
        client_message_id TEXT,
        intent_fingerprint TEXT NOT NULL CHECK (
            length(intent_fingerprint) = 64
            AND intent_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        created_at TEXT NOT NULL,
        UNIQUE (turn_id, ordinal)
    );

    CREATE INDEX idx_turn_input_revisions_turn
        ON turn_input_revisions(turn_id, ordinal);

    CREATE UNIQUE INDEX idx_turn_input_revisions_client_message
        ON turn_input_revisions(thread_id, client_message_id)
        WHERE client_message_id IS NOT NULL;

    CREATE TRIGGER turn_input_revisions_validate_parent
    BEFORE INSERT ON turn_input_revisions
    WHEN NOT EXISTS (
        SELECT 1 FROM turns
        WHERE turn_id = NEW.turn_id AND thread_id = NEW.thread_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'turn input revision parent is inconsistent');
    END;

    CREATE TRIGGER turn_input_revisions_require_next_ordinal
    BEFORE INSERT ON turn_input_revisions
    WHEN NEW.ordinal != COALESCE(
        (SELECT MAX(ordinal) + 1 FROM turn_input_revisions WHERE turn_id = NEW.turn_id),
        0
    )
    BEGIN
        SELECT RAISE(ABORT, 'turn input revision ordinal is not contiguous');
    END;

    CREATE TRIGGER turn_input_revisions_no_update
    BEFORE UPDATE ON turn_input_revisions
    BEGIN
        SELECT RAISE(ABORT, 'turn input revisions are append-only');
    END;

    CREATE TRIGGER turn_input_revisions_no_delete
    BEFORE DELETE ON turn_input_revisions
    BEGIN
        SELECT RAISE(ABORT, 'turn input revisions are append-only');
    END;

    CREATE TABLE turn_execution_batches (
        batch_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        turn_id TEXT NOT NULL REFERENCES turns(turn_id),
        first_revision_ordinal INTEGER NOT NULL CHECK (first_revision_ordinal >= 0),
        last_revision_ordinal INTEGER NOT NULL CHECK (
            last_revision_ordinal >= first_revision_ordinal
        ),
        config_snapshot_id TEXT NOT NULL CHECK (length(config_snapshot_id) > 0),
        capability_snapshot_id TEXT NOT NULL CHECK (length(capability_snapshot_id) > 0),
        permission_snapshot_id TEXT NOT NULL CHECK (length(permission_snapshot_id) > 0),
        model_catalog_snapshot_id TEXT NOT NULL CHECK (length(model_catalog_snapshot_id) > 0),
        extension_snapshot_id TEXT NOT NULL CHECK (length(extension_snapshot_id) > 0),
        identity_sha256 TEXT NOT NULL UNIQUE CHECK (
            length(identity_sha256) = 64
            AND identity_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        created_at TEXT NOT NULL,
        UNIQUE (turn_id, first_revision_ordinal),
        FOREIGN KEY (turn_id, first_revision_ordinal)
            REFERENCES turn_input_revisions(turn_id, ordinal),
        FOREIGN KEY (turn_id, last_revision_ordinal)
            REFERENCES turn_input_revisions(turn_id, ordinal)
    );

    CREATE INDEX idx_turn_execution_batches_turn
        ON turn_execution_batches(turn_id, first_revision_ordinal, last_revision_ordinal);

    CREATE TRIGGER turn_execution_batches_validate_parent
    BEFORE INSERT ON turn_execution_batches
    WHEN NOT EXISTS (
        SELECT 1 FROM turns
        WHERE turn_id = NEW.turn_id AND thread_id = NEW.thread_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'turn execution batch parent is inconsistent');
    END;

    CREATE TRIGGER turn_execution_batches_require_next_range
    BEFORE INSERT ON turn_execution_batches
    WHEN NEW.first_revision_ordinal != COALESCE(
        (
            SELECT MAX(last_revision_ordinal) + 1
            FROM turn_execution_batches
            WHERE turn_id = NEW.turn_id
        ),
        0
    )
    BEGIN
        SELECT RAISE(ABORT, 'turn execution batch range is not contiguous');
    END;

    CREATE TRIGGER turn_execution_batches_no_update
    BEFORE UPDATE ON turn_execution_batches
    BEGIN
        SELECT RAISE(ABORT, 'turn execution batches are immutable');
    END;

    CREATE TRIGGER turn_execution_batches_no_delete
    BEFORE DELETE ON turn_execution_batches
    BEGIN
        SELECT RAISE(ABORT, 'turn execution batches are immutable');
    END;
    """,
    object_names=(
        "turn_input_revisions",
        "idx_turn_input_revisions_turn",
        "idx_turn_input_revisions_client_message",
        "turn_input_revisions_validate_parent",
        "turn_input_revisions_require_next_ordinal",
        "turn_input_revisions_no_update",
        "turn_input_revisions_no_delete",
        "turn_execution_batches",
        "idx_turn_execution_batches_turn",
        "turn_execution_batches_validate_parent",
        "turn_execution_batches_require_next_range",
        "turn_execution_batches_no_update",
        "turn_execution_batches_no_delete",
    ),
)


__all__ = ["TURN_EXECUTION_SCHEMA_FRAGMENT"]

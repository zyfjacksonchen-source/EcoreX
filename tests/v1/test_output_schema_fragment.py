from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from ecorex.output import OutputRepository
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_fragments.output import OUTPUT_SCHEMA_FRAGMENT


_OUTPUT_OBJECTS = {
    "output_policy_snapshots",
    "output_policy_snapshots_no_update",
    "output_policy_snapshots_no_delete",
    "output_preferences",
    "output_preferences_revision_fence",
    "output_preference_history",
    "output_preference_history_no_update",
    "output_preference_history_no_delete",
    "output_materializations",
    "output_materialization_identity_immutable",
    "output_name_claims",
    "output_name_collisions",
    "output_idempotency",
    "output_idempotency_no_update",
    "output_idempotency_no_delete",
    "output_audit",
    "output_audit_no_update",
    "output_audit_no_delete",
}


def _output_schema(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name LIKE 'output_%' ORDER BY type,name"
            ).fetchall()
        )


def test_output_fragment_declares_the_complete_final_object_inventory() -> None:
    assert OUTPUT_SCHEMA_FRAGMENT.fragment_id == "output"
    assert set(OUTPUT_SCHEMA_FRAGMENT.object_names) == _OUTPUT_OBJECTS
    assert len(OUTPUT_SCHEMA_FRAGMENT.object_names) == 18


def test_output_repository_rejects_missing_table_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "missing-output-table.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE output_name_collisions")
    before = _output_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema objects are missing: output_name_collisions",
    ):
        OutputRepository(path)

    assert _output_schema(path) == before
    assert "output_name_collisions" not in {record[1] for record in before}


def test_output_repository_rejects_tampered_trigger_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered-output-trigger.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER output_audit_no_delete;
            CREATE TRIGGER output_audit_no_delete
            BEFORE DELETE ON output_audit BEGIN
                SELECT 1;
            END;
            """
        )
    before = _output_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema fragment output is incompatible",
    ):
        OutputRepository(path)

    assert _output_schema(path) == before
    tampered = next(record[3] for record in before if record[1] == "output_audit_no_delete")
    assert "SELECT 1" in tampered

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from ecorex.artifacts import ArtifactRepository
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_fragments.artifacts import ARTIFACT_SCHEMA_FRAGMENT


_ARTIFACT_OBJECTS = {
    "artifact_entities",
    "idx_artifact_entities_visibility_order",
    "idx_artifact_entities_owner_visibility_order",
    "artifact_entity_scope_immutable",
    "artifact_display_name_claims",
    "artifact_revisions",
    "idx_artifact_revisions_artifact",
    "artifact_lineage_sources",
    "artifact_renditions",
    "artifact_feedback",
    "idx_artifact_feedback_current",
    "artifact_external_actions",
    "idx_artifact_external_actions_status",
    "artifact_retouch_jobs",
    "idx_artifact_retouch_durable_job",
    "idx_artifact_retouch_external_key",
    "artifact_retouch_workspaces",
    "idx_artifact_retouch_workspaces_owner",
}


def _artifact_schema(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name LIKE 'artifact_%' "
                "OR name LIKE 'idx_artifact_%' ORDER BY type,name"
            ).fetchall()
        )


def test_artifact_fragment_declares_the_complete_repository_inventory() -> None:
    assert ARTIFACT_SCHEMA_FRAGMENT.fragment_id == "artifacts"
    assert set(ARTIFACT_SCHEMA_FRAGMENT.object_names) == _ARTIFACT_OBJECTS
    assert len(ARTIFACT_SCHEMA_FRAGMENT.object_names) == 18


def test_artifact_repository_rejects_missing_index_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-artifact-index.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_artifact_retouch_external_key")
    before = _artifact_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema objects are missing: idx_artifact_retouch_external_key",
    ):
        ArtifactRepository(path)

    assert _artifact_schema(path) == before
    assert "idx_artifact_retouch_external_key" not in {
        record[1] for record in before
    }


def test_artifact_repository_rejects_tampered_trigger_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered-artifact-trigger.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER artifact_entity_scope_immutable;
            CREATE TRIGGER artifact_entity_scope_immutable
            BEFORE UPDATE OF owner_account_id, thread_id, turn_id, created_by_tool_id
            ON artifact_entities BEGIN
                SELECT 1;
            END;
            """
        )
    before = _artifact_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema fragment artifacts is incompatible",
    ):
        ArtifactRepository(path)

    assert _artifact_schema(path) == before
    tampered = next(
        record[3]
        for record in before
        if record[1] == "artifact_entity_scope_immutable"
    )
    assert "SELECT 1" in tampered

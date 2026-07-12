from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from ecorex.integration import ArtifactEventOutbox, ManagedImageJobJournal
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_fragments.integration import INTEGRATION_SCHEMA_FRAGMENT


_INTEGRATION_OBJECTS = {
    "artifact_event_outbox",
    "artifact_event_outbox_pending",
    "image_tool_publications",
    "image_tool_publication_cloud_result_unique",
    "image_tool_publication_identity_immutable",
    "artifact_image_publication_marker_unique",
    "managed_image_job_journal",
    "managed_image_journal_identity_immutable",
}


def _integration_schema(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ",".join("?" for _ in _INTEGRATION_OBJECTS)
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                f"WHERE name IN ({placeholders}) ORDER BY type,name",
                tuple(sorted(_INTEGRATION_OBJECTS)),
            ).fetchall()
        )


def test_integration_fragment_declares_the_complete_local_inventory() -> None:
    assert INTEGRATION_SCHEMA_FRAGMENT.fragment_id == "integration"
    assert set(INTEGRATION_SCHEMA_FRAGMENT.object_names) == _INTEGRATION_OBJECTS
    assert len(INTEGRATION_SCHEMA_FRAGMENT.object_names) == 8


def test_managed_image_journal_rejects_missing_table_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-managed-image-journal.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE managed_image_job_journal")
    before = _integration_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema objects are missing: managed_image_job_journal",
    ):
        ManagedImageJobJournal(path)

    assert _integration_schema(path) == before
    assert "managed_image_job_journal" not in {record[1] for record in before}


def test_outbox_rejects_tampered_cross_artifact_index_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tampered-image-marker-index.sqlite3"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP INDEX artifact_image_publication_marker_unique;
            CREATE INDEX artifact_image_publication_marker_unique
            ON artifact_entities(owner_account_id, created_by_tool_id);
            """
        )
    before = _integration_schema(path)

    with pytest.raises(
        SchemaVersionError,
        match="product schema fragment integration is incompatible",
    ):
        ArtifactEventOutbox(path)

    assert _integration_schema(path) == before
    tampered = next(
        record[3]
        for record in before
        if record[1] == "artifact_image_publication_marker_unique"
    )
    assert "WHERE" not in tampered.upper()


def test_image_client_configuration_never_changes_the_physical_schema(
    tmp_path: Path,
) -> None:
    without_client = tmp_path / "without-image-client.sqlite3"
    with_client = tmp_path / "with-image-client.sqlite3"

    create_app(
        settings=RuntimeSettings(
            database_path=without_client,
            artifact_root=tmp_path / "without-image-client-artifacts",
            runtime_bearer_token="r" * 32,
            csrf_token="c" * 32,
        )
    )
    create_app(
        settings=RuntimeSettings(
            database_path=with_client,
            artifact_root=tmp_path / "with-image-client-artifacts",
            runtime_bearer_token="R" * 32,
            csrf_token="C" * 32,
            image_orchestration_client=object(),  # type: ignore[arg-type]
            allow_unmanaged_model_gateway_for_testing=True,
            close_image_orchestration_client_on_shutdown=False,
        )
    )

    assert _integration_schema(with_client) == _integration_schema(without_client)

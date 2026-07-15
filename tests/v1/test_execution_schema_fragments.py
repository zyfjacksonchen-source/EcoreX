from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ecorex.capabilities import CapabilitySnapshotRepository
from ecorex.runtime import (
    RuntimeSettings,
    RuntimeSnapshotRepository,
    SQLiteDatabase,
    ToolExecutionRepository,
    create_app,
)
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.runtime.schema_fragments.execution import EXECUTION_SCHEMA_FRAGMENTS


EXECUTION_OBJECTS = tuple(
    name for fragment in EXECUTION_SCHEMA_FRAGMENTS for name in fragment.object_names
)


class _UnusedGateway:
    async def stream(self, _request):
        if False:  # pragma: no cover - the supervisor is not started in this test.
            yield None

    async def aclose(self) -> None:
        return None


def _schema_records(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    placeholders = ",".join("?" for _ in EXECUTION_OBJECTS)
    with sqlite3.connect(path) as connection:
        return tuple(
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                f"WHERE name IN ({placeholders}) ORDER BY type, name",
                EXECUTION_OBJECTS,
            ).fetchall()
        )


def test_execution_schema_fragments_are_static_product_inventory() -> None:
    inventory = dict(product_schema_inventory())

    for fragment in EXECUTION_SCHEMA_FRAGMENTS:
        assert inventory[fragment.fragment_id] == fragment.object_names


@pytest.mark.parametrize(
    ("tamper_sql", "repository_type"),
    (
        (
            """
            DROP TRIGGER capability_snapshots_no_update;
            CREATE TRIGGER capability_snapshots_no_update
            BEFORE UPDATE ON capability_snapshots
            BEGIN
                SELECT 1;
            END;
            """,
            CapabilitySnapshotRepository,
        ),
        (
            "DROP INDEX idx_runtime_snapshots_kind_created;",
            RuntimeSnapshotRepository,
        ),
        (
            "DROP TRIGGER tool_executions_identity_immutable;",
            ToolExecutionRepository,
        ),
    ),
)
def test_execution_repository_rejects_schema_tamper_without_repair(
    tmp_path: Path,
    tamper_sql: str,
    repository_type,
) -> None:
    path = tmp_path / "runtime.db"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(tamper_sql)
    tampered = _schema_records(path)

    with pytest.raises(SchemaVersionError):
        repository_type(path)

    assert _schema_records(path) == tampered


def test_model_worker_feature_flag_does_not_change_execution_schema(
    tmp_path: Path,
) -> None:
    offline_path = tmp_path / "offline" / "runtime.db"
    online_path = tmp_path / "online" / "runtime.db"
    offline = create_app(settings=RuntimeSettings(database_path=offline_path))
    online = create_app(
        settings=RuntimeSettings(
            database_path=online_path,
            model_gateway=_UnusedGateway(),
            allow_unmanaged_model_gateway_for_testing=True,
        )
    )

    assert offline.state.model_worker_supervisor is None
    assert online.state.model_worker_supervisor is not None
    assert _schema_records(offline_path) == _schema_records(online_path)
    assert {record[1] for record in _schema_records(offline_path)} == set(
        EXECUTION_OBJECTS
    )

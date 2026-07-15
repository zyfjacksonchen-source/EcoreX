from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ecorex.connectors import SQLiteConnectorRepository, builtin_connector_registry
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.runtime.schema_fragments.connectors import CONNECTORS_SCHEMA_FRAGMENT
from ecorex.migration.schema import initialize_target_database


class _ProviderAdapter:
    async def begin_auth(self, **_kwargs):
        raise AssertionError("schema test must not call the provider")

    async def complete_auth(self, **_kwargs):
        raise AssertionError("schema test must not call the provider")

    async def check_health(self, _credentials):
        raise AssertionError("schema test must not call the provider")

    def invoke(self, **_kwargs):
        raise AssertionError("schema test must not call the provider")


def _schema_records(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    names = CONNECTORS_SCHEMA_FRAGMENT.object_names
    placeholders = ",".join("?" for _ in names)
    with sqlite3.connect(path) as connection:
        return tuple(
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                f"WHERE name IN ({placeholders}) ORDER BY type, name",
                names,
            ).fetchall()
        )


def test_connector_schema_fragment_is_static_v6_product_inventory(
    tmp_path: Path,
) -> None:
    assert dict(product_schema_inventory())[CONNECTORS_SCHEMA_FRAGMENT.fragment_id] == (
        CONNECTORS_SCHEMA_FRAGMENT.object_names
    )
    repository = SQLiteConnectorRepository(tmp_path / "runtime.db")
    with sqlite3.connect(repository.database) as connection:
        version = connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone()[0]
    assert version == 6


def test_provider_feature_toggle_does_not_change_connector_schema(
    tmp_path: Path,
) -> None:
    builtin_path = tmp_path / "builtin.db"
    provider_path = tmp_path / "provider.db"
    builtin = SQLiteConnectorRepository(builtin_path)
    provider = SQLiteConnectorRepository(provider_path)
    builtin.sync_definitions(builtin_connector_registry().definitions())
    provider.sync_definitions(
        builtin_connector_registry({"feishu": _ProviderAdapter()}).definitions()
    )

    assert _schema_records(builtin_path) == _schema_records(provider_path)
    assert {record[1] for record in _schema_records(builtin_path)} == set(
        CONNECTORS_SCHEMA_FRAGMENT.object_names
    )


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "DROP INDEX idx_connector_outbox_pending;",
        """
        DROP INDEX idx_connector_invocations_instance;
        CREATE INDEX idx_connector_invocations_instance
            ON connector_invocations(action_id, created_at);
        """,
    ),
)
def test_connector_schema_tamper_fails_closed_without_repair(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    path = tmp_path / "runtime.db"
    SQLiteConnectorRepository(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(tamper_sql)
    tampered = _schema_records(path)

    with pytest.raises(SchemaVersionError):
        SQLiteConnectorRepository(path)

    assert _schema_records(path) == tampered


def test_v030_copy_on_write_target_is_created_directly_with_connectors_v6(
    tmp_path: Path,
) -> None:
    target = tmp_path / "v030-import-target.db"
    initialize_target_database(target)
    # First v1 product composition initializes the domain metadata row; the
    # physical target tables already come from the final compiled fragment.
    SQLiteConnectorRepository(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone()[0] == 6
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(connector_result_staging)"
            ).fetchall()
        }
    assert {
        "invocation_id",
        "delivery_hint",
        "inline_data_json",
        "result_sha256",
        "runtime_context_json",
        "status",
        "result_json",
    } <= columns


def test_unreleased_connectors_v5_prototype_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prototype-v5.db"
    SQLiteConnectorRepository(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP INDEX idx_connector_result_staging_status;
            DROP TABLE connector_result_staging;
            ALTER TABLE connector_invocations DROP COLUMN result_json;
            UPDATE connector_schema SET schema_version=5 WHERE singleton=1;
            """
        )
    before = path.read_bytes()

    with pytest.raises(SchemaVersionError):
        SQLiteConnectorRepository(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT schema_version FROM connector_schema WHERE singleton=1"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type='table' AND name='connector_result_staging'"
        ).fetchone() is None

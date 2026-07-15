from __future__ import annotations

import sqlite3

import pytest

from ecorex.extensions import (
    EXTENSION_STORAGE_SCHEMA_VERSION,
    ExtensionIntegrityError,
    SQLiteExtensionRepository,
)
from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory


def _schema_records(database: SQLiteDatabase) -> tuple[tuple[str, str, str, str], ...]:
    with database.reader() as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return tuple(
        (str(row["type"]), str(row["name"]), str(row["tbl_name"]), str(row["sql"]))
        for row in rows
    )


def _extension_objects() -> frozenset[str]:
    return frozenset(
        name
        for fragment_id, object_names in product_schema_inventory()
        if fragment_id == "extensions"
        for name in object_names
    )


def test_extension_enablement_does_not_change_product_schema(tmp_path) -> None:
    disabled = SQLiteDatabase(tmp_path / "extensions-disabled.sqlite3")
    enabled = SQLiteDatabase(tmp_path / "extensions-enabled.sqlite3")

    before = _schema_records(enabled)
    SQLiteExtensionRepository(enabled)
    after = _schema_records(enabled)

    assert before == after
    assert _schema_records(disabled) == after
    assert _extension_objects() <= {record[1] for record in after}


def test_extension_repository_rejects_missing_object_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "missing-extension-index.sqlite3")
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_extension_events_extension_seq")

    with pytest.raises(
        SchemaVersionError, match="idx_extension_events_extension_seq"
    ):
        SQLiteExtensionRepository(database)

    with database.reader() as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE name='idx_extension_events_extension_seq'"
        ).fetchone()
    assert missing is None


def test_extension_repository_rejects_weakened_trigger_without_repair(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "weakened-extension-trigger.sqlite3")
    weakened_sql = """
        CREATE TRIGGER extension_events_no_delete
        BEFORE DELETE ON extension_events
        WHEN OLD.seq < 0
        BEGIN
            SELECT RAISE(ABORT, 'weakened extension event trigger');
        END
    """
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER extension_events_no_delete")
        connection.execute(weakened_sql)
        before = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='extension_events_no_delete'"
        ).fetchone()["sql"]

    with pytest.raises(
        SchemaVersionError, match="fragment extensions is incompatible"
    ):
        SQLiteExtensionRepository(database)

    with database.reader() as connection:
        after = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='extension_events_no_delete'"
        ).fetchone()["sql"]
    assert after == before


def test_future_extension_schema_version_fails_before_any_repository_write(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "future-extension-version.sqlite3")
    SQLiteExtensionRepository(database)
    future_version = EXTENSION_STORAGE_SCHEMA_VERSION + 1
    with database.transaction() as connection:
        connection.execute(
            "UPDATE extension_meta SET value=? WHERE key='storage_schema_version'",
            (str(future_version),),
        )
        # Any attempted INSERT/UPDATE/DELETE after the version check would turn
        # this test into sqlite3.IntegrityError instead of the typed version error.
        connection.execute(
            """
            CREATE TRIGGER extension_meta_block_writes
            BEFORE INSERT ON extension_meta
            BEGIN
                SELECT RAISE(ABORT, 'extension meta write attempted');
            END
            """
        )

    with pytest.raises(
        ExtensionIntegrityError, match="extension storage schema is incompatible"
    ):
        SQLiteExtensionRepository(database)

    with database.reader() as connection:
        value = connection.execute(
            "SELECT value FROM extension_meta WHERE key='storage_schema_version'"
        ).fetchone()["value"]
    assert value == str(future_version)


def test_extension_fragment_object_inventory_is_complete() -> None:
    from ecorex.runtime.schema_fragments.extensions import EXTENSIONS_SCHEMA_FRAGMENT

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(EXTENSIONS_SCHEMA_FRAGMENT.sql)
        observed = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert observed == set(EXTENSIONS_SCHEMA_FRAGMENT.object_names)
    finally:
        connection.close()

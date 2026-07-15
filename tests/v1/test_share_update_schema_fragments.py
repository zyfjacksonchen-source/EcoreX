from __future__ import annotations

import sqlite3

import pytest

from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.sharing import ShareRepository
from ecorex.update import UpdateStateRepository


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


def _fragment_objects(fragment_id: str) -> frozenset[str]:
    return frozenset(
        names
        for current_id, object_names in product_schema_inventory()
        if current_id == fragment_id
        for names in object_names
    )


def test_share_and_update_feature_construction_does_not_change_product_schema(
    tmp_path,
) -> None:
    feature_off = SQLiteDatabase(tmp_path / "feature-off.sqlite3")
    feature_on = SQLiteDatabase(tmp_path / "feature-on.sqlite3")

    before = _schema_records(feature_on)
    ShareRepository(feature_on)
    UpdateStateRepository(feature_on, current_version="1.0.0")
    after = _schema_records(feature_on)

    assert before == after
    assert _schema_records(feature_off) == after
    observed_names = {record[1] for record in after}
    assert _fragment_objects("sharing") <= observed_names
    assert _fragment_objects("update") <= observed_names


def test_share_repository_rejects_missing_schema_object_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "missing-share-trigger.sqlite3")
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER share_operations_no_update")

    with pytest.raises(SchemaVersionError, match="share_operations_no_update"):
        ShareRepository(database)

    with database.reader() as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='share_operations_no_update'"
        ).fetchone()
    assert missing is None


def test_update_repository_rejects_tampered_schema_object_without_repair(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "tampered-update-trigger.sqlite3")
    tampered_sql = """
        CREATE TRIGGER runtime_update_events_no_delete
        BEFORE DELETE ON runtime_update_events
        BEGIN
            SELECT RAISE(ABORT, 'tampered update event trigger');
        END
    """
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER runtime_update_events_no_delete")
        connection.execute(tampered_sql)
        before = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='runtime_update_events_no_delete'"
        ).fetchone()["sql"]

    with pytest.raises(SchemaVersionError, match="fragment update is incompatible"):
        UpdateStateRepository(database, current_version="1.0.0")

    with database.reader() as connection:
        after = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='runtime_update_events_no_delete'"
        ).fetchone()["sql"]
    assert after == before


def test_fragment_modules_remain_valid_sqlite_ddl() -> None:
    # Keep a narrow, local contract: importing the compiled fragments must not
    # require a filesystem database, and every explicit object must be listed.
    from ecorex.runtime.schema_fragments.sharing import SHARING_SCHEMA_FRAGMENT
    from ecorex.runtime.schema_fragments.update import UPDATE_SCHEMA_FRAGMENT

    for fragment in (SHARING_SCHEMA_FRAGMENT, UPDATE_SCHEMA_FRAGMENT):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY)")
            connection.executescript(fragment.sql)
            observed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' AND name != 'jobs'"
                ).fetchall()
            }
            assert observed == set(fragment.object_names)
        finally:
            connection.close()

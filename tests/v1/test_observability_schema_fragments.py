from __future__ import annotations

import sqlite3

import pytest

from ecorex.observability import SystemObservabilityService, TraceOutbox
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


def _fragment_objects(fragment_id: str) -> frozenset[str]:
    return frozenset(
        name
        for current_id, object_names in product_schema_inventory()
        if current_id == fragment_id
        for name in object_names
    )


def _trace(database: SQLiteDatabase, account_id: str = "account-observe") -> TraceOutbox:
    return TraceOutbox(
        database,
        account_id=account_id,
        cipher=object(),  # type: ignore[arg-type]
        projector=object(),  # type: ignore[arg-type]
        publisher=None,
    )


def test_trace_exporter_toggle_does_not_change_product_schema(tmp_path) -> None:
    exporter_disabled = SQLiteDatabase(tmp_path / "trace-disabled.sqlite3")
    exporter_enabled = SQLiteDatabase(tmp_path / "trace-enabled.sqlite3")
    SystemObservabilityService(exporter_disabled)

    before = _schema_records(exporter_enabled)
    SystemObservabilityService(exporter_enabled)
    _trace(exporter_enabled)
    after = _schema_records(exporter_enabled)

    assert before == after
    assert _schema_records(exporter_disabled) == after
    observed = {record[1] for record in after}
    assert _fragment_objects("system_observability") <= observed
    assert _fragment_objects("trace_outbox") <= observed


def test_system_observability_rejects_missing_object_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "missing-system-trigger.sqlite3")
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER system_health_events_no_update")

    with pytest.raises(SchemaVersionError, match="system_health_events_no_update"):
        SystemObservabilityService(database)

    with database.reader() as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE name='system_health_events_no_update'"
        ).fetchone()
    assert missing is None


def test_trace_outbox_rejects_weakened_index_without_repair_or_cursor_write(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "weakened-trace-index.sqlite3")
    weakened_sql = """
        CREATE INDEX idx_observability_trace_pending
        ON observability_trace_outbox(created_at, batch_id)
    """
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_observability_trace_pending")
        connection.execute(weakened_sql)
        before = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='idx_observability_trace_pending'"
        ).fetchone()["sql"]

    with pytest.raises(SchemaVersionError, match="fragment trace_outbox is incompatible"):
        _trace(database, account_id="account-no-write")

    with database.reader() as connection:
        after = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='idx_observability_trace_pending'"
        ).fetchone()["sql"]
        cursor = connection.execute(
            "SELECT 1 FROM observability_trace_cursors "
            "WHERE account_id='account-no-write'"
        ).fetchone()
    assert after == before
    assert cursor is None


def test_observability_fragment_object_inventories_are_complete() -> None:
    from ecorex.runtime.schema_fragments.system_observability import (
        SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT,
    )
    from ecorex.runtime.schema_fragments.trace_outbox import (
        TRACE_OUTBOX_SCHEMA_FRAGMENT,
    )

    for fragment in (
        SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT,
        TRACE_OUTBOX_SCHEMA_FRAGMENT,
    ):
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(fragment.sql)
            observed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            assert observed == set(fragment.object_names)
        finally:
            connection.close()

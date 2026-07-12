from __future__ import annotations

import hashlib
import sqlite3

import pytest

from ecorex.observability import AuditIntegrityError, AuditOutbox, AuditPayloadCipher
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


def _outbox(database: SQLiteDatabase, account_id: str = "account-audit") -> AuditOutbox:
    return AuditOutbox(
        database,
        account_id=account_id,
        cipher=AuditPayloadCipher(b"a" * 32),
    )


def test_audit_feature_toggle_does_not_change_product_schema(tmp_path) -> None:
    disabled = SQLiteDatabase(tmp_path / "audit-disabled.sqlite3")
    enabled = SQLiteDatabase(tmp_path / "audit-enabled.sqlite3")

    before = _schema_records(enabled)
    _outbox(enabled)
    after = _schema_records(enabled)

    assert before == after
    assert _schema_records(disabled) == after
    expected = next(
        names
        for fragment_id, names in product_schema_inventory()
        if fragment_id == "audit_outbox"
    )
    assert set(expected) <= {record[1] for record in after}


def test_audit_outbox_rejects_missing_index_without_repair_or_cursor_write(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "missing-audit-index.sqlite3")
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_observability_audit_pending_v2")

    with pytest.raises(
        SchemaVersionError, match="idx_observability_audit_pending_v2"
    ):
        _outbox(database, account_id="account-no-cursor")

    with database.reader() as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE name='idx_observability_audit_pending_v2'"
        ).fetchone()
        cursor = connection.execute(
            "SELECT 1 FROM observability_audit_cursors "
            "WHERE account_id='account-no-cursor'"
        ).fetchone()
    assert missing is None
    assert cursor is None


def test_audit_outbox_rejects_weakened_index_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "weakened-audit-index.sqlite3")
    weakened_sql = """
        CREATE INDEX idx_observability_audit_thread
        ON observability_audit_outbox(account_id, created_at)
    """
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_observability_audit_thread")
        connection.execute(weakened_sql)
        before = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='idx_observability_audit_thread'"
        ).fetchone()["sql"]

    with pytest.raises(SchemaVersionError, match="fragment audit_outbox is incompatible"):
        _outbox(database)

    with database.reader() as connection:
        after = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='idx_observability_audit_thread'"
        ).fetchone()["sql"]
    assert after == before


def test_plaintext_audit_row_requires_signed_transform_without_runtime_rewrite(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "plaintext-audit.sqlite3")
    plaintext = '{"task":"legacy"}'
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO observability_audit_outbox("
            "audit_id,source_event_id,category,event_type,account_id,payload_json,"
            "payload_format,payload_sha256,binary_included,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,0,?)",
            (
                "audit-plaintext",
                "event-plaintext",
                "task",
                "job.completed",
                "account-plaintext",
                plaintext,
                "plaintext-v0",
                digest,
                "2026-07-10T00:00:00+00:00",
            ),
        )

    with pytest.raises(AuditIntegrityError, match="signed encryption migration"):
        _outbox(database, account_id="account-plaintext")

    with database.reader() as connection:
        row = connection.execute(
            "SELECT payload_json,payload_format,payload_sha256 "
            "FROM observability_audit_outbox WHERE audit_id='audit-plaintext'"
        ).fetchone()
        cursor = connection.execute(
            "SELECT 1 FROM observability_audit_cursors "
            "WHERE account_id='account-plaintext'"
        ).fetchone()
    assert tuple(row) == (plaintext, "plaintext-v0", digest)
    assert cursor is None


def test_audit_outbox_fragment_inventory_is_complete() -> None:
    from ecorex.runtime.schema_fragments.audit_outbox import (
        AUDIT_OUTBOX_SCHEMA_FRAGMENT,
    )

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(AUDIT_OUTBOX_SCHEMA_FRAGMENT.sql)
        observed = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert observed == set(AUDIT_OUTBOX_SCHEMA_FRAGMENT.object_names)
    finally:
        connection.close()

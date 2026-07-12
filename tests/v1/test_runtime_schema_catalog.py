from __future__ import annotations

import sqlite3

import pytest

from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime import schema_catalog
from ecorex.runtime import schema_fragments


PROBE_SQL = """
CREATE TABLE catalog_probe (
    probe_id TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX idx_catalog_probe_value ON catalog_probe(value);
CREATE TRIGGER catalog_probe_no_delete
BEFORE DELETE ON catalog_probe
BEGIN
    SELECT RAISE(ABORT, 'catalog probe is immutable');
END;
"""


def _install_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = schema_catalog.SchemaFragment(
        fragment_id="catalog.probe",
        sql=PROBE_SQL,
        object_names=(
            "catalog_probe",
            "idx_catalog_probe_value",
            "catalog_probe_no_delete",
        ),
    )
    monkeypatch.setattr(schema_fragments, "PRODUCT_SCHEMA_FRAGMENTS", (fragment,))
    schema_catalog._compiled_fragment_digests.cache_clear()
    schema_catalog.compiled_product_schema_digest.cache_clear()


def test_fragment_allows_trigger_body_but_rejects_transaction_control() -> None:
    schema_catalog.SchemaFragment(
        fragment_id="catalog.trigger",
        sql=PROBE_SQL,
        object_names=(
            "catalog_probe",
            "idx_catalog_probe_value",
            "catalog_probe_no_delete",
        ),
    )
    with pytest.raises(ValueError, match="only CREATE"):
        schema_catalog.SchemaFragment(
            fragment_id="catalog.transaction",
            sql="BEGIN IMMEDIATE; CREATE TABLE forbidden(value TEXT); COMMIT;",
            object_names=("forbidden",),
        )


def test_bootstrap_is_complete_and_later_validation_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe(monkeypatch)
    connection = sqlite3.connect(":memory:")
    try:
        schema_catalog.bootstrap_product_schema(connection)
        first_digest = schema_catalog.validate_product_schema(connection)
        assert len(first_digest) == 64

        connection.execute("DROP TRIGGER catalog_probe_no_delete")
        with pytest.raises(SchemaVersionError, match="objects are missing"):
            schema_catalog.validate_product_schema(connection)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema "
                "WHERE type='trigger' AND name='catalog_probe_no_delete'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()
        schema_catalog._compiled_fragment_digests.cache_clear()
        schema_catalog.compiled_product_schema_digest.cache_clear()


def test_same_name_definition_drift_is_rejected_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe(monkeypatch)
    connection = sqlite3.connect(":memory:")
    try:
        schema_catalog.bootstrap_product_schema(connection)
        connection.executescript(
            "DROP TRIGGER catalog_probe_no_delete;"
            "CREATE TRIGGER catalog_probe_no_delete BEFORE DELETE ON catalog_probe "
            "BEGIN SELECT 1; END;"
        )
        tampered = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='catalog_probe_no_delete'"
        ).fetchone()[0]

        with pytest.raises(SchemaVersionError, match="incompatible"):
            schema_catalog.validate_product_schema(connection)

        assert connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='catalog_probe_no_delete'"
        ).fetchone()[0] == tampered
    finally:
        connection.close()
        schema_catalog._compiled_fragment_digests.cache_clear()
        schema_catalog.compiled_product_schema_digest.cache_clear()

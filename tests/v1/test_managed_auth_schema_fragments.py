from __future__ import annotations

import sqlite3

import pytest

from ecorex.connectors import InMemoryCredentialVault
from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError
from ecorex.runtime.schema_catalog import product_schema_inventory
from ecorex.session import (
    ManagedDeviceAuthorizationService,
    ManagedSessionRepository,
    ManagedSessionService,
    RejectingSessionLeaseVerifier,
)


class _Broker:
    async def begin(self, *, idempotency_key: str):  # pragma: no cover - constructor only
        raise AssertionError(idempotency_key)

    async def poll(self, **values):  # pragma: no cover - constructor only
        raise AssertionError(values)


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


def _session(database: SQLiteDatabase):
    vault = InMemoryCredentialVault()
    return (
        ManagedSessionService(
            database,
            vault=vault,
            verifier=RejectingSessionLeaseVerifier(),
        ),
        vault,
    )


def test_managed_auth_feature_construction_does_not_change_product_schema(
    tmp_path,
) -> None:
    feature_off = SQLiteDatabase(tmp_path / "feature-off.sqlite3")
    feature_on = SQLiteDatabase(tmp_path / "feature-on.sqlite3")

    before = _schema_records(feature_on)
    session, vault = _session(feature_on)
    ManagedDeviceAuthorizationService(
        feature_on,
        session=session,
        vault=vault,
        broker=_Broker(),
    )
    after = _schema_records(feature_on)

    assert before == after
    assert _schema_records(feature_off) == after
    observed = {record[1] for record in after}
    assert _fragment_objects("managed_session") <= observed
    assert _fragment_objects("device_authorization") <= observed


def test_managed_session_rejects_missing_schema_object_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "missing-session-index.sqlite3")
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_managed_session_installs_status")

    with pytest.raises(
        SchemaVersionError, match="idx_managed_session_installs_status"
    ):
        ManagedSessionRepository(database)

    with database.reader() as connection:
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE name='idx_managed_session_installs_status'"
        ).fetchone()
    assert missing is None


def test_device_authorization_rejects_tampered_schema_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "tampered-device-trigger.sqlite3")
    session, vault = _session(database)
    tampered_sql = """
        CREATE TRIGGER managed_device_audit_no_delete
        BEFORE DELETE ON managed_device_audit
        BEGIN
            SELECT RAISE(ABORT, 'tampered managed device audit trigger');
        END
    """
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER managed_device_audit_no_delete")
        connection.execute(tampered_sql)
        before = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='managed_device_audit_no_delete'"
        ).fetchone()["sql"]

    with pytest.raises(
        SchemaVersionError,
        match="fragment device_authorization is incompatible",
    ):
        ManagedDeviceAuthorizationService(
            database,
            session=session,
            vault=vault,
            broker=_Broker(),
        )

    with database.reader() as connection:
        after = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='managed_device_audit_no_delete'"
        ).fetchone()["sql"]
    assert after == before


def test_device_authorization_requires_managed_session_database(tmp_path) -> None:
    session_database = SQLiteDatabase(tmp_path / "session.sqlite3")
    device_database = SQLiteDatabase(tmp_path / "device.sqlite3")
    session, vault = _session(session_database)

    with pytest.raises(ValueError, match="must use one database"):
        ManagedDeviceAuthorizationService(
            device_database,
            session=session,
            vault=vault,
            broker=_Broker(),
        )


def test_managed_auth_fragment_object_inventories_are_complete() -> None:
    from ecorex.runtime.schema_fragments.device_authorization import (
        DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT,
    )
    from ecorex.runtime.schema_fragments.managed_session import (
        MANAGED_SESSION_SCHEMA_FRAGMENT,
    )

    for fragment in (
        MANAGED_SESSION_SCHEMA_FRAGMENT,
        DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT,
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

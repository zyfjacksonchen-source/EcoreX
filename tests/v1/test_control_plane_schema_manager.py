from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from ecorex.control_plane import (
    CONTROL_PLANE_SCHEMA_SHA256,
    ControlPlaneRepository,
    ControlPlaneSchemaError,
    ControlPlaneSchemaManager,
)
from ecorex.control_plane.schema import CONTROL_PLANE_CORE_SCHEMA_SQL


class _Verifier:
    def verify(self, _payload, _signature) -> bool:
        return True


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_repository_requires_explicit_migration_without_creating_database(
    tmp_path,
) -> None:
    database = tmp_path / "uninitialized-control-plane.sqlite3"

    with pytest.raises(ControlPlaneSchemaError, match="unavailable"):
        ControlPlaneRepository(database, verifier=_Verifier())

    assert not database.exists()


def test_control_plane_migration_is_versioned_idempotent_and_concurrent(
    tmp_path,
) -> None:
    database = tmp_path / "control-plane.sqlite3"

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda _index: ControlPlaneSchemaManager(database).migrate(),
                range(4),
            )
        )
    repository = ControlPlaneRepository(database, verifier=_Verifier())

    assert all(receipt == receipts[0] for receipt in receipts)
    assert repository.schema_receipt == receipts[0]
    assert receipts[0].migration_version == 1
    assert receipts[0].target_schema_sha256 == CONTROL_PLANE_SCHEMA_SHA256
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT migration_checksum,receipt_json,receipt_sha256 "
            "FROM control_schema_migrations ORDER BY version"
        ).fetchall()
    assert len(rows) == 1
    assert hashlib.sha256(rows[0][1].encode("utf-8")).hexdigest() == rows[0][2]
    assert json.loads(rows[0][1])["migration_checksum"] == rows[0][0]


def test_known_pre_authority_core_is_adopted_without_losing_data(tmp_path) -> None:
    database = tmp_path / "legacy-control-plane.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(CONTROL_PLANE_CORE_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO control_clients("
            "client_id,account_id,platform,architecture,current_version,"
            "update_state,last_seen_at) VALUES(?,?,?,?,?,?,?)",
            (
                "client-existing",
                "account-existing",
                "windows",
                "x64",
                "0.3.0",
                "idle",
                "2026-07-10T00:00:00+00:00",
            ),
        )
        # Co-located domains are intentionally outside this migration authority.
        connection.execute("CREATE TABLE cloud_share_placeholder(value TEXT)")

    receipt = ControlPlaneSchemaManager(database).migrate()
    ControlPlaneRepository(database, verifier=_Verifier())

    with sqlite3.connect(database) as connection:
        client = connection.execute(
            "SELECT current_version FROM control_clients WHERE client_id=?",
            ("client-existing",),
        ).fetchone()
        cloud_object = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='cloud_share_placeholder'"
        ).fetchone()
    assert receipt.source_schema_sha256 != receipt.target_schema_sha256
    assert client == ("0.3.0",)
    assert cloud_object == (1,)


def test_repository_rejects_tampered_index_without_repair(tmp_path) -> None:
    database = tmp_path / "tampered-control-plane.sqlite3"
    ControlPlaneSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_control_update_signals_created")
        connection.execute(
            "CREATE INDEX idx_control_update_signals_created "
            "ON control_update_signals(sequence)"
        )

    with pytest.raises(ControlPlaneSchemaError, match="fingerprint"):
        ControlPlaneRepository(database, verifier=_Verifier())

    with sqlite3.connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='idx_control_update_signals_created'"
        ).fetchone()[0]
    assert "created_at" not in sql


def test_repository_rejects_tampered_migration_checksum(tmp_path) -> None:
    database = tmp_path / "tampered-history-control-plane.sqlite3"
    ControlPlaneSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='control_schema_migrations_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER control_schema_migrations_no_update")
        connection.execute(
            "UPDATE control_schema_migrations SET migration_checksum=? WHERE version=1",
            ("f" * 64,),
        )
        connection.execute(trigger_sql)

    with pytest.raises(ControlPlaneSchemaError, match="history is invalid"):
        ControlPlaneRepository(database, verifier=_Verifier())

    with sqlite3.connect(database) as connection:
        checksum = connection.execute(
            "SELECT migration_checksum FROM control_schema_migrations WHERE version=1"
        ).fetchone()[0]
    assert checksum == "f" * 64


def test_repository_rejects_future_history_without_writing(tmp_path) -> None:
    database = tmp_path / "future-control-plane.sqlite3"
    ControlPlaneSchemaManager(database).migrate()
    future_receipt = _canonical({"future": True})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO control_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,receipt_json,receipt_sha256,installed_at"
            ") VALUES(2,?,?,?,?,?,?,?)",
            (
                "future-control-plane-schema",
                "f" * 64,
                CONTROL_PLANE_SCHEMA_SHA256,
                CONTROL_PLANE_SCHEMA_SHA256,
                future_receipt,
                hashlib.sha256(future_receipt.encode("utf-8")).hexdigest(),
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(ControlPlaneSchemaError, match="newer"):
        ControlPlaneRepository(database, verifier=_Verifier())

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM control_schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,)]


def test_migration_rejects_unknown_managed_shape_without_repair(tmp_path) -> None:
    database = tmp_path / "unknown-control-plane.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE control_unknown_state(value TEXT NOT NULL)")

    with pytest.raises(ControlPlaneSchemaError, match="source shape is unknown"):
        ControlPlaneSchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    assert objects == {"control_unknown_state"}

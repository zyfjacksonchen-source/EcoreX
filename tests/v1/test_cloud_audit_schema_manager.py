from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from ecorex.control_plane import (
    CLOUD_AUDIT_SCHEMA_SHA256,
    CloudAuditRepository,
    CloudAuditSchemaError,
    CloudAuditSchemaManager,
    ControlPlaneRepository,
    ControlPlaneSchemaError,
    migrate_control_plane_database,
)
from ecorex.control_plane.audit_schema import CLOUD_AUDIT_CORE_SCHEMA_SQL


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


def _repository(database) -> CloudAuditRepository:
    return CloudAuditRepository(
        database,
        encryption_key=b"e" * 32,
        integrity_key=b"h" * 32,
    )


def test_cloud_audit_repository_requires_migration_without_creating_database(
    tmp_path,
) -> None:
    database = tmp_path / "uninitialized-cloud-audit.sqlite3"

    with pytest.raises(CloudAuditSchemaError, match="unavailable"):
        _repository(database)

    assert not database.exists()


def test_cloud_audit_migration_is_concurrent_versioned_and_core_compatible(
    tmp_path,
) -> None:
    database = tmp_path / "shared-control-plane.sqlite3"
    migrate_control_plane_database(database)

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda _index: CloudAuditSchemaManager(database).migrate(),
                range(4),
            )
        )
    audit = _repository(database)
    control = ControlPlaneRepository(database, verifier=_Verifier())

    assert all(receipt == receipts[0] for receipt in receipts)
    assert audit.schema_receipt == receipts[0]
    assert receipts[0].migration_version == 1
    assert receipts[0].target_schema_sha256 == CLOUD_AUDIT_SCHEMA_SHA256
    assert control.schema_receipt.migration_version == 1
    with sqlite3.connect(database) as connection:
        history = connection.execute(
            "SELECT migration_checksum,receipt_json,receipt_sha256 "
            "FROM cloud_audit_schema_migrations ORDER BY version"
        ).fetchall()
        managed_objects = connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE sql IS NOT NULL AND ("
            "name GLOB 'cloud_audit_*' OR name GLOB 'idx_cloud_audit_*')"
        ).fetchone()[0]
    assert len(history) == 1
    assert hashlib.sha256(history[0][1].encode("utf-8")).hexdigest() == history[0][2]
    assert json.loads(history[0][1])["migration_checksum"] == history[0][0]
    assert managed_objects == 15


def test_known_pre_authority_audit_is_adopted_without_losing_data(tmp_path) -> None:
    database = tmp_path / "legacy-audit.sqlite3"
    migrate_control_plane_database(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(CLOUD_AUDIT_CORE_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO cloud_audit_daily("
            "day_utc,category,event_type,record_count) VALUES(?,?,?,?)",
            ("2026-07-10", "artifact", "artifact.previewed", 7),
        )

    receipt = CloudAuditSchemaManager(database).migrate()
    _repository(database)
    ControlPlaneRepository(database, verifier=_Verifier())

    with sqlite3.connect(database) as connection:
        aggregate = connection.execute(
            "SELECT record_count FROM cloud_audit_daily WHERE day_utc='2026-07-10'"
        ).fetchone()
    assert receipt.source_schema_sha256 != receipt.target_schema_sha256
    assert aggregate == (7,)


def test_cloud_audit_validation_does_not_adopt_core_schema_authority(tmp_path) -> None:
    database = tmp_path / "independent-authorities.sqlite3"
    migrate_control_plane_database(database)
    CloudAuditSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_control_rollouts_active")

    audit = _repository(database)

    assert audit.schema_receipt.target_schema_sha256 == CLOUD_AUDIT_SCHEMA_SHA256
    with pytest.raises(ControlPlaneSchemaError, match="fingerprint"):
        ControlPlaneRepository(database, verifier=_Verifier())


def test_cloud_audit_rejects_tampered_index_without_repair(tmp_path) -> None:
    database = tmp_path / "tampered-audit.sqlite3"
    CloudAuditSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_cloud_audit_account_time")
        connection.execute(
            "CREATE INDEX idx_cloud_audit_account_time ON cloud_audit_records(audit_id)"
        )

    with pytest.raises(CloudAuditSchemaError, match="fingerprint"):
        _repository(database)

    with sqlite3.connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='idx_cloud_audit_account_time'"
        ).fetchone()[0]
    assert "created_at" not in sql


def test_cloud_audit_rejects_tampered_history_checksum(tmp_path) -> None:
    database = tmp_path / "tampered-audit-history.sqlite3"
    CloudAuditSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE name='cloud_audit_schema_migrations_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER cloud_audit_schema_migrations_no_update")
        connection.execute(
            "UPDATE cloud_audit_schema_migrations "
            "SET migration_checksum=? WHERE version=1",
            ("f" * 64,),
        )
        connection.execute(trigger_sql)

    with pytest.raises(CloudAuditSchemaError, match="history is invalid"):
        _repository(database)

    with sqlite3.connect(database) as connection:
        checksum = connection.execute(
            "SELECT migration_checksum FROM cloud_audit_schema_migrations "
            "WHERE version=1"
        ).fetchone()[0]
    assert checksum == "f" * 64


def test_cloud_audit_rejects_future_history_without_writing(tmp_path) -> None:
    database = tmp_path / "future-audit.sqlite3"
    CloudAuditSchemaManager(database).migrate()
    future_receipt = _canonical({"future": True})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cloud_audit_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,receipt_json,receipt_sha256,installed_at"
            ") VALUES(2,?,?,?,?,?,?,?)",
            (
                "future-cloud-audit-schema",
                "f" * 64,
                CLOUD_AUDIT_SCHEMA_SHA256,
                CLOUD_AUDIT_SCHEMA_SHA256,
                future_receipt,
                hashlib.sha256(future_receipt.encode("utf-8")).hexdigest(),
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(CloudAuditSchemaError, match="newer"):
        _repository(database)

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM cloud_audit_schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,)]


def test_cloud_audit_rejects_unknown_shape_without_repair(tmp_path) -> None:
    database = tmp_path / "unknown-audit.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cloud_audit_unknown(value TEXT NOT NULL)")

    with pytest.raises(CloudAuditSchemaError, match="source shape is unknown"):
        CloudAuditSchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    assert objects == {"cloud_audit_unknown"}

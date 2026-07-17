from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from ecorex.image_orchestrator import (
    SQLITE_IMAGE_SCHEMA_SHA256,
    SQLiteImageJobStore,
    SQLiteImageSchemaError,
    SQLiteImageSchemaManager,
)
from ecorex.image_orchestrator.sqlite_schema import (
    MIGRATION_001_CHECKSUM,
    MIGRATION_001_NAME,
    SQLITE_IMAGE_CORE_SCHEMA_SQL,
    SQLITE_IMAGE_SCHEMA_HISTORY_SQL,
    SQLiteImageSchemaReceipt,
    main as sqlite_image_schema_main,
)


_DOMAIN_TABLES = {
    "image_jobs",
    "image_scheduler_accounts",
    "image_inputs",
    "image_results",
    "image_usage",
    "image_events",
    "image_breakers",
    "image_recovery_requests",
}
_SCHEMA_TABLES = _DOMAIN_TABLES | {"image_schema_migrations"}
_SCHEMA_INDEXES = {
    "image_events_job_seq",
    "image_jobs_account_status",
    "image_jobs_model_status",
    "image_jobs_schedulable",
}
_SCHEMA_TRIGGERS = {
    "image_events_no_delete",
    "image_events_no_update",
    "image_inputs_no_delete",
    "image_inputs_no_update",
    "image_recovery_no_delete",
    "image_recovery_no_update",
    "image_results_no_delete",
    "image_results_no_update",
    "image_schema_migrations_no_delete",
    "image_schema_migrations_no_update",
    "image_usage_no_delete",
    "image_usage_no_update",
}


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_store_requires_explicit_migration_without_creating_database(tmp_path) -> None:
    database = tmp_path / "uninitialized-image.sqlite3"

    with pytest.raises(SQLiteImageSchemaError, match="unavailable"):
        SQLiteImageJobStore(database)

    assert not database.exists()
    assert not database.parent.joinpath("uninitialized-image.sqlite3-wal").exists()

    nested_database = tmp_path / "missing-parent" / "image.sqlite3"
    with pytest.raises(SQLiteImageSchemaError, match="unavailable"):
        SQLiteImageJobStore(nested_database)
    assert not nested_database.parent.exists()


def test_schema_inventory_constraints_and_receipt_are_exact(tmp_path) -> None:
    database = tmp_path / "exact-image.sqlite3"
    receipt = SQLiteImageSchemaManager(database).migrate()

    assert isinstance(receipt, SQLiteImageSchemaReceipt)
    assert receipt.migration_name == MIGRATION_001_NAME
    assert receipt.migration_checksum == MIGRATION_001_CHECKSUM
    with sqlite3.connect(database) as connection:
        objects = connection.execute(
            "SELECT type,name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        ).fetchall()
        by_type = {
            object_type: {name for row_type, name, _sql in objects if row_type == object_type}
            for object_type in ("table", "index", "trigger")
        }
        foreign_keys = {
            table: connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            for table in ("image_results", "image_usage", "image_events", "image_recovery_requests")
        }
        jobs_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='image_jobs'"
        ).fetchone()[0]
        inputs_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='image_inputs'"
        ).fetchone()[0]
        history_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='image_schema_migrations'"
        ).fetchone()[0]
        history = connection.execute(
            "SELECT version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,receipt_json,receipt_sha256,installed_at "
            "FROM image_schema_migrations"
        ).fetchall()

    assert by_type == {
        "table": _SCHEMA_TABLES,
        "index": _SCHEMA_INDEXES,
        "trigger": _SCHEMA_TRIGGERS,
    }
    assert all(len(rows) == 1 and rows[0][2] == "image_jobs" for rows in foreign_keys.values())
    normalized_jobs = " ".join(jobs_sql.split()).casefold()
    normalized_inputs = " ".join(inputs_sql.split()).casefold()
    normalized_history = " ".join(history_sql.split()).casefold()
    assert "check(weight > 0)" in normalized_jobs
    assert "unique(account_id, client_request_id)" in normalized_jobs
    assert "provider_idempotency_key text not null unique" in normalized_jobs
    assert "primary key(account_id,sha256)" in normalized_inputs
    assert "check(size_bytes > 0)" in normalized_inputs
    assert "check(version > 0)" in normalized_history
    assert "migration_name text not null unique" in normalized_history
    assert len(history) == 1
    stored = history[0]
    assert stored[0] == 1
    assert stored[1] == receipt.migration_name
    assert stored[2] == receipt.migration_checksum
    assert stored[3] == receipt.source_schema_sha256
    assert stored[4] == receipt.target_schema_sha256
    assert hashlib.sha256(stored[5].encode("utf-8")).hexdigest() == stored[6]
    assert json.loads(stored[5]) == receipt.to_dict()
    assert stored[7] == receipt.installed_at


def test_validate_never_changes_journal_and_migrate_reactivates_wal(tmp_path) -> None:
    database = tmp_path / "journal-recovery-image.sqlite3"
    manager = SQLiteImageSchemaManager(database)
    receipt = manager.migrate()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"

    assert manager.validate() == receipt
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    assert manager.migrate() == receipt
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_wal_activation_failure_is_recoverable_by_explicit_migrate(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "retry-wal-image.sqlite3"
    original = SQLiteImageSchemaManager._activate_wal
    calls = 0

    def fail_once(connection: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SQLiteImageSchemaError("injected WAL activation failure")
        original(connection)

    monkeypatch.setattr(
        SQLiteImageSchemaManager,
        "_activate_wal",
        staticmethod(fail_once),
    )
    manager = SQLiteImageSchemaManager(database)
    with pytest.raises(SQLiteImageSchemaError, match="injected WAL"):
        manager.migrate()

    # The schema/history transaction is durable even though the independent
    # journal activation failed.  A deployment retry must revalidate it and
    # retry WAL instead of returning early.
    receipt = manager.migrate()
    assert calls == 2
    assert SQLiteImageJobStore(database).schema_receipt == receipt
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_store_runtime_connections_require_existing_rw_database(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "store-rw-image.sqlite3"
    SQLiteImageSchemaManager(database).migrate()
    store = SQLiteImageJobStore(database)
    real_connect = sqlite3.connect
    calls: list[tuple[object, dict[str, object]]] = []

    def recording_connect(target, *args, **kwargs):
        calls.append((target, dict(kwargs)))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(
        "ecorex.image_orchestrator.sqlite_store.sqlite3.connect",
        recording_connect,
    )
    store.metrics()

    assert calls
    assert all("?mode=rw" in str(target) for target, _kwargs in calls)
    assert all(kwargs.get("uri") is True for _target, kwargs in calls)


def test_migration_is_concurrent_versioned_and_has_one_history_row(tmp_path) -> None:
    database = tmp_path / "image.sqlite3"

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _index: SQLiteImageSchemaManager(database).migrate(),
                range(8),
            )
        )
    store = SQLiteImageJobStore(database)

    assert all(receipt == receipts[0] for receipt in receipts)
    assert store.schema_receipt == receipts[0]
    assert receipts[0].migration_version == 1
    assert receipts[0].target_schema_sha256 == SQLITE_IMAGE_SCHEMA_SHA256
    with sqlite3.connect(database) as connection:
        history = connection.execute(
            "SELECT migration_checksum,receipt_json,receipt_sha256 "
            "FROM image_schema_migrations ORDER BY version"
        ).fetchall()
        domain_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name LIKE 'image_%' AND name!='image_schema_migrations'"
            )
        }
    assert len(history) == 1
    assert hashlib.sha256(history[0][1].encode("utf-8")).hexdigest() == history[0][2]
    assert json.loads(history[0][1])["migration_checksum"] == history[0][0]
    assert domain_tables == _DOMAIN_TABLES


def test_migration_serializes_schema_and_wal_phases_per_database(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "serialized-image.sqlite3"
    real_activate_wal = SQLiteImageSchemaManager._activate_wal
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def slow_activate_wal(connection):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.01)
            real_activate_wal(connection)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        SQLiteImageSchemaManager,
        "_activate_wal",
        staticmethod(slow_activate_wal),
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _index: SQLiteImageSchemaManager(database).migrate(),
                range(8),
            )
        )

    assert peak == 1
    assert all(receipt == receipts[0] for receipt in receipts)


def test_deployment_cli_migrates_then_validates(tmp_path, capsys) -> None:
    database = tmp_path / "image-cli.sqlite3"

    assert sqlite_image_schema_main(["migrate", str(database)]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["target_schema_sha256"] == SQLITE_IMAGE_SCHEMA_SHA256
    assert sqlite_image_schema_main(["validate", str(database)]) == 0
    validated = json.loads(capsys.readouterr().out)

    assert validated == migrated


def test_known_pre_authority_schema_is_adopted_without_losing_data(tmp_path) -> None:
    database = tmp_path / "pre-authority-image.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(SQLITE_IMAGE_CORE_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO image_jobs("
            "job_id,account_id,operation,model_id,size_class,weight,priority,"
            "client_request_id,request_fingerprint,request_json,status,max_attempts,"
            "fair_finish,available_at,deadline,provider_idempotency_key,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "img_existing_job",
                "tenant-001",
                "generate",
                "image-2",
                "1024x1024",
                1,
                0,
                "existing-request",
                "f" * 64,
                "{}",
                "queued",
                4,
                1.0,
                "2026-07-11T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
                "provider-existing",
                "2026-07-11T00:00:00+00:00",
                "2026-07-11T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO image_events(event_id,job_id,account_id,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "evt_existing",
                "img_existing_job",
                "tenant-001",
                "image.queued",
                "{}",
                "2026-07-11T00:00:00+00:00",
            ),
        )

    receipt = SQLiteImageSchemaManager(database).migrate()
    SQLiteImageJobStore(database)

    with sqlite3.connect(database) as connection:
        job = connection.execute(
            "SELECT account_id,status FROM image_jobs WHERE job_id='img_existing_job'"
        ).fetchone()
        event = connection.execute(
            "SELECT event_type FROM image_events WHERE event_id='evt_existing'"
        ).fetchone()
        history_count = connection.execute(
            "SELECT COUNT(*) FROM image_schema_migrations"
        ).fetchone()[0]
    assert receipt.source_schema_sha256 != receipt.target_schema_sha256
    assert job == ("tenant-001", "queued")
    assert event == ("image.queued",)
    assert history_count == 1


def test_same_name_object_tamper_is_rejected_without_repair(tmp_path) -> None:
    database = tmp_path / "tampered-image.sqlite3"
    SQLiteImageSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX image_jobs_model_status")
        connection.execute(
            "CREATE INDEX image_jobs_model_status ON image_jobs(status)"
        )

    with pytest.raises(SQLiteImageSchemaError, match="fingerprint"):
        SQLiteImageJobStore(database)
    with pytest.raises(SQLiteImageSchemaError, match="fingerprint"):
        SQLiteImageSchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='image_jobs_model_status'"
        ).fetchone()[0]
    assert "model_id" not in sql


def test_history_tamper_is_rejected_without_repair(tmp_path) -> None:
    database = tmp_path / "tampered-history-image.sqlite3"
    SQLiteImageSchemaManager(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER image_schema_migrations_no_update")
        connection.execute(
            "UPDATE image_schema_migrations SET migration_checksum=? WHERE version=1",
            ("f" * 64,),
        )
        connection.executescript(SQLITE_IMAGE_SCHEMA_HISTORY_SQL)

    with pytest.raises(SQLiteImageSchemaError, match="history is invalid"):
        SQLiteImageJobStore(database)

    with sqlite3.connect(database) as connection:
        checksum = connection.execute(
            "SELECT migration_checksum FROM image_schema_migrations WHERE version=1"
        ).fetchone()[0]
    assert checksum == "f" * 64


def test_future_history_is_rejected_without_writing(tmp_path) -> None:
    database = tmp_path / "future-image.sqlite3"
    SQLiteImageSchemaManager(database).migrate()
    future_receipt = _canonical({"future": True})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO image_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,receipt_json,receipt_sha256,installed_at"
            ") VALUES(2,?,?,?,?,?,?,?)",
            (
                "future-sqlite-image-schema",
                "f" * 64,
                SQLITE_IMAGE_SCHEMA_SHA256,
                SQLITE_IMAGE_SCHEMA_SHA256,
                future_receipt,
                hashlib.sha256(future_receipt.encode("utf-8")).hexdigest(),
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(SQLiteImageSchemaError, match="newer"):
        SQLiteImageJobStore(database)

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM image_schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,)]


def test_unknown_pre_authority_shape_is_rejected_without_repair(tmp_path) -> None:
    database = tmp_path / "unknown-image.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(SQLITE_IMAGE_CORE_SCHEMA_SQL)
        connection.execute("DROP TRIGGER image_events_no_delete")

    with pytest.raises(SQLiteImageSchemaError, match="source shape is unknown"):
        SQLiteImageSchemaManager(database).migrate()

    with sqlite3.connect(database) as connection:
        history = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='image_schema_migrations'"
        ).fetchone()
        missing = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='image_events_no_delete'"
        ).fetchone()
    assert history is None
    assert missing is None

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import ecorex.control_plane.share_schema as share_schema_module

from ecorex.control_plane import (
    CLOUD_SHARE_SCHEMA_SHA256,
    CloudShareKeyRing,
    CloudShareMediaMigrationReceipt,
    CloudShareRepository,
    CloudShareSchemaError,
    CloudShareSchemaManager,
    LocalShareObjectStore,
    ShareObjectCapacityError,
    ShareObjectError,
    finalize_cloud_share_media_objects,
    migrate_cloud_share_database,
    migrate_cloud_share_media_objects,
    prepare_cloud_share_media_objects,
    validate_cloud_share_database,
)
from ecorex.control_plane.share_schema import (
    LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256,
    LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL,
    LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SQL,
    main as share_schema_main,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"bounded-legacy-media"
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()
NOW = "2026-07-11T00:00:00+00:00"


def keyring() -> CloudShareKeyRing:
    return CloudShareKeyRing(active_key_id="test", keys={"test": b"k" * 32})


def seed_legacy_blob(
    database: Path,
    *,
    rows: int = 1,
    content: bytes = PNG,
    digest: str | None = None,
    mime_type: str = "image/png",
) -> None:
    declared = digest or hashlib.sha256(content).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO cloud_share_operations VALUES(?,?,?,?,?,?,?)",
            ("op-preserved", "account-0", "operation-0", "publish", "share-0", "f" * 64, NOW),
        )
        for index in range(rows):
            connection.execute(
                "INSERT INTO cloud_share_media("
                "account_id,source_share_id,media_id,idempotency_key,kind,mime_type,"
                "size_bytes,sha256,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"account-{index}",
                    f"shr_{index}",
                    f"preview-{index}",
                    f"upload-{index}",
                    "preview",
                    mime_type,
                    len(content),
                    declared,
                    content,
                    NOW,
                ),
            )


def repository(database: Path, *, object_store=None) -> CloudShareRepository:
    return CloudShareRepository(
        database,
        keyring=keyring(),
        public_base_url="https://share.ecorex.test/s",
        object_store=object_store,
    )


def test_repository_requires_migration_and_never_recreates_deleted_storage(tmp_path) -> None:
    database = tmp_path / "share.sqlite3"
    with pytest.raises(CloudShareSchemaError, match="unavailable"):
        repository(database)
    assert not database.exists()

    CloudShareSchemaManager(database, keyring=keyring()).migrate()
    shares = repository(database)
    database.unlink()
    with pytest.raises(CloudShareSchemaError, match="unavailable"):
        shares.reap_expired_media()
    assert not database.exists()


def test_concurrent_migration_has_one_history_row_and_always_reasserts_wal(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "share.sqlite3"
    activation_lock = threading.Lock()
    activation_calls = 0
    activate_wal = CloudShareSchemaManager._activate_wal

    def record_activation(connection) -> None:
        nonlocal activation_calls
        with activation_lock:
            activation_calls += 1
        activate_wal(connection)

    monkeypatch.setattr(
        CloudShareSchemaManager,
        "_activate_wal",
        staticmethod(record_activation),
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _index: CloudShareSchemaManager(database, keyring=keyring()).migrate(),
                range(8),
            )
        )
    assert all(receipt == receipts[0] for receipt in receipts)
    assert activation_calls == 8
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_schema_migrations").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].casefold() == "delete"
    assert CloudShareSchemaManager(database, keyring=keyring()).migrate() == receipts[0]
    assert activation_calls == 9
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"


def test_barrier_aligned_first_migrations_converge_without_wal_lock_failures(
    tmp_path,
) -> None:
    for round_index in range(12):
        database = tmp_path / f"share-concurrent-{round_index}.sqlite3"
        barrier = threading.Barrier(8)

        def migrate_once(_index: int):
            barrier.wait(timeout=5)
            return CloudShareSchemaManager(database, keyring=keyring()).migrate()

        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(migrate_once, range(8)))
        assert all(receipt == receipts[0] for receipt in receipts)
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cloud_share_schema_migrations"
            ).fetchone()[0] == 1
            assert (
                connection.execute("PRAGMA journal_mode").fetchone()[0].casefold()
                == "wal"
            )


def test_wal_activation_retries_lock_contention_and_has_a_hard_deadline(
    tmp_path,
    monkeypatch,
) -> None:
    class Cursor:
        def __init__(self, value: str) -> None:
            self.value = value

        def fetchone(self):
            return (self.value,)

    class TransientlyLockedConnection:
        def __init__(self) -> None:
            self.attempts = 0

        def execute(self, statement: str):
            if statement == "PRAGMA journal_mode=WAL":
                self.attempts += 1
                if self.attempts < 4:
                    raise sqlite3.OperationalError("database is locked")
                return Cursor("wal")
            assert statement == "PRAGMA journal_mode"
            return Cursor("wal")

    transient = TransientlyLockedConnection()
    CloudShareSchemaManager(tmp_path / "unused.sqlite3", keyring=keyring())._activate_wal(
        transient
    )
    assert transient.attempts == 4

    class PermanentlyLockedConnection:
        def __init__(self) -> None:
            self.attempts = 0

        def execute(self, statement: str):
            assert statement == "PRAGMA journal_mode=WAL"
            self.attempts += 1
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        share_schema_module,
        "_WAL_ACTIVATION_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        share_schema_module,
        "_WAL_ACTIVATION_MAX_RETRY_SECONDS",
        0.005,
    )
    locked = PermanentlyLockedConnection()
    started = time.monotonic()
    with pytest.raises(CloudShareSchemaError, match="WAL activation timed out"):
        CloudShareSchemaManager(
            tmp_path / "timeout-unused.sqlite3", keyring=keyring()
        )._activate_wal(locked)
    assert time.monotonic() - started < 0.5
    assert locked.attempts >= 2


def test_wal_activation_does_not_retry_non_lock_sqlite_failures(tmp_path) -> None:
    class BrokenConnection:
        def __init__(self) -> None:
            self.attempts = 0

        def execute(self, statement: str):
            assert statement == "PRAGMA journal_mode=WAL"
            self.attempts += 1
            raise sqlite3.OperationalError("disk I/O error")

    broken = BrokenConnection()
    with pytest.raises(CloudShareSchemaError, match="WAL activation failed"):
        CloudShareSchemaManager(
            tmp_path / "broken-unused.sqlite3", keyring=keyring()
        )._activate_wal(broken)
    assert broken.attempts == 1


def test_wrappers_and_python_module_cli_migrate_then_validate(tmp_path, capsys) -> None:
    direct = tmp_path / "direct.sqlite3"
    migrated = migrate_cloud_share_database(direct, keyring=keyring())
    assert validate_cloud_share_database(direct, keyring=keyring()) == migrated

    cli = tmp_path / "cli.sqlite3"
    assert share_schema_main(["migrate", str(cli)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["target_schema_sha256"] == CLOUD_SHARE_SCHEMA_SHA256
    result = subprocess.run(
        [sys.executable, "-m", "ecorex.control_plane.share_schema", "validate", str(cli)],
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == first


def test_validate_and_repository_constructor_never_change_journal_mode(tmp_path) -> None:
    database = tmp_path / "validate-only.sqlite3"
    receipt = CloudShareSchemaManager(database, keyring=keyring()).migrate()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].casefold() == "delete"
    assert CloudShareSchemaManager(database, keyring=keyring()).validate() == receipt
    assert repository(database).schema_receipt == receipt
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "delete"


def test_known_pre_authority_schema_is_adopted_but_unknown_shape_is_not_repaired(tmp_path) -> None:
    known = tmp_path / "known.sqlite3"
    with sqlite3.connect(known) as connection:
        connection.executescript(LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SQL)
    receipt = CloudShareSchemaManager(known, keyring=keyring()).migrate()
    assert receipt.target_schema_sha256 == CLOUD_SHARE_SCHEMA_SHA256

    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as connection:
        connection.executescript(LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL)
        connection.execute("DROP TRIGGER cloud_share_media_no_update")
    with pytest.raises(CloudShareSchemaError, match="source shape is unknown"):
        CloudShareSchemaManager(unknown, keyring=keyring()).migrate()
    with sqlite3.connect(unknown) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='cloud_share_schema_migrations'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='cloud_share_media_no_update'"
        ).fetchone() is None


def test_same_name_tamper_and_future_history_fail_closed_without_repair(tmp_path) -> None:
    tampered = tmp_path / "tampered.sqlite3"
    CloudShareSchemaManager(tampered, keyring=keyring()).migrate()
    with sqlite3.connect(tampered) as connection:
        connection.execute("DROP INDEX cloud_share_media_orphan_age")
        connection.execute(
            "CREATE INDEX cloud_share_media_orphan_age ON cloud_share_media(media_id)"
        )
    with pytest.raises(CloudShareSchemaError, match="fingerprint"):
        repository(tampered)
    with sqlite3.connect(tampered) as connection:
        assert "created_at" not in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='cloud_share_media_orphan_age'"
        ).fetchone()[0]

    future = tmp_path / "future.sqlite3"
    CloudShareSchemaManager(future, keyring=keyring()).migrate()
    encoded = json.dumps({"future": True}, separators=(",", ":"), sort_keys=True)
    with sqlite3.connect(future) as connection:
        connection.execute(
            "INSERT INTO cloud_share_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,transformed_rows,receipt_json,receipt_sha256,installed_at"
            ") VALUES(2,?,?,?,?,?,?,?,?)",
            (
                "future",
                "f" * 64,
                CLOUD_SHARE_SCHEMA_SHA256,
                CLOUD_SHARE_SCHEMA_SHA256,
                0,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                NOW,
            ),
        )
    with pytest.raises(CloudShareSchemaError, match="newer"):
        repository(future)
    with sqlite3.connect(future) as connection:
        assert connection.execute(
            "SELECT version FROM cloud_share_schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]


def test_two_phase_media_migration_preserves_data_deduplicates_and_is_idempotent(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    seed_legacy_blob(database, rows=2)
    objects = LocalShareObjectStore(tmp_path / "objects")

    with pytest.raises(CloudShareSchemaError, match="object migration command"):
        CloudShareSchemaManager(database, keyring=keyring()).migrate()
    with sqlite3.connect(database) as connection:
        assert _cloud_digest(connection) == LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256

    checkpoint = prepare_cloud_share_media_objects(database, object_store=objects)
    checkpoint_path = database.with_name(database.name + ".share-media-v1.checkpoint.json")
    assert checkpoint.total_bytes == len(PNG) * 2
    assert checkpoint_path.is_file()
    with sqlite3.connect(database) as connection:
        assert "content" in {
            row[1] for row in connection.execute("PRAGMA table_info(cloud_share_media)")
        }
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='cloud_share_schema_migrations'"
        ).fetchone() is None

    receipt = finalize_cloud_share_media_objects(database, object_store=objects)
    assert isinstance(receipt, CloudShareMediaMigrationReceipt)
    assert (receipt.migrated_rows, receipt.migrated_objects, receipt.migrated_bytes) == (
        2,
        1,
        len(PNG) * 2,
    )
    assert not checkpoint_path.exists()
    assert migrate_cloud_share_media_objects(database, object_store=objects) == receipt
    assert repository(database, object_store=objects).schema_receipt.source_schema_sha256 == (
        LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256
    )
    with sqlite3.connect(database) as connection:
        assert "content" not in {
            row[1] for row in connection.execute("PRAGMA table_info(cloud_share_media)")
        }
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 1
        assert connection.execute("SELECT ref_count FROM cloud_share_objects").fetchone()[0] == 2
        assert connection.execute(
            "SELECT action FROM cloud_share_operations WHERE operation_id='op-preserved'"
        ).fetchone() == ("publish",)
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_media_migrations").fetchone()[0] == 1


def test_media_history_tamper_and_future_version_are_rejected_without_repair(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    seed_legacy_blob(database)
    objects = LocalShareObjectStore(tmp_path / "objects")
    migrate_cloud_share_media_objects(database, object_store=objects)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER cloud_share_media_migrations_no_update")
        connection.execute(
            "UPDATE cloud_share_media_migrations SET migration_checksum=? WHERE version=1",
            ("f" * 64,),
        )
        connection.execute(
            "CREATE TRIGGER cloud_share_media_migrations_no_update "
            "BEFORE UPDATE ON cloud_share_media_migrations BEGIN "
            "SELECT RAISE(ABORT, 'cloud share media migration history is immutable'); END"
        )
    with pytest.raises(CloudShareSchemaError, match="media migration history is invalid"):
        repository(database, object_store=objects)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT migration_checksum FROM cloud_share_media_migrations WHERE version=1"
        ).fetchone()[0] == "f" * 64

    future = tmp_path / "future-media.sqlite3"
    seed_legacy_blob(future)
    future_objects = LocalShareObjectStore(tmp_path / "future-objects")
    migrate_cloud_share_media_objects(future, object_store=future_objects)
    encoded = json.dumps({"future": True}, separators=(",", ":"), sort_keys=True)
    with sqlite3.connect(future) as connection:
        connection.execute(
            "INSERT INTO cloud_share_media_migrations VALUES(?,?,?,?,?)",
            (2, "e" * 64, encoded, hashlib.sha256(encoded.encode()).hexdigest(), NOW),
        )
    with pytest.raises(CloudShareSchemaError, match="media schema is newer"):
        repository(future, object_store=future_objects)


def test_cas_failure_interruption_and_retry_never_partially_switch_metadata(tmp_path) -> None:
    class FaultStore:
        def __init__(self, root: Path) -> None:
            self.delegate = LocalShareObjectStore(root)
            self.fail_put = True
            self.fail_open = False

        def put(self, content, *, sha256, mime_type):
            if self.fail_put:
                raise ShareObjectError("injected put failure")
            return self.delegate.put(content, sha256=sha256, mime_type=mime_type)

        def open(self, object_key, *, sha256, size_bytes, mime_type):
            if self.fail_open:
                raise ShareObjectError("injected open failure")
            return self.delegate.open(
                object_key,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
            )

        def delete(self, object_key, *, sha256):
            return self.delegate.delete(object_key, sha256=sha256)

    database = tmp_path / "legacy.sqlite3"
    seed_legacy_blob(database)
    store = FaultStore(tmp_path / "objects")
    with pytest.raises(CloudShareSchemaError, match="CAS preparation failed"):
        migrate_cloud_share_media_objects(database, object_store=store)
    checkpoint_path = database.with_name(database.name + ".share-media-v1.checkpoint.json")
    assert not checkpoint_path.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT content FROM cloud_share_media").fetchone()[0] == PNG

    store.fail_put = False
    prepare_cloud_share_media_objects(database, object_store=store)
    store.fail_open = True
    with pytest.raises(CloudShareSchemaError, match="object is unavailable"):
        finalize_cloud_share_media_objects(database, object_store=store)
    assert checkpoint_path.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT content FROM cloud_share_media").fetchone()[0] == PNG

    store.fail_open = False
    # Simulates process restart: the orchestrator resumes from the durable
    # checkpoint and does not need another BLOB-to-object write.
    receipt = migrate_cloud_share_media_objects(database, object_store=store)
    assert receipt.migrated_rows == 1
    assert not checkpoint_path.exists()


def test_concurrent_media_migration_commits_one_receipt_and_one_history(tmp_path) -> None:
    database = tmp_path / "legacy-concurrent.sqlite3"
    seed_legacy_blob(database, rows=2)
    objects = LocalShareObjectStore(tmp_path / "objects", max_open_streams=16)
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(
            pool.map(
                lambda _index: migrate_cloud_share_media_objects(
                    database, object_store=objects
                ),
                range(6),
            )
        )
    assert all(receipt == receipts[0] for receipt in receipts)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cloud_share_schema_migrations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM cloud_share_media_migrations"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 1
def test_source_change_and_corrupt_blob_fail_closed_with_retry_evidence(tmp_path) -> None:
    database = tmp_path / "changed.sqlite3"
    seed_legacy_blob(database)
    objects = LocalShareObjectStore(tmp_path / "objects")
    prepare_cloud_share_media_objects(database, object_store=objects)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cloud_share_media VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "account-extra",
                "shr_extra",
                "preview-extra",
                "upload-extra",
                "preview",
                "image/png",
                len(PNG),
                PNG_SHA256,
                PNG,
                NOW,
            ),
        )
    with pytest.raises(CloudShareSchemaError, match="changed"):
        finalize_cloud_share_media_objects(database, object_store=objects)
    with sqlite3.connect(database) as connection:
        assert _cloud_digest(connection) == LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256

    corrupt = tmp_path / "corrupt.sqlite3"
    seed_legacy_blob(corrupt, digest="0" * 64)
    with pytest.raises(CloudShareSchemaError, match="integrity"):
        prepare_cloud_share_media_objects(
            corrupt,
            object_store=LocalShareObjectStore(tmp_path / "corrupt-objects"),
        )


def test_local_object_streams_are_constant_memory_bounded_and_release_slots(tmp_path) -> None:
    objects = LocalShareObjectStore(tmp_path / "objects", max_open_streams=1)
    stored = objects.put(PNG, sha256=PNG_SHA256, mime_type="image/png")

    first = objects.open(
        stored.object_key,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
    )
    assert first._handle.__class__.__name__ != "BytesIO"
    with pytest.raises(ShareObjectCapacityError, match="capacity"):
        objects.open(
            stored.object_key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            mime_type=stored.mime_type,
        )
    iterator = first.iter_range(0, stored.size_bytes - 1, chunk_bytes=4)
    assert next(iterator) == PNG[:4]
    iterator.close()  # client disconnect / cancelled StreamingResponse

    reopened = objects.open(
        stored.object_key,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
    )
    reopened.close()
    # Reclamation uses the same descriptor-stream verifier, not the artifact
    # CAS BytesIO helper that copies the entire object.
    objects._store(create=False).open = lambda _sha: (_ for _ in ()).throw(
        AssertionError("unbounded ContentAddressedStore.open must not be used")
    )
    assert objects.delete(stored.object_key, sha256=stored.sha256) is True


def _cloud_digest(connection: sqlite3.Connection) -> str:
    records = tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name LIKE 'cloud_share_%' AND sql IS NOT NULL ORDER BY type,name"
        )
    )
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildError,
    ReleaseBuilder,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from ecorex.runtime.storage_migrations import (
    STORAGE_MIGRATION_FILE_NAME,
    StorageMigrationError,
    StorageMigrationIdentity,
    StorageMigrationManifest,
    StorageMigrationReceipt,
    apply_live_storage_migration,
    current_storage_schema_sha256,
    dry_run_storage_migration,
    load_live_storage_migration_receipt,
    migration_receipt_path,
)
from ecorex.runtime.database import SCHEMA_VERSION, SQLiteDatabase
from ecorex.server import (
    ProductRuntimeConfig,
    ProductRuntimeConfigurationError,
    load_product_runtime,
)
from ecorex.server import config as product_config_module
from ecorex.update import Ed25519SignatureVerifier, ReleaseChannel, SlotStore
from tests.v1.test_product_runtime_entrypoint import (
    _config,
    _public,
    _sources,
    _stage_product,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_checksum(value: dict) -> str:
    unsigned = dict(value)
    unsigned.pop("receipt_digest", None)
    return hashlib.sha256(
        b"ecorex-storage-migration-receipt-v1\0" + _canonical(unsigned)
    ).hexdigest()


def _schema_sha256(connection: sqlite3.Connection) -> str:
    records = [
        {
            "type": object_type,
            "name": name,
            "table": table,
            "sql": " ".join(sql.split()),
        }
        for object_type, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE type IN ('table','index','trigger') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    return hashlib.sha256(_canonical(records)).hexdigest()


def _database_schema_sha256(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return _schema_sha256(connection)
    finally:
        connection.close()


def _current_plan(database: Path) -> StorageMigrationManifest:
    """Create a v2 no-step plan for the intentionally tiny test schema."""

    return StorageMigrationManifest.current(
        1,
        target_schema_sha256=_database_schema_sha256(database),
    )


def _upgrade_plan() -> StorageMigrationManifest:
    target = sqlite3.connect(":memory:")
    try:
        target.executescript(
            """
            CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE reports (report_id TEXT PRIMARY KEY, title TEXT NOT NULL);
            ALTER TABLE "reports" ADD COLUMN "summary" TEXT NOT NULL DEFAULT '';
            CREATE INDEX "idx_reports_summary" ON "reports" ("summary");
            """
        )
        target_schema_sha256 = _schema_sha256(target)
    finally:
        target.close()
    return StorageMigrationManifest.from_bytes(
        _canonical(
            {
                "schema_version": 2,
                "document_type": "ecorex.storage-migration-plan",
                "target_schema_version": 2,
                "target_schema_sha256": target_schema_sha256,
                "steps": [
                    {
                        "step_id": "runtime-v1-to-v2",
                        "from_schema_version": 1,
                        "to_schema_version": 2,
                        "operations": [
                            {
                                "op": "add_column",
                                "table": "reports",
                                "column": {
                                    "name": "summary",
                                    "type": "TEXT",
                                    "nullable": False,
                                    "primary_key": False,
                                    "default": "",
                                },
                            },
                            {
                                "op": "create_index",
                                "name": "idx_reports_summary",
                                "table": "reports",
                                "columns": ["summary"],
                                "unique": False,
                            },
                        ],
                    }
                ],
            }
        )
    )


def _future_product_plan() -> StorageMigrationManifest:
    """Advance only the logical version while preserving the full v1 schema."""

    return StorageMigrationManifest.from_bytes(
        _canonical(
            {
                "schema_version": 2,
                "document_type": "ecorex.storage-migration-plan",
                "target_schema_version": 2,
                "target_schema_sha256": current_storage_schema_sha256(),
                "steps": [
                    {
                        "step_id": "runtime-v1-to-v2",
                        "from_schema_version": 1,
                        "to_schema_version": 2,
                        "operations": [
                            {
                                "op": "create_index",
                                "name": "idx_runtime_meta_migration_probe",
                                "table": "runtime_meta",
                                "columns": ["value"],
                                "unique": False,
                            },
                            {
                                "op": "drop_index",
                                "name": "idx_runtime_meta_migration_probe",
                            },
                        ],
                    }
                ],
            }
        )
    )


def _identity() -> StorageMigrationIdentity:
    return StorageMigrationIdentity(
        release_id="release-stable-storage-test",
        build_digest=hashlib.sha256(b"storage-build").hexdigest(),
        artifact_id="core-windows-x64",
        artifact_sha256=hashlib.sha256(b"signed-core").hexdigest(),
    )


def test_product_schema_authorizer_requires_applied_signed_live_receipt(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    database = product["database"]
    SQLiteDatabase(database)
    slots = SlotStore(product["install_root"])
    slot_id = slots.pointers().current
    assert slot_id is not None
    manifest = slots.release_manifest(slot_id)
    artifact = manifest.artifact(product["artifact_id"])
    migration_manifest = StorageMigrationManifest.from_bytes(
        (product["payload"] / STORAGE_MIGRATION_FILE_NAME).read_bytes()
    )
    identity = StorageMigrationIdentity(
        release_id=manifest.release_id,
        build_digest=manifest.build_digest,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
    )
    receipt_root = database.parent / "migration-receipts"
    preflight = dry_run_storage_migration(
        database,
        manifest=migration_manifest,
        identity=identity,
        receipt_root=receipt_root,
        phase="live_preflight",
    )
    live = apply_live_storage_migration(
        database,
        manifest=migration_manifest,
        identity=identity,
        receipt_root=receipt_root,
        preflight=preflight,
    )
    authorizer = product_config_module._verified_applied_storage_schema_authorizer(
        database_path=database,
        receipt_root=receipt_root,
        install_root=product["install_root"],
        verifier=Ed25519SignatureVerifier(
            {"release-key": _public(product["release_private"])}
        ),
        platform="windows",
        architecture="x64",
    )
    assert authorizer(live.target_schema_version, live.target_schema_sha256)
    assert not authorizer(live.target_schema_version + 1, live.target_schema_sha256)
    assert not authorizer(live.target_schema_version, "0" * 64)

    # A local table mutation is not a signed schema successor even when an old
    # receipt remains present and checksum-valid.
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unsigned_extra(value TEXT)")
    assert not authorizer(live.target_schema_version, _database_schema_sha256(database))


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO runtime_meta(key, value) VALUES ('storage_schema_version', '1');
            CREATE TABLE reports (report_id TEXT PRIMARY KEY, title TEXT NOT NULL);
            INSERT INTO reports(report_id, title) VALUES ('report-1', 'Quarterly report');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_copy_on_write_and_live_use_one_declarative_plan_with_bound_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "migration-receipts"
    _database(database)
    manifest = _upgrade_plan()
    identity = _identity()

    admission = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
    )
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )

    source = sqlite3.connect(database)
    try:
        assert source.execute(
            "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
        ).fetchone() == ("1",)
        assert "summary" not in {
            row[1] for row in source.execute("PRAGMA table_info(reports)")
        }
    finally:
        source.close()
    assert admission.identity == identity
    assert admission.plan_sha256 == manifest.sha256
    assert admission.source_schema_version == 1
    assert admission.target_schema_version == 2
    assert admission.source_table_counts == {"reports": 1, "runtime_meta": 1}
    assert admission.target_table_counts == {"reports": 1, "runtime_meta": 1}
    assert admission.source_database_sha256 != admission.target_database_sha256
    assert len(admission.source_schema_sha256) == 64
    assert len(admission.target_schema_sha256) == 64
    assert admission.source_schema_sha256 != admission.target_schema_sha256
    assert admission.quick_check == "ok"
    assert admission.foreign_key_violations == 0

    live = apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    assert admission.plan_sha256 == preflight.plan_sha256 == live.plan_sha256
    assert live.source_database_sha256 == preflight.source_database_sha256
    assert (
        admission.source_schema_sha256
        == preflight.source_schema_sha256
        == live.source_schema_sha256
    )
    assert (
        admission.target_schema_sha256
        == preflight.target_schema_sha256
        == live.target_schema_sha256
    )
    assert live.matches(
        identity=identity,
        manifest=manifest,
        phase="live",
        source_schema_sha256=live.source_schema_sha256,
        target_schema_sha256=live.target_schema_sha256,
    )
    assert not live.matches(
        identity=identity,
        manifest=manifest,
        target_schema_sha256="0" * 64,
    )
    assert dict(live.source_table_counts) == dict(preflight.source_table_counts)
    target = sqlite3.connect(database)
    try:
        assert target.execute(
            "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
        ).fetchone() == ("2",)
        assert target.execute(
            "SELECT summary FROM reports WHERE report_id='report-1'"
        ).fetchone() == ("",)
        assert target.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' "
            "AND name='idx_reports_summary'"
        ).fetchone() == ("idx_reports_summary",)
    finally:
        target.close()
    stored = StorageMigrationReceipt.from_bytes(
        migration_receipt_path(receipts, identity, "live").read_bytes()
    )
    assert stored == live
    assert stored.identity.artifact_sha256 == identity.artifact_sha256


def test_admission_rejects_a_signed_target_digest_that_the_plan_does_not_build(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    _database(database)
    raw = _upgrade_plan().to_dict()
    raw["target_schema_sha256"] = "0" * 64
    manifest = StorageMigrationManifest.from_bytes(_canonical(raw))

    with pytest.raises(StorageMigrationError, match="signed manifest"):
        dry_run_storage_migration(
            database,
            manifest=manifest,
            identity=_identity(),
            receipt_root=receipts,
        )

    assert not receipts.exists()
    connection = sqlite3.connect(database)
    try:
        assert "summary" not in {
            row[1] for row in connection.execute("PRAGMA table_info(reports)")
        }
    finally:
        connection.close()


def test_schema_one_no_step_still_produces_real_integrity_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    _database(database)
    manifest = _current_plan(database)
    identity = _identity()
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    live = apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    assert preflight.source_database_sha256 == preflight.target_database_sha256
    assert preflight.source_schema_sha256 == preflight.target_schema_sha256
    assert live.source_schema_sha256 == live.target_schema_sha256
    assert live.source_schema_version == live.target_schema_version == 1
    assert live.quick_check == "ok"
    assert live.foreign_key_violations == 0
    assert migration_receipt_path(receipts, identity, "live").is_file()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO reports(report_id,title) VALUES ('report-2','Later data')"
        )
        connection.commit()
    finally:
        connection.close()
    assert load_live_storage_migration_receipt(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
    ) == live


def test_first_install_receipt_materializes_the_complete_product_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "new-product.sqlite3"
    receipts = tmp_path / "receipts"
    manifest = StorageMigrationManifest.current(SCHEMA_VERSION)
    identity = _identity()

    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    assert dict(preflight.source_table_counts) == {}
    assert preflight.source_database_sha256 != preflight.target_database_sha256
    assert preflight.source_schema_sha256 != preflight.target_schema_sha256
    assert {
        "runtime_meta",
        "events",
        "artifact_entities",
        "connector_schema",
        "runtime_permission_state",
        "share_snapshots",
        "runtime_update_state",
    } <= set(preflight.target_table_counts)

    live = apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    assert dict(live.target_table_counts) == dict(preflight.target_table_counts)
    assert live.target_schema_sha256 == preflight.target_schema_sha256
    SQLiteDatabase(database)
    connection = sqlite3.connect(database)
    try:
        product_digest = connection.execute(
            "SELECT value FROM runtime_meta WHERE key='product_schema_sha256'"
        ).fetchone()
    finally:
        connection.close()
    assert product_digest is not None and len(product_digest[0]) == 64


def test_schema_digest_canonicalizes_sql_whitespace_and_ignores_sqlite_objects(
    tmp_path: Path,
) -> None:
    first_database = tmp_path / "first.sqlite3"
    second_database = tmp_path / "second.sqlite3"
    _database(first_database)
    connection = sqlite3.connect(second_database)
    try:
        connection.executescript(
            """
            CREATE    TABLE runtime_meta
                (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO runtime_meta(key, value)
                VALUES ('storage_schema_version', '1');
            CREATE    TABLE reports
                (report_id TEXT PRIMARY KEY, title TEXT NOT NULL);
            INSERT INTO reports(report_id, title)
                VALUES ('report-1', 'Quarterly report');
            """
        )
        connection.commit()
    finally:
        connection.close()

    manifest = _current_plan(first_database)
    first = dry_run_storage_migration(
        first_database,
        manifest=manifest,
        identity=_identity(),
        receipt_root=tmp_path / "first-receipts",
    )
    second = dry_run_storage_migration(
        second_database,
        manifest=manifest,
        identity=_identity(),
        receipt_root=tmp_path / "second-receipts",
    )
    assert first.source_schema_sha256 == second.source_schema_sha256
    assert first.target_schema_sha256 == second.target_schema_sha256


def test_live_receipt_rejects_schema_drift_without_row_or_version_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    _database(database)
    manifest = _current_plan(database)
    identity = _identity()
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    connection = sqlite3.connect(database)
    try:
        before_counts = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM runtime_meta),"
                "(SELECT COUNT(*) FROM reports)"
            ).fetchone()
        )
        connection.execute("CREATE INDEX idx_reports_title ON reports(title)")
        connection.commit()
        after_counts = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM runtime_meta),"
                "(SELECT COUNT(*) FROM reports)"
            ).fetchone()
        )
    finally:
        connection.close()
    assert after_counts == before_counts

    with pytest.raises(StorageMigrationError, match="schema no longer matches"):
        load_live_storage_migration_receipt(
            database,
            manifest=manifest,
            identity=identity,
            receipt_root=receipts,
        )


def test_receipt_schema_digest_fields_are_canonical_and_tamper_evident(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    _database(database)
    manifest = _current_plan(database)
    identity = _identity()
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    live = apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    raw = live.to_dict()
    assert set(raw) >= {"source_schema_sha256", "target_schema_sha256"}

    for invalid in ("F" * 64, "0" * 63):
        malformed = dict(raw)
        malformed["target_schema_sha256"] = invalid
        malformed["receipt_digest"] = _receipt_checksum(malformed)
        with pytest.raises(StorageMigrationError, match="digest is invalid"):
            StorageMigrationReceipt.from_bytes(_canonical(malformed))

    missing = dict(raw)
    missing.pop("source_schema_sha256")
    missing["receipt_digest"] = _receipt_checksum(missing)
    with pytest.raises(StorageMigrationError, match="fields are invalid"):
        StorageMigrationReceipt.from_bytes(_canonical(missing))

    changed = dict(raw)
    changed["target_schema_sha256"] = (
        "0" * 64 if live.target_schema_sha256 != "0" * 64 else "1" * 64
    )
    changed["receipt_digest"] = _receipt_checksum(changed)
    receipt_path = migration_receipt_path(receipts, identity, "live")
    receipt_path.write_bytes(_canonical(changed))
    with pytest.raises(StorageMigrationError, match="does not match the candidate"):
        load_live_storage_migration_receipt(
            database,
            manifest=manifest,
            identity=identity,
            receipt_root=receipts,
        )


def test_live_receipt_does_not_mask_later_database_integrity_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO runtime_meta(key, value)
            VALUES ('storage_schema_version', '1');
            CREATE TABLE parents (parent_id TEXT PRIMARY KEY);
            CREATE TABLE children (
                child_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL REFERENCES parents(parent_id)
            );
            """
        )
        connection.commit()
    finally:
        connection.close()
    manifest = _current_plan(database)
    identity = _identity()
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    apply_live_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=receipts,
        preflight=preflight,
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO children(child_id, parent_id) VALUES ('child-1', 'missing')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageMigrationError, match="integrity checks failed"):
        load_live_storage_migration_receipt(
            database,
            manifest=manifest,
            identity=identity,
            receipt_root=receipts,
        )


def test_receipt_root_link_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    _database(database)
    outside = tmp_path / "outside"
    outside.mkdir()
    receipts = tmp_path / "receipts"
    try:
        receipts.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(StorageMigrationError, match="real directory"):
        dry_run_storage_migration(
            database,
            manifest=_current_plan(database),
            identity=_identity(),
            receipt_root=receipts,
        )
    assert tuple(outside.iterdir()) == ()


def test_live_application_fails_closed_when_storage_changes_after_preflight(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    _database(database)
    manifest = _upgrade_plan()
    identity = _identity()
    preflight = dry_run_storage_migration(
        database,
        manifest=manifest,
        identity=identity,
        receipt_root=tmp_path / "receipts",
        phase="live_preflight",
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO reports(report_id, title) VALUES ('report-2', 'Changed')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageMigrationError, match="changed after"):
        apply_live_storage_migration(
            database,
            manifest=manifest,
            identity=identity,
            receipt_root=tmp_path / "receipts",
            preflight=preflight,
        )
    check = sqlite3.connect(database)
    try:
        assert check.execute(
            "SELECT value FROM runtime_meta WHERE key='storage_schema_version'"
        ).fetchone() == ("1",)
        assert "summary" not in {row[1] for row in check.execute("PRAGMA table_info(reports)")}
    finally:
        check.close()


def test_native_sqlite_plan_failure_is_a_redacted_migration_error(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    _database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "ALTER TABLE reports ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        StorageMigrationError,
        match="copy-on-write storage migration failed",
    ) as failure:
        dry_run_storage_migration(
            database,
            manifest=_upgrade_plan(),
            identity=_identity(),
            receipt_root=tmp_path / "receipts",
        )
    assert str(database) not in str(failure.value)


def test_runtime_preflight_is_pre_data_and_live_failure_is_roll_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _stage_product(tmp_path / "preflight")
    calls: list[str] = []

    def fail_preflight(*_args, **_kwargs):
        calls.append("preflight")
        raise StorageMigrationError("native path and secret must not escape")

    def barrier(*_args, **_kwargs):
        calls.append("barrier")
        return True

    monkeypatch.setattr(
        product_config_module,
        "load_live_storage_migration_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        product_config_module,
        "dry_run_storage_migration",
        fail_preflight,
    )
    monkeypatch.setattr(
        product_config_module.ProvisionalActivationController,
        "mark_data_barrier_crossed",
        barrier,
    )
    with pytest.raises(ProductRuntimeConfigurationError) as preflight_failure:
        load_product_runtime(
            payload_root=product["payload"],
            environment={"ECOREX_BOOTSTRAPPED": "1"},
            vault_factory=lambda: object(),
            host_platform=product["platform"],
            host_architecture=product["architecture"],
        )
    assert preflight_failure.value.stage_code == "storage_migration_preflight"
    assert calls == ["preflight"]
    assert "secret" not in str(preflight_failure.value)

    monkeypatch.undo()
    product = _stage_product(tmp_path / "live")
    calls.clear()
    original_dry_run = product_config_module.dry_run_storage_migration
    monkeypatch.setattr(
        product_config_module,
        "load_live_storage_migration_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        product_config_module,
        "dry_run_storage_migration",
        original_dry_run,
    )
    monkeypatch.setattr(
        product_config_module.ProvisionalActivationController,
        "mark_data_barrier_crossed",
        barrier,
    )

    def fail_live(*_args, **_kwargs):
        calls.append("live")
        raise StorageMigrationError("native path and secret must not escape")

    monkeypatch.setattr(
        product_config_module,
        "apply_live_storage_migration",
        fail_live,
    )
    with pytest.raises(ProductRuntimeConfigurationError) as live_failure:
        load_product_runtime(
            payload_root=product["payload"],
            environment={"ECOREX_BOOTSTRAPPED": "1"},
            vault_factory=lambda: object(),
            host_platform=product["platform"],
            host_architecture=product["architecture"],
        )
    assert live_failure.value.stage_code == "storage_migration_live"
    assert calls == ["barrier", "live"]
    assert "secret" not in str(live_failure.value)


def test_manifest_rejects_candidate_sql_or_noncanonical_json() -> None:
    raw_sql = {
        "schema_version": 2,
        "document_type": "ecorex.storage-migration-plan",
        "target_schema_version": 2,
        "target_schema_sha256": "0" * 64,
        "steps": [
            {
                "step_id": "unsafe",
                "from_schema_version": 1,
                "to_schema_version": 2,
                "operations": [{"op": "execute_sql", "sql": "DROP TABLE events"}],
            }
        ],
    }
    with pytest.raises(StorageMigrationError, match="unsupported"):
        StorageMigrationManifest.from_bytes(_canonical(raw_sql))
    with pytest.raises(StorageMigrationError, match="canonical"):
        StorageMigrationManifest.from_bytes(
            json.dumps(
                StorageMigrationManifest.current(SCHEMA_VERSION).to_dict(),
                indent=2,
            ).encode()
        )


def test_legacy_manifest_requires_an_explicit_non_product_boundary(
    tmp_path: Path,
) -> None:
    legacy = _canonical(
        {
            "schema_version": 1,
            "document_type": "ecorex.storage-migration-plan",
            "target_schema_version": 1,
            "steps": [],
        }
    )
    with pytest.raises(StorageMigrationError, match="explicit test boundary"):
        StorageMigrationManifest.from_bytes(legacy)

    parsed = StorageMigrationManifest.from_legacy_v1_bytes_for_test(legacy)
    assert parsed.schema_version == 1
    assert parsed.target_schema_sha256 is None
    assert parsed.to_bytes() == legacy
    with pytest.raises(StorageMigrationError, match="requires a v1 manifest"):
        StorageMigrationManifest.from_legacy_v1_bytes_for_test(
            StorageMigrationManifest.current(SCHEMA_VERSION).to_bytes()
        )

    database = tmp_path / "runtime.sqlite3"
    receipts = tmp_path / "receipts"
    _database(database)
    identity = _identity()
    with pytest.raises(StorageMigrationError, match="signed target schema digest"):
        dry_run_storage_migration(
            database,
            manifest=parsed,
            identity=identity,
            receipt_root=receipts,
        )

    bound = _current_plan(database)
    preflight = dry_run_storage_migration(
        database,
        manifest=bound,
        identity=identity,
        receipt_root=receipts,
        phase="live_preflight",
    )
    with pytest.raises(StorageMigrationError, match="signed target schema digest"):
        apply_live_storage_migration(
            database,
            manifest=parsed,
            identity=identity,
            receipt_root=receipts,
            preflight=preflight,
        )
    with pytest.raises(StorageMigrationError, match="signed target schema digest"):
        load_live_storage_migration_receipt(
            database,
            manifest=parsed,
            identity=identity,
            receipt_root=receipts,
        )


def test_product_and_candidate_loaders_reject_legacy_manifest_without_digest(
    tmp_path: Path,
) -> None:
    slot = tmp_path / "slot"
    payload = slot / "payload"
    payload.mkdir(parents=True)
    (payload / "runtime-config.json").write_bytes(b"{}")
    (payload / STORAGE_MIGRATION_FILE_NAME).write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "document_type": "ecorex.storage-migration-plan",
                "target_schema_version": 1,
                "steps": [],
            }
        )
    )

    assert not product_config_module._candidate_health_check(slot)
    with pytest.raises(StorageMigrationError, match="explicit test boundary"):
        product_config_module._load_storage_migration_manifest_from_payload(
            payload,
            stop=slot,
        )


def test_release_builder_injects_current_plan_and_rejects_invalid_override(
    tmp_path: Path,
) -> None:
    release_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / "bin/ecorex.exe").write_bytes(b"runtime")
    (core / "runtime-config.json").write_bytes(
        _config(_public(release_private), _public(session_private))
    )
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    javascript = b"document.body.dataset.ready='true';\n"
    digest = hashlib.sha256(javascript).hexdigest()
    asset = f"assets/app.{digest[:16]}.js"
    (dist / asset).write_bytes(javascript)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{asset}"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-11T00:00:00+00:00",
        sources=_sources(),
        artifacts=(
            ArtifactBuildInput(
                source_dir=core,
                kind=ArtifactKind.CORE,
                platform="windows",
                architecture="x64",
                executable_paths=("bin/ecorex.exe",),
                product_runtime=True,
            ),
        ),
        web_bundle=WebBundleBuildInput(dist),
        dependency_lock_sha256=hashlib.sha256(
            (Path(__file__).resolve().parents[2] / "requirements/locks/manifest.json").read_bytes()
        ).hexdigest(),
    )
    signer = Ed25519MemorySigner("release-key", release_private)
    built = ReleaseBuilder(signer).build(spec, tmp_path / "release")
    with zipfile.ZipFile(built.artifact_paths["core-windows-x64"]) as archive:
        embedded = archive.read(STORAGE_MIGRATION_FILE_NAME)
    embedded_manifest = StorageMigrationManifest.from_bytes(embedded)
    assert embedded_manifest == StorageMigrationManifest.current(SCHEMA_VERSION)
    assert embedded_manifest.schema_version == 2
    assert embedded_manifest.target_schema_sha256 == current_storage_schema_sha256()

    (core / STORAGE_MIGRATION_FILE_NAME).write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "document_type": "ecorex.storage-migration-plan",
                "target_schema_version": SCHEMA_VERSION,
                "steps": [],
            }
        )
    )
    with pytest.raises(ReleaseBuildError, match="legacy"):
        ReleaseBuilder(signer).build(spec, tmp_path / "legacy-release")

    wrong_target = StorageMigrationManifest.current(
        SCHEMA_VERSION,
        target_schema_sha256="0" * 64,
    )
    (core / STORAGE_MIGRATION_FILE_NAME).write_bytes(wrong_target.to_bytes())
    with pytest.raises(ReleaseBuildError, match="target schema digest"):
        ReleaseBuilder(signer).build(spec, tmp_path / "wrong-target-release")

    (core / STORAGE_MIGRATION_FILE_NAME).write_text(
        json.dumps(
            StorageMigrationManifest.current(SCHEMA_VERSION).to_dict(),
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReleaseBuildError, match="canonical"):
        ReleaseBuilder(signer).build(spec, tmp_path / "invalid-release")


def test_installed_runtime_admits_a_future_signed_schema_without_candidate_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Release construction runs in the future candidate build, where the
    # compiled database schema is v2. Admission below deliberately runs in
    # this installed v1 process and must not require candidate target == v1.
    from ecorex.runtime import database as runtime_database
    from ecorex.runtime import storage_migrations as storage_migrations_module

    release_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    install_root = tmp_path / "installed"
    database = install_root / "state/runtime.sqlite3"
    database.parent.mkdir(parents=True)
    SQLiteDatabase(database)
    future_plan = _future_product_plan()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / "bin/ecorex.exe").write_bytes(b"future-runtime")
    (core / "runtime-config.json").write_bytes(
        _config(_public(release_private), _public(session_private))
    )
    (core / STORAGE_MIGRATION_FILE_NAME).write_bytes(future_plan.to_bytes())
    sentinel = tmp_path / "candidate-code-was-executed"
    (core / "candidate_migration.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('unsafe', encoding='utf-8')\n",
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    javascript = b"document.body.dataset.ready='future';\n"
    digest = hashlib.sha256(javascript).hexdigest()
    asset = f"assets/app.{digest[:16]}.js"
    (dist / asset).write_bytes(javascript)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{asset}"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_database, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        storage_migrations_module,
        "current_storage_schema_sha256",
        lambda: future_plan.target_schema_sha256,
    )
    built = ReleaseBuilder(
        Ed25519MemorySigner("release-key", release_private)
    ).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-07-11T00:00:00+00:00",
            sources=_sources(),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=core,
                    kind=ArtifactKind.CORE,
                    platform="windows",
                    architecture="x64",
                    executable_paths=("bin/ecorex.exe",),
                    product_runtime=True,
                ),
            ),
            web_bundle=WebBundleBuildInput(dist),
            dependency_lock_sha256=hashlib.sha256(
                (Path(__file__).resolve().parents[2] / "requirements/locks/manifest.json").read_bytes()
            ).hexdigest(),
        ),
        tmp_path / "release",
    )
    artifact = built.manifest.artifact("core-windows-x64")
    slots = SlotStore(install_root)
    slot = slots.stage(
        built.artifact_paths[artifact.artifact_id],
        slot_id="slot-future-schema",
        manifest=built.manifest,
        artifact=artifact,
    )
    config = ProductRuntimeConfig.from_bytes(
        (slot / "payload/runtime-config.json").read_bytes()
    )
    assert product_config_module._migration_dry_run(
        database,
        slot,
        config=config,
        install_root=install_root,
        platform="windows",
        architecture="x64",
    )
    assert not sentinel.exists()
    receipt_files = tuple((database.parent / "migration-receipts").glob("*.json"))
    assert len(receipt_files) == 1
    receipt = StorageMigrationReceipt.from_bytes(receipt_files[0].read_bytes())
    assert receipt.phase == "admission_dry_run"
    assert receipt.source_schema_version == 1
    assert receipt.target_schema_version == 2
    assert receipt.target_schema_sha256 == future_plan.target_schema_sha256
    assert receipt.plan_sha256 == future_plan.sha256


def test_admission_reverifies_candidate_payload_before_writing_receipt(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    config = ProductRuntimeConfig.from_bytes(product["config"].read_bytes())
    assert product_config_module._migration_dry_run(
        product["database"],
        product["slot_path"],
        config=config,
        install_root=product["install_root"],
        platform="windows",
        architecture="x64",
    )
    receipt_root = product["database"].parent / "migration-receipts"
    before = {path.name: path.read_bytes() for path in receipt_root.iterdir()}

    executable = product["payload"] / "bin/ecorex.exe"
    executable.write_bytes(b"unsigned-candidate-mutation")
    assert not product_config_module._migration_dry_run(
        product["database"],
        product["slot_path"],
        config=config,
        install_root=product["install_root"],
        platform="windows",
        architecture="x64",
    )
    assert {path.name: path.read_bytes() for path in receipt_root.iterdir()} == before

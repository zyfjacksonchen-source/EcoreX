from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from ecorex.artifacts import ArtifactService
from ecorex.connectors import InMemoryCredentialVault
from ecorex.bootstrap.cli import _parser as bootstrap_parser
from ecorex.migration import (
    PRODUCT_MIGRATION_COMPLETION_NAME,
    PRODUCT_MIGRATION_PLAN_NAME,
    PRODUCT_MIGRATION_RECEIPT_NAME,
    ProductLegacyMigrationCoordinator,
    ProductMigrationError,
    MigrationQuarantineService,
    SourceLayoutError,
    TARGET_ARTIFACT_ROOT_NAME,
    TARGET_DATABASE_NAME,
    inventory_source,
    migrate_v030_to_v1,
    write_product_migration_plan,
)
from ecorex.update import InstallCoordinator, InstallState
from ecorex.migration.schema_identity import physical_schema_sha256
from tests.v1.test_migration_copy_on_write import _create_legacy_fixture
from tests.v1.test_product_runtime_entrypoint import _loader, _stage_product
from tests.v1.test_update_coordinator import (
    AcceptingTestVerifier,
    _fetcher,
    _manifest,
    _package,
)


def _product(tmp_path: Path, *, source_version: str = "0.3.0"):
    install = tmp_path / "install"
    state = install / "state"
    candidate = install / "slots" / "candidate-v1"
    source = tmp_path / "legacy"
    state.mkdir(parents=True)
    candidate.mkdir(parents=True)
    source.mkdir()
    _create_legacy_fixture(source)
    (state / "migration-receipts").mkdir()
    (state / "migration-receipts" / "admission.json").write_text(
        "{}\n", encoding="utf-8"
    )
    write_product_migration_plan(install, source, source_version=source_version)
    return install, state, candidate, source


def _count(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_bootstrap_accepts_an_installer_selected_legacy_source() -> None:
    arguments = bootstrap_parser().parse_args(
        [
            "--install-root",
            "install",
            "--trusted-public-key",
            "release-key=release.pub",
            "--legacy-v030-source",
            "legacy",
        ]
    )
    assert arguments.legacy_v030_source == "legacy"


def test_bootstrap_accepts_versioned_v0292_upgrade_source() -> None:
    arguments = bootstrap_parser().parse_args(
        [
            "--install-root",
            "install",
            "--trusted-public-key",
            "release-key=release.pub",
            "--legacy-source",
            "legacy-workspace",
            "--legacy-source-version",
            "0.2.9.2",
            "--legacy-release-evidence",
            "old-runtime/runtime-manifest.json",
        ]
    )
    assert arguments.legacy_source == "legacy-workspace"
    assert arguments.legacy_source_version == "0.2.9.2"
    assert arguments.legacy_release_evidence.endswith("runtime-manifest.json")


def test_product_import_dry_runs_then_swaps_verified_state(tmp_path: Path) -> None:
    install, state, candidate, source = _product(tmp_path)
    before = inventory_source(source)
    vault = InMemoryCredentialVault()
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )

    assert migration.dry_run(candidate, "transaction-dry-run") is True
    assert not (state / TARGET_DATABASE_NAME).exists()
    assert inventory_source(source) == before

    assert migration.commit(candidate, "transaction-commit") is True
    assert inventory_source(source) == before
    assert (state / TARGET_DATABASE_NAME).is_file()
    assert (state / TARGET_ARTIFACT_ROOT_NAME / "blobs").is_dir()
    assert _count(state / TARGET_DATABASE_NAME, "threads") == 1
    assert _count(state / TARGET_DATABASE_NAME, "turn_input_revisions") == 1
    receipt = json.loads((install / PRODUCT_MIGRATION_RECEIPT_NAME).read_text())
    assert receipt["state"] == "committed"
    assert receipt["slot_id"] == candidate.name
    assert receipt["transaction_id"] == "transaction-commit"
    assert receipt["quarantine_entry_count"] > 0
    completion = migration.completion_authority()
    assert completion is not None
    assert completion["data_generation_id"] == receipt["data_generation_id"]
    assert completion["target_schema_sha256"] == receipt["target_schema_sha256"]
    assert not (install / PRODUCT_MIGRATION_PLAN_NAME).exists()
    assert migration.cleanup_prior_state() is True
    assert not list((install / "migration").glob("v030-prior-state-*"))


def test_product_upgrade_preserves_v0292_generation_identity(tmp_path: Path) -> None:
    install, state, candidate, source = _product(
        tmp_path, source_version="0.2.9.2"
    )
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )

    assert migration.dry_run(candidate, "v0292-dry-run") is True
    assert migration.commit(candidate, "v0292-commit") is True

    completion = migration.completion_authority()
    assert completion is not None
    assert completion["source_version"] == "0.2.9.2"
    report = json.loads(
        (state / "migration-report.json").read_text(encoding="utf-8")
    )
    assert report["source_version"] == "0.2.9.2"
    assert _count(state / TARGET_DATABASE_NAME, "threads") == 1
    assert _count(state / TARGET_DATABASE_NAME, "project_thread_bindings") == 1


def test_source_change_after_dry_run_cannot_replace_live_state(tmp_path: Path) -> None:
    install, state, candidate, source = _product(tmp_path)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.dry_run(candidate) is True
    (source / "late-user-data.txt").write_text("changed after admission", encoding="utf-8")

    with pytest.raises(ProductMigrationError, match="differs from its dry-run"):
        migration.commit(candidate)

    assert not (state / TARGET_DATABASE_NAME).exists()
    assert (state / "migration-receipts" / "admission.json").is_file()
    assert (source / "late-user-data.txt").read_text(encoding="utf-8") == (
        "changed after admission"
    )


def test_directory_swap_recovers_after_process_death(tmp_path: Path) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()

    def crash(phase: str) -> None:
        if phase == "prior_state_renamed":
            raise KeyboardInterrupt("simulated installer process death")

    interrupted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
        fault_hook=crash,
    )
    interrupted.dry_run(candidate)
    with pytest.raises(KeyboardInterrupt):
        interrupted.commit(candidate)
    assert not state.exists()
    assert inventory_source(source).source_version == "0.3.0"

    recovered = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert recovered.commit(candidate) is True
    assert (state / TARGET_DATABASE_NAME).is_file()
    assert _count(state / TARGET_DATABASE_NAME, "migration_runs") == 1


def test_completed_import_restarts_after_legacy_source_is_removed(tmp_path: Path) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert migration.commit(candidate, "transaction-completed") is True
    completion_before = migration.completion_authority()
    receipt_before = json.loads((install / PRODUCT_MIGRATION_RECEIPT_NAME).read_text())
    shutil.rmtree(source)

    restarted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert restarted.has_plan is False
    assert restarted.has_completion is True
    assert restarted.commit(candidate) is True
    assert restarted.completion_authority() == completion_before
    assert json.loads((install / PRODUCT_MIGRATION_RECEIPT_NAME).read_text()) == receipt_before


def test_completion_persist_crash_consumes_stale_plan_without_source(
    tmp_path: Path,
) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()

    def crash(phase: str) -> None:
        if phase == "completion_persisted":
            raise KeyboardInterrupt("simulated crash after completion durability")

    interrupted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
        fault_hook=crash,
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.commit(candidate, "transaction-completion-crash")
    assert (install / PRODUCT_MIGRATION_COMPLETION_NAME).is_file()
    assert (install / PRODUCT_MIGRATION_PLAN_NAME).is_file()
    shutil.rmtree(source)

    recovered = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert recovered.commit(candidate) is True
    assert not (install / PRODUCT_MIGRATION_PLAN_NAME).exists()
    assert _count(state / TARGET_DATABASE_NAME, "threads") == 1


def test_activated_state_crash_recovers_after_legacy_source_is_removed(
    tmp_path: Path,
) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()

    def crash(phase: str) -> None:
        if phase == "migrated_state_activated":
            raise KeyboardInterrupt("simulated crash after state activation")

    interrupted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
        fault_hook=crash,
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.commit(candidate, "transaction-state-crash")
    assert (state / TARGET_DATABASE_NAME).is_file()
    assert not (install / PRODUCT_MIGRATION_COMPLETION_NAME).exists()
    shutil.rmtree(source)

    recovered = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert recovered.commit(candidate) is True
    assert recovered.has_completion is True
    assert recovered.has_plan is False
    assert _count(state / TARGET_DATABASE_NAME, "threads") == 1


def test_existing_target_requires_exact_activation_receipt(tmp_path: Path) -> None:
    install, state, candidate, source = _product(tmp_path)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.dry_run(candidate) is True
    other_source = tmp_path / "other-legacy"
    other_source.mkdir()
    _create_legacy_fixture(other_source)
    (other_source / "different.txt").write_text("different", encoding="utf-8")
    shutil.rmtree(state)
    migrate_v030_to_v1(
        other_source,
        state,
        quarantine_key=b"q" * 32,
    )

    with pytest.raises(ProductMigrationError, match="activation receipt"):
        migration.commit(candidate)


def test_completion_and_database_generation_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    install, state, candidate, _source = _product(tmp_path)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.commit(candidate) is True
    completion_path = install / PRODUCT_MIGRATION_COMPLETION_NAME
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    completion["authority_digest"] = "0" * 64
    completion_path.write_text(json.dumps(completion), encoding="utf-8")

    restarted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    with pytest.raises(ProductMigrationError, match="completion authority"):
        restarted.commit(candidate)


def test_completed_generation_is_global_and_does_not_block_the_next_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    import ecorex.migration.target_authority as target_authority

    stable_hash = target_authority.stable_sha256_file
    first_commit_blob_reads = 0

    def count_first_commit_blob_reads(path, **kwargs):
        nonlocal first_commit_blob_reads
        if "/blobs/" in Path(path).as_posix():
            first_commit_blob_reads += 1
        return stable_hash(path, **kwargs)

    monkeypatch.setattr(
        target_authority,
        "stable_sha256_file",
        count_first_commit_blob_reads,
    )
    assert migration.commit(candidate, "transaction-origin") is True
    assert first_commit_blob_reads > 0
    original = migration.completion_authority()
    assert original is not None
    shutil.rmtree(source)

    next_candidate = install / "slots" / "candidate-v1-1"
    next_candidate.mkdir()
    def no_blob_or_backup_rehash(path, **kwargs):
        normalized = Path(path).as_posix()
        if "/blobs/" in normalized or "/backups/" in normalized:
            raise AssertionError("completed generation re-hashed immutable bulk content")
        return stable_hash(path, **kwargs)

    monkeypatch.setattr(target_authority, "stable_sha256_file", no_blob_or_backup_rehash)
    restarted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert restarted.dry_run(next_candidate, "transaction-next") is True
    assert restarted.commit(next_candidate, "transaction-next") is True
    assert restarted.completion_authority() == original
    assert original["slot_id"] == candidate.name


def test_prepared_publish_crash_recovers_exact_target_without_legacy_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, state, candidate, source = _product(tmp_path)
    vault = InMemoryCredentialVault()
    import ecorex.migration.product as product_module

    real_migrate = product_module.migrate_v030_to_v1

    def publish_then_die(*args, **kwargs):
        result = real_migrate(*args, **kwargs)
        if not kwargs.get("dry_run", False):
            raise KeyboardInterrupt("simulated death after prepared publication")
        return result

    monkeypatch.setattr(product_module, "migrate_v030_to_v1", publish_then_die)
    interrupted = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.commit(candidate, "transaction-publish-crash")
    receipt = json.loads((install / PRODUCT_MIGRATION_RECEIPT_NAME).read_text())
    assert receipt["state"] == "publishing"
    assert (install / "migration" / "v030-imported-state" / TARGET_DATABASE_NAME).is_file()

    monkeypatch.setattr(product_module, "migrate_v030_to_v1", real_migrate)
    shutil.rmtree(source)
    recovered = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert recovered.commit(candidate, "transaction-publish-crash") is True
    assert recovered.has_completion is True
    assert recovered.has_plan is False


def test_product_plan_rejects_install_root_inside_source_before_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    _create_legacy_fixture(source)
    install = source / "v1-install"
    install.mkdir()
    before = inventory_source(source)

    with pytest.raises(ProductMigrationError, match="overlap"):
        write_product_migration_plan(install, source)

    assert inventory_source(source) == before
    assert not (install / PRODUCT_MIGRATION_PLAN_NAME).exists()


def test_windows_junction_or_posix_symlink_is_rejected_before_inventory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("must not be read", encoding="utf-8")
    linked = source / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("this Windows filesystem cannot create a directory junction")
    else:
        linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SourceLayoutError, match="symlink or reparse"):
        inventory_source(source)


def test_report_schema_and_raw_quarantine_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    install, state, candidate, _source = _product(tmp_path)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.commit(candidate, "transaction-tamper") is True

    report_path = state / "migration-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"] = {"tampered": 999}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ProductMigrationError, match="database is inconsistent"):
        migration.completion_authority()

    # Restore the exact report from the immutable migration ledger, then prove
    # that physical schema drift is independently rejected.
    connection = sqlite3.connect(state / TARGET_DATABASE_NAME)
    stored_report = connection.execute(
        "SELECT report_json FROM migration_runs"
    ).fetchone()[0]
    report_path.write_text(stored_report, encoding="utf-8")
    connection.execute("CREATE TABLE unexpected_schema_drift(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ProductMigrationError, match="database is inconsistent"):
        migration.completion_authority()


def test_quarantine_requires_product_deletion_tombstone(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw-delete"
    install, state, candidate, _source = _product(raw_root)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.commit(candidate, "transaction-raw-delete") is True
    (state / "quarantine" / "legacy-secrets.aesgcm").unlink()
    with pytest.raises(ProductMigrationError, match="file authority"):
        migration.completion_authority()

    authorised_root = tmp_path / "authorised-delete"
    install, state, candidate, _source = _product(authorised_root)
    authorised = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert authorised.commit(candidate, "transaction-authorised-delete") is True
    deleted = MigrationQuarantineService(state).delete(
        confirmed=True,
        client_request_id="delete-legacy-credentials-authorised-0001",
    )
    assert deleted.status == "deleted"
    assert authorised.completion_authority() is not None


def test_completion_allows_runtime_data_growth_without_expanding_imported_cas(
    tmp_path: Path,
) -> None:
    install, state, candidate, _source = _product(tmp_path)
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=InMemoryCredentialVault(),
    )
    assert migration.commit(candidate, "transaction-runtime-growth") is True
    ArtifactService(
        state / TARGET_ARTIFACT_ROOT_NAME,
        database_path=state / TARGET_DATABASE_NAME,
    ).create_artifact(
        b"%PDF-1.7\nnew v1 user artifact\n%%EOF\n",
        requested_name="新版办公产物.pdf",
        mime_type="application/pdf",
    )

    assert migration.completion_authority() is not None


def test_signed_storage_successor_authority_is_separate_from_legacy_completion(
    tmp_path: Path,
) -> None:
    install, state, candidate, _source = _product(tmp_path)
    vault = InMemoryCredentialVault()
    migration = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
    )
    assert migration.commit(candidate, "transaction-schema-origin") is True

    connection = sqlite3.connect(state / TARGET_DATABASE_NAME)
    connection.execute("CREATE TABLE signed_storage_successor(value TEXT)")
    connection.execute(
        "UPDATE runtime_meta SET value='2' WHERE key='storage_schema_version'"
    )
    successor_digest = physical_schema_sha256(connection)
    connection.commit()
    connection.close()

    with pytest.raises(ProductMigrationError, match="database is inconsistent"):
        migration.completion_authority()

    observed: list[tuple[int, str]] = []

    def signed_live_receipt(version: int, digest: str) -> bool:
        observed.append((version, digest))
        return (version, digest) == (2, successor_digest)

    authorised = ProductLegacyMigrationCoordinator(
        install,
        state / TARGET_DATABASE_NAME,
        vault=vault,
        storage_schema_authorizer=signed_live_receipt,
    )
    assert authorised.completion_authority() is not None
    assert observed == [(2, successor_digest)]


def test_install_coordinator_prepares_migration_before_slot_activation(
    tmp_path: Path,
) -> None:
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    calls: list[tuple[str, str]] = []
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        drainer=lambda: calls.append(("drain", "")) is None,
        migration_dry_run=lambda slot: calls.append(("dry_run", slot.name)) is None,
        migration_prepare=lambda slot, transaction_id: (
            calls.append(("prepare", f"{slot.name}:{transaction_id}")) is None
        ),
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=True,
    )
    prepared = coordinator.prepare_update(manifest, "core-windows-x64")

    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.HEALTHCHECKING
    assert [name for name, _value in calls] == ["drain", "dry_run", "prepare"]
    assert calls[-1][1].endswith(f":{prepared.transaction_id}")
    assert "migration_prepared" in {
        entry.event for entry in coordinator.journal.entries()
    }


def test_product_runtime_first_full_start_imports_before_opening_live_data(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path / "product")
    source = tmp_path / "legacy-runtime"
    source.mkdir()
    _create_legacy_fixture(source)
    write_product_migration_plan(product["install_root"], source)
    vault = InMemoryCredentialVault()

    composition = _loader(product, vault)(host="127.0.0.1", port=8765)
    try:
        assert product["database"].is_file()
        assert _count(product["database"], "threads") == 1
        receipt = json.loads(
            (product["install_root"] / PRODUCT_MIGRATION_RECEIPT_NAME).read_text()
        )
        assert receipt["state"] == "committed"
        assert receipt["slot_id"] == product["slot_path"].name
    finally:
        composition.close_unstarted()

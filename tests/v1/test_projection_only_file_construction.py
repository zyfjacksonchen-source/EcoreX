from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from ecorex.artifacts import ArtifactService
from ecorex.artifacts.actions import ArtifactActionExecutor
from ecorex.connectors import composition as connector_composition_module
from ecorex.connectors.composition import build_connector_composition
from ecorex.extensions.local_bundle import LocalSkillBundleStore
from ecorex.integration.artifact_events import ArtifactEventOutbox
from ecorex.integration.image_tools import RuntimeImageToolBackend
from ecorex.migration.quarantine import MigrationQuarantineService
from ecorex.observability.system import SystemObservabilityService
from ecorex.output import OutputLocationAlias, OutputService
from ecorex.runtime import RuntimeKernel, SQLiteDatabase
from ecorex.sharing import ShareRepository, ShareSnapshotService
from ecorex.update import InstallCoordinator, ReleaseChannel, RuntimeUpdateService
from ecorex.update.service import UpdateServiceError, UpdateStateRepository


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    records: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append((relative, "symlink", str(path.readlink())))
        elif path.is_dir():
            records.append((relative, "directory", ""))
        else:
            payload = path.read_bytes()
            records.append(
                (relative, "file", hashlib.sha256(payload).hexdigest())
            )
    return tuple(records)


def _update_rows(database: SQLiteDatabase) -> tuple[tuple[str, tuple], ...]:
    tables = (
        "runtime_update_activation_requests",
        "runtime_update_events",
        "runtime_update_signals",
        "runtime_update_state",
    )
    with database.reader() as connection:
        return tuple(
            (table, tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")))
            for table in tables
        )


def _coordinator(root: Path, *, create_storage: bool) -> InstallCoordinator:
    return InstallCoordinator(
        root,
        fetcher=object(),
        health_checker=lambda _path: True,
        host_platform="windows",
        host_architecture="x86_64",
        create_storage=create_storage,
    )


def test_projection_only_domain_construction_creates_no_files_or_directories(
    tmp_path,
    monkeypatch,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    kernel = RuntimeKernel(database)
    expected = _filesystem_snapshot(tmp_path)

    def unchanged(label: str) -> None:
        assert _filesystem_snapshot(tmp_path) == expected, label

    artifact_root = tmp_path / "artifacts"
    artifacts = ArtifactService(
        artifact_root,
        database_path=database.path,
        create_storage=False,
    )
    unchanged("ArtifactService")

    LocalSkillBundleStore(tmp_path / "extension-cas", create=False)
    unchanged("LocalSkillBundleStore")

    output_service = OutputService(
        artifact_service=artifacts,
        database_path=database.path,
        runtime_database_path=database.path,
        account_id="local-user",
        configured_roots={"workspace": tmp_path / "outputs"},
        default_alias=OutputLocationAlias.WORKSPACE,
        prepare_output_roots=False,
    )
    assert output_service.project_preference().location_alias is OutputLocationAlias.WORKSPACE
    unchanged("OutputService")

    ArtifactActionExecutor(artifacts, create_storage=False)
    unchanged("ArtifactActionExecutor")

    quarantine = MigrationQuarantineService(tmp_path)
    assert quarantine.status().status == "absent"
    unchanged("MigrationQuarantineService")

    ArtifactEventOutbox(database)
    unchanged("ArtifactEventOutbox")

    RuntimeImageToolBackend(
        database_path=database,
        artifacts=artifacts,
        kernel=kernel,
        account_id="local-user",
        client=None,
    )
    unchanged("RuntimeImageToolBackend")

    share_repository = ShareRepository(database, jobs=kernel.jobs)
    ShareSnapshotService(
        kernel,
        repository=share_repository,
        publisher=object(),
        account_id="local-user",
        allowed_public_hosts=frozenset({"share.example"}),
        artifacts=artifacts,
    )
    unchanged("Share")

    observability = SystemObservabilityService(database)
    observability.collect(persist=False)
    unchanged("SystemObservabilityService")

    updates = UpdateStateRepository(
        database,
        current_version="1.0.0",
        initialize=False,
    )
    assert updates.snapshot(can_activate=False).state == "idle"
    unchanged("UpdateStateRepository")

    coordinator = _coordinator(tmp_path / "updates", create_storage=False)
    runtime_updates = RuntimeUpdateService(
        database,
        coordinator=coordinator,
        feed=object(),
        artifact_id="ecorex-core-windows-x86_64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x86_64",
        initialize=False,
    )
    assert runtime_updates.snapshot().state == "idle"
    unchanged("InstallCoordinator/RuntimeUpdateService")

    def reject_production_vault() -> Any:
        raise AssertionError("projection-only composition selected the OS vault")

    monkeypatch.setattr(
        connector_composition_module,
        "production_credential_vault",
        reject_production_vault,
    )
    connectors = build_connector_composition(
        database_path=database.path,
        oauth_return_uri=(
            "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
        ),
        initialize=False,
    )
    assert connectors.service.catalog()
    unchanged("Connector composition")


def test_projection_only_services_read_existing_storage_without_writing(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    artifact_root = tmp_path / "artifacts"
    artifacts = ArtifactService(artifact_root, database_path=database.path)
    blob = artifacts.blobs.put_bytes(b"existing artifact")
    LocalSkillBundleStore(tmp_path / "extension-cas")
    output = OutputService(
        artifact_service=artifacts,
        database_path=database.path,
        runtime_database_path=database.path,
        configured_roots={"workspace": tmp_path / "outputs"},
        default_alias=OutputLocationAlias.WORKSPACE,
    )
    expected_preference = output.project_preference()
    ArtifactActionExecutor(artifacts)
    coordinator = _coordinator(tmp_path / "updates", create_storage=True)
    expected_pointers = coordinator.slots.pointers()
    expected = _filesystem_snapshot(tmp_path)

    read_only_artifacts = ArtifactService(
        artifact_root,
        database_path=database.path,
        create_storage=False,
    )
    assert read_only_artifacts.blobs.read_bytes(blob.sha256) == b"existing artifact"
    LocalSkillBundleStore(tmp_path / "extension-cas", create=False)
    read_only_output = OutputService(
        artifact_service=read_only_artifacts,
        database_path=database.path,
        runtime_database_path=database.path,
        configured_roots={"workspace": tmp_path / "outputs"},
        default_alias=OutputLocationAlias.WORKSPACE,
        prepare_output_roots=False,
    )
    assert read_only_output.project_preference() == expected_preference
    ArtifactActionExecutor(read_only_artifacts, create_storage=False)
    read_only_coordinator = _coordinator(
        tmp_path / "updates",
        create_storage=False,
    )
    assert read_only_coordinator.slots.pointers() == expected_pointers
    assert _filesystem_snapshot(tmp_path) == expected


def test_update_projection_only_state_is_pure_and_mutations_fail_until_converged(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    before = _update_rows(database)
    repository = UpdateStateRepository(
        database,
        current_version="1.0.0",
        initialize=False,
    )

    assert repository.snapshot(can_activate=True).state == "idle"
    with pytest.raises(UpdateServiceError, match="not initialized"):
        repository.set(state="idle", event_type="update.test")
    assert _update_rows(database) == before

    assert repository.converge_startup().state == "idle"
    converged = _update_rows(database)
    assert converged != before
    repository.converge_startup()
    assert _update_rows(database) == converged

    install_root = tmp_path / "updates"
    coordinator = _coordinator(install_root, create_storage=False)
    assert not install_root.exists()
    coordinator.converge_startup()
    assert (install_root / "slots").is_dir()
    assert (install_root / "transactions").is_dir()
    storage_converged = _filesystem_snapshot(install_root)
    coordinator.converge_startup()
    assert _filesystem_snapshot(install_root) == storage_converged

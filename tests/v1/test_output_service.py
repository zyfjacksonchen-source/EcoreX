from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecorex.artifacts import (
    ArtifactLineage,
    ArtifactProjection,
    ArtifactRole,
    ArtifactScope,
    ArtifactService,
    ArtifactStatus,
    ArtifactVisibility,
    QualityEvidence,
)
from ecorex.artifacts.models import ArtifactFamily
from ecorex.output import (
    MaterializationStatus,
    OutputArtifactNotEligible,
    OutputIdempotencyConflict,
    OutputLocationAlias,
    OutputPolicyBindingMissing,
    OutputRevisionConflict,
    OutputRootChanged,
    OutputRootUnsafe,
    OutputService,
    OutputValidationError,
    create_output_router,
)
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.event_store import EventStore
from ecorex.runtime.snapshots import RuntimeSnapshotRepository


def _roots(tmp_path: Path) -> dict[str, Path]:
    return {
        "documents": tmp_path / "documents",
        "downloads": tmp_path / "downloads",
        "workspace": tmp_path / "workspace",
    }


def _service(tmp_path: Path, *, fault_hook=None):
    database = tmp_path / "product.sqlite3"
    # Product composition owns the shared database bootstrap. Domain
    # repositories may add their tables only after the versioned core schema
    # exists; reversing this order would create an unversioned partial store.
    SQLiteDatabase(database)
    artifacts = ArtifactService(tmp_path / "artifact-cas", database_path=database)
    output = OutputService(
        artifact_service=artifacts,
        database_path=database,
        runtime_database_path=database,
        configured_roots=_roots(tmp_path),
        fault_hook=fault_hook,
    )
    return artifacts, output, database


def _pdf(artifacts: ArtifactService, content: bytes = b"%PDF-1.4\noffice"):
    return artifacts.create_artifact(
        content,
        requested_name="月度报告.pdf",
        mime_type="application/pdf",
    )


def test_preference_is_compare_and_swap_and_strictly_idempotent(tmp_path: Path) -> None:
    _artifacts, output, _database = _service(tmp_path)
    initial = output.get_preference()
    assert initial.revision == 1
    assert initial.location_alias is OutputLocationAlias.DOCUMENTS
    assert not any("path" in key or "root" in key for key in initial.to_dict())

    def same_request(_index: int):
        return output.set_preference(
            "downloads",
            expected_revision=initial.revision,
            client_request_id="preference-idempotent-request",
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(same_request, range(32)))
    assert {item.revision for item in results} == {2}
    assert {item.output_policy_snapshot_id for item in results} == {
        results[0].output_policy_snapshot_id
    }

    with pytest.raises(OutputIdempotencyConflict):
        output.set_preference(
            "workspace",
            expected_revision=initial.revision,
            client_request_id="preference-idempotent-request",
        )
    with pytest.raises(OutputRevisionConflict):
        output.set_preference(
            "workspace",
            expected_revision=initial.revision,
            client_request_id="preference-stale-request",
        )


def test_competing_preference_writers_only_advance_one_revision(tmp_path: Path) -> None:
    _artifacts, output, _database = _service(tmp_path)
    initial = output.get_preference()

    def update(index: int):
        try:
            return output.set_preference(
                "downloads",
                expected_revision=initial.revision,
                client_request_id=f"competing-preference-{index:03d}",
            )
        except OutputRevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(update, range(40)))
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert output.get_preference().revision == 2


def test_user_selected_workspace_root_is_private_and_restored_on_restart(
    tmp_path: Path,
) -> None:
    artifacts, output, database = _service(tmp_path)
    initial = output.get_preference()
    selected = tmp_path / "user-selected-office-output"

    preference = output.select_workspace_location(
        selected,
        expected_revision=initial.revision,
        client_request_id="pick-private-output-root",
    )

    assert preference.location_alias is OutputLocationAlias.WORKSPACE
    assert preference.revision == initial.revision + 1
    assert str(tmp_path) not in repr(preference.to_dict())
    output.close()

    restarted = OutputService(
        artifact_service=artifacts,
        database_path=database,
        runtime_database_path=database,
        configured_roots=_roots(tmp_path),
    )
    assert restarted.get_preference() == preference
    assert (
        restarted.filesystem.configured_root_path(OutputLocationAlias.WORKSPACE)
        == selected.resolve()
    )


def test_failed_workspace_selection_restores_the_previous_root(tmp_path: Path) -> None:
    _artifacts, output, _database = _service(tmp_path)
    initial = output.get_preference()
    output.set_preference(
        "downloads",
        expected_revision=initial.revision,
        client_request_id="advance-output-revision",
    )
    previous = output.filesystem.configured_root_path(OutputLocationAlias.WORKSPACE)

    with pytest.raises(OutputRevisionConflict):
        output.select_workspace_location(
            tmp_path / "must-not-remain-selected",
            expected_revision=initial.revision,
            client_request_id="stale-folder-selection",
        )

    assert output.filesystem.configured_root_path(OutputLocationAlias.WORKSPACE) == previous


def test_turn_accepted_config_freezes_policy_across_later_preference_change(
    tmp_path: Path,
) -> None:
    artifacts, output, database_path = _service(tmp_path)
    database = SQLiteDatabase(database_path)
    snapshots = RuntimeSnapshotRepository(database)
    events = EventStore(database)

    frozen = output.current_policy()
    config = snapshots.save(
        "config", {"output_policy_snapshot_id": frozen.output_policy_snapshot_id}
    )
    events.append(
        thread_id="thread-output-freeze",
        turn_id="turn-output-freeze",
        event_type="turn.accepted",
        config_snapshot_id=config.snapshot_id,
    )
    artifact = artifacts.create_artifact(
        b"%PDF-1.4\nfrozen",
        requested_name="冻结策略.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(
            thread_id="thread-output-freeze", turn_id="turn-output-freeze"
        ),
    )

    preference = output.get_preference()
    output.set_preference(
        "downloads",
        expected_revision=preference.revision,
        client_request_id="change-after-turn-accepted",
    )
    resolved = output.resolve_policy_for_artifact(
        artifact.artifact_id, artifact.revision_id
    )
    assert resolved.output_policy_snapshot_id == frozen.output_policy_snapshot_id
    receipt = output.materialize_artifact_revision(
        artifact.artifact_id,
        artifact.revision_id,
        client_request_id="materialize-frozen-turn",
    )
    assert receipt.location_alias is OutputLocationAlias.DOCUMENTS
    assert (_roots(tmp_path)["documents"] / receipt.display_name).read_bytes() == b"%PDF-1.4\nfrozen"
    assert not (_roots(tmp_path)["downloads"] / receipt.display_name).exists()


def test_modern_turn_without_policy_binding_never_falls_back_to_current(
    tmp_path: Path,
) -> None:
    artifacts, output, database_path = _service(tmp_path)
    database = SQLiteDatabase(database_path)
    config = RuntimeSnapshotRepository(database).save("config", {"legacy": True})
    EventStore(database).append(
        thread_id="thread-missing-output-policy",
        turn_id="turn-missing-output-policy",
        event_type="turn.accepted",
        config_snapshot_id=config.snapshot_id,
    )
    artifact = artifacts.create_artifact(
        b"%PDF-1.4\nmodern",
        requested_name="modern.pdf",
        mime_type="application/pdf",
        scope=ArtifactScope(
            thread_id="thread-missing-output-policy",
            turn_id="turn-missing-output-policy",
        ),
    )
    with pytest.raises(OutputPolicyBindingMissing):
        output.resolve_policy_for_artifact(artifact.artifact_id, artifact.revision_id)


class _BlobMap:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    def read_bytes(self, sha256: str, *, verify: bool = True) -> bytes:
        value = self.values[sha256]
        if verify:
            assert hashlib.sha256(value).hexdigest() == sha256
        return value


class _ProjectionRepository:
    def __init__(self, projections: dict[str, ArtifactProjection]) -> None:
        self.projections = projections

    def get_revision_projection(
        self,
        artifact_id: str,
        revision_id: str,
        *,
        include_internal: bool,
        account_id: str,
    ) -> ArtifactProjection:
        projection = self.projections[artifact_id]
        assert projection.revision_id == revision_id
        return projection


class _ArtifactDouble:
    def __init__(self, projections: dict[str, ArtifactProjection], blobs: dict[str, bytes]):
        self.repository = _ProjectionRepository(projections)
        self.blobs = _BlobMap(blobs)

    def get_artifact_scope(self, artifact_id: str) -> ArtifactScope:
        return ArtifactScope()


def test_one_hundred_concurrent_same_names_never_overwrite(tmp_path: Path) -> None:
    projections: dict[str, ArtifactProjection] = {}
    blobs: dict[str, bytes] = {}
    for index in range(100):
        content = f"%PDF-1.4\n{index}".encode()
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"artifact-{index:03d}"
        revision_id = f"revision-{index:03d}"
        blobs[digest] = content
        projections[artifact_id] = ArtifactProjection(
            artifact_id=artifact_id,
            revision_id=revision_id,
            family=ArtifactFamily.PDF,
            role=ArtifactRole.DELIVERABLE,
            visibility=ArtifactVisibility.PRIMARY,
            status=ArtifactStatus.READY,
            display_name="同名报告.pdf",
            mime_type="application/pdf",
            size_bytes=len(content),
            sha256=digest,
            created_at="2026-07-10T00:00:00Z",
            lineage=ArtifactLineage(),
            quality_evidence=QualityEvidence(),
        )
    artifact_double = _ArtifactDouble(projections, blobs)
    output = OutputService(
        artifact_service=artifact_double,  # type: ignore[arg-type]
        database_path=tmp_path / "output.sqlite3",
        configured_roots=_roots(tmp_path),
    )

    def materialize(index: int):
        return output.materialize_artifact_revision(
            f"artifact-{index:03d}",
            f"revision-{index:03d}",
            client_request_id=f"materialize-same-name-{index:03d}",
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        receipts = list(executor.map(materialize, range(100)))
    assert len({item.display_name.casefold() for item in receipts}) == 100
    files = [item for item in _roots(tmp_path)["documents"].iterdir() if item.is_file()]
    assert len(files) == 100
    assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == set(blobs)
    assert all(item.status is MaterializationStatus.COMPLETED for item in receipts)


def test_root_replacement_is_rejected_before_publication(tmp_path: Path) -> None:
    artifacts, output, _database = _service(tmp_path)
    artifact = _pdf(artifacts)
    output.current_policy()
    root = _roots(tmp_path)["documents"]
    root.rmdir()
    root.mkdir()
    with pytest.raises(OutputRootChanged):
        output.materialize_artifact_revision(
            artifact.artifact_id,
            artifact.revision_id,
            client_request_id="materialize-after-root-replacement",
        )
    assert list(root.iterdir()) == []


def test_root_swap_between_checks_is_rejected(tmp_path: Path) -> None:
    switched = threading.Event()
    root = _roots(tmp_path)["documents"]

    def swap(phase: str, _identity: str) -> None:
        if phase == "after_root_validation" and not switched.is_set():
            switched.set()
            root.rmdir()
            root.mkdir()

    artifacts, output, _database = _service(tmp_path, fault_hook=swap)
    artifact = _pdf(artifacts)
    with pytest.raises(OutputRootChanged):
        output.materialize_artifact_revision(
            artifact.artifact_id,
            artifact.revision_id,
            client_request_id="materialize-during-root-swap",
        )
    assert list(root.iterdir()) == []


def test_symlink_destination_is_never_followed(tmp_path: Path) -> None:
    artifacts, output, _database = _service(tmp_path)
    artifact = _pdf(artifacts)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"do-not-overwrite")
    destination = _roots(tmp_path)["documents"] / artifact.display_name
    try:
        os.symlink(outside, destination)
    except OSError:
        pytest.skip("this Windows account cannot create symbolic links")
    with pytest.raises(OutputRootUnsafe):
        output.materialize_artifact_revision(
            artifact.artifact_id,
            artifact.revision_id,
            client_request_id="materialize-symlink-destination",
        )
    assert outside.read_bytes() == b"do-not-overwrite"


def test_crash_after_exclusive_publish_recovers_on_restart(tmp_path: Path) -> None:
    crashed = threading.Event()

    def crash(phase: str, _identity: str) -> None:
        if phase == "after_publish" and not crashed.is_set():
            crashed.set()
            raise KeyboardInterrupt("simulated process loss after link publication")

    artifacts, output, database = _service(tmp_path, fault_hook=crash)
    artifact = _pdf(artifacts, b"%PDF-1.4\ncrash-safe")
    with pytest.raises(KeyboardInterrupt):
        output.materialize_artifact_revision(
            artifact.artifact_id,
            artifact.revision_id,
            client_request_id="crash-after-output-publish",
        )
    files = list(_roots(tmp_path)["documents"].iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"%PDF-1.4\ncrash-safe"

    recovered = OutputService(
        artifact_service=artifacts,
        database_path=database,
        runtime_database_path=database,
        configured_roots=_roots(tmp_path),
    )
    receipt = recovered.materialize_artifact_revision(
        artifact.artifact_id,
        artifact.revision_id,
        client_request_id="crash-after-output-publish",
    )
    assert receipt.status is MaterializationStatus.COMPLETED
    assert receipt.reused_existing is True
    assert len(list(_roots(tmp_path)["documents"].iterdir())) == 1
    actions = {item.action for item in recovered.list_audit()}
    assert "output.materialization.published" in actions
    assert "output.materialization.completed" in actions


def test_internal_source_artifacts_cannot_be_materialized(tmp_path: Path) -> None:
    artifacts, output, _database = _service(tmp_path)
    source = artifacts.create_artifact(
        b"print('internal')",
        requested_name="worker.py",
        mime_type="text/x-python",
    )
    assert source.visibility is ArtifactVisibility.INTERNAL
    with pytest.raises(OutputArtifactNotEligible):
        output.materialize_artifact_revision(
            source.artifact_id,
            source.revision_id,
            client_request_id="materialize-internal-source",
        )
    assert list(_roots(tmp_path)["documents"].iterdir()) == []


def test_product_cas_is_streamed_without_read_bytes_buffering(tmp_path: Path) -> None:
    artifacts, output, _database = _service(tmp_path)
    artifact = _pdf(artifacts, b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024))

    def forbidden_buffer_read(*_args, **_kwargs):
        raise AssertionError("materialization must stream from the verified CAS path")

    artifacts.blobs.read_bytes = forbidden_buffer_read  # type: ignore[method-assign]
    receipt = output.materialize_artifact_revision(
        artifact.artifact_id,
        artifact.revision_id,
        client_request_id="stream-large-cas-artifact",
    )
    assert receipt.status is MaterializationStatus.COMPLETED
    assert (_roots(tmp_path)["documents"] / receipt.display_name).stat().st_size == artifact.size_bytes


def test_restart_keeps_policy_identity_receipt_and_path_private(tmp_path: Path) -> None:
    artifacts, output, database = _service(tmp_path)
    artifact = _pdf(artifacts)
    first = output.materialize_artifact_revision(
        artifact.artifact_id,
        artifact.revision_id,
        client_request_id="restart-materialization-request",
    )
    restarted = OutputService(
        artifact_service=artifacts,
        database_path=database,
        runtime_database_path=database,
        configured_roots=_roots(tmp_path),
    )
    second = restarted.materialize_artifact_revision(
        artifact.artifact_id,
        artifact.revision_id,
        client_request_id="restart-materialization-request",
    )
    assert second == first
    serialized = second.to_dict()
    assert not any("path" in key or "root" in key for key in serialized)
    assert str(tmp_path) not in repr(serialized)
    assert all(str(tmp_path) not in repr(item.to_dict()) for item in restarted.list_audit())


def test_http_contract_accepts_aliases_only_and_never_returns_host_paths(
    tmp_path: Path,
) -> None:
    artifacts, output, _database = _service(tmp_path)
    artifact = _pdf(artifacts)
    app = FastAPI()
    app.include_router(create_output_router(output))
    client = TestClient(app)

    preference = client.get("/api/v1/output/preference")
    assert preference.status_code == 200
    assert str(tmp_path) not in preference.text
    assert client.put(
        "/api/v1/output/preference",
        json={
            "location_alias": "documents",
            "expected_revision": preference.json()["revision"],
            "client_request_id": "api-output-preference-noop",
            "raw_path": str(tmp_path / "attacker-selected"),
        },
    ).status_code == 422

    response = client.post(
        f"/api/v1/output/artifacts/{artifact.artifact_id}/materialize",
        json={
            "revision_id": artifact.revision_id,
            "client_request_id": "api-output-materialization",
        },
    )
    assert response.status_code == 200
    assert str(tmp_path) not in response.text
    assert response.json()["status"] == "completed"


def test_native_folder_picker_binds_workspace_without_returning_its_path(
    tmp_path: Path,
) -> None:
    _artifacts, output, _database = _service(tmp_path)
    selected = tmp_path / "private-picker-result"
    app = FastAPI()
    app.include_router(create_output_router(output, folder_picker=lambda: selected))
    client = TestClient(app)
    initial = client.get("/api/v1/output/preference").json()

    response = client.post(
        "/api/v1/output/locations/pick",
        json={
            "expected_revision": initial["revision"],
            "client_request_id": "native-picker-private-path",
        },
    )

    assert response.status_code == 200
    assert response.json()["location_alias"] == "workspace"
    assert str(tmp_path) not in response.text
    assert "path" not in response.text.lower()
    assert output.filesystem.configured_root_path(OutputLocationAlias.WORKSPACE) == selected.resolve()


def test_router_uses_service_bound_managed_account_not_local_user(
    tmp_path: Path,
) -> None:
    database = tmp_path / "managed-runtime.sqlite3"
    SQLiteDatabase(database)
    artifacts = ArtifactService(tmp_path / "managed-cas", database_path=database)
    output = OutputService(
        artifact_service=artifacts,
        database_path=database,
        runtime_database_path=database,
        configured_roots=_roots(tmp_path),
        account_id="managed-account-42",
    )
    app = FastAPI()
    app.include_router(create_output_router(output))
    response = TestClient(app).get("/api/v1/output/preference")
    assert response.status_code == 200
    assert response.json()["account_id"] == "managed-account-42"
    with sqlite3.connect(database) as connection:
        accounts = {
            row[0]
            for row in connection.execute(
                "SELECT account_id FROM output_preferences"
                ).fetchall()
        }
    assert accounts == set()
    initialized = output.get_preference()
    assert initialized.account_id == "managed-account-42"
    with sqlite3.connect(database) as connection:
        accounts = {
            row[0]
            for row in connection.execute(
                "SELECT account_id FROM output_preferences"
            ).fetchall()
        }
    assert accounts == {"managed-account-42"}
    with pytest.raises(OutputValidationError):
        output.get_preference(account_id="local-user")


def test_output_facts_cannot_use_a_database_outside_runtime(tmp_path: Path) -> None:
    runtime_database = tmp_path / "runtime.sqlite3"
    artifacts = ArtifactService(tmp_path / "cas", database_path=runtime_database)
    with pytest.raises(OutputValidationError, match="authoritative Runtime database"):
        OutputService(
            artifact_service=artifacts,
            database_path=tmp_path / "separate-output.sqlite3",
            runtime_database_path=runtime_database,
            configured_roots=_roots(tmp_path),
        )

    other_artifacts = ArtifactService(
        tmp_path / "other-cas", database_path=tmp_path / "other-artifacts.sqlite3"
    )
    with pytest.raises(OutputValidationError, match="Artifact and output facts"):
        OutputService(
            artifact_service=other_artifacts,
            database_path=runtime_database,
            runtime_database_path=runtime_database,
            configured_roots=_roots(tmp_path),
        )

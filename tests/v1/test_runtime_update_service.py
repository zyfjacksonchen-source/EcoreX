from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ecorex.protocol import ActivateUpdateResponse, UpdateSnapshot
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.activation_drain import (
    RuntimeActivationDrainController,
    RuntimeActivationDrainTimeout,
)
from ecorex.runtime.errors import LeaseError
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.kernel import RuntimeKernel
from ecorex.update import (
    InstallCoordinator,
    LocalSourceFetcher,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    RuntimeUpdateService,
    SignatureEnvelope,
    SourceKind,
    UpdateActivationUnavailable,
    UpdateAvailableSignal,
    UpdateStateConflict,
    UpdateStateRepository,
)


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


class Feed:
    def __init__(self, manifest):
        self.manifest = manifest
        self.calls = 0
        self.update_states: list[str] = []

    def latest(self, **kwargs):
        self.calls += 1
        self.update_states.append(kwargs["update_state"])
        return self.manifest


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"test-signature").decode(),
    )


def _package(version: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("runtime/version.txt", version)
        archive.writestr("web/assets/app.0123456789abcdef.js", "console.log('ready')")
    return stream.getvalue()


def _manifest(payload: bytes) -> ReleaseManifest:
    return ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.1-stable",
        version="1.0.1",
        build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.test/v1"),
            ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://github.test/v1"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.test/v1"),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def _coordinator(tmp_path, payload, **kwargs):
    directories = {}
    for source in ("mirror", "github", "cdn"):
        directory = tmp_path / source
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ecorex-core.zip").write_bytes(payload)
        directories[source] = directory
    return InstallCoordinator(
        tmp_path / "install",
        fetcher=LocalSourceFetcher(directories),
        verifier=AcceptingVerifier(),
        health_checker=lambda slot: (slot / "payload/runtime/version.txt").is_file(),
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
        **kwargs,
    )


def test_background_prepare_waits_for_user_then_activation_requests_restart(tmp_path) -> None:
    payload = _package("1.0.1")
    manifest = _manifest(payload)
    restarts: list[str] = []
    feed = Feed(manifest)
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=_coordinator(tmp_path, payload),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=restarts.append,
    )

    prepared = asyncio.run(service.check_now())

    assert prepared.state == "awaiting_user"
    assert prepared.can_activate is True
    assert prepared.target_version == "1.0.1"
    assert service.coordinator.slots.pointers().current is None

    response = asyncio.run(
        service.activate(
            transaction_id=prepared.transaction_id,
            client_request_id="activate-1",
        )
    )
    duplicate = asyncio.run(
        service.activate(
            transaction_id=prepared.transaction_id,
            client_request_id="activate-1",
        )
    )

    assert response == duplicate
    assert response.restart_scheduled is True
    assert response.update.state == "activating"
    assert response.update.requires_refresh is True
    assert restarts == [prepared.transaction_id]
    assert service.coordinator.slots.pointers().current is not None
    assert feed.update_states == [
        "idle",
        "downloading",
        "awaiting_user",
        "awaiting_user",
        "activating",
    ]

    after_restart = UpdateStateRepository(
        tmp_path / "runtime.db", current_version="1.0.1"
    ).snapshot(can_activate=True)
    assert after_restart.state == "idle"
    assert after_restart.target_version is None


@pytest.mark.parametrize(
    "state",
    ("available", "downloading", "awaiting_user", "activating", "failed"),
)
def test_restart_clears_stale_update_for_the_running_version(tmp_path, state: str) -> None:
    """The activated version must never continue advertising itself as new."""

    database = tmp_path / "runtime.db"
    digest = hashlib.sha256(b"build-1.0.5").hexdigest()
    before_restart = UpdateStateRepository(database, current_version="1.0.4")
    before_restart.set(
        state=state,
        event_type="update.test_stale_same_version",
        target_version="1.0.5",
        release_id="release-1.0.5-stable",
        build_digest=digest,
        transaction_id="transaction-1.0.5",
    )

    after_restart = UpdateStateRepository(database, current_version="1.0.5")
    snapshot = after_restart.snapshot(can_activate=True)

    assert snapshot.state == "idle"
    assert snapshot.target_version is None
    assert snapshot.can_activate is False


def test_activation_drain_timeout_preserves_staged_candidate_for_retry(tmp_path) -> None:
    payload = _package("1.0.1")
    database = tmp_path / "runtime.db"
    service = RuntimeUpdateService(
        database,
        coordinator=_coordinator(tmp_path, payload),
        feed=Feed(_manifest(payload)),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: True,
    )
    prepared = asyncio.run(service.check_now())
    assert prepared.transaction_id is not None
    staged_slot = service.coordinator.slots.slot_path(
        service.coordinator._load_active()["slot_id"]
    )

    kernel = RuntimeKernel(database)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="long-task-before-update",
    )
    leased = kernel.jobs.lease_next("long-task-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "long-task-worker", leased.lease_token)
    service.bind_runtime_activation_drainer(
        RuntimeActivationDrainController(
            kernel.jobs,
            gate,
            timeout_seconds=0.1,
            poll_seconds=0.01,
        )
    )

    with pytest.raises(RuntimeActivationDrainTimeout):
        asyncio.run(
            service.activate_verified_local(
                transaction_id=prepared.transaction_id,
                client_request_id="activation-before-long-task-checkpoint",
                execution_guard=lambda: None,
            )
        )

    assert service.snapshot().state == "awaiting_user"
    assert staged_slot.is_dir()
    assert service.coordinator.latest_state.value == "awaiting_user"
    assert gate.snapshot().healthy is True

    kernel.jobs.complete(
        leased.job_id,
        "long-task-worker",
        leased.lease_token,
    )
    response = asyncio.run(
        service.activate_verified_local(
            transaction_id=prepared.transaction_id,
            client_request_id="activation-after-long-task-checkpoint",
            execution_guard=lambda: None,
        )
    )
    assert response.update.requires_refresh is True


def test_atomic_pointer_interrupt_keeps_runtime_admission_drained(tmp_path) -> None:
    payload = _package("1.0.1")
    database = tmp_path / "runtime.db"
    coordinator = _coordinator(tmp_path, payload)
    service = RuntimeUpdateService(
        database,
        coordinator=coordinator,
        feed=Feed(_manifest(payload)),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: True,
    )
    prepared = asyncio.run(service.check_now())
    assert prepared.transaction_id is not None

    kernel = RuntimeKernel(database)
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    service.bind_runtime_activation_drainer(
        RuntimeActivationDrainController(
            kernel.jobs,
            gate,
            timeout_seconds=1,
            poll_seconds=0.01,
        )
    )
    original_switch = coordinator.slots.switch_to

    def switch_then_interrupt(slot_id):
        original_switch(slot_id)
        raise KeyboardInterrupt("simulated loss after atomic pointer switch")

    coordinator.slots.switch_to = switch_then_interrupt  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            service.activate_verified_local(
                transaction_id=prepared.transaction_id,
                client_request_id="activation-pointer-interrupt",
                execution_guard=lambda: None,
            )
        )

    assert coordinator.activation_boundary_crossed(prepared.transaction_id) is True
    assert gate.snapshot().draining is True
    assert gate.snapshot().healthy is False
    with pytest.raises(LeaseError, match="epoch is closed"):
        kernel.jobs.enqueue(
            kind="maintenance",
            payload={},
            idempotency_key="must-not-enter-after-pointer-switch",
        )


def test_verified_local_activation_rechecks_staged_state_without_cloud_feed(
    tmp_path,
) -> None:
    payload = _package("1.0.1")
    feed = Feed(_manifest(payload))
    restarts: list[str] = []
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=_coordinator(tmp_path, payload),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=restarts.append,
    )
    prepared = asyncio.run(service.check_now())
    assert prepared.transaction_id is not None
    feed_calls = feed.calls
    guard_calls = 0

    def execution_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1

    with pytest.raises(UpdateStateConflict, match="not awaiting"):
        asyncio.run(
            service.activate_verified_local(
                transaction_id="0" * 32,
                client_request_id="wrong-local-activation",
                execution_guard=execution_guard,
            )
        )

    response = asyncio.run(
        service.activate_verified_local(
            transaction_id=prepared.transaction_id,
            client_request_id="verified-local-activation",
            execution_guard=execution_guard,
        )
    )

    assert response.restart_scheduled is True
    assert response.update.state == "activating"
    assert service.coordinator.authorizes_local_pending(prepared.transaction_id) is False
    assert feed.calls == feed_calls
    assert restarts == [prepared.transaction_id]
    assert guard_calls >= 8


def test_activation_fails_closed_without_restart_controller(tmp_path) -> None:
    payload = _package("1.0.1")
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=_coordinator(tmp_path, payload),
        feed=Feed(_manifest(payload)),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
    )
    prepared = asyncio.run(service.check_now())
    assert prepared.can_activate is False
    with pytest.raises(UpdateActivationUnavailable):
        asyncio.run(
            service.activate(
                transaction_id=prepared.transaction_id,
                client_request_id="activate-no-restart",
            )
        )
    assert service.coordinator.slots.pointers().current is None


def test_activation_rechecks_rollout_after_background_download(tmp_path) -> None:
    payload = _package("1.0.1")
    feed = Feed(_manifest(payload))
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=_coordinator(tmp_path, payload),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: None,
    )
    prepared = asyncio.run(service.check_now())
    staged_slot = service.coordinator.slots.slot_path(
        service.coordinator._load_active()["slot_id"]
    )
    feed.manifest = None

    with pytest.raises(UpdateStateConflict, match="no longer authorized"):
        asyncio.run(
            service.activate(
                transaction_id=prepared.transaction_id,
                client_request_id="activation-after-kill-switch",
            )
        )

    assert service.coordinator.slots.pointers().current is None
    assert service.snapshot().state == "failed"
    assert not staged_slot.exists()


def test_activation_reauthorization_binds_the_exact_staged_manifest(tmp_path) -> None:
    payload = _package("1.0.1")
    original = _manifest(payload)
    feed = Feed(original)
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=_coordinator(tmp_path, payload),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: None,
    )
    prepared = asyncio.run(service.check_now())
    staged_slot = service.coordinator.slots.slot_path(
        service.coordinator._load_active()["slot_id"]
    )
    # Same release/version/build/artifact identity, but not the exact manifest
    # authorized and staged earlier.
    feed.manifest = replace(original, created_at="2026-07-10T13:00:00+08:00")

    with pytest.raises(UpdateStateConflict, match="no longer authorized"):
        asyncio.run(
            service.activate(
                transaction_id=prepared.transaction_id,
                client_request_id="activation-manifest-substitution",
            )
        )

    assert service.snapshot().state == "failed"
    assert service.coordinator.slots.pointers().current is None
    assert not staged_slot.exists()


def test_recovery_rechecks_kill_switch_before_a_reversible_pointer_switch(tmp_path) -> None:
    payload = _package("1.0.1")
    manifest = _manifest(payload)

    def crash_after_confirmation():
        raise KeyboardInterrupt("simulated process loss while draining")

    crashing = _coordinator(tmp_path, payload, drainer=crash_after_confirmation)
    prepared = crashing.prepare(manifest, "core-windows-x64")
    with pytest.raises(KeyboardInterrupt):
        crashing.activate(prepared.transaction_id)
    assert crashing.latest_state.value == "draining"

    repository = UpdateStateRepository(tmp_path / "runtime.db", current_version="1.0.0")
    repository.set(
        state="activating",
        event_type="update.activation_confirmed",
        target_version=manifest.version,
        release_id=manifest.release_id,
        build_digest=manifest.build_digest,
        transaction_id=prepared.transaction_id,
    )
    restarted = _coordinator(tmp_path, payload, drainer=lambda: True)
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=restarted,
        feed=Feed(None),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: None,
    )

    asyncio.run(service._recover())

    assert service.snapshot().state == "failed"
    assert restarted.latest_state.value == "failed"
    assert restarted.slots.pointers().current is None
    assert not prepared.slot_path.exists()


def test_recovery_rolls_forward_if_pointer_switched_before_kill_switch(tmp_path) -> None:
    payload = _package("1.0.1")
    manifest = _manifest(payload)
    crashing = _coordinator(tmp_path, payload)
    prepared = crashing.prepare(manifest, "core-windows-x64")
    original_switch = crashing.slots.switch_to

    def switch_then_crash(slot_id):
        original_switch(slot_id)
        raise KeyboardInterrupt("simulated loss after atomic pointer switch")

    crashing.slots.switch_to = switch_then_crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        crashing.activate(prepared.transaction_id)
    assert crashing.latest_state.value == "activating"
    assert crashing.slots.pointers().current == prepared.slot_id

    repository = UpdateStateRepository(tmp_path / "runtime.db", current_version="1.0.0")
    repository.set(
        state="activating",
        event_type="update.activation_confirmed",
        target_version=manifest.version,
        release_id=manifest.release_id,
        build_digest=manifest.build_digest,
        transaction_id=prepared.transaction_id,
    )
    restarted = _coordinator(tmp_path, payload)
    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=restarted,
        feed=Feed(None),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=lambda _transaction_id: None,
    )

    asyncio.run(service._recover())

    snapshot = service.snapshot()
    assert snapshot.state == "activating"
    assert snapshot.requires_refresh is True
    assert restarted.slots.pointers().current == prepared.slot_id
    assert prepared.slot_id in restarted.slots.pointers().known_good


def test_signal_deduplication_is_durable_and_identity_bound(tmp_path) -> None:
    repository = UpdateStateRepository(
        tmp_path / "runtime.db", current_version="1.0.0"
    )
    signal = UpdateAvailableSignal(
        event_id="signal-1",
        release_id="release-1.0.1-stable",
        version="1.0.1",
        build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
        channel=ReleaseChannel.STABLE,
    )
    assert repository.record_signal(signal) is True
    assert repository.record_signal(signal) is False
    with pytest.raises(UpdateStateConflict):
        repository.record_signal(
            UpdateAvailableSignal(
                event_id="signal-1",
                release_id=signal.release_id,
                version="1.0.2",
                build_digest=signal.build_digest,
                channel=signal.channel,
            )
        )


def test_durable_update_state_tampering_fails_closed(tmp_path) -> None:
    repository = UpdateStateRepository(
        tmp_path / "runtime.db", current_version="1.0.0"
    )
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runtime_update_state SET state = 'awaiting_user', "
            "target_version = NULL, transaction_id = NULL WHERE singleton = 1"
        )
    with pytest.raises(Exception, match="durable Runtime update state"):
        repository.snapshot(can_activate=True)


class FakeRuntimeUpdateService:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.checks = 0
        self.activations = []
        self.value = UpdateSnapshot(
            current_version="1.0.0",
            state="awaiting_user",
            target_version="1.0.1",
            release_id="release-1.0.1-stable",
            build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
            transaction_id="transaction-1",
            can_activate=True,
        )

    def snapshot(self):
        return self.value

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def check_now(self):
        self.checks += 1
        return self.value

    async def activate(self, *, transaction_id, client_request_id):
        self.activations.append((transaction_id, client_request_id))
        self.value = self.value.model_copy(
            update={"state": "activating", "can_activate": False, "requires_refresh": True}
        )
        return ActivateUpdateResponse(update=self.value, restart_scheduled=True)

    async def activate_verified_local(
        self,
        *,
        transaction_id,
        client_request_id,
        execution_guard,
    ):
        execution_guard()
        response = await self.activate(
            transaction_id=transaction_id,
            client_request_id=client_request_id,
        )
        execution_guard()
        return response


def test_runtime_update_api_is_authenticated_confirmed_and_lifecycle_owned(tmp_path) -> None:
    service = FakeRuntimeUpdateService()
    token = "r" * 43
    csrf = "c" * 43
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime-api.db",
            product_version="1.0.0",
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=("http://testserver",),
            update_service=service,
        )
    )
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {**auth, "Origin": "http://testserver", "X-EcoreX-CSRF": csrf}

    with TestClient(app) as client:
        assert service.started == 1
        bootstrap = client.get("/api/v1/bootstrap", headers=auth).json()
        assert bootstrap["update"]["state"] == "awaiting_user"
        checked = client.post("/api/v1/update/check", headers=mutation)
        assert checked.status_code == 200
        assert service.checks == 1
        rejected = client.post(
            "/api/v1/update/activate",
            json={
                "transaction_id": "transaction-1",
                "confirmed": False,
                "client_request_id": "activate-api",
            },
            headers=mutation,
        )
        assert rejected.status_code == 422
        activated = client.post(
            "/api/v1/update/activate",
            json={
                "transaction_id": "transaction-1",
                "confirmed": True,
                "client_request_id": "activate-api",
            },
            headers=mutation,
        )
        assert activated.status_code == 200
        assert activated.json()["restart_scheduled"] is True
        assert service.activations == [("transaction-1", "activate-api")]

    assert service.stopped == 1

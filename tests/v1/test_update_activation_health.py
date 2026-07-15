from __future__ import annotations

import asyncio
import hashlib
import json
import io
import zipfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecorex.bootstrap import (
    BootstrapReason,
    BootstrapSupervisor,
    RUNTIME_RESTART_EXIT_CODE,
    RuntimeEndpoint,
)
from ecorex.server.activation import ActivationProbeSettings, create_activation_probe_app
from ecorex.server.config import (
    ActivationProbeComposition,
    ProductRuntimeComposition,
    load_product_runtime,
)
from ecorex.server.cli import build_product_runtime_server
from ecorex.update import (
    ACTIVATION_NONCE_ENV,
    ACTIVATION_NONCE_HEADER,
    ACTIVATION_TRANSACTION_ENV,
    InstallCoordinator,
    InstallJournal,
    InstallState,
    ProvisionalActivationController,
    ReleaseChannel,
    RuntimeUpdateService,
    SlotPointers,
    SlotStore,
    Ed25519SignatureVerifier,
    activation_health_response,
    verify_activation_health_response,
)
from ecorex.update.storage import atomic_write_json
from tests.v1.test_update_coordinator import (
    AcceptingTestVerifier,
    _fetcher,
    _manifest,
)


def _package(version: str, *, invalid_web_digest: bool = False) -> bytes:
    output = io.BytesIO()
    config = {
        "paths": {
            "database": "state/runtime.sqlite3",
            "web_manifest": "web-manifest.json",
        }
    }
    executable = zipfile.ZipInfo("bin/ecorex.exe")
    executable.create_system = 3
    executable.external_attr = (0o100755) << 16
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(executable, f"runtime {version}")
        archive.writestr("runtime/version.txt", version)
        archive.writestr(
            "runtime-config.json",
            json.dumps(config, sort_keys=True, separators=(",", ":")),
        )
        archive.writestr(
            "web-manifest.json",
            json.dumps(
                {
                    "version": version,
                    "bundle_sha256": (
                        "invalid"
                        if invalid_web_digest
                        else hashlib.sha256(f"web:{version}".encode()).hexdigest()
                    ),
                }
            ),
        )
    return output.getvalue()


def _coordinator(root: Path, payload: bytes) -> InstallCoordinator:
    return InstallCoordinator(
        root / "install",
        fetcher=_fetcher(root, payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: (_ for _ in ()).throw(
            AssertionError("old Runtime must never health-confirm a candidate")
        ),
        host_platform="windows",
        host_architecture="x64",
    )


def _prepare(root: Path, version: str):
    payload = _package(version)
    manifest = _manifest(version, payload)
    coordinator = _coordinator(root, payload)
    prepared = coordinator.prepare_update(manifest, "core-windows-x64")
    return coordinator, prepared


def _confirm_and_cross_barrier(coordinator, prepared) -> None:
    coordinator.activate(prepared.transaction_id)
    intent = coordinator.activations.load_intent()
    assert intent is not None
    coordinator.activations.confirm(prepared.transaction_id, intent.health_identity)
    assert coordinator.activations.mark_data_barrier_crossed(prepared.slot_id) is True


def _prepare_upgrade(first, source_root: Path, version: str = "1.0.1"):
    payload = _package(version)
    manifest = _manifest(version, payload)
    coordinator = InstallCoordinator(
        first.root,
        fetcher=_fetcher(source_root, payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
    )
    prepared = coordinator.prepare_update(manifest, "core-windows-x64")
    return coordinator, prepared


class _Child:
    def __init__(self, exit_code: int, on_wait=None) -> None:
        self.exit_code = exit_code
        self.on_wait = on_wait
        self.signals: list[int] = []

    def wait(self) -> int:
        if self.on_wait is not None:
            callback, self.on_wait = self.on_wait, None
            callback()
        return self.exit_code

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)


class _Launcher:
    def __init__(self, children: list[_Child]) -> None:
        self.children = list(children)
        self.specs = []

    def start(self, spec):
        self.specs.append(spec)
        return self.children.pop(0)


class _Probe:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = []

    def probe(self, endpoint, activation, nonce) -> bool:
        self.calls.append((endpoint, activation, nonce))
        response = activation_health_response(activation.intent.health_identity, nonce)
        assert verify_activation_health_response(
            activation.intent.health_identity, nonce, response
        )
        return self.result


class _CrashProbe:
    def probe(self, _endpoint, _activation, _nonce) -> bool:
        raise KeyboardInterrupt("simulated Bootstrap crash during health probe")


def _supervisor(root: Path, launcher, probe) -> BootstrapSupervisor:
    return BootstrapSupervisor(
        root,
        endpoint=RuntimeEndpoint("127.0.0.1", 9451),
        verifier=AcceptingTestVerifier(),
        launcher=launcher,
        activation_health_probe=probe,
        host_platform="windows",
        host_architecture="x64",
    )


def test_old_runtime_stops_at_provisional_health_and_bootstrap_confirms(tmp_path: Path) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.HEALTHCHECKING
    pointers = coordinator.slots.pointers()
    assert pointers.current == prepared.slot_id
    assert prepared.slot_id not in pointers.known_good
    intent = coordinator.activations.load_intent()
    assert intent is not None
    assert intent.transaction_id == prepared.transaction_id
    assert coordinator.journal.latest().event == "slot_activated_provisionally"

    coordinator.activations.confirm(
        prepared.transaction_id,
        intent.health_identity,
    )
    assert coordinator.journal.latest().state is InstallState.COMPLETED
    assert prepared.slot_id in coordinator.slots.pointers().known_good
    assert not coordinator.activations.intent_path.exists()
    assert not coordinator.activations.active_path.exists()
    assert coordinator.activations.mark_data_barrier_crossed(prepared.slot_id) is True
    receipt = json.loads(coordinator.activations.receipt_path.read_text(encoding="utf-8"))
    assert receipt["data_barrier_crossed"] is True


def test_invalid_probe_identity_fails_before_pointer_switch(tmp_path: Path) -> None:
    payload = _package("1.0.0", invalid_web_digest=True)
    manifest = _manifest("1.0.0", payload)
    coordinator = _coordinator(tmp_path, payload)
    prepared = coordinator.prepare_update(manifest, "core-windows-x64")

    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert coordinator.slots.pointers() == SlotPointers()
    assert coordinator.journal.latest().state is InstallState.FAILED
    assert not coordinator.activations.intent_path.exists()


def test_runtime_update_service_requests_restart_while_candidate_is_still_provisional(
    tmp_path: Path,
) -> None:
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    coordinator = _coordinator(tmp_path, payload)
    restarts: list[str] = []

    class Feed:
        def latest(self, **_kwargs):
            return manifest

    service = RuntimeUpdateService(
        tmp_path / "runtime.sqlite3",
        coordinator=coordinator,
        feed=Feed(),
        artifact_id="core-windows-x64",
        current_version="0.3.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=restarts.append,
    )
    prepared = asyncio.run(service.check_now())
    response = asyncio.run(
        service.activate(
            transaction_id=prepared.transaction_id or "",
            client_request_id="activation-provisional-service",
        )
    )

    assert response.restart_scheduled is True
    assert restarts == [prepared.transaction_id]
    assert response.update.state == "activating"
    assert response.update.requires_refresh is True
    assert coordinator.journal.latest().state is InstallState.HEALTHCHECKING
    assert coordinator.slots.pointers().current not in coordinator.slots.pointers().known_good


def test_probe_app_never_exposes_nonce_and_gates_all_product_mutations(tmp_path: Path) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    intent = coordinator.activations.load_intent()
    assert intent is not None
    nonce = "N" * 43
    app = create_activation_probe_app(
        ActivationProbeSettings(
            host="127.0.0.1",
            port=9451,
            identity=intent.health_identity,
            nonce=nonce,
        )
    )
    client = TestClient(app, base_url="http://127.0.0.1:9451")

    denied = client.get("/api/v1/activation-health")
    assert denied.status_code == 403
    assert nonce not in denied.text
    spoofed = client.get(
        "/api/v1/activation-health",
        headers={ACTIVATION_NONCE_HEADER: "S" * 43},
    )
    assert spoofed.status_code == 403
    healthy = client.get(
        "/api/v1/activation-health",
        headers={ACTIVATION_NONCE_HEADER: nonce},
    )
    assert healthy.status_code == 200
    assert nonce not in healthy.text
    assert verify_activation_health_response(
        intent.health_identity,
        nonce,
        healthy.json(),
    )
    mutation = client.post("/api/v1/threads", json={"title": "must not run"})
    assert mutation.status_code == 503
    assert mutation.json()["code"] == "activation_health_pending"


def test_probe_process_watchdog_exits_when_bootstrap_parent_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.server.activation as activation_module

    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    intent = coordinator.activations.load_intent()
    assert intent is not None
    observed_parent = [4242]
    exits: list[int] = []
    monkeypatch.setattr(activation_module.os, "getppid", lambda: observed_parent[0])
    app = create_activation_probe_app(
        ActivationProbeSettings(
            host="127.0.0.1",
            port=9451,
            identity=intent.health_identity,
            nonce="W" * 43,
            parent_poll_seconds=0.05,
            watchdog_seconds=5.0,
            exit_process=lambda code: exits.append(code),
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:9451"):
        observed_parent[0] = 4343
        deadline = time.monotonic() + 1.0
        while not exits and time.monotonic() < deadline:
            time.sleep(0.02)
    assert exits == [70]


def test_bootstrap_probe_confirms_then_relaunches_full_known_good_runtime(
    tmp_path: Path,
) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    launcher = _Launcher(
        [
            _Child(0),
            _Child(
                0,
                on_wait=lambda: coordinator.activations.mark_data_barrier_crossed(
                    prepared.slot_id
                ),
            ),
        ]
    )
    probe = _Probe(True)
    # Activation already switched the pointer, as it would immediately before
    # the old Runtime exits with the dedicated restart code.
    coordinator.activate(prepared.transaction_id)

    result = _supervisor(tmp_path / "install", launcher, probe).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (prepared.slot_id, prepared.slot_id)
    assert len(probe.calls) == 1
    candidate, full = launcher.specs
    assert candidate.argv == full.argv
    assert ACTIVATION_TRANSACTION_ENV in candidate.environment
    assert ACTIVATION_NONCE_ENV in candidate.environment
    assert ACTIVATION_TRANSACTION_ENV not in full.environment
    assert ACTIVATION_NONCE_ENV not in full.environment
    assert candidate.environment[ACTIVATION_NONCE_ENV] not in repr(candidate)
    assert candidate.environment[ACTIVATION_NONCE_ENV] not in " ".join(candidate.argv)
    nonce = candidate.environment[ACTIVATION_NONCE_ENV]
    for record in (tmp_path / "install").rglob("*"):
        if record.is_file() and record.suffix in {".json", ".ndjson"}:
            assert nonce not in record.read_text(encoding="utf-8")
    assert launcher.children == []
    assert prepared.slot_id in coordinator.slots.pointers().known_good
    assert coordinator.journal.latest().state is InstallState.COMPLETED


def test_failed_or_spoofed_candidate_restores_and_relaunches_prior_known_good(
    tmp_path: Path,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)

    second_payload = _package("1.0.1")
    second_manifest = _manifest("1.0.1", second_payload)
    second = InstallCoordinator(
        first.root,
        fetcher=_fetcher(tmp_path / "v2", second_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
    )
    second_prepared = second.prepare_update(second_manifest, "core-windows-x64")
    second.activate(second_prepared.transaction_id)
    launcher = _Launcher([_Child(0), _Child(0)])

    result = _supervisor(first.root, launcher, _Probe(False)).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (second_prepared.slot_id, first_prepared.slot_id)
    pointers = SlotStore(first.root).pointers()
    assert pointers.current == first_prepared.slot_id
    assert first_prepared.slot_id in pointers.known_good
    assert second.journal.latest().state is InstallState.ROLLBACK
    assert launcher.specs[0].slot_id == second_prepared.slot_id
    assert launcher.specs[1].slot_id == first_prepared.slot_id


def test_first_install_probe_failure_leaves_no_untrusted_current_slot(tmp_path: Path) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    launcher = _Launcher([_Child(0)])

    result = _supervisor(tmp_path / "install", launcher, _Probe(False)).run()

    assert result.reason is BootstrapReason.RUNTIME_FAILED
    assert SlotStore(tmp_path / "install").pointers() == SlotPointers()
    assert coordinator.journal.latest().state is InstallState.ROLLBACK


def test_bootstrap_recovers_confirmation_crash_without_reprobing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    intent = coordinator.activations.load_intent()
    assert intent is not None
    original_append = coordinator.activations.journal.append

    def crash_before_completed(*, transaction_id, state, event, details=None):
        if state is InstallState.COMPLETED:
            raise KeyboardInterrupt("confirmation crash")
        return original_append(
            transaction_id=transaction_id,
            state=state,
            event=event,
            details=details,
        )

    monkeypatch.setattr(coordinator.activations.journal, "append", crash_before_completed)
    with pytest.raises(KeyboardInterrupt):
        coordinator.activations.confirm(prepared.transaction_id, intent.health_identity)
    assert prepared.slot_id in coordinator.slots.pointers().known_good
    assert coordinator.activations.intent_path.exists()

    launcher = _Launcher(
        [
            _Child(
                0,
                on_wait=lambda: coordinator.activations.mark_data_barrier_crossed(
                    prepared.slot_id
                ),
            )
        ]
    )
    probe = _Probe(True)
    result = _supervisor(tmp_path / "install", launcher, probe).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert probe.calls == []
    assert coordinator.journal.latest().state is InstallState.COMPLETED
    assert not coordinator.activations.intent_path.exists()


def test_intent_survives_crash_before_pointer_switch_and_bootstrap_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")

    def crash_before_switch(_transaction_id=None):
        raise KeyboardInterrupt("after intent before switch")

    monkeypatch.setattr(
        coordinator.activations,
        "ensure_pending_current",
        crash_before_switch,
    )
    with pytest.raises(KeyboardInterrupt):
        coordinator.activate(prepared.transaction_id)
    assert coordinator.slots.pointers() == SlotPointers()
    assert coordinator.activations.intent_path.exists()
    assert coordinator.journal.latest().state is InstallState.ACTIVATING

    launcher = _Launcher(
        [
            _Child(0),
            _Child(
                0,
                on_wait=lambda: coordinator.activations.mark_data_barrier_crossed(
                    prepared.slot_id
                ),
            ),
        ]
    )
    result = _supervisor(tmp_path / "install", launcher, _Probe(True)).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (prepared.slot_id, prepared.slot_id)
    assert prepared.slot_id in coordinator.slots.pointers().known_good


def test_health_identity_or_proof_spoof_is_rejected(tmp_path: Path) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    intent = coordinator.activations.load_intent()
    assert intent is not None
    nonce = "A" * 43
    response = activation_health_response(intent.health_identity, nonce)
    response["proof"] = "0" * 64
    assert not verify_activation_health_response(intent.health_identity, nonce, response)
    response = activation_health_response(intent.health_identity, nonce)
    response["identity"]["slot_id"] = "spoofed-slot"
    assert not verify_activation_health_response(intent.health_identity, nonce, response)


def test_bootstrap_crash_during_probe_keeps_durable_intent_for_fresh_nonce(
    tmp_path: Path,
) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)
    first_launcher = _Launcher([_Child(0)])
    with pytest.raises(KeyboardInterrupt):
        _supervisor(tmp_path / "install", first_launcher, _CrashProbe()).run()
    first_nonce = first_launcher.specs[0].environment[ACTIVATION_NONCE_ENV]
    assert coordinator.activations.intent_path.exists()
    assert prepared.slot_id not in coordinator.slots.pointers().known_good

    second_launcher = _Launcher(
        [
            _Child(0),
            _Child(
                0,
                on_wait=lambda: coordinator.activations.mark_data_barrier_crossed(
                    prepared.slot_id
                ),
            ),
        ]
    )
    result = _supervisor(
        tmp_path / "install", second_launcher, _Probe(True)
    ).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    second_nonce = second_launcher.specs[0].environment[ACTIVATION_NONCE_ENV]
    assert second_nonce != first_nonce
    assert prepared.slot_id in coordinator.slots.pointers().known_good


def test_candidate_process_launch_failure_rolls_back_before_relaunching_prior(
    tmp_path: Path,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)

    payload = _package("1.0.1")
    manifest = _manifest("1.0.1", payload)
    second = InstallCoordinator(
        first.root,
        fetcher=_fetcher(tmp_path / "v2", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
    )
    prepared = second.prepare_update(manifest, "core-windows-x64")
    second.activate(prepared.transaction_id)

    class FailCandidateOnce:
        def __init__(self) -> None:
            self.calls = 0
            self.specs = []

        def start(self, spec):
            self.calls += 1
            self.specs.append(spec)
            if self.calls == 1:
                raise OSError("candidate process failed before binding")
            return _Child(0)

    launcher = FailCandidateOnce()
    result = _supervisor(first.root, launcher, _Probe(True)).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert [spec.slot_id for spec in launcher.specs] == [
        prepared.slot_id,
        first_prepared.slot_id,
    ]
    assert second.slots.pointers().current == first_prepared.slot_id
    assert second.journal.latest().state is InstallState.ROLLBACK


def test_confirmed_candidate_exit_before_data_barrier_restores_prior(
    tmp_path: Path,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)
    second, prepared = _prepare_upgrade(first, tmp_path / "v2")
    second.activate(prepared.transaction_id)
    launcher = _Launcher([_Child(0), _Child(70), _Child(0)])

    result = _supervisor(first.root, launcher, _Probe(True)).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (
        prepared.slot_id,
        prepared.slot_id,
        first_prepared.slot_id,
    )
    assert second.slots.pointers().current == first_prepared.slot_id
    assert second.journal.latest().state is InstallState.ROLLBACK
    receipt = json.loads(second.activations.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "rolled_back_pre_data"
    assert receipt["data_barrier_crossed"] is False
    assert not second.slots.slot_path(prepared.slot_id).exists()
    assert (
        second.activations.mark_data_barrier_crossed(first_prepared.slot_id) is False
    )


def test_data_barrier_forces_roll_forward_after_confirmed_candidate_exit(
    tmp_path: Path,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)
    second, prepared = _prepare_upgrade(first, tmp_path / "v2")
    second.activate(prepared.transaction_id)
    launcher = _Launcher(
        [
            _Child(0),
            _Child(
                70,
                on_wait=lambda: second.activations.mark_data_barrier_crossed(
                    prepared.slot_id
                ),
            ),
        ]
    )

    result = _supervisor(first.root, launcher, _Probe(True)).run()

    assert result.reason is BootstrapReason.RUNTIME_FAILED
    assert result.launched_slots == (prepared.slot_id, prepared.slot_id)
    assert second.slots.pointers().current == prepared.slot_id
    assert prepared.slot_id in second.slots.pointers().known_good
    assert second.journal.latest().state is InstallState.COMPLETED
    receipt = json.loads(second.activations.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "confirmed"
    assert receipt["data_barrier_crossed"] is True


def test_confirmed_candidate_full_launch_failure_restores_prior(
    tmp_path: Path,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)
    second, prepared = _prepare_upgrade(first, tmp_path / "v2")
    second.activate(prepared.transaction_id)

    class FailFullCandidateOnce:
        def __init__(self) -> None:
            self.specs = []

        def start(self, spec):
            self.specs.append(spec)
            if len(self.specs) == 2:
                raise OSError("confirmed full Runtime failed before process creation")
            return _Child(0)

    launcher = FailFullCandidateOnce()
    result = _supervisor(first.root, launcher, _Probe(True)).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert [spec.slot_id for spec in launcher.specs] == [
        prepared.slot_id,
        prepared.slot_id,
        first_prepared.slot_id,
    ]
    assert result.launched_slots == (prepared.slot_id, first_prepared.slot_id)
    assert second.slots.pointers().current == first_prepared.slot_id
    assert second.journal.latest().state is InstallState.ROLLBACK


def test_pending_confirmed_rollback_is_replayed_after_bootstrap_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, first_prepared = _prepare(tmp_path / "v1", "1.0.0")
    _confirm_and_cross_barrier(first, first_prepared)
    second, prepared = _prepare_upgrade(first, tmp_path / "v2")
    second.activate(prepared.transaction_id)
    intent = second.activations.load_intent()
    assert intent is not None
    second.activations.confirm(prepared.transaction_id, intent.health_identity)

    def crash_before_pointer_restore(_prior):
        raise KeyboardInterrupt("power loss after durable rollback intent")

    monkeypatch.setattr(second.activations.slots, "restore", crash_before_pointer_restore)
    with pytest.raises(KeyboardInterrupt):
        second.activations.rollback_confirmed_pre_data(
            prepared.slot_id,
            error_code="confirmed_runtime_exit_70",
        )
    receipt = json.loads(second.activations.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "rollback_pending"
    assert second.slots.pointers().current == prepared.slot_id

    result = _supervisor(
        first.root,
        _Launcher([_Child(0)]),
        _Probe(True),
    ).run()

    assert result.reason is BootstrapReason.RUNTIME_COMPLETED
    assert result.launched_slots == (first_prepared.slot_id,)
    assert second.slots.pointers().current == first_prepared.slot_id
    assert second.journal.latest().state is InstallState.ROLLBACK
    receipt = json.loads(second.activations.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "rolled_back_pre_data"


def test_probe_timeout_or_transport_exception_is_a_pre_data_rollback(
    tmp_path: Path,
) -> None:
    coordinator, prepared = _prepare(tmp_path, "1.0.0")
    coordinator.activate(prepared.transaction_id)

    class TimeoutProbe:
        def probe(self, _endpoint, _activation, _nonce):
            raise TimeoutError("candidate never became ready")

    result = _supervisor(
        tmp_path / "install",
        _Launcher([_Child(0)]),
        TimeoutProbe(),
    ).run()

    assert result.reason is BootstrapReason.RUNTIME_FAILED
    assert coordinator.slots.pointers() == SlotPointers()
    assert coordinator.journal.latest().state is InstallState.ROLLBACK


def test_real_signed_product_candidate_uses_probe_only_composition_without_opening_db(
    tmp_path: Path,
) -> None:
    from ecorex.connectors import InMemoryCredentialVault
    from tests.v1.test_product_runtime_entrypoint import _public, _stage_product

    product = _stage_product(tmp_path)
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(product["artifact_id"])
    prior = SlotPointers()
    slots.write_pointers(prior)
    transaction_id = "a" * 32
    active = {
        "transaction_id": transaction_id,
        "release_id": manifest.release_id,
        "version": manifest.version,
        "build_digest": manifest.build_digest,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.sha256,
        "slot_id": "slot-product-entrypoint",
        "first_install": True,
        "rollback_authorized": False,
        "admission_current_slot": None,
        "prior_pointers": prior.to_dict(),
        "source_index": 0,
    }
    atomic_write_json(install_root / "active-transaction.json", active)
    journal = InstallJournal(install_root / "install-journal.ndjson")
    journal.append(
        transaction_id=transaction_id,
        state=InstallState.RESOLVING,
        event="transaction_started",
        details={
            "release_id": manifest.release_id,
            "version": manifest.version,
            "build_digest": manifest.build_digest,
            "artifact_id": artifact.artifact_id,
            "first_install": True,
            "rollback_authorized": False,
            "admission_current_slot": None,
        },
    )
    for state in (
        InstallState.DOWNLOADING,
        InstallState.VERIFYING,
        InstallState.STAGING,
        InstallState.AWAITING_USER,
        InstallState.DRAINING,
        InstallState.ACTIVATING,
    ):
        journal.append(
            transaction_id=transaction_id,
            state=state,
            event=f"test_{state.value}",
            details={},
        )
    verifier = Ed25519SignatureVerifier(
        {"release-key": _public(product["release_private"])}
    )
    controller = ProvisionalActivationController(
        install_root,
        verifier=verifier,
        host_platform=product["platform"],
        host_architecture=product["architecture"],
    )
    intent = controller.create_intent(
        active=active,
        manifest=manifest,
        artifact=artifact,
        prior_pointers=prior,
    )
    journal.append(
        transaction_id=transaction_id,
        state=InstallState.ACTIVATING,
        event="activation_intent_persisted",
        details={"intent_digest": intent.intent_digest},
    )
    controller.ensure_pending_current(transaction_id)
    nonce = "P" * 43

    def unexpected_probe_vault():
        raise AssertionError("the probe-only activation process must not open a vault")

    composition = load_product_runtime(
        payload_root=product["payload"],
        host="127.0.0.1",
        port=9451,
        environment={
            "ECOREX_BOOTSTRAPPED": "1",
            ACTIVATION_TRANSACTION_ENV: transaction_id,
            ACTIVATION_NONCE_ENV: nonce,
        },
        vault_factory=unexpected_probe_vault,
        host_platform=product["platform"],
        host_architecture=product["architecture"],
    )

    assert isinstance(composition, ActivationProbeComposition)
    assert not product["database"].exists()
    assert "P" * 43 not in repr(composition)
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=9451,
        runtime_loader=lambda **_kwargs: composition,
    )
    assert server.composition is composition
    client = TestClient(server.app, base_url="http://127.0.0.1:9451")
    assert client.post("/api/v1/update/check").status_code == 503
    response = client.get(
        "/api/v1/activation-health",
        headers={ACTIVATION_NONCE_HEADER: nonce},
    )
    assert response.status_code == 200
    assert nonce not in response.text

    controller.confirm(transaction_id, intent.health_identity)
    full_vault_calls = 0
    full_vault = InMemoryCredentialVault()

    def full_runtime_vault():
        nonlocal full_vault_calls
        full_vault_calls += 1
        return full_vault

    full = load_product_runtime(
        payload_root=product["payload"],
        host="127.0.0.1",
        port=9451,
        environment={"ECOREX_BOOTSTRAPPED": "1"},
        vault_factory=full_runtime_vault,
        host_platform=product["platform"],
        host_architecture=product["architecture"],
    )
    assert isinstance(full, ProductRuntimeComposition)
    assert full_vault_calls == 1
    assert full.update.coordinator.bootstrap_health_confirmation is True
    receipt = json.loads(controller.receipt_path.read_text(encoding="utf-8"))
    assert receipt["data_barrier_crossed"] is True
    assert product["database"].exists()
    full.close_unstarted()

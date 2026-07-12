from __future__ import annotations

import base64
import hashlib
import io
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ecorex.update import (
    InstallCoordinator as _InstallCoordinator,
    InstallState,
    LocalSourceFetcher,
    DownloadFailed,
    CoreDeltaEndpoint,
    PinnedTargetError,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    TargetAdmissionError,
    core_delta_artifact_id,
    core_delta_file_name,
    create_core_delta_archive,
)


def InstallCoordinator(*args, **kwargs):
    """Create the Windows-x64 coordinator exercised by these portable tests."""

    kwargs.setdefault("host_platform", "windows")
    kwargs.setdefault("host_architecture", "x64")
    kwargs.setdefault("bootstrap_health_confirmation", False)
    return _InstallCoordinator(*args, **kwargs)


class AcceptingTestVerifier:
    """A test-only trust provider; production defaults remain fail-closed."""

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        assert payload
        assert signature.key_id == "test-release-key"
        return True


class CrashAfterCompleteFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(
        self,
        source,
        artifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        del source, artifact
        assert max_bytes == len(self.payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if resume_from else "wb"
        with destination.open(mode) as stream:
            stream.write(self.payload[resume_from:])
            stream.flush()
            os.fsync(stream.fileno())
        raise KeyboardInterrupt("simulated process death after durable download")


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="test-release-key",
        value=base64.b64encode(b"test-signature").decode("ascii"),
    )


def _package(version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("runtime/version.txt", version)
        archive.writestr("web/assets/app-deadbeef.js", f"console.log('{version}')")
    return output.getvalue()


def _manifest(version: str, payload: bytes) -> ReleaseManifest:
    return ReleaseManifest(
        schema_version=1,
        release_id=f"release-{version.replace('.', '-')}-stable",
        version=version,
        build_digest=hashlib.sha256(f"build:{version}".encode()).hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource(
                "cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                f"https://mirror.example/ecorex/v{version}",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                f"https://github.com/ecorex/releases/download/v{version}",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                f"https://cdn.example/ecorex/v{version}",
            ),
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


def _write_source(root: Path, source_id: str, payload: bytes) -> Path:
    directory = root / f"source-{source_id}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ecorex-core.zip").write_bytes(payload)
    return directory


def _fetcher(source_root: Path, payload: bytes, *, corrupt_cn: bool = False) -> LocalSourceFetcher:
    corrupt = bytes([payload[0] ^ 0xFF]) + payload[1:] if corrupt_cn else payload
    return LocalSourceFetcher(
        {
            "cn": _write_source(source_root, "cn", corrupt),
            "github": _write_source(source_root, "github", payload),
            "cdn": _write_source(source_root, "cdn", payload),
        }
    )


def test_prepare_uses_signed_source_priority_then_waits_for_user(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path, payload, corrupt_cn=True),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    prepared = coordinator.prepare_update(manifest, "core-windows-x64")

    assert prepared.state is InstallState.AWAITING_USER
    assert (prepared.slot_path / "payload/runtime/version.txt").read_text() == "1.0.0"
    assert coordinator.slots.pointers().current is None
    source_events = [
        entry.details.get("source_id")
        for entry in coordinator.journal.entries()
        if entry.event == "source_attempted"
    ]
    assert source_events == ["cn", "github"]
    assert coordinator.journal.latest().state is InstallState.AWAITING_USER


def test_cancelled_staged_slot_runs_security_cleanup_before_deletion(
    tmp_path: Path,
) -> None:
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    cleanup_calls: list[Path] = []

    def cleanup(path, release, artifact, marker) -> None:
        assert path.is_dir()
        assert release == manifest
        assert artifact.artifact_id == "core-windows-x64"
        assert marker == {"contract": "test-slot-security-v1"}
        cleanup_calls.append(path)

    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path, payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        payload_security_preparer=lambda *_args: {"prepared": True},
        payload_security_attester=lambda *_args: {
            "contract": "test-slot-security-v1"
        },
        payload_security_cleanup=lambda *_args: None,
        slot_security_validator=lambda *_args: True,
        slot_security_cleanup=cleanup,
    )
    prepared = coordinator.prepare_update(manifest, "core-windows-x64")
    assert prepared.slot_path.is_dir()

    result = coordinator.cancel_pending_activation(prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert cleanup_calls == [prepared.slot_path]
    assert not prepared.slot_path.exists()


def test_activation_switches_side_by_side_and_failed_healthcheck_rolls_back(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    first_payload = _package("1.0.0")
    first_manifest = _manifest("1.0.0", first_payload)
    first = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "v1", first_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda slot: (slot / "payload/runtime/version.txt").is_file(),
    )
    first_prepared = first.prepare(first_manifest, "core-windows-x64")
    first_result = first.activate_pending(first_prepared.transaction_id)

    assert first_result.state is InstallState.COMPLETED
    assert first_result.current_slot == first_prepared.slot_id
    assert first_result.previous_slot is None
    assert not (install_root / "transactions" / first_prepared.transaction_id).exists()
    assert (first_prepared.slot_path / ".release-package").is_file()

    second_payload = _package("1.0.1")
    second_manifest = _manifest("1.0.1", second_payload)
    second = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "v2", second_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
        rollforward_guard=lambda _slot: False,
    )
    second_prepared = second.prepare(second_manifest, "core-windows-x64")
    second_result = second.activate(second_prepared.transaction_id)

    assert second_result.state is InstallState.ROLLBACK
    assert second_result.rolled_back is True
    assert second_result.current_slot == first_prepared.slot_id
    assert second_result.previous_slot is None
    assert second.journal.latest().state is InstallState.ROLLBACK
    assert (install_root / "current").read_text().strip() == first_prepared.slot_id
    assert not (install_root / "transactions" / second_prepared.transaction_id).exists()


def test_update_prefers_signed_delta_and_never_fetches_full_target(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    base_payload = bytes((index * 17 + 31) % 256 for index in range(1024 * 1024))
    target_payload = bytearray(base_payload)
    target_payload[4 * 64 * 1024 : 5 * 64 * 1024] = (
        b"one-signed-delta-change\n" * 3000
    )[: 64 * 1024]
    target_payload = bytes(target_payload)
    base_manifest = _manifest("1.0.0", base_payload)
    base = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "base", base_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    base_prepared = base.prepare_update(base_manifest, "core-windows-x64")
    base.activate(base_prepared.transaction_id)

    target_core_manifest = _manifest("1.0.1", target_payload)
    base_file = tmp_path / "base-retained.bin"
    target_file = tmp_path / "target-full.bin"
    base_file.write_bytes(base_payload)
    target_file.write_bytes(target_payload)
    base_artifact = base_manifest.artifact("core-windows-x64")
    target_artifact = target_core_manifest.artifact("core-windows-x64")
    delta_name = core_delta_file_name(
        platform="windows",
        architecture="x64",
        base_artifact_sha256=base_artifact.sha256,
        target_artifact_sha256=target_artifact.sha256,
    )
    delta_file = tmp_path / delta_name
    create_core_delta_archive(
        base_package=base_file,
        target_package=target_file,
        base=CoreDeltaEndpoint.from_release(base_manifest, base_artifact),
        target=CoreDeltaEndpoint.from_release(
            target_core_manifest, target_artifact
        ),
        destination=delta_file,
    )
    delta_payload = delta_file.read_bytes()
    delta_artifact = ReleaseArtifact(
        artifact_id=core_delta_artifact_id(
            platform="windows",
            architecture="x64",
            base_artifact_sha256=base_artifact.sha256,
        ),
        platform="windows",
        architecture="x64",
        file_name=delta_name,
        size_bytes=len(delta_payload),
        sha256=hashlib.sha256(delta_payload).hexdigest(),
        signature=_signature(),
    )
    target_manifest = replace(
        target_core_manifest,
        artifacts=(target_artifact, delta_artifact),
    )
    source_directories = {}
    for source in target_manifest.sources:
        directory = tmp_path / "delta-sources" / source.source_id
        directory.mkdir(parents=True)
        (directory / delta_name).write_bytes(delta_payload)
        # Deliberately omit ecorex-core.zip. A full-package fetch would fail.
        source_directories[source.source_id] = directory
    updater = InstallCoordinator(
        install_root,
        fetcher=LocalSourceFetcher(source_directories),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    prepared = updater.prepare_update(target_manifest, "core-windows-x64")

    assert prepared.state is InstallState.AWAITING_USER
    assert prepared.package_path.read_bytes() == target_payload
    events = [entry.event for entry in updater.journal.entries()]
    assert "delta_applied" in events
    assert events.count("source_attempted") == 1  # base install only

    # Leave only the verified delta object: the full target CAS object is
    # deliberately removed and every origin loses the delta. A new
    # transaction must rebuild the target from the cached signed delta.
    target_cache_object = next(
        updater.download_cache.objects.rglob(target_artifact.sha256)
    )
    target_cache_object.unlink()
    updater.cancel_pending_activation(prepared.transaction_id)
    for directory in source_directories.values():
        (directory / delta_name).unlink()

    replay = updater.prepare_update(target_manifest, "core-windows-x64")

    assert replay.package_path.read_bytes() == target_payload
    replay_events = [entry.event for entry in updater.journal.entries()]
    assert "delta_restored_from_download_cache" in replay_events
    assert replay_events.count("source_attempted") == 1


def test_invalid_signed_delta_falls_back_to_verified_full_package(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    base_payload = b"base-core-block\n" * 64 * 1024
    target_payload = b"target-core-block\n" * 64 * 1024
    base_manifest = _manifest("1.0.0", base_payload)
    first = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "base-full", base_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    first_prepared = first.prepare_update(base_manifest, "core-windows-x64")
    first.activate(first_prepared.transaction_id)

    target_only = _manifest("1.0.1", target_payload)
    base_artifact = base_manifest.artifact("core-windows-x64")
    target_artifact = target_only.artifact("core-windows-x64")
    delta_name = core_delta_file_name(
        platform="windows",
        architecture="x64",
        base_artifact_sha256=base_artifact.sha256,
        target_artifact_sha256=target_artifact.sha256,
    )
    malformed_delta = b"signed-but-structurally-invalid-delta\n" * 10
    delta_artifact = ReleaseArtifact(
        artifact_id=core_delta_artifact_id(
            platform="windows",
            architecture="x64",
            base_artifact_sha256=base_artifact.sha256,
        ),
        platform="windows",
        architecture="x64",
        file_name=delta_name,
        size_bytes=len(malformed_delta),
        sha256=hashlib.sha256(malformed_delta).hexdigest(),
        signature=_signature(),
    )
    target_manifest = replace(
        target_only,
        artifacts=(target_artifact, delta_artifact),
    )
    directories = {}
    for source in target_manifest.sources:
        directory = tmp_path / "fallback-sources" / source.source_id
        directory.mkdir(parents=True)
        (directory / delta_name).write_bytes(malformed_delta)
        (directory / target_artifact.file_name).write_bytes(target_payload)
        directories[source.source_id] = directory
    updater = InstallCoordinator(
        install_root,
        fetcher=LocalSourceFetcher(directories),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    prepared = updater.prepare_update(target_manifest, "core-windows-x64")

    assert prepared.package_path.read_bytes() == target_payload
    events = [entry.event for entry in updater.journal.entries()]
    assert events.count("delta_rejected") == 3
    assert events[-3:] == ["download_finished", "artifact_verified", "slot_staged"]


def test_first_install_pin_blocks_release_push_until_registration(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    coordinator = _InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "v1", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
    )

    prepared = coordinator.prepare(
        manifest,
        "core-windows-x64",
        first_install=True,
    )
    assert coordinator.pinned_target is not None
    assert coordinator.accepts_manifest(manifest, "core-windows-x64") is True
    assert (
        coordinator.accepts_manifest(
            _manifest("1.0.1", _package("1.0.1")), "core-windows-x64"
        )
        is False
    )

    assert (
        coordinator.activate(prepared.transaction_id).state
        is InstallState.HEALTHCHECKING
    )
    intent = coordinator.activations.load_intent()
    assert intent is not None
    coordinator.activations.confirm(prepared.transaction_id, intent.health_identity)
    assert coordinator.activations.mark_data_barrier_crossed(prepared.slot_id) is True
    # A restart must retain the pin until registration explicitly completes.
    restarted = _InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "restart", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
    )
    assert restarted.pinned_target is not None
    registration = {
        "account_id": "account-1",
        "organization_id": "organization-1",
        "lease_id": "lease-1",
        "lease_digest": "a" * 64,
        "session_generation": 1,
        "lease_revision": 1,
    }
    # Device polling records authority but cannot clear the Bootstrap pin.
    assert restarted.record_registration_authority(registration) is False
    assert restarted.pinned_target is not None
    changed_lease = dict(registration)
    changed_lease["lease_digest"] = "b" * 64
    with pytest.raises(PinnedTargetError, match="differs from device registration"):
        restarted.mark_registration_complete(changed_lease)
    assert restarted.pinned_target is not None
    assert restarted.mark_registration_complete(registration) is True
    assert restarted.pinned_target is None


def test_recover_resumes_a_durable_partial_transaction_without_network(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    crashing = InstallCoordinator(
        install_root,
        fetcher=CrashAfterCompleteFetcher(payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        crashing.prepare(manifest, "core-windows-x64")

    assert crashing.journal.latest().state is InstallState.DOWNLOADING
    restarted = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "recovery", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    recovered = restarted.recover()

    assert recovered is not None
    assert recovered.state is InstallState.AWAITING_USER
    artifact = manifest.artifact("core-windows-x64")
    cached = tuple(restarted.download_cache.objects.rglob(artifact.sha256))
    assert len(cached) == 1
    assert cached[0].read_bytes() == payload


def test_recover_awaiting_user_never_activates_without_confirmation(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    prepared = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "prepare", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    ).prepare(manifest, "core-windows-x64")
    drain_calls: list[str] = []

    recovered = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "restart", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        drainer=lambda: drain_calls.append("called") or True,
    ).recover()

    assert recovered is not None
    assert recovered.state is InstallState.AWAITING_USER
    assert recovered.transaction_id == prepared.transaction_id
    assert drain_calls == []
    assert InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "inspect", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    ).slots.pointers().current is None


def test_staging_recovery_reverifies_the_download(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    crashing = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "prepare", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    def crash_before_stage(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt("crash before extraction")

    crashing.slots.stage = crash_before_stage  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt, match="before extraction"):
        crashing.prepare(manifest, "core-windows-x64")
    assert crashing.journal.latest().state is InstallState.STAGING
    active = crashing._load_active()
    assert active is not None
    package_path = crashing._package_path(active, manifest.artifacts[0])
    package_path.write_bytes(_package("FORGED-UNSIGNED"))

    with pytest.raises(Exception, match="mismatch"):
        InstallCoordinator(
            install_root,
            fetcher=_fetcher(tmp_path / "restart", payload),
            verifier=AcceptingTestVerifier(),
            health_checker=lambda _slot: True,
        ).recover()


@pytest.mark.parametrize("gate", ["drainer", "migration"])
def test_activation_gates_require_explicit_true(tmp_path: Path, gate: str) -> None:
    payload = _package("1.0.0")
    kwargs = {
        "drainer": (lambda: None) if gate == "drainer" else (lambda: True),
        "migration_dry_run": (
            (lambda _slot: None) if gate == "migration" else (lambda _slot: True)
        ),
    }
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        **kwargs,
    )
    prepared = coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64")

    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert result.current_slot is None


def test_staged_payload_tampering_is_rejected_before_activation(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    prepared = coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64")
    (prepared.slot_path / "payload/runtime/version.txt").write_text("tampered")

    with pytest.raises(Exception, match="modified"):
        coordinator.activate(prepared.transaction_id)
    assert coordinator.slots.pointers().current is None
    assert coordinator.journal.latest().state is InstallState.FAILED
    assert not prepared.slot_path.exists()


def test_label_failure_cannot_create_failed_but_switched_state(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    prepared = coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64")

    def fail_labels(_pointers) -> None:
        raise OSError("simulated Windows sharing violation")

    coordinator.slots._sync_human_labels = fail_labels  # type: ignore[method-assign]
    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.COMPLETED
    assert coordinator.journal.latest().state is InstallState.COMPLETED
    assert coordinator.slots.pointers().current == prepared.slot_id


def test_post_commit_pointer_fault_restores_the_prior_authority(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    first_payload = _package("1.0.0")
    first = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "first", first_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    first_prepared = first.prepare(_manifest("1.0.0", first_payload), "core-windows-x64")
    assert first.activate(first_prepared.transaction_id).state is InstallState.COMPLETED

    second_payload = _package("1.0.1")
    second = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "second", second_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    second_prepared = second.prepare(_manifest("1.0.1", second_payload), "core-windows-x64")
    original = second.slots._sync_human_labels
    calls = 0

    def fail_once(pointers) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fault after authoritative pointer commit")
        original(pointers)

    second.slots._sync_human_labels = fail_once  # type: ignore[method-assign]
    result = second.activate(second_prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert second.slots.pointers().current == first_prepared.slot_id
    assert second.journal.latest().state is InstallState.FAILED


def test_first_install_pin_binds_the_exact_artifact(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    base = _manifest("1.0.0", payload)
    macos = ReleaseArtifact(
        artifact_id="core-macos-arm64",
        platform="macos",
        architecture="arm64",
        file_name="ecorex-core.zip",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=_signature(),
    )
    manifest = replace(base, artifacts=(base.artifacts[0], macos))
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    first = coordinator.prepare(manifest, "core-windows-x64", first_install=True)
    assert coordinator.activate(first.transaction_id).state is InstallState.COMPLETED

    assert coordinator.accepts_manifest(manifest, "core-macos-arm64") is False
    with pytest.raises((PinnedTargetError, TargetAdmissionError)):
        coordinator.prepare(manifest, "core-macos-arm64")


def test_failed_first_install_pin_requires_authorized_recovery(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=LocalSourceFetcher({}),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        pin_recovery_authorizer=lambda _pin, token: token == "authorized",
    )
    with pytest.raises(DownloadFailed):
        coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64", first_install=True)
    assert coordinator.pinned_target is not None

    with pytest.raises(PinnedTargetError, match="not authorized"):
        coordinator.recover_first_install_pin("wrong")
    coordinator.recover_first_install_pin("authorized")
    assert coordinator.pinned_target is None


def test_host_channel_and_downgrade_admission(tmp_path: Path) -> None:
    payload = _package("1.0.1")
    manifest = _manifest("1.0.1", payload)
    with pytest.raises(TargetAdmissionError, match="host is macos/arm64"):
        InstallCoordinator(
            tmp_path / "wrong-host",
            fetcher=_fetcher(tmp_path / "wrong-host-sources", payload),
            verifier=AcceptingTestVerifier(),
            health_checker=lambda _slot: True,
            host_platform="macos",
            host_architecture="arm64",
        ).prepare(manifest, "core-windows-x64")

    canary = replace(manifest, channel=ReleaseChannel.CANARY)
    with pytest.raises(TargetAdmissionError, match="channel"):
        InstallCoordinator(
            tmp_path / "wrong-channel",
            fetcher=_fetcher(tmp_path / "wrong-channel-sources", payload),
            verifier=AcceptingTestVerifier(),
            health_checker=lambda _slot: True,
        ).prepare(canary, "core-windows-x64")

    install_root = tmp_path / "downgrade"
    current = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "current-sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    current_prepared = current.prepare(manifest, "core-windows-x64")
    assert current.activate(current_prepared.transaction_id).state is InstallState.COMPLETED

    older_payload = _package("1.0.0")
    older = _manifest("1.0.0", older_payload)
    rejected = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "older-rejected", older_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    with pytest.raises(TargetAdmissionError, match="authorization"):
        rejected.prepare(older, "core-windows-x64")

    authorized = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "older-authorized", older_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        rollback_authorizer=lambda _current, _target, token: token == "rollback-ok",
    )
    prepared = authorized.prepare(
        older,
        "core-windows-x64",
        rollback_authorization="rollback-ok",
    )
    assert authorized.activate(prepared.transaction_id).state is InstallState.COMPLETED


def test_known_good_retention_and_rollforward_barrier(tmp_path: Path) -> None:
    install_root = tmp_path / "known-good"
    prepared_slots: list[str] = []
    for version in ("1.0.0", "1.0.1", "1.0.2"):
        payload = _package(version)
        coordinator = InstallCoordinator(
            install_root,
            fetcher=_fetcher(tmp_path / f"sources-{version}", payload),
            verifier=AcceptingTestVerifier(),
            health_checker=lambda _slot: True,
        )
        prepared = coordinator.prepare(_manifest(version, payload), "core-windows-x64")
        prepared_slots.append(prepared.slot_id)
        assert coordinator.activate(prepared.transaction_id).state is InstallState.COMPLETED

    pointers = coordinator.slots.pointers()
    assert pointers.known_good == tuple(reversed(prepared_slots))
    assert all(coordinator.slots.slot_path(slot).exists() for slot in prepared_slots)

    failed_payload = _package("1.0.3")
    failed = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "failed-sources", failed_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
        rollforward_guard=lambda _slot: False,
    )
    bad = failed.prepare(_manifest("1.0.3", failed_payload), "core-windows-x64")
    assert failed.activate(bad.transaction_id).state is InstallState.ROLLBACK
    assert failed.slots.pointers().known_good == tuple(reversed(prepared_slots))
    assert not bad.slot_path.exists()

    next_payload = _package("1.0.3")
    rollforward = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "rollforward-sources", next_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
        rollforward_guard=lambda _slot: True,
    )
    pending = rollforward.prepare(_manifest("1.0.3", next_payload), "core-windows-x64")
    result = rollforward.activate(pending.transaction_id)
    assert result.state is InstallState.FAILED
    assert result.rolled_back is False
    assert result.error == "RollForwardRequired"
    assert result.current_slot == pending.slot_id


def test_corrupt_known_good_slot_blocks_unsafe_rollback(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    first_payload = _package("1.0.0")
    first = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "first", first_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    first_prepared = first.prepare(_manifest("1.0.0", first_payload), "core-windows-x64")
    assert first.activate(first_prepared.transaction_id).state is InstallState.COMPLETED

    second_payload = _package("1.0.1")
    second = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "second", second_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
        rollforward_guard=lambda _slot: False,
    )
    second_prepared = second.prepare(_manifest("1.0.1", second_payload), "core-windows-x64")
    (first_prepared.slot_path / "payload/runtime/version.txt").write_text("corrupt")

    result = second.activate(second_prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert result.error == "RollForwardRequired"
    assert result.current_slot == second_prepared.slot_id


def test_health_failure_without_explicit_rollback_guard_fails_closed(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
    )
    prepared = coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64")

    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.FAILED
    assert result.error == "RollForwardRequired"
    assert result.current_slot == prepared.slot_id


def test_recovery_does_not_skip_a_source_after_verification_crash(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    manifest = _manifest("1.0.0", payload)
    install_root = tmp_path / "install"
    crashing = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "crashing", payload, corrupt_cn=True),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    original_transition = crashing._transition

    def crash_after_source_advance(transaction_id, state, event, details):
        if event == "source_artifact_rejected":
            raise KeyboardInterrupt("crash after durable source advance")
        return original_transition(transaction_id, state, event, details)

    crashing._transition = crash_after_source_advance  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        crashing.prepare(manifest, "core-windows-x64")
    assert crashing.journal.latest().state is InstallState.VERIFYING

    recovered_coordinator = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "recovered", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )
    recovered = recovered_coordinator.recover()
    assert recovered is not None and recovered.state is InstallState.AWAITING_USER
    attempts = [
        entry.details.get("source_id")
        for entry in recovered_coordinator.journal.entries()
        if entry.event == "source_attempted"
    ]
    assert attempts[-1] == "github"


def test_semver_build_metadata_produces_a_portable_slot_id(tmp_path: Path) -> None:
    payload = _package("1.0.0+build.1")
    base = _manifest("1.0.0", payload)
    manifest = replace(
        base,
        release_id="release-1-0-0-build-1-stable",
        version="1.0.0+build.1",
    )
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
    )

    prepared = coordinator.prepare(manifest, "core-windows-x64")

    assert "+" not in prepared.slot_id
    assert len(prepared.slot_id) < 120
    assert prepared.slot_path.is_dir()


def test_disk_space_failure_is_terminal_and_cleans_transaction_payload(tmp_path: Path) -> None:
    payload = _package("1.0.0")
    coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=_fetcher(tmp_path / "sources", payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        disk_free_provider=lambda _path: 0,
        disk_reserve_bytes=0,
    )

    with pytest.raises(DownloadFailed, match="disk space"):
        coordinator.prepare(_manifest("1.0.0", payload), "core-windows-x64")

    assert coordinator.journal.latest().state is InstallState.FAILED
    active = coordinator._load_active()
    assert active is not None
    assert not (coordinator.transactions_dir / active["transaction_id"]).exists()

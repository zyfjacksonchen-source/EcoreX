from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

import ecorex.update.pack_install as pack_install_module

from ecorex.bootstrap import CurrentSlotVerifier
from ecorex.capabilities import (
    CapabilityPackManifest,
    PackServiceBinding,
    PackToolBinding,
    builtin_capability_registry,
    builtin_pack_service_specs,
    tool_spec_digest,
)
from ecorex.pack_catalog import (
    CAPABILITY_PACK_SERVICE_IDS,
    CAPABILITY_PACK_TOOL_IDS,
    REQUIRED_CAPABILITY_PACK_IDS,
)
from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.update import (
    ArtifactFetcher,
    InstallCoordinator,
    InstallState,
    LocalSourceFetcher,
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SlotStore,
    SourceKind,
)
from ecorex.update.pack_install import (
    IncompletePackSet,
    PackDownloadFailed,
    PackInstallError,
)
from ecorex.update.storage import _payload_tree_digest


PACK_TOOLS = CAPABILITY_PACK_TOOL_IDS
PACK_SERVICES = CAPABILITY_PACK_SERVICE_IDS


def test_verified_pack_copy_streams_without_materializing_the_full_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large-pack.zip"
    destination = tmp_path / "slot" / "large-pack.zip"
    payload = b"bounded-stream-chunk" * (3 * 1024 * 1024 // 20)
    source.write_bytes(payload)

    def reject_full_file_read(_path: Path, _maximum: int) -> bytes:
        raise AssertionError("full-file allocation is forbidden for Pack copy")

    monkeypatch.setattr(pack_install_module, "_stable_bytes", reject_full_file_read)
    pack_install_module._copy_stable(source, destination)

    assert destination.read_bytes() == payload


class AcceptingTestVerifier:
    """Test-only signature seam; content hashes and identities remain real."""

    def verify(self, payload: bytes, signature: SignatureEnvelope) -> bool:
        assert payload
        assert signature.key_id == "test-release-key"
        return True


class CrashAfterArtifactFetcher:
    def __init__(self, delegate: LocalSourceFetcher, artifact_id: str) -> None:
        self.delegate = delegate
        self.artifact_id = artifact_id
        self.crashed = False

    def fetch(
        self,
        source,
        artifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        self.delegate.fetch(
            source,
            artifact,
            destination,
            resume_from=resume_from,
            max_bytes=max_bytes,
        )
        if artifact.artifact_id == self.artifact_id and not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("simulated death after durable Pack download")


class CountingFetcher:
    def __init__(self, delegate: LocalSourceFetcher) -> None:
        self.delegate = delegate
        self.artifact_ids: list[str] = []

    def fetch(
        self,
        source,
        artifact,
        destination: Path,
        *,
        resume_from: int,
        max_bytes: int,
    ) -> None:
        self.artifact_ids.append(artifact.artifact_id)
        self.delegate.fetch(
            source,
            artifact,
            destination,
            resume_from=resume_from,
            max_bytes=max_bytes,
        )


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="test-release-key",
        value=base64.b64encode(b"test-signature").decode("ascii"),
    )


def _zip(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def _release(version: str = "1.0.0") -> tuple[ReleaseManifest, dict[str, bytes]]:
    registry = builtin_capability_registry()
    files: dict[str, bytes] = {
        "ecorex-core.zip": _zip(
            {
                "runtime/version.txt": version,
                "web/assets/app-deadbeef.js": "console.log('atomic packs')",
            }
        )
    }
    artifacts: list[ReleaseArtifact] = [
        ReleaseArtifact(
            artifact_id="core-windows-x64",
            platform="windows",
            architecture="x64",
            file_name="ecorex-core.zip",
            size_bytes=len(files["ecorex-core.zip"]),
            sha256=hashlib.sha256(files["ecorex-core.zip"]).hexdigest(),
            signature=_signature(),
        )
    ]
    services = builtin_pack_service_specs()
    for pack_id in REQUIRED_CAPABILITY_PACK_IDS:
        tool_ids = PACK_TOOLS[pack_id]
        service_ids = PACK_SERVICES[pack_id]
        archive_name = (
            f"ecorex-capability-pack-{pack_id}-windows-x64-{version}.zip"
        )
        sidecar_name = (
            f"ecorex-capability-pack-{pack_id}-windows-x64-{version}.json"
        )
        archive = _zip(
            {
                "__main__.py": f"PACK_ID = {pack_id!r}\n",
                "ecorex-pack.json": json.dumps(
                    {
                        "pack_id": pack_id,
                        "services": list(service_ids),
                        "tools": list(tool_ids),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        tool_bindings = tuple(
            PackToolBinding(
                tool_id=tool_id,
                tool_version=registry.get(tool_id).version,
                spec_sha256=tool_spec_digest(registry.get(tool_id)),
            )
            for tool_id in tool_ids
        )
        service_bindings = tuple(
            PackServiceBinding(
                service_id=service_id,
                service_version=services[service_id].version,
                contract_sha256=services[service_id].contract_sha256,
            )
            for service_id in service_ids
        )
        sidecar = CapabilityPackManifest(
            schema_version=2,
            pack_id=pack_id,
            version=version,
            runtime_api_version="1.0.0",
            platform="windows",
            architecture="x64",
            artifact_file_name=archive_name,
            artifact_size_bytes=len(archive),
            artifact_sha256=hashlib.sha256(archive).hexdigest(),
            tools=tool_bindings,
            services=service_bindings,
            signature=_signature(),
        ).to_bytes()
        files[archive_name] = archive
        files[sidecar_name] = sidecar
        artifacts.extend(
            (
                ReleaseArtifact(
                    artifact_id=f"capability-pack-{pack_id}-windows-x64",
                    platform="windows",
                    architecture="x64",
                    file_name=archive_name,
                    size_bytes=len(archive),
                    sha256=hashlib.sha256(archive).hexdigest(),
                    signature=_signature(),
                ),
                ReleaseArtifact(
                    artifact_id=(
                        f"capability-pack-{pack_id}-windows-x64-manifest"
                    ),
                    platform="windows",
                    architecture="x64",
                    file_name=sidecar_name,
                    size_bytes=len(sidecar),
                    sha256=hashlib.sha256(sidecar).hexdigest(),
                    signature=_signature(),
                ),
            )
        )
    manifest = ReleaseManifest(
        schema_version=1,
        release_id=f"release-{version.replace('.', '-')}-packs",
        version=version,
        build_digest=hashlib.sha256(f"build:{version}:packs".encode()).hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-11T12:00:00+08:00",
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
        artifacts=tuple(artifacts),
        signature=_signature(),
    )
    return manifest, files


def _fetcher(
    root: Path,
    files: dict[str, bytes],
    *,
    corrupt_cn: str | None = None,
    missing: str | None = None,
) -> LocalSourceFetcher:
    sources: dict[str, Path] = {}
    for source_id in ("cn", "github", "cdn"):
        directory = root / source_id
        directory.mkdir(parents=True)
        for name, payload in files.items():
            if name == missing:
                continue
            value = payload
            if source_id == "cn" and name == corrupt_cn:
                value = bytes([payload[0] ^ 0xFF]) + payload[1:]
            (directory / name).write_bytes(value)
        sources[source_id] = directory
    return LocalSourceFetcher(sources)


def _coordinator(
    install_root: Path,
    fetcher: ArtifactFetcher,
    *,
    content_verifier=verify_product_capability_pack,
) -> InstallCoordinator:
    return InstallCoordinator(
        install_root,
        fetcher=fetcher,
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
        pack_content_verifier=content_verifier,
    )


def test_core_and_six_packs_stage_and_activate_as_one_slot(tmp_path: Path) -> None:
    manifest, files = _release()
    install_root = tmp_path / "install"
    coordinator = _coordinator(
        install_root,
        _fetcher(tmp_path / "sources", files),
    )

    prepared = coordinator.prepare(manifest, "core-windows-x64", first_install=True)

    assert prepared.state is InstallState.AWAITING_USER
    assert coordinator.slots.pointers().current is None
    marker = json.loads((prepared.slot_path / ".slot.json").read_text("utf-8"))
    assert marker["supplemental"]["kind"] == "ecorex-capability-pack-set"
    assert [record["pack_id"] for record in marker["supplemental"]["packs"]] == [
        "browser",
        "channels",
        "image",
        "ocr",
        "office",
        "sandbox",
    ]
    for pack_id in REQUIRED_CAPABILITY_PACK_IDS:
        expected = {
            f"ecorex-capability-pack-{pack_id}-windows-x64-1.0.0.zip",
            f"ecorex-capability-pack-{pack_id}-windows-x64-1.0.0.json",
        }
        assert {
            path.name
            for path in (prepared.slot_path / "payload/capability-packs" / pack_id).iterdir()
        } == expected

    result = coordinator.activate(prepared.transaction_id)

    assert result.state is InstallState.COMPLETED
    verified = CurrentSlotVerifier(
        SlotStore(install_root),
        verifier=AcceptingTestVerifier(),
        host_platform="windows",
        host_architecture="x64",
        pack_content_verifier=verify_product_capability_pack,
    ).verify_current()
    assert verified.slot_id == prepared.slot_id


def test_cancelled_transaction_reuses_core_and_all_pack_artifacts_from_verified_cache(
    tmp_path: Path,
) -> None:
    manifest, files = _release()
    install_root = tmp_path / "install"
    fetcher = CountingFetcher(_fetcher(tmp_path / "sources", files))
    coordinator = _coordinator(install_root, fetcher)

    first = coordinator.prepare(manifest, "core-windows-x64")
    first_fetches = tuple(fetcher.artifact_ids)
    coordinator.cancel_pending_activation(first.transaction_id)
    second = coordinator.prepare(manifest, "core-windows-x64")

    assert second.state is InstallState.AWAITING_USER
    assert tuple(fetcher.artifact_ids) == first_fetches
    assert set(first_fetches) == {artifact.artifact_id for artifact in manifest.artifacts}


def test_partial_signed_pack_set_is_rejected_before_transaction(tmp_path: Path) -> None:
    manifest, files = _release()
    partial = replace(
        manifest,
        artifacts=tuple(
            artifact
            for artifact in manifest.artifacts
            if artifact.artifact_id
            != "capability-pack-sandbox-windows-x64-manifest"
        ),
    )
    coordinator = _coordinator(
        tmp_path / "install",
        _fetcher(tmp_path / "sources", files),
    )

    with pytest.raises(IncompletePackSet, match="exact host Pack set"):
        coordinator.prepare(partial, "core-windows-x64")

    assert coordinator.journal.latest() is None
    assert not (tmp_path / "install/active-transaction.json").exists()


def test_missing_pack_from_every_source_leaves_no_candidate_slot(tmp_path: Path) -> None:
    manifest, files = _release()
    missing = "ecorex-capability-pack-image-windows-x64-1.0.0.zip"
    coordinator = _coordinator(
        tmp_path / "install",
        _fetcher(tmp_path / "sources", files, missing=missing),
    )

    with pytest.raises(PackDownloadFailed, match="all signed sources failed"):
        coordinator.prepare(manifest, "core-windows-x64")

    latest = coordinator.journal.latest()
    assert latest is not None and latest.state is InstallState.FAILED
    assert coordinator.slots.pointers().current is None
    assert list((tmp_path / "install/slots").iterdir()) == []


def test_corrupt_domestic_pack_falls_through_to_github(tmp_path: Path) -> None:
    manifest, files = _release()
    corrupt = "ecorex-capability-pack-browser-windows-x64-1.0.0.zip"
    coordinator = _coordinator(
        tmp_path / "install",
        _fetcher(tmp_path / "sources", files, corrupt_cn=corrupt),
    )

    prepared = coordinator.prepare(manifest, "core-windows-x64")

    progress = json.loads(
        (
            tmp_path
            / "install/transactions"
            / prepared.transaction_id
            / "pack-install.json"
        ).read_text("utf-8")
    )
    artifact_id = "capability-pack-browser-windows-x64"
    assert progress["artifacts"][artifact_id] == {
        "source_index": 1,
        "status": "verified",
    }
    assert coordinator.slots.pointers().current is None


def test_restart_resumes_verified_pack_bytes_without_refetch(tmp_path: Path) -> None:
    manifest, files = _release()
    install_root = tmp_path / "install"
    source_root = tmp_path / "sources"
    target_id = "capability-pack-browser-windows-x64"
    target_name = "ecorex-capability-pack-browser-windows-x64-1.0.0.zip"
    durable_sources = _fetcher(source_root, files)
    crashing = _coordinator(install_root, durable_sources)
    crashing.fetcher = CrashAfterArtifactFetcher(durable_sources, target_id)
    crashing.pack_downloader.fetcher = crashing.fetcher

    with pytest.raises(KeyboardInterrupt, match="durable Pack download"):
        crashing.prepare(manifest, "core-windows-x64")

    assert crashing.journal.latest().state is InstallState.STAGING
    for source_id in ("cn", "github", "cdn"):
        (source_root / source_id / target_name).unlink()
    restarted = _coordinator(
        install_root,
        LocalSourceFetcher(
            {source_id: source_root / source_id for source_id in ("cn", "github", "cdn")}
        ),
    )

    recovered = restarted.recover()

    assert recovered is not None
    assert recovered.state is InstallState.AWAITING_USER
    assert restarted.slots.pointers().current is None


def test_declared_pack_release_fails_closed_without_product_verifier(
    tmp_path: Path,
) -> None:
    manifest, files = _release()
    coordinator = _coordinator(
        tmp_path / "install",
        _fetcher(tmp_path / "sources", files),
        content_verifier=None,
    )

    with pytest.raises(PackInstallError, match="content verifier is unavailable"):
        coordinator.prepare(manifest, "core-windows-x64")

    assert coordinator.slots.pointers().current is None
    assert list((tmp_path / "install/slots").iterdir()) == []


def test_installed_pack_tampering_is_rejected_without_pointer_mutation(
    tmp_path: Path,
) -> None:
    manifest, files = _release()
    install_root = tmp_path / "install"
    coordinator = _coordinator(
        install_root,
        _fetcher(tmp_path / "sources", files),
    )
    prepared = coordinator.prepare(manifest, "core-windows-x64")
    assert coordinator.activate(prepared.transaction_id).state is InstallState.COMPLETED
    original = coordinator.slots.pointers()
    archive = (
        prepared.slot_path
        / "payload/capability-packs/image"
        / "ecorex-capability-pack-image-windows-x64-1.0.0.zip"
    )
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(Exception, match="failed signed slot verification"):
        CurrentSlotVerifier(
            SlotStore(install_root),
            verifier=AcceptingTestVerifier(),
            host_platform="windows",
            host_architecture="x64",
            pack_content_verifier=verify_product_capability_pack,
        ).verify_current()

    assert coordinator.slots.pointers() == original


def test_mutable_composite_marker_cannot_reauthorize_modified_core(
    tmp_path: Path,
) -> None:
    manifest, files = _release()
    install_root = tmp_path / "install"
    coordinator = _coordinator(
        install_root,
        _fetcher(tmp_path / "sources", files),
    )
    prepared = coordinator.prepare(manifest, "core-windows-x64")
    assert coordinator.activate(prepared.transaction_id).state is InstallState.COMPLETED
    version_file = prepared.slot_path / "payload/runtime/version.txt"
    version_file.write_text("attacker-replaced-core", encoding="utf-8")
    marker_path = prepared.slot_path / ".slot.json"
    marker = json.loads(marker_path.read_text("utf-8"))
    marker["payload_digest"] = _payload_tree_digest(prepared.slot_path / "payload")
    marker_path.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="failed signed slot verification"):
        CurrentSlotVerifier(
            SlotStore(install_root),
            verifier=AcceptingTestVerifier(),
            host_platform="windows",
            host_architecture="x64",
            pack_content_verifier=verify_product_capability_pack,
        ).verify_current()


def test_failed_health_rolls_back_the_complete_core_and_pack_slot(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    first_manifest, first_files = _release("1.0.0")
    first = _coordinator(
        install_root,
        _fetcher(tmp_path / "v1", first_files),
    )
    first_prepared = first.prepare(first_manifest, "core-windows-x64")
    assert first.activate(first_prepared.transaction_id).state is InstallState.COMPLETED

    second_manifest, second_files = _release("1.0.1")
    second = InstallCoordinator(
        install_root,
        fetcher=_fetcher(tmp_path / "v2", second_files),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: False,
        rollforward_guard=lambda _slot: False,
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
        pack_content_verifier=verify_product_capability_pack,
    )
    second_prepared = second.prepare(second_manifest, "core-windows-x64")

    result = second.activate(second_prepared.transaction_id)

    assert result.state is InstallState.ROLLBACK
    assert result.rolled_back is True
    pointers = second.slots.pointers()
    assert pointers.current == first_prepared.slot_id
    assert second_prepared.slot_id not in pointers.known_good
    assert CurrentSlotVerifier(
        SlotStore(install_root),
        verifier=AcceptingTestVerifier(),
        host_platform="windows",
        host_architecture="x64",
        pack_content_verifier=verify_product_capability_pack,
    ).verify_current().slot_id == first_prepared.slot_id

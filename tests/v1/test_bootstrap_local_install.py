from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ecorex.release.builder as release_builder_module
from ecorex import __version__
from ecorex.bootstrap import install_local
from ecorex.bootstrap.companion import BootstrapCompanionInstaller
from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.pack_catalog import (
    CAPABILITY_PACK_SERVICE_IDS,
    CAPABILITY_PACK_TOOL_IDS,
    REQUIRED_CAPABILITY_PACK_IDS,
)
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    InstallCoordinator,
    LocalSourceFetcher,
    ProvisionalActivationController,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
)


def _pack(root: Path, pack_id: str) -> ArtifactBuildInput:
    source = root / pack_id
    source.mkdir(parents=True)
    tools = CAPABILITY_PACK_TOOL_IDS[pack_id]
    services = CAPABILITY_PACK_SERVICE_IDS[pack_id]
    if pack_id == "image":
        descriptor = (
            '{"adapter":"core-managed-image-v1","pack_id":"image",'
            '"runtime_api_version":"1.0.0","schema_version":1,'
            '"tools":["imagegen","vision"]}\n'
        )
        (source / "ecorex-image-pack.json").write_text(
            descriptor, encoding="utf-8"
        )
        executable_paths: tuple[str, ...] = ()
    elif pack_id in {"browser", "sandbox"}:
        (source / "__main__.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        tool_json = ",".join(f'"{tool}"' for tool in tools)
        descriptor = (
            '{"pack_id":"'
            + pack_id
            + '","protocol":"ecorex-stdio-tool-v1",'
            '"runtime_api_version":"1.0.0","schema_version":1,"tools":['
            + tool_json
            + "]}\n"
        )
        (source / "ecorex-pack.json").write_text(descriptor, encoding="utf-8")
        executable_paths = ("__main__.py",)
    else:
        (source / "ecorex-dependency-pack.json").write_text(
            '{"kind":"dependency-service","pack_id":"'
            + pack_id
            + '","runtime_api_version":"1.0.0","schema_version":1}\n',
            encoding="utf-8",
        )
        (source / "runtime-inventory.json").write_text(
            '{"distributions":[],"pack_id":"'
            + pack_id
            + '","schema_version":1}\n',
            encoding="utf-8",
        )
        executable_paths = ()
    return ArtifactBuildInput(
        source_dir=source,
        kind=ArtifactKind.CAPABILITY_PACK,
        platform=_target()[0],
        architecture=_target()[1],
        executable_paths=executable_paths,
        pack_id=pack_id,
        pack_tool_ids=tools,
        pack_service_ids=services,
    )


def _target() -> tuple[str, str]:
    try:
        return install_local._host_target()
    except ValueError:
        pytest.skip("Bootstrap local install requires a supported product target")


def _release(
    tmp_path: Path,
    *,
    private: Ed25519PrivateKey | None = None,
    publication_private: Ed25519PrivateKey | None = None,
):
    platform, architecture = _target()
    private = private or Ed25519PrivateKey.generate()
    publication_private = publication_private or Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("bootstrap-test-key", private)
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / "version-marker.txt").write_text(__version__, encoding="utf-8")
    launcher = "ecorex.exe" if platform == "windows" else "ecorex"
    (core / "bin" / launcher).write_bytes(b"signed-test-launcher")
    bootstrap = tmp_path / "bootstrap-companion"
    (bootstrap / "bin").mkdir(parents=True)
    bootstrap_launcher = (
        "ecorex-bootstrap.exe"
        if platform == "windows"
        else "ecorex-bootstrap"
    )
    installer_launcher = (
        "EcoreX Installer.cmd"
        if platform == "windows"
        else "EcoreX Installer.command"
    )
    (bootstrap / "bin" / bootstrap_launcher).write_bytes(
        b"signed-test-bootstrap"
    )
    (bootstrap / installer_launcher).write_text(
        "@echo off\r\n" if platform == "windows" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    bootstrap_executables = [f"bin/{bootstrap_launcher}", installer_launcher]
    helper_digest = ""
    if platform == "windows":
        helper = bootstrap / "bin" / "ecorex-sandbox-host.exe"
        helper.write_bytes(
            b"signed-test-sandbox-helper"
        )
        helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
        bootstrap_executables.append("bin/ecorex-sandbox-host.exe")
    release_public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    publication_public = publication_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    _major, minor, patch = (int(value) for value in __version__.split("."))
    sequence = minor * 1_000_000 + patch + 1
    minimum_payload = (
        b"ecorex.bootstrap-minimum-stable.v1\0"
        + str(sequence).encode()
        + b"\0"
        + __version__.encode()
    )
    (bootstrap / "bootstrap-config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "public_index_url": (
                    "https://dl.ecoremedia.net/ecorex-agent/"
                    "public-bootstrap-index.json"
                ),
                "release_public_keys": {
                    signer.key_id: base64.b64encode(release_public).decode()
                },
                "publication_public_keys": {
                    "publication-test-key": base64.b64encode(
                        publication_public
                    ).decode()
                },
                "sandbox_helper_sha256": helper_digest,
                "minimum_stable": {
                    "sequence": sequence,
                    "version": __version__,
                    "signature": {
                        "algorithm": "ed25519",
                        "key_id": signer.key_id,
                        "value": base64.b64encode(
                            private.sign(minimum_payload)
                        ).decode(),
                    },
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sources = (
        ReleaseSource(
            "mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"
        ),
        ReleaseSource(
            "github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"
        ),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
    )
    definitions = (
        ArtifactBuildInput(
            source_dir=core,
            kind=ArtifactKind.CORE,
            platform=platform,
            architecture=architecture,
            executable_paths=(f"bin/{launcher}",),
        ),
        ArtifactBuildInput(
            source_dir=bootstrap,
            kind=ArtifactKind.BOOTSTRAP,
            platform=platform,
            architecture=architecture,
            executable_paths=tuple(bootstrap_executables),
        ),
        *(
            _pack(tmp_path / "packs", pack_id)
            for pack_id in REQUIRED_CAPABILITY_PACK_IDS
        ),
    )
    built = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at=datetime.now(UTC).isoformat(),
            sources=sources,
            artifacts=definitions,
        ),
        tmp_path / "release",
    )
    public = tmp_path / "release.pub"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return built, f"{signer.key_id}={public}"


def _sandbox_test_boundary(tmp_path: Path, monkeypatch) -> dict[str, str]:
    """Replace the native ACL boundary only in the signed handoff unit test.

    Real AppContainer provisioning, strict attestation and cleanup are covered
    by the Windows native suites.  This module verifies manifest/Pack atomicity
    and still requires a security marker on Windows so it cannot silently
    regress to an unsecured InstallCoordinator path.
    """

    platform, _architecture = _target()
    if platform != "windows":
        return {}

    class _SandboxSecurity:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def prepare(self, *_args: Any, **_kwargs: Any) -> dict[str, object]:
            return {"state": "prepared"}

        def attest(self, *_args: Any, **_kwargs: Any) -> dict[str, object]:
            return {"contract": "test-appcontainer-attestation-v1"}

        def validate(self, *_args: Any, **_kwargs: Any) -> bool:
            marker = _args[-1]
            return marker == {"contract": "test-appcontainer-attestation-v1"}

        def cleanup_failed(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def cleanup_abandoned(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def cleanup_slot(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(install_local, "WindowsSandboxSlotSecurity", _SandboxSecurity)
    helper = tmp_path / "bootstrap" / "ecorex-sandbox-host.exe"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"unit-test-native-security-boundary")
    return {
        "sandbox_helper": str(helper),
        "sandbox_helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }


def test_signed_bootstrap_handoff_stages_core_and_six_packs_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    built, trust = _release(tmp_path)
    sandbox = _sandbox_test_boundary(tmp_path, monkeypatch)

    result = install_local.install(
        manifest_path=str(built.manifest_path),
        artifacts_path=str(built.output_dir),
        install_root=str(tmp_path / "install"),
        trusted_public_keys=(trust,),
        desktop_directory=str(tmp_path / "Desktop"),
        **sandbox,
    )

    assert result["state"] == "healthchecking"
    slot = tmp_path / "install" / "slots" / str(result["slot_id"])
    for pack_id in REQUIRED_CAPABILITY_PACK_IDS:
        pack_root = slot / "payload" / "capability-packs" / pack_id
        assert len(tuple(pack_root.glob("*.zip"))) == 1
        assert len(tuple(pack_root.glob("*.json"))) == 1


def test_signed_bootstrap_handoff_upgrades_an_existing_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import sys

    test_module = sys.modules[__name__]
    release_private = Ed25519PrivateKey.generate()
    publication_private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(release_builder_module, "__version__", "1.0.7")
    monkeypatch.setattr(test_module, "__version__", "1.0.7")
    previous, previous_trust = _release(
        tmp_path / "previous",
        private=release_private,
        publication_private=publication_private,
    )
    previous_sandbox = _sandbox_test_boundary(tmp_path / "previous", monkeypatch)
    first = install_local.install(
        manifest_path=str(previous.manifest_path),
        artifacts_path=str(previous.output_dir),
        install_root=str(tmp_path / "install"),
        trusted_public_keys=(previous_trust,),
        desktop_directory=str(tmp_path / "Desktop"),
        **previous_sandbox,
    )
    platform, architecture = _target()
    verifier = Ed25519SignatureVerifier(
        install_local._read_public_keys((previous_trust,))
    )
    activations = ProvisionalActivationController(
        tmp_path / "install",
        verifier=verifier,
        host_platform=platform,
        host_architecture=architecture,
        pack_content_verifier=verify_product_capability_pack,
    )
    intent = activations.load_intent()
    assert intent is not None
    companion = BootstrapCompanionInstaller(
        tmp_path / "install",
        platform=platform,
        architecture=architecture,
        verifier=verifier,
        desktop_directory=tmp_path / "Desktop",
    )
    companion.prepare_transaction(first["transaction_id"])
    activations.confirm(first["transaction_id"], intent.health_identity)
    assert activations.mark_data_barrier_crossed(first["slot_id"]) is True
    companion.commit_activation(first["transaction_id"])
    registration = {
        "account_id": "account-upgrade-test",
        "organization_id": "organization-upgrade-test",
        "lease_id": "lease-upgrade-test",
        "lease_digest": "a" * 64,
        "session_generation": 1,
        "lease_revision": 1,
    }
    registration_coordinator = InstallCoordinator(
        tmp_path / "install",
        fetcher=LocalSourceFetcher({}),
        health_checker=lambda _slot: False,
        verifier=verifier,
        host_platform=platform,
        host_architecture=architecture,
        pack_content_verifier=verify_product_capability_pack,
    )
    assert registration_coordinator.mark_runtime_ready(registration) is True

    user_data = tmp_path / "install" / "data" / "user-state.json"
    user_data.parent.mkdir(parents=True, exist_ok=True)
    user_data.write_text('{"conversation":"preserved"}', encoding="utf-8")

    monkeypatch.setattr(release_builder_module, "__version__", "1.0.8")
    monkeypatch.setattr(test_module, "__version__", "1.0.8")
    current, current_trust = _release(
        tmp_path / "current",
        private=release_private,
        publication_private=publication_private,
    )
    previous_manifest = json.loads(previous.manifest_path.read_text(encoding="utf-8"))
    current_manifest = json.loads(current.manifest_path.read_text(encoding="utf-8"))
    assert previous_manifest["version"] == "1.0.7"
    assert current_manifest["version"] == "1.0.8"
    assert (
        next(
            artifact["sha256"]
            for artifact in previous_manifest["artifacts"]
            if artifact["artifact_id"].startswith("core-")
        )
        != next(
            artifact["sha256"]
            for artifact in current_manifest["artifacts"]
            if artifact["artifact_id"].startswith("core-")
        )
    )
    current_sandbox = _sandbox_test_boundary(tmp_path / "current", monkeypatch)
    upgraded = install_local.install(
        manifest_path=str(current.manifest_path),
        artifacts_path=str(current.output_dir),
        install_root=str(tmp_path / "install"),
        trusted_public_keys=(current_trust,),
        desktop_directory=str(tmp_path / "Desktop"),
        **current_sandbox,
    )

    pointers = json.loads(
        (tmp_path / "install" / "slot-pointers.json").read_text(encoding="utf-8")
    )
    assert upgraded["state"] == "healthchecking"
    assert upgraded["slot_id"] != first["slot_id"]
    assert pointers["current"] == upgraded["slot_id"]
    assert pointers["previous"] == first["slot_id"]
    upgraded_marker = (
        tmp_path
        / "install"
        / "slots"
        / str(upgraded["slot_id"])
        / "payload"
        / "version-marker.txt"
    )
    assert upgraded_marker.read_text(encoding="utf-8") == "1.0.8"
    assert user_data.read_text(encoding="utf-8") == '{"conversation":"preserved"}'


def test_signed_bootstrap_handoff_rejects_tampered_pack_before_pointer_switch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    built, trust = _release(tmp_path)
    sandbox = _sandbox_test_boundary(tmp_path, monkeypatch)
    pack = next(
        path
        for artifact_id, path in built.artifact_paths.items()
        if artifact_id.startswith("capability-pack-browser-")
        and not artifact_id.endswith("-manifest")
    )
    pack.write_bytes(pack.read_bytes() + b"tampered")

    with pytest.raises(Exception):
        install_local.install(
            manifest_path=str(built.manifest_path),
            artifacts_path=str(built.output_dir),
            install_root=str(tmp_path / "install"),
            trusted_public_keys=(trust,),
            desktop_directory=str(tmp_path / "Desktop"),
            **sandbox,
        )

    assert (tmp_path / "install" / "slot-pointers.json").exists() is False

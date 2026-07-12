from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.bootstrap import install_local
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
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


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


def _release(tmp_path: Path):
    platform, architecture = _target()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    launcher = "ecorex.exe" if platform == "windows" else "ecorex"
    (core / "bin" / launcher).write_bytes(b"signed-test-launcher")
    private = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("bootstrap-test-key", private)
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
        **sandbox,
    )

    assert result["state"] == "healthchecking"
    slot = tmp_path / "install" / "slots" / str(result["slot_id"])
    for pack_id in REQUIRED_CAPABILITY_PACK_IDS:
        pack_root = slot / "payload" / "capability-packs" / pack_id
        assert len(tuple(pack_root.glob("*.zip"))) == 1
        assert len(tuple(pack_root.glob("*.json"))) == 1


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
            **sandbox,
        )

    assert (tmp_path / "install" / "slot-pointers.json").exists() is False

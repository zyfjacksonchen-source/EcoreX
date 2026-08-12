from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator, ValidationError
import pytest

from ecorex.capabilities import (
    CapabilityPackManifest,
    builtin_capability_registry,
    tool_spec_digest,
    verify_capability_pack,
)
from ecorex.pack_catalog import (
    CAPABILITY_PACK_SERVICE_IDS,
    CAPABILITY_PACK_TOOL_IDS,
    REQUIRED_CAPABILITY_PACK_IDS,
)
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildError,
    ReleaseBuildSpec,
    ReleaseBuilder,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
)


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.test/v1"),
        ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://github.test/v1"),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.test/v1"),
    )


def _pack(
    source: Path,
    *,
    pack_id: str = "image",
    tool_ids: tuple[str, ...] = ("imagegen",),
    service_ids: tuple[str, ...] = (),
):
    return ArtifactBuildInput(
        source_dir=source,
        kind=ArtifactKind.CAPABILITY_PACK,
        platform="windows",
        architecture="x64",
        pack_id=pack_id,
        pack_tool_ids=tool_ids,
        pack_service_ids=service_ids,
        runtime_api_version="1.0.0",
    )


def _spec(*artifacts: ArtifactBuildInput) -> ReleaseBuildSpec:
    return ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=_sources(),
        artifacts=artifacts,
    )


def test_release_builder_emits_double_signed_verifiable_capability_pack(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pack-source"
    source.mkdir()
    (source / "adapter.bin").write_bytes(b"managed image adapter dependencies")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer = Ed25519MemorySigner("release-key", private)

    built = ReleaseBuilder(signer).build(_spec(_pack(source)), tmp_path / "release")

    pack_id = "capability-pack-image-windows-x64"
    sidecar_id = f"{pack_id}-manifest"
    assert set(built.artifact_paths) == {pack_id, sidecar_id}
    verifier = Ed25519SignatureVerifier({"release-key": public})
    verify_manifest_signature(built.manifest, verifier)
    for artifact in built.manifest.artifacts:
        verify_artifact_file(
            built.artifact_paths[artifact.artifact_id],
            built.manifest,
            artifact,
            verifier,
        )

    sidecar = CapabilityPackManifest.from_bytes(
        built.artifact_paths[sidecar_id].read_bytes()
    )
    verified = verify_capability_pack(
        sidecar,
        built.artifact_paths[pack_id],
        verifier=verifier,
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )
    assert verified.manifest.pack_id == "image"
    assert [binding.tool_id for binding in verified.manifest.tools] == ["imagegen"]
    imagegen = builtin_capability_registry().get("imagegen")
    assert verified.manifest.tools[0].tool_version == imagegen.version
    assert verified.manifest.tools[0].spec_sha256 == tool_spec_digest(imagegen)
    assert verified.manifest.services == ()
    metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
    assert {entry["kind"] for entry in metadata["artifacts"]} == {
        "capability-pack",
        "capability_pack_manifest",
    }

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "release/v1/manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    projection = built.manifest.to_dict()
    archive = next(
        item
        for item in projection["artifacts"]
        if item["artifact_id"] == pack_id
    )
    archive["size_bytes"] = 200 * 1024 * 1024
    validator.validate(projection)
    archive["artifact_id"] = "core-windows-x64"
    with pytest.raises(ValidationError, match="greater than the maximum"):
        validator.validate(projection)


def test_release_builder_emits_verifiable_service_only_pack_without_fake_tool(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ocr-source"
    source.mkdir()
    (source / "runtime-inventory.json").write_text("{}", encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer = Ed25519MemorySigner("release-key", private)

    built = ReleaseBuilder(signer).build(
        _spec(
            _pack(
                source,
                pack_id="ocr",
                tool_ids=(),
                service_ids=CAPABILITY_PACK_SERVICE_IDS["ocr"],
            )
        ),
        tmp_path / "release",
    )
    artifact_id = "capability-pack-ocr-windows-x64"
    manifest = CapabilityPackManifest.from_bytes(
        built.artifact_paths[f"{artifact_id}-manifest"].read_bytes()
    )
    verified = verify_capability_pack(
        manifest,
        built.artifact_paths[artifact_id],
        verifier=Ed25519SignatureVerifier({"release-key": public}),
        platform="windows",
        architecture="x64",
        runtime_api_version="1.0.0",
    )

    assert verified.manifest.schema_version == 2
    assert verified.manifest.tools == ()
    assert [binding.service_id for binding in verified.manifest.services] == [
        CAPABILITY_PACK_SERVICE_IDS["ocr"][0]
    ]


def test_cow_release_catalog_builds_without_a_sandbox_tool_pack(tmp_path: Path) -> None:
    expected = ("browser", "channels", "image", "ocr", "office")
    assert REQUIRED_CAPABILITY_PACK_IDS == expected
    assert tuple(CAPABILITY_PACK_TOOL_IDS) == expected
    assert tuple(CAPABILITY_PACK_SERVICE_IDS) == expected

    inputs = []
    for pack_id in expected:
        source = tmp_path / pack_id
        source.mkdir()
        (source / "payload.bin").write_bytes(pack_id.encode())
        inputs.append(
            _pack(
                source,
                pack_id=pack_id,
                tool_ids=CAPABILITY_PACK_TOOL_IDS[pack_id],
                service_ids=CAPABILITY_PACK_SERVICE_IDS[pack_id],
            )
        )
    built = ReleaseBuilder(
        Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())
    ).build(_spec(*inputs), tmp_path / "release")

    assert set(built.artifact_paths) == {
        artifact_id
        for pack_id in expected
        for artifact_id in (
            f"capability-pack-{pack_id}-windows-x64",
            f"capability-pack-{pack_id}-windows-x64-manifest",
        )
    }


def test_capability_pack_build_rejects_unbound_or_ambiguous_contracts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pack-source"
    source.mkdir()
    (source / "adapter.bin").write_bytes(b"adapter")
    signer = Ed25519MemorySigner("release-key", Ed25519PrivateKey.generate())

    with pytest.raises(ReleaseBuildError, match="does not require pack"):
        ReleaseBuilder(signer).build(
            _spec(_pack(source, tool_ids=("read",))),
            tmp_path / "wrong-tool",
        )
    with pytest.raises(ReleaseBuildError, match="duplicate release target"):
        ReleaseBuilder(signer).build(
            _spec(_pack(source), _pack(source)),
            tmp_path / "duplicate",
        )


def test_non_pack_artifacts_cannot_claim_pack_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only capability-pack"):
        ArtifactBuildInput(
            source_dir=tmp_path,
            kind=ArtifactKind.CORE,
            platform="windows",
            architecture="x64",
            pack_id="image",
            pack_tool_ids=("imagegen",),
        )


def test_capability_pack_requires_tool_or_service_binding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tool or service"):
        _pack(tmp_path, tool_ids=(), service_ids=())

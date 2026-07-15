from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex import __version__
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from ecorex.server import WebBundleManifest
from ecorex.server.bundle import load_verified_web_bundle
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
)


RUNTIME_MARKER = "<!--__ECOREX_RUNTIME_CONFIG__-->"


def _write_asset(dist: Path, stem: str, suffix: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    relative = f"assets/{stem}.{digest[:12]}{suffix}"
    target = dist / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return relative


def _web_dist(root: Path, *, css: bytes = b"body{margin:0}\n") -> Path:
    root.mkdir(parents=True)
    javascript_path = _write_asset(
        root,
        "app",
        ".js",
        b"document.body.dataset.ecorex='ready';\n",
    )
    css_path = _write_asset(root, "app", ".css", css)
    index = (
        "<!doctype html><html><head>"
        f"{RUNTIME_MARKER}"
        f'<script type="module" src="/{javascript_path}"></script>'
        f'<link rel="stylesheet" href="/{css_path}">'
        "</head><body><div id=\"root\"></div></body></html>"
    )
    (root / "index.html").write_text(index, encoding="utf-8", newline="\n")
    return root


def _core(root: Path) -> ArtifactBuildInput:
    root.mkdir(parents=True)
    (root / "ecorex-runtime.txt").write_text("runtime\n", encoding="utf-8")
    return ArtifactBuildInput(
        source_dir=root,
        kind=ArtifactKind.CORE,
        platform="windows",
        architecture="x64",
    )


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/releases",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/releases",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/releases",
        ),
    )


def _spec(core: ArtifactBuildInput, dist: Path) -> ReleaseBuildSpec:
    return ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=_sources(),
        artifacts=(core,),
        web_bundle=WebBundleBuildInput(dist),
    )


def _signer():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return Ed25519MemorySigner("release-web-key", private), public


def test_builder_emits_server_compatible_signed_web_manifest_artifact(
    tmp_path: Path,
) -> None:
    signer, public = _signer()
    dist = _web_dist(tmp_path / "dist")
    result = ReleaseBuilder(signer).build(
        _spec(_core(tmp_path / "core"), dist),
        tmp_path / "release",
    )

    artifact = result.manifest.artifact("web-manifest")
    assert artifact.file_name == "web-manifest.json"
    assert (artifact.platform, artifact.architecture) == ("all", "all")
    web_manifest_path = result.artifact_paths["web-manifest"]
    web_manifest = WebBundleManifest.from_json(web_manifest_path.read_bytes())
    assert web_manifest.release_id == result.manifest.release_id
    assert web_manifest.version == __version__
    assert web_manifest.build_digest == result.manifest.build_digest
    assert web_manifest.entrypoint == "index.html"
    assert [record.path for record in web_manifest.files] == sorted(
        record.path for record in web_manifest.files
    )
    index_record = next(
        record for record in web_manifest.files if record.path == "index.html"
    )
    assert index_record.immutable is False
    assert all(
        record.immutable
        for record in web_manifest.files
        if record.path != "index.html"
    )
    assert web_manifest.bundle_sha256 == WebBundleManifest.compute_bundle_sha256(
        web_manifest.files
    )

    verifier = Ed25519SignatureVerifier({"release-web-key": public})
    verify_manifest_signature(result.manifest, verifier)
    verify_artifact_file(web_manifest_path, result.manifest, artifact, verifier)
    assert verifier.verify(web_manifest.canonical_payload(), web_manifest.signature) is True

    loaded = load_verified_web_bundle(
        web_root=dist,
        release_manifest_path=result.manifest_path,
        web_manifest_path=web_manifest_path,
        trusted_public_keys={"release-web-key": public},
    )
    assert loaded.web_manifest == web_manifest
    assert set(loaded.files) == {record.path for record in web_manifest.files}
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert next(
        item for item in metadata["artifacts"] if item["artifact_id"] == "web-manifest"
    )["kind"] == "web_manifest"
    sbom = json.loads(result.sbom_path.read_text(encoding="utf-8"))
    component_names = {component["name"] for component in sbom["components"]}
    assert "web-manifest.json" in component_names
    assert {record.path for record in web_manifest.files} <= component_names


def test_web_manifest_and_release_outputs_are_deterministic(tmp_path: Path) -> None:
    signer, _public = _signer()
    first = ReleaseBuilder(signer).build(
        _spec(_core(tmp_path / "core-a"), _web_dist(tmp_path / "dist-a")),
        tmp_path / "release-a",
    )
    second = ReleaseBuilder(signer).build(
        _spec(_core(tmp_path / "core-b"), _web_dist(tmp_path / "dist-b")),
        tmp_path / "release-b",
    )

    assert first.manifest == second.manifest
    assert set(path.name for path in first.output_dir.iterdir()) == set(
        path.name for path in second.output_dir.iterdir()
    )
    for first_path in first.output_dir.iterdir():
        assert first_path.read_bytes() == (second.output_dir / first_path.name).read_bytes()


def test_web_bundle_changes_are_bound_into_the_release_build_digest(tmp_path: Path) -> None:
    signer, _public = _signer()
    first = ReleaseBuilder(signer).build(
        _spec(_core(tmp_path / "core-a"), _web_dist(tmp_path / "dist-a")),
        tmp_path / "release-a",
    )
    second = ReleaseBuilder(signer).build(
        _spec(
            _core(tmp_path / "core-b"),
            _web_dist(tmp_path / "dist-b", css=b"body{margin:4px}\n"),
        ),
        tmp_path / "release-b",
    )

    assert first.manifest.build_digest != second.manifest.build_digest
    assert first.manifest.release_id != second.manifest.release_id


def test_exact_allowlist_keeps_hash_named_lazy_dependencies(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    lazy_path = _write_asset(dist, "lazy", ".js", b"export const ready=true;\n")
    main_content = f'import("./{Path(lazy_path).name}");\n'.encode("utf-8")
    main_path = _write_asset(dist, "app", ".js", main_content)
    (dist / "index.html").write_text(
        f"<html><head>{RUNTIME_MARKER}"
        f'<script type="module" src="/{main_path}"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    signer, _public = _signer()

    result = ReleaseBuilder(signer).build(
        _spec(_core(tmp_path / "core"), dist), tmp_path / "release"
    )
    web_manifest = WebBundleManifest.from_json(
        result.artifact_paths["web-manifest"].read_bytes()
    )

    assert {record.path for record in web_manifest.files} == {
        "index.html",
        main_path,
        lazy_path,
    }


def test_builder_can_construct_a_web_manifest_only_release_candidate(
    tmp_path: Path,
) -> None:
    signer, _public = _signer()
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.CANARY,
        created_at="2026-07-10T12:00:00+08:00",
        sources=_sources(),
        artifacts=(),
        web_bundle=WebBundleBuildInput(_web_dist(tmp_path / "dist")),
    )

    result = ReleaseBuilder(signer).build(spec, tmp_path / "release")

    assert [artifact.artifact_id for artifact in result.manifest.artifacts] == [
        "web-manifest"
    ]

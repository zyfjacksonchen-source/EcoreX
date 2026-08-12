from __future__ import annotations

import hashlib
import copy
import json
import os
import runpy
import shutil
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex import __version__
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    CoreDeltaBuildInput,
    MAX_CORE_EXPANDED_BYTES,
    ReleaseBuildError,
    ReleaseBuilder,
    ReleaseBuildSpec,
)
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
    verify_artifact_file,
    verify_manifest_signature,
    select_core_delta_artifact,
)


def _sources(version: str = __version__) -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource(
            "github-cn",
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
    )


def _source_tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "empty").mkdir()
    (root / "bin" / "ecorex").write_bytes(b"#!/bin/sh\necho EcoreX\n")
    (root / "config" / "defaults.json").write_text(
        '{"theme":"system"}\n', encoding="utf-8", newline="\n"
    )
    return root


def _input(root: Path, *, platform: str = "windows", architecture: str = "x64"):
    return ArtifactBuildInput(
        source_dir=root,
        kind=ArtifactKind.CORE,
        platform=platform,
        architecture=architecture,
        executable_paths=("bin/ecorex",),
    )


def _spec(*artifacts: ArtifactBuildInput) -> ReleaseBuildSpec:
    return ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=_sources(),
        artifacts=artifacts,
    )


def _signer() -> tuple[Ed25519MemorySigner, bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return Ed25519MemorySigner("release-key-2026", private), public_raw, private_raw


def test_builder_is_deterministic_and_uses_the_single_version_source(
    tmp_path: Path,
) -> None:
    signer, public_key, private_raw = _signer()
    first_source = _source_tree(tmp_path / "source-a")
    second_source = _source_tree(tmp_path / "source-b")
    first = ReleaseBuilder(signer).build(
        _spec(_input(first_source)), tmp_path / "release-a"
    )
    second = ReleaseBuilder(signer).build(
        _spec(_input(second_source)), tmp_path / "release-b"
    )

    assert first.manifest.version == __version__
    assert first.manifest == second.manifest
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.metadata_path.read_bytes() == second.metadata_path.read_bytes()
    assert first.sbom_path.read_bytes() == second.sbom_path.read_bytes()
    first_zip = first.artifact_paths[first.manifest.artifacts[0].artifact_id]
    second_zip = second.artifact_paths[second.manifest.artifacts[0].artifact_id]
    assert first_zip.read_bytes() == second_zip.read_bytes()

    verifier = Ed25519SignatureVerifier({"release-key-2026": public_key})
    verify_manifest_signature(first.manifest, verifier)
    verify_artifact_file(
        first_zip, first.manifest, first.manifest.artifacts[0], verifier
    )
    all_output = b"".join(
        path.read_bytes() for path in first.output_dir.iterdir() if path.is_file()
    )
    assert private_raw not in all_output
    assert "private" not in repr(signer).lower()


def test_macos_native_inventory_emits_semantic_cyclonedx_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.release.builder as builder_module

    signer, _, _ = _signer()
    source = _source_tree(tmp_path / "source")
    pack = source / "bin" / "pack-python"
    library = pack / "lib" / "libssl.3.dylib"
    notice = pack / "licenses" / "python-macos-installer-License.rtf"
    license_contract = builder_module.MACOS_NATIVE_LICENSES["openssl"]
    license_text = pack / license_contract.archive_path
    library.parent.mkdir(parents=True)
    notice.parent.mkdir(parents=True)
    license_text.parent.mkdir(parents=True, exist_ok=True)
    library.write_bytes(b"relocated-openssl")
    notice.write_bytes(b"OpenSSL 3.0.13")
    license_text.write_bytes(
        (
            Path(__file__).resolve().parents[2] / license_contract.repository_path
        ).read_bytes()
    )
    notice_contract = {
        "path": "licenses/python-macos-installer-License.rtf",
        "size_bytes": len(notice.read_bytes()),
        "sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
        "tokens": (b"OpenSSL 3.0.13",),
    }
    monkeypatch.setattr(builder_module, "PYTHON_MACOS_LICENSE", notice_contract)
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    inventory = {
        "architecture": "arm64",
        "components": [
            {
                "license": "Apache-2.0",
                "license_text": license_contract.archive_path,
                "name": "OpenSSL",
                "path": "lib/libssl.3.dylib",
                "sha256": digest,
                "source_sha256": (
                    "22f984c4947e9ea11528ad86d219f145ae9cd45983e3850d34d781d1b38ce5d6"
                ),
                "version": "3.0.13",
            }
        ],
        "distribution": dict(builder_module.PYTHON_MACOS_DISTRIBUTION),
        "license_notice": {
            "path": notice_contract["path"],
            "sha256": notice_contract["sha256"],
            "size_bytes": notice_contract["size_bytes"],
        },
        "license_texts": [
            {
                "path": license_contract.archive_path,
                "provenance": license_contract.provenance,
                "sha256": license_contract.sha256,
                "size_bytes": license_contract.size_bytes,
                "source_archive_sha256": license_contract.source_archive_sha256,
                "source_internal_path": license_contract.source_internal_path,
                "source_url": license_contract.source_url,
            }
        ],
        "platform": "macos",
        "schema_version": 1,
    }
    (pack / "native-components.json").write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result = ReleaseBuilder(signer).build(
        _spec(_input(source, platform="macos", architecture="arm64")),
        tmp_path / "release",
    )
    sbom = json.loads(result.sbom_path.read_text(encoding="utf-8"))
    component = next(
        item
        for item in sbom["components"]
        if item["bom-ref"] == "native:macos:arm64:lib/libssl.3.dylib"
    )

    assert component["type"] == "library"
    assert component["name"] == "OpenSSL"
    assert component["version"] == "3.0.13"
    assert component["licenses"] == [{"license": {"id": "Apache-2.0"}}]
    assert component["hashes"] == [{"alg": "SHA-256", "content": digest}]
    assert component["externalReferences"][1]["type"] == "other"
    checker = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "check-v1-candidate-supply-chain.py"
        )
    )
    checker["_verify_macos_native_sbom"].__globals__["PYTHON_MACOS_LICENSE"] = (
        notice_contract
    )
    artifact = result.manifest.artifacts[0]
    checker["_verify_macos_native_sbom"](
        result.artifact_paths[artifact.artifact_id],
        artifact,
        sbom["components"],
    )
    with pytest.raises(ValueError, match="candidate_native_sbom_mismatch"):
        checker["_verify_native_reference_union"](
            [
                "native:macos:arm64:lib/libssl.3.dylib",
                "native:macos:x86_64:lib/libssl.3.dylib",
            ],
            {"native:macos:arm64:lib/libssl.3.dylib"},
        )
    mutations = []
    for field, value in (
        ("version", "tampered"),
        ("licenses", [{"license": {"name": "Fake"}}]),
    ):
        changed = copy.deepcopy(sbom["components"])
        target = next(
            item
            for item in changed
            if item["bom-ref"] == "native:macos:arm64:lib/libssl.3.dylib"
        )
        target[field] = value
        mutations.append(changed)
    changed = copy.deepcopy(sbom["components"])
    target = next(item for item in changed if item.get("type") == "library")
    target["externalReferences"][0]["url"] = "https://invalid.example/pkg"
    mutations.append(changed)
    changed = copy.deepcopy(sbom["components"])
    target = next(item for item in changed if item.get("type") == "library")
    next(
        item for item in target["properties"] if item["name"] == "ecorex:source-sha256"
    )["value"] = "0" * 64
    mutations.append(changed)
    mutations.append(
        [
            item
            for item in copy.deepcopy(sbom["components"])
            if item.get("type") != "library"
        ]
    )
    changed = copy.deepcopy(sbom["components"])
    extra = copy.deepcopy(component)
    extra["bom-ref"] = "native:macos:arm64:lib/extra.dylib"
    changed.append(extra)
    mutations.append(changed)
    for changed in mutations:
        with pytest.raises(ValueError, match="candidate_native_sbom_mismatch"):
            checker["_verify_macos_native_sbom"](
                result.artifact_paths[artifact.artifact_id],
                artifact,
                changed,
            )

    def rewritten_archive(
        name: str,
        mutate: object,
    ) -> Path:
        source_archive = result.artifact_paths[artifact.artifact_id]
        destination = tmp_path / name
        with (
            zipfile.ZipFile(source_archive) as source_zip,
            zipfile.ZipFile(
                destination, "w", compression=zipfile.ZIP_DEFLATED
            ) as output_zip,
        ):
            for info in source_zip.infolist():
                payload = source_zip.read(info)
                if (
                    info.filename == "bin/pack-python/native-components.json"
                    and mutate == "source"
                ):
                    value = json.loads(payload)
                    value["components"][0]["source_sha256"] = "0" * 64
                    payload = (
                        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode()
                if (
                    info.filename
                    == "bin/pack-python/licenses/python-macos-installer-License.rtf"
                    and mutate == "notice"
                ):
                    payload = b"tampered notice"
                if (
                    info.filename == f"bin/pack-python/{license_contract.archive_path}"
                    and mutate == "license-text"
                ):
                    payload = b"x" * len(payload)
                if (
                    info.filename == "bin/pack-python/native-components.json"
                    and mutate == "omit-component"
                ):
                    value = json.loads(payload)
                    value["components"] = []
                    value["license_notice"] = None
                    value["license_texts"] = []
                    payload = (
                        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
                    ).encode()
                output_zip.writestr(info.filename, payload)
        return destination

    for name, mutation in (
        ("bad-source.zip", "source"),
        ("bad-notice.zip", "notice"),
        ("bad-license-text.zip", "license-text"),
        ("omitted-component.zip", "omit-component"),
    ):
        with pytest.raises(ValueError, match="candidate_native_inventory_invalid"):
            checker["_verify_macos_native_sbom"](
                rewritten_archive(name, mutation),
                artifact,
                sbom["components"],
            )

    component["version"] = "tampered"
    with pytest.raises(ValueError, match="candidate_native_sbom_mismatch"):
        checker["_verify_macos_native_sbom"](
            result.artifact_paths[artifact.artifact_id],
            artifact,
            sbom["components"],
        )


def test_all_macos_native_license_texts_are_tracked_regular_exact_files() -> None:
    from ecorex.release.macos_native_contract import (
        MACOS_NATIVE_LICENSES,
        MACOS_PACK_PYTHON_RUNTIME_DYLIBS,
    )

    root = Path(__file__).resolve().parents[2]
    assert MACOS_PACK_PYTHON_RUNTIME_DYLIBS == frozenset(
        {"lib/libpython3.11.dylib"}
    )
    for contract in MACOS_NATIVE_LICENSES.values():
        path = root / contract.repository_path
        assert path.is_file()
        assert not path.is_symlink()
        payload = path.read_bytes()
        assert len(payload) == contract.size_bytes
        assert hashlib.sha256(payload).hexdigest() == contract.sha256


def test_release_scoped_builder_resolves_cn_proxy_to_github_tag(tmp_path: Path) -> None:
    signer, _, _ = _signer()
    sources = (
        ReleaseSource(
            "github-cn",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://ghproxy.net/https://github.com/ecorex/installers/releases/download",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.com/ecorex/installers/releases/download",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/ecorex/v1.0.0/stable",
        ),
    )
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-16T12:00:00+08:00",
        sources=sources,
        artifacts=(_input(_source_tree(tmp_path / "source")),),
        release_scoped_sources=True,
    )
    built = ReleaseBuilder(signer).build(spec, tmp_path / "release")
    assert built.manifest.sources[0].base_url.endswith(
        f"/releases/download/v{__version__}"
    )
    assert built.manifest.sources[1].base_url.endswith(
        f"/releases/download/v{__version__}"
    )
    assert built.manifest.sources[2].base_url.endswith(
        f"/stable/{built.manifest.release_id}"
    )


def test_build_identity_binds_the_signed_source_roots(tmp_path: Path) -> None:
    signer, _public, _private = _signer()
    first_spec = _spec(_input(_source_tree(tmp_path / "source-a")))
    changed_sources = (
        replace(
            first_spec.sources[0],
            base_url="https://other-mirror.example/ecorex/v1.0.0",
        ),
        *first_spec.sources[1:],
    )
    second_spec = replace(
        _spec(_input(_source_tree(tmp_path / "source-b"))),
        sources=changed_sources,
    )

    first = ReleaseBuilder(signer).build(first_spec, tmp_path / "release-a")
    second = ReleaseBuilder(signer).build(second_spec, tmp_path / "release-b")

    assert first.manifest.build_digest != second.manifest.build_digest
    assert first.manifest.release_id != second.manifest.release_id


def test_builder_derives_and_signs_beneficial_base_bound_core_delta(
    tmp_path: Path,
) -> None:
    signer, public_key, _private = _signer()
    base_source = _source_tree(tmp_path / "base-source")
    blobs = base_source / "runtime" / "blobs"
    blobs.mkdir(parents=True)
    for index in range(16):
        (blobs / f"blob-{index:02d}.bin").write_bytes(os.urandom(128 * 1024))
    base_release = ReleaseBuilder(signer).build(
        _spec(_input(base_source)), tmp_path / "base-release"
    )
    base_manifest = replace(
        base_release.manifest,
        release_id="release-stable-previous",
        # The legacy WebUI used a four-part product version. The signed v1
        # manifest is SemVer, so preserve that baseline as build metadata.
        version="0.2.9+legacy.2",
        build_digest=hashlib.sha256(b"previous-build").hexdigest(),
    )
    base_artifact = base_manifest.artifact("core-windows-x64")
    target_source = tmp_path / "target-source"
    shutil.copytree(base_source, target_source)
    (target_source / "runtime" / "blobs" / "blob-07.bin").write_bytes(
        os.urandom(128 * 1024)
    )
    spec = replace(
        _spec(_input(target_source)),
        core_delta_bases=(
            CoreDeltaBuildInput(
                base_manifest=base_manifest,
                base_artifact=base_artifact,
                base_package=base_release.artifact_paths[base_artifact.artifact_id],
            ),
        ),
    )

    target_release = ReleaseBuilder(signer).build(spec, tmp_path / "target-release")

    target_artifact = target_release.manifest.artifact("core-windows-x64")
    delta_artifact = select_core_delta_artifact(
        target_release.manifest,
        target_artifact=target_artifact,
        base_artifact=base_artifact,
    )
    assert delta_artifact is not None
    assert delta_artifact.size_bytes < target_artifact.size_bytes
    verifier = Ed25519SignatureVerifier({"release-key-2026": public_key})
    verify_manifest_signature(target_release.manifest, verifier)
    verify_artifact_file(
        target_release.artifact_paths[delta_artifact.artifact_id],
        target_release.manifest,
        delta_artifact,
        verifier,
    )
    metadata = json.loads(target_release.metadata_path.read_text(encoding="utf-8"))
    assert any(item["kind"] == "core_delta" for item in metadata["artifacts"])


def test_builder_rejects_same_or_future_delta_base_version(tmp_path: Path) -> None:
    signer, _public, _private = _signer()
    base_source = _source_tree(tmp_path / "base-source")
    base_release = ReleaseBuilder(signer).build(
        _spec(_input(base_source)), tmp_path / "base-release"
    )
    base_package = base_release.artifact_paths["core-windows-x64"]

    major, minor, patch = (int(part) for part in __version__.split("."))
    next_patch = f"{major}.{minor}.{patch + 1}"
    for version in (__version__, next_patch):
        base_manifest = replace(
            base_release.manifest,
            release_id=f"release-stable-base-{version.replace('.', '-')}",
            version=version,
            build_digest=hashlib.sha256(version.encode()).hexdigest(),
        )
        base_artifact = base_manifest.artifact("core-windows-x64")
        spec = replace(
            _spec(_input(_source_tree(tmp_path / f"target-{version}"))),
            core_delta_bases=(
                CoreDeltaBuildInput(
                    base_manifest=base_manifest,
                    base_artifact=base_artifact,
                    base_package=base_package,
                ),
            ),
        )
        with pytest.raises(ReleaseBuildError, match="earlier immutable"):
            ReleaseBuilder(signer).build(
                spec, tmp_path / f"release-{version.replace('.', '-')}"
            )


def test_deterministic_zip_has_fixed_order_timestamp_and_modes(tmp_path: Path) -> None:
    signer, _public, _private = _signer()
    result = ReleaseBuilder(signer).build(
        _spec(
            _input(
                _source_tree(tmp_path / "source"),
                platform="macos",
                architecture="arm64",
            )
        ),
        tmp_path / "release",
    )
    package = next(iter(result.artifact_paths.values()))

    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        assert [entry.filename for entry in entries] == sorted(
            entry.filename for entry in entries
        )
        assert all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in entries)
        modes = {
            entry.filename: stat.S_IMODE(entry.external_attr >> 16) for entry in entries
        }
        assert modes["bin/ecorex"] == 0o755
        assert modes["config/defaults.json"] == 0o644
        assert modes["empty/"] == 0o755


def test_builder_emits_machine_verifiable_metadata_and_sbom(tmp_path: Path) -> None:
    signer, _public, _private = _signer()
    result = ReleaseBuilder(signer).build(
        _spec(_input(_source_tree(tmp_path / "source"))), tmp_path / "release"
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    sbom = json.loads(result.sbom_path.read_text(encoding="utf-8"))
    package = next(iter(result.artifact_paths.values()))

    assert metadata["version"] == __version__
    assert metadata["build_digest"] == result.manifest.build_digest
    assert (
        metadata["manifest_sha256"]
        == hashlib.sha256(result.manifest_path.read_bytes()).hexdigest()
    )
    assert (
        metadata["sbom_sha256"]
        == hashlib.sha256(result.sbom_path.read_bytes()).hexdigest()
    )
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert any(
        component.get("name") == package.name
        and component["hashes"][0]["content"]
        == hashlib.sha256(package.read_bytes()).hexdigest()
        for component in sbom["components"]
    )


def test_builder_rejects_sbom_above_bootstrap_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.release.builder as builder_module

    signer, _public, _private = _signer()
    monkeypatch.setattr(builder_module, "MAX_RELEASE_SBOM_BYTES", 1)

    with pytest.raises(
        ReleaseBuildError,
        match="release SBOM exceeds its Bootstrap bound",
    ):
        ReleaseBuilder(signer).build(
            _spec(_input(_source_tree(tmp_path / "source"))),
            tmp_path / "release",
        )


def test_builder_rejects_core_above_bootstrap_expanded_bound(
    tmp_path: Path,
) -> None:
    signer, _public, _private = _signer()
    source = _source_tree(tmp_path / "source")
    with (source / "oversized.bin").open("wb") as stream:
        stream.truncate(MAX_CORE_EXPANDED_BYTES + 1)

    with pytest.raises(
        ReleaseBuildError,
        match=f"source expands above the {MAX_CORE_EXPANDED_BYTES} byte hard limit",
    ):
        ReleaseBuilder(signer).build(
            _spec(_input(source)),
            tmp_path / "release",
        )


@pytest.mark.parametrize(
    ("platform", "architecture"),
    [("windows", "x64"), ("macos", "arm64"), ("macos", "x64")],
)
def test_builder_supports_only_the_product_target_matrix(
    tmp_path: Path, platform: str, architecture: str
) -> None:
    signer, _public, _private = _signer()
    result = ReleaseBuilder(signer).build(
        _spec(
            _input(
                _source_tree(tmp_path / "source"),
                platform=platform,
                architecture=architecture,
            )
        ),
        tmp_path / "release",
    )
    artifact = result.manifest.artifacts[0]
    assert (artifact.platform, artifact.architecture) == (platform, architecture)
    assert [source.kind for source in result.manifest.sources] == [
        SourceKind.GITHUB_CN_MIRROR,
        SourceKind.GITHUB_RELEASE,
        SourceKind.ECOREX_CDN,
    ]


def test_builder_publishes_all_platform_cores_and_optional_bootstrap_together(
    tmp_path: Path,
) -> None:
    signer, public_key, _private = _signer()
    source = _source_tree(tmp_path / "source")
    artifacts = (
        _input(source, platform="windows", architecture="x64"),
        _input(source, platform="macos", architecture="arm64"),
        _input(source, platform="macos", architecture="x64"),
        replace(
            _input(source, platform="windows", architecture="x64"),
            kind=ArtifactKind.BOOTSTRAP,
        ),
    )

    result = ReleaseBuilder(signer).build(_spec(*artifacts), tmp_path / "release")

    assert set(result.artifact_paths) == {
        "bootstrap-windows-x64",
        "core-windows-x64",
        "core-macos-arm64",
        "core-macos-x64",
    }
    verifier = Ed25519SignatureVerifier({"release-key-2026": public_key})
    verify_manifest_signature(result.manifest, verifier)
    for artifact in result.manifest.artifacts:
        verify_artifact_file(
            result.artifact_paths[artifact.artifact_id],
            result.manifest,
            artifact,
            verifier,
        )


def test_builder_rejects_unsupported_target_and_portable_path_collision(
    tmp_path: Path,
) -> None:
    signer, _public, _private = _signer()
    source = _source_tree(tmp_path / "source")
    with pytest.raises(ReleaseBuildError, match="unsupported target"):
        ReleaseBuilder(signer).build(
            _spec(_input(source, platform="linux", architecture="x64")),
            tmp_path / "unsupported",
        )

    collision_source = _source_tree(tmp_path / "collision")
    (collision_source / "ｅcorex.txt").write_text("NFKC collision", encoding="utf-8")
    with pytest.raises(ReleaseBuildError, match="portable|normalized|collision"):
        ReleaseBuilder(signer).build(
            _spec(_input(collision_source)), tmp_path / "collision-release"
        )


def test_builder_rejects_links_without_publishing_partial_release(
    tmp_path: Path,
) -> None:
    signer, _public, _private = _signer()
    source = _source_tree(tmp_path / "source")
    link = source / "config" / "outside-link"
    try:
        link.symlink_to(tmp_path / "outside")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    destination = tmp_path / "release"

    with pytest.raises(ReleaseBuildError, match="link|reparse"):
        ReleaseBuilder(signer).build(_spec(_input(source)), destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("kind", "limit"),
    [(ArtifactKind.CORE, 512), (ArtifactKind.BOOTSTRAP, 256)],
)
def test_builder_enforces_core_and_bootstrap_compressed_limits(
    tmp_path: Path, kind: ArtifactKind, limit: int
) -> None:
    signer, _public, _private = _signer()
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.bin").write_bytes(os.urandom(4096))
    artifact = replace(_input(source), kind=kind, executable_paths=())
    builder = ReleaseBuilder(
        signer,
        max_core_bytes=limit if kind is ArtifactKind.CORE else 150 * 1024 * 1024,
        max_bootstrap_bytes=limit
        if kind is ArtifactKind.BOOTSTRAP
        else 10 * 1024 * 1024,
    )
    destination = tmp_path / "release"

    with pytest.raises(ReleaseBuildError, match="size limit"):
        builder.build(_spec(artifact), destination)
    assert not destination.exists()


def test_builder_refuses_to_replace_an_existing_release_directory(
    tmp_path: Path,
) -> None:
    signer, _public, _private = _signer()
    source = _source_tree(tmp_path / "source")
    destination = tmp_path / "release"
    destination.mkdir()
    sentinel = destination / "owner-data.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="already exists"):
        ReleaseBuilder(signer).build(_spec(_input(source)), destination)
    assert sentinel.read_text(encoding="utf-8") == "keep"

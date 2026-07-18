from __future__ import annotations

import base64
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.control_plane.cli import (
    _parser,
    _publication_coordinator,
    _release_directory_files,
    run,
)
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    GitHubAssetReceipt,
    GitHubReleaseDraft,
    ReleaseBuilder,
    ReleaseBuildSpec,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind
from ecorex.update.locking import LockUnavailable, ProductFileLock


class Publisher:
    def __init__(self) -> None:
        self.assets: list[tuple[str, str]] = []
        self.published = False
        self.closed = False

    def ensure_draft(self, *, version, channel, release_id):
        assert version == "1.0.0"
        assert channel is ReleaseChannel.STABLE
        assert release_id.startswith("release-stable-")
        return GitHubReleaseDraft(
            release_id=91,
            tag_name="v1.0.0",
            upload_url=(
                "https://uploads.github.com/repos/acme/ecorex/releases/91/"
                "assets{?name,label}"
            ),
            draft=True,
        )

    def ensure_asset(self, release, path, *, expected_sha256):
        del release
        file_path = Path(path)
        self.assets.append((file_path.name, expected_sha256))
        return GitHubAssetReceipt(
            asset_id=len(self.assets),
            name=file_path.name,
            size_bytes=file_path.stat().st_size,
            sha256=expected_sha256,
            browser_download_url=(
                "https://github.com/acme/ecorex/releases/download/v1.0.0/"
                + file_path.name
            ),
        )

    def publish(self, release):
        self.published = True
        return GitHubReleaseDraft(
            release.release_id,
            release.tag_name,
            release.upload_url,
            False,
        )

    def close(self):
        self.closed = True


def _built_release(tmp_path: Path):
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "ecorex.exe").write_bytes(b"packaged product")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource(
                "github-cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://mirror.example/ecorex/v1.0.0",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.com/acme/ecorex/releases/download/v1.0.0",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                "https://cdn.example/ecorex/v1.0.0",
            ),
        ),
        artifacts=(
            ArtifactBuildInput(
                source_dir=source,
                kind=ArtifactKind.CORE,
                platform="windows",
                architecture="x64",
                executable_paths=("bin/ecorex.exe",),
            ),
        ),
    )
    built = ReleaseBuilder(Ed25519MemorySigner("release-key", private)).build(
        spec, tmp_path / "release"
    )
    return built, public


def test_upload_github_cli_verifies_and_resumes_exact_release_bytes(
    tmp_path: Path,
    capsys,
) -> None:
    built, public = _built_release(tmp_path)
    publisher = Publisher()

    def no_control_plane(_args):
        raise AssertionError("GitHub upload must not construct a Control Plane client")

    exit_code = run(
        [
            "upload-github",
            "--release-dir",
            str(built.output_dir),
            "--owner",
            "acme",
            "--repository",
            "ecorex",
            "--trusted-key",
            "release-key=" + base64.b64encode(public).decode("ascii"),
        ],
        client_factory=no_control_plane,
        github_publisher_factory=lambda _args: publisher,
    )
    assert exit_code == 0
    assert publisher.published is False
    assert publisher.closed is True
    assert {name for name, _digest in publisher.assets} == {
        built.manifest.artifacts[0].file_name,
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
    }
    output = capsys.readouterr()
    assert '"draft": true' in output.out
    assert output.err == ""


def test_upload_cli_rejects_tampering_before_remote_asset_mutation(
    tmp_path: Path,
    capsys,
) -> None:
    built, public = _built_release(tmp_path)
    artifact = next(
        path
        for path in built.output_dir.iterdir()
        if path.suffix == ".zip"
    )
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    publisher = Publisher()
    exit_code = run(
        [
            "upload-github",
            "--release-dir",
            str(built.output_dir),
            "--owner",
            "acme",
            "--repository",
            "ecorex",
            "--trusted-key",
            "release-key=" + base64.b64encode(public).decode("ascii"),
        ],
        github_publisher_factory=lambda _args: publisher,
    )
    assert exit_code == 1
    assert publisher.assets == []
    assert publisher.closed is True
    assert "tampered" not in capsys.readouterr().err


class PublicationCoordinator:
    def __init__(self) -> None:
        self.closed = False
        self.called = False

    def publish(self, *, manifest, files, expected_sha256, publish_github):
        self.called = True
        assert publish_github is True
        assert {path.name for path in files} == set(expected_sha256)
        return SimpleNamespace(
            release_id=manifest.release_id,
            source_receipts=MappingProxyType(
                {
                    "github-cn": (
                        MappingProxyType(
                            {
                                "name": path.name,
                                "size_bytes": path.stat().st_size,
                                "sha256": expected_sha256[path.name],
                                "url": "https://mirror.example/" + path.name,
                            }
                        )
                        for path in files
                    )
                }
            ),
        )

    def close(self):
        self.closed = True


def test_publish_assets_cli_uses_one_verified_multi_source_transaction(
    tmp_path: Path,
    capsys,
) -> None:
    built, public = _built_release(tmp_path)
    publication_config = tmp_path / "publication.json"
    publication_config.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"
    coordinator = PublicationCoordinator()

    def no_control_plane(_args):
        raise AssertionError("asset publication must not construct a Control Plane client")

    exit_code = run(
        [
            "publish-assets",
            "--release-dir",
            str(built.output_dir),
            "--publication-config",
            str(publication_config),
            "--receipt",
            str(receipt),
            "--trusted-key",
            "release-key=" + base64.b64encode(public).decode("ascii"),
            "--publish-github",
        ],
        client_factory=no_control_plane,
        publication_coordinator_factory=lambda _args: coordinator,
    )
    assert exit_code == 0
    assert coordinator.called is True
    assert coordinator.closed is True
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_value["release_id"] == built.manifest.release_id
    assert receipt_value["schema_version"] == 2
    assert receipt_value["publication_policy"] == "stable-primary-only"
    assert set(receipt_value["source_receipts"]) == {"github-cn"}
    output = capsys.readouterr()
    assert '"publication_policy": "stable-primary-only"' in output.out
    assert '"github-cn"' in output.out
    assert output.err == ""


def test_publication_config_builds_fixed_https_publishers_without_secrets(
    tmp_path: Path,
) -> None:
    config = tmp_path / "publication.json"
    config.write_text(
        """
        {
          "schema_version": 1,
          "github": {
            "owner": "acme",
            "repository": "ecorex",
            "token_env": "ECOREX_GITHUB_TOKEN"
          },
          "mirror": {
            "source_id": "github-cn",
            "endpoint": "https://publisher.mirror.example/api/v1/releases",
            "allowed_hosts": ["publisher.mirror.example"],
            "public_hosts": ["mirror.example"],
            "token_env": "ECOREX_MIRROR_TOKEN"
          },
          "cdn": {
            "source_id": "cdn",
            "endpoint": "https://publisher.cdn.example/api/v1/releases",
            "allowed_hosts": ["publisher.cdn.example"],
            "public_hosts": ["cdn.example"],
            "token_env": "ECOREX_CDN_TOKEN"
          }
        }
        """,
        encoding="utf-8",
    )
    args = _parser().parse_args(
        [
            "publish-assets",
            "--release-dir",
            str(tmp_path),
            "--publication-config",
            str(config),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--trusted-key",
            "release-key=" + base64.b64encode(b"x" * 32).decode("ascii"),
        ]
    )
    coordinator = _publication_coordinator(args)
    try:
        assert coordinator.mirror.source_id == "github-cn"
        assert coordinator.cdn is not None
        assert coordinator.cdn.source_id == "cdn"
        assert coordinator.github.owner == "acme"
    finally:
        coordinator.close()

    shared_origin = json.loads(config.read_text(encoding="utf-8"))
    shared_origin["cdn"]["public_hosts"] = ["mirror.example"]
    config.write_text(json.dumps(shared_origin), encoding="utf-8")
    with pytest.raises(ValueError, match="download hosts must be distinct"):
        _publication_coordinator(args)


def test_publication_config_builds_read_through_mirror_without_credentials(
    tmp_path: Path,
) -> None:
    config = tmp_path / "publication-read-through.json"
    config.write_text(
        """
        {
          "schema_version": 1,
          "github": {
            "owner": "zhangyifanjackson-dotcom",
            "repository": "EcoreX-installers",
            "token_env": "ECOREX_GITHUB_RELEASE_TOKEN"
          },
          "mirror": {
            "source_id": "github-cn",
            "mode": "github-read-through",
            "public_hosts": ["ghproxy.net"]
          },
          "cdn": {
            "source_id": "cdn",
            "endpoint": "https://publisher.cdn.example/api/v1/releases",
            "allowed_hosts": ["publisher.cdn.example"],
            "public_hosts": ["dl.ecoremedia.net"],
            "token_env": "ECOREX_CDN_TOKEN"
          }
        }
        """,
        encoding="utf-8",
    )
    args = _parser().parse_args(
        [
            "publish-assets",
            "--release-dir",
            str(tmp_path),
            "--publication-config",
            str(config),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--trusted-key",
            "release-key=" + base64.b64encode(b"x" * 32).decode("ascii"),
        ]
    )

    coordinator = _publication_coordinator(args)
    try:
        assert coordinator.mirror.read_through is True
        assert coordinator.mirror.public_hosts == frozenset({"ghproxy.net"})
        assert coordinator.github.repository == "EcoreX-installers"
    finally:
        coordinator.close()


def test_receipt_identity_conflict_stops_before_any_remote_publication(
    tmp_path: Path,
    capsys,
) -> None:
    built, public = _built_release(tmp_path)
    publication_config = tmp_path / "publication.json"
    publication_config.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "release-stable-ffffffffffffffffffffffff",
                "manifest_sha256": "f" * 64,
            }
        ),
        encoding="utf-8",
    )
    coordinator = PublicationCoordinator()
    exit_code = run(
        [
            "publish-assets",
            "--release-dir",
            str(built.output_dir),
            "--publication-config",
            str(publication_config),
            "--receipt",
            str(receipt),
            "--trusted-key",
            "release-key=" + base64.b64encode(public).decode("ascii"),
            "--publish-github",
        ],
        publication_coordinator_factory=lambda _args: coordinator,
    )
    assert exit_code == 1
    assert coordinator.called is False
    assert coordinator.closed is True
    assert "different release" in capsys.readouterr().err


def test_publication_receipt_lock_is_held_before_remote_mutation(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    receipt = tmp_path / "receipt.json"
    lock_path = receipt.with_suffix(receipt.suffix + ".lock")

    class LockCheckingCoordinator(PublicationCoordinator):
        def publish(self, **kwargs):
            contender = ProductFileLock(lock_path, timeout=0)
            with pytest.raises(LockUnavailable):
                contender.acquire()
            return super().publish(**kwargs)

    coordinator = LockCheckingCoordinator()
    exit_code = run(
        [
            "publish-assets",
            "--release-dir",
            str(built.output_dir),
            "--publication-config",
            str(tmp_path / "unused.json"),
            "--receipt",
            str(receipt),
            "--trusted-key",
            "release-key=" + base64.b64encode(public).decode("ascii"),
            "--publish-github",
        ],
        publication_coordinator_factory=lambda _args: coordinator,
    )
    assert exit_code == 0
    assert coordinator.called is True


def test_release_directory_rejects_reserved_or_duplicate_artifact_names(
    tmp_path: Path,
) -> None:
    for name in ("release-manifest.json", "release-metadata.json", "sbom.cdx.json"):
        (tmp_path / name).write_bytes(b"x")
    manifest = SimpleNamespace(
        artifacts=(SimpleNamespace(file_name="release-manifest.json"),)
    )
    with pytest.raises(ValueError, match="filenames collide"):
        _release_directory_files(tmp_path, manifest)


def test_checked_in_publication_example_schema_and_cli_parser_stay_bound(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    example_path = root / "release" / "v1" / "publication-config.example.json"
    schema_path = root / "release" / "v1" / "publication-config.schema.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert example["schema_version"] == 2
    assert len(schema["oneOf"]) == 2
    assert set(example["mirror"]) == set(
        schema["$defs"]["replica"]["required"]
    ) == set(schema["$defs"]["replica"]["properties"])
    assert example["mirror"]["endpoint"].startswith("https://")
    assert ".invalid/" in example["mirror"]["endpoint"]
    assert example["mirror"]["public_hosts"] == ["dl.ecoremedia.net"]
    assert "github" not in example and "cdn" not in example
    assert set(example["mirror"]) == set(
        schema["$defs"]["replica"]["required"]
    ) == set(schema["$defs"]["replica"]["properties"])
    serialized = json.dumps(example, sort_keys=True)
    assert "Bearer " not in serialized
    assert "password" not in serialized.casefold()

    args = _parser().parse_args(
        [
            "publish-assets",
            "--release-dir",
            str(tmp_path),
            "--publication-config",
            str(example_path),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--trusted-key",
            "release-key=" + base64.b64encode(b"x" * 32).decode("ascii"),
        ]
    )
    coordinator = _publication_coordinator(args)
    try:
        assert coordinator.mirror.source_id == example["mirror"]["source_id"]
        assert coordinator.cdn is None
        assert coordinator.github is None
    finally:
        coordinator.close()

    invalid = dict(example)
    invalid["secret"] = "must-never-be-accepted"
    invalid_path = tmp_path / "invalid-publication.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    invalid_args = _parser().parse_args(
        [
            "publish-assets",
            "--release-dir",
            str(tmp_path),
            "--publication-config",
            str(invalid_path),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--trusted-key",
            "release-key=" + base64.b64encode(b"x" * 32).decode("ascii"),
        ]
    )
    with pytest.raises(ValueError, match="invalid shape"):
        _publication_coordinator(invalid_args)

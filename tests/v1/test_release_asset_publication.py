from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from ecorex.release import (
    GitHubAssetReceipt,
    GitHubReleaseDraft,
    ReleaseAssetPublicationCoordinator,
    ReleaseReplicaReceipt,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


RELEASE_ID = "release-stable-0123456789abcdef01234567"


class Replica:
    def __init__(self, source_id: str, base_url: str, order: list[str]) -> None:
        self.source_id = source_id
        self.base_url = base_url
        self.public_hosts = frozenset({urlsplit(base_url).hostname})
        self.order = order
        self.bad_url = False
        self.closed = False
        self.fail_close = False

    def ensure_asset(self, *, release_id, path, expected_sha256):
        name = Path(path).name
        self.order.append(f"{self.source_id}:asset:{name}")
        base = "https://evil.example" if self.bad_url else self.base_url
        return ReleaseReplicaReceipt(
            self.source_id,
            release_id,
            name,
            Path(path).stat().st_size,
            expected_sha256,
            f"{base}/{name}",
        )

    def finalize(self, *, release_id, manifest_sha256):
        del release_id, manifest_sha256
        self.order.append(f"{self.source_id}:finalize")
        return True

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


class GitHub:
    owner = "acme"
    repository = "ecorex"

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.closed = False
        self.published = False

    def ensure_draft(self, *, version, channel, release_id):
        del version, channel, release_id
        self.order.append("github:draft")
        return GitHubReleaseDraft(
            71,
            "v1.0.0",
            "https://uploads.github.com/repos/acme/ecorex/releases/71/assets{?name}",
            True,
        )

    def ensure_asset(self, draft, path, *, expected_sha256):
        del draft
        name = Path(path).name
        self.order.append(f"github:asset:{name}")
        return GitHubAssetReceipt(
            len(self.order),
            name,
            Path(path).stat().st_size,
            expected_sha256,
            f"https://github.com/acme/ecorex/releases/download/v1.0.0/{name}",
        )

    def publish(self, draft):
        self.order.append("github:publish")
        self.published = True
        return GitHubReleaseDraft(
            draft.release_id, draft.tag_name, draft.upload_url, False
        )

    def close(self):
        self.closed = True


def _inputs(tmp_path: Path):
    names = (
        "core.zip",
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
    )
    files = []
    digests = {}
    for name in names:
        path = tmp_path / name
        path.write_bytes(("bytes-" + name).encode())
        files.append(path)
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = SimpleNamespace(
        release_id=RELEASE_ID,
        version="1.0.0",
        channel=ReleaseChannel.STABLE,
        sources=(
            ReleaseSource(
                "github-cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                f"https://mirror.example/releases/{RELEASE_ID}",
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
                f"https://cdn.example/releases/{RELEASE_ID}",
            ),
        ),
    )
    return manifest, tuple(files), digests


def test_mirror_is_ready_before_github_and_cdn_before_publication(
    tmp_path: Path,
) -> None:
    manifest, files, digests = _inputs(tmp_path)
    order: list[str] = []
    mirror = Replica("github-cn", manifest.sources[0].base_url, order)
    github = GitHub(order)
    cdn = Replica("cdn", manifest.sources[2].base_url, order)
    coordinator = ReleaseAssetPublicationCoordinator(
        mirror=mirror, github=github, cdn=cdn
    )
    result = coordinator.publish(
        manifest=manifest,
        files=files,
        expected_sha256=digests,
        publish_github=True,
    )
    assert order.index("github-cn:finalize") < order.index("github:draft")
    assert order.index("cdn:finalize") < order.index("github:publish")
    assert result.github_draft is False
    assert tuple(result.source_receipts) == ("github-cn", "github", "cdn")
    coordinator.close()
    assert mirror.closed and github.closed and cdn.closed


def test_bad_replica_receipt_stops_before_github_publish(tmp_path: Path) -> None:
    manifest, files, digests = _inputs(tmp_path)
    order: list[str] = []
    mirror = Replica("github-cn", manifest.sources[0].base_url, order)
    mirror.bad_url = True
    github = GitHub(order)
    cdn = Replica("cdn", manifest.sources[2].base_url, order)
    coordinator = ReleaseAssetPublicationCoordinator(
        mirror=mirror, github=github, cdn=cdn
    )
    with pytest.raises(ValueError, match="signed source"):
        coordinator.publish(
            manifest=manifest,
            files=files,
            expected_sha256=digests,
            publish_github=True,
        )
    assert "github:draft" not in order
    assert github.published is False


def test_signed_source_mismatch_fails_before_first_remote_mutation(
    tmp_path: Path,
) -> None:
    manifest, files, digests = _inputs(tmp_path)
    order: list[str] = []
    mirror = Replica("github-cn", manifest.sources[0].base_url, order)
    github = GitHub(order)
    cdn = Replica("cdn", manifest.sources[2].base_url, order)
    github.owner = "other"
    coordinator = ReleaseAssetPublicationCoordinator(
        mirror=mirror, github=github, cdn=cdn
    )
    with pytest.raises(ValueError, match="signed source URL"):
        coordinator.publish(
            manifest=manifest,
            files=files,
            expected_sha256=digests,
            publish_github=True,
        )
    assert order == []


def test_duplicate_local_asset_names_fail_before_first_remote_mutation(
    tmp_path: Path,
) -> None:
    manifest, files, digests = _inputs(tmp_path)
    order: list[str] = []
    mirror = Replica("github-cn", manifest.sources[0].base_url, order)
    github = GitHub(order)
    cdn = Replica("cdn", manifest.sources[2].base_url, order)
    coordinator = ReleaseAssetPublicationCoordinator(
        mirror=mirror, github=github, cdn=cdn
    )
    duplicate = tmp_path / "nested" / files[0].name
    duplicate.parent.mkdir()
    duplicate.write_bytes(files[0].read_bytes())
    with pytest.raises(ValueError, match="digest map"):
        coordinator.publish(
            manifest=manifest,
            files=(*files, duplicate),
            expected_sha256=digests,
            publish_github=False,
        )
    assert order == []


def test_coordinator_attempts_every_close_when_one_transport_fails() -> None:
    order: list[str] = []
    mirror = Replica("github-cn", "https://mirror.example/releases", order)
    github = GitHub(order)
    cdn = Replica("cdn", "https://cdn.example/releases", order)
    cdn.fail_close = True
    coordinator = ReleaseAssetPublicationCoordinator(
        mirror=mirror, github=github, cdn=cdn
    )
    with pytest.raises(RuntimeError, match="close_failed"):
        coordinator.close()
    assert mirror.closed and github.closed and cdn.closed

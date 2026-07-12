"""One resumable release-asset publication pipeline for all download sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote, urlsplit

from ecorex.update import ReleaseManifest, SourceKind

from .github import GitHubAssetReceipt, GitHubReleasePublisher
from .identity import release_tag
from .replica import HTTPSReleaseReplicaPublisher, ReleaseReplicaReceipt


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublishedReleaseAssets:
    release_id: str
    github_release_id: int
    github_draft: bool
    source_receipts: Mapping[str, tuple[Mapping[str, object], ...]]

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        github_release_id: int,
        github_draft: bool,
        source_receipts: Mapping[str, tuple[Mapping[str, object], ...]],
    ) -> "PublishedReleaseAssets":
        copied = {
            source_id: tuple(MappingProxyType(dict(item)) for item in receipts)
            for source_id, receipts in source_receipts.items()
        }
        return cls(
            release_id,
            github_release_id,
            github_draft,
            MappingProxyType(copied),
        )


class ReleaseAssetPublicationCoordinator:
    """Upload mirror first, GitHub second and CDN last; publish only after all pass."""

    def __init__(
        self,
        *,
        mirror: HTTPSReleaseReplicaPublisher,
        github: GitHubReleasePublisher,
        cdn: HTTPSReleaseReplicaPublisher,
    ) -> None:
        self.mirror = mirror
        self.github = github
        self.cdn = cdn

    def close(self) -> None:
        failed = False
        for resource in (self.cdn, self.github, self.mirror):
            try:
                resource.close()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("release_publisher_close_failed")

    def publish(
        self,
        *,
        manifest: ReleaseManifest,
        files: tuple[Path, ...],
        expected_sha256: Mapping[str, str],
        publish_github: bool,
    ) -> PublishedReleaseAssets:
        names = tuple(path.name for path in files)
        if (
            not 1 <= len(files) <= 100
            or len(names) != len(set(names))
            or set(expected_sha256) != set(names)
            or "release-manifest.json" not in expected_sha256
            or any(
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                for digest in expected_sha256.values()
            )
        ):
            raise ValueError("publication digest map does not match release files")
        sources = tuple(manifest.sources)
        if len(sources) != 3:
            raise ValueError("signed release source contract is incomplete")
        mirror_source, github_source, cdn_source = sources
        if (
            mirror_source.kind is not SourceKind.GITHUB_CN_MIRROR
            or github_source.kind is not SourceKind.GITHUB_RELEASE
            or cdn_source.kind is not SourceKind.ECOREX_CDN
            or self.mirror.source_id != mirror_source.source_id
            or self.cdn.source_id != cdn_source.source_id
        ):
            raise ValueError("publisher identities do not match signed source order")
        self._validate_publisher_sources(
            manifest=manifest,
            mirror_base_url=mirror_source.base_url,
            github_base_url=github_source.base_url,
            cdn_base_url=cdn_source.base_url,
        )

        receipts: dict[str, tuple[Mapping[str, object], ...]] = {}
        mirror_receipts = tuple(
            self.mirror.ensure_asset(
                release_id=manifest.release_id,
                path=path,
                expected_sha256=expected_sha256[path.name],
            )
            for path in files
        )
        self._validate_replica_urls(mirror_source.base_url, mirror_receipts)
        self.mirror.finalize(
            release_id=manifest.release_id,
            manifest_sha256=expected_sha256["release-manifest.json"],
        )
        receipts[mirror_source.source_id] = tuple(
            _replica_projection(item) for item in mirror_receipts
        )

        draft = self.github.ensure_draft(
            version=manifest.version,
            channel=manifest.channel,
            release_id=manifest.release_id,
        )
        github_receipts = tuple(
            self.github.ensure_asset(
                draft,
                path,
                expected_sha256=expected_sha256[path.name],
            )
            for path in files
        )
        self._validate_github_urls(github_source.base_url, github_receipts)
        receipts[github_source.source_id] = tuple(
            _github_projection(item) for item in github_receipts
        )

        cdn_receipts = tuple(
            self.cdn.ensure_asset(
                release_id=manifest.release_id,
                path=path,
                expected_sha256=expected_sha256[path.name],
            )
            for path in files
        )
        self._validate_replica_urls(cdn_source.base_url, cdn_receipts)
        self.cdn.finalize(
            release_id=manifest.release_id,
            manifest_sha256=expected_sha256["release-manifest.json"],
        )
        receipts[cdn_source.source_id] = tuple(
            _replica_projection(item) for item in cdn_receipts
        )

        if publish_github:
            draft = self.github.publish(draft)
        return PublishedReleaseAssets.create(
            release_id=manifest.release_id,
            github_release_id=draft.release_id,
            github_draft=draft.draft,
            source_receipts=receipts,
        )

    def _validate_publisher_sources(
        self,
        *,
        manifest: ReleaseManifest,
        mirror_base_url: str,
        github_base_url: str,
        cdn_base_url: str,
    ) -> None:
        """Fence signed public identities before the first remote mutation."""

        self._validate_replica_base_url(
            mirror_base_url,
            public_hosts=self.mirror.public_hosts,
        )
        self._validate_replica_base_url(
            cdn_base_url,
            public_hosts=self.cdn.public_hosts,
        )
        expected_github = (
            f"https://github.com/{quote(self.github.owner, safe='')}/"
            f"{quote(self.github.repository, safe='')}/releases/download/"
            f"{quote(release_tag(manifest.version, manifest.channel, release_id=manifest.release_id), safe='')}"
        )
        if github_base_url.rstrip("/") != expected_github:
            raise ValueError("GitHub publisher does not match signed source URL")

    @staticmethod
    def _validate_replica_base_url(
        base_url: str,
        *,
        public_hosts: frozenset[str],
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") not in public_hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path
        ):
            raise ValueError("replica publisher does not match signed source URL")

    @staticmethod
    def _validate_replica_urls(
        base_url: str,
        receipts: tuple[ReleaseReplicaReceipt, ...],
    ) -> None:
        base = base_url.rstrip("/")
        for receipt in receipts:
            if receipt.url != f"{base}/{quote(receipt.name, safe='')}":
                raise ValueError("replica receipt URL does not match signed source")

    @staticmethod
    def _validate_github_urls(
        base_url: str,
        receipts: tuple[GitHubAssetReceipt, ...],
    ) -> None:
        base = base_url.rstrip("/")
        for receipt in receipts:
            if receipt.browser_download_url != f"{base}/{quote(receipt.name, safe='')}":
                raise ValueError("GitHub asset URL does not match signed source")


def _replica_projection(receipt: ReleaseReplicaReceipt) -> Mapping[str, object]:
    return {
        "name": receipt.name,
        "size_bytes": receipt.size_bytes,
        "sha256": receipt.sha256,
        "url": receipt.url,
    }


def _github_projection(receipt: GitHubAssetReceipt) -> Mapping[str, object]:
    return {
        "name": receipt.name,
        "size_bytes": receipt.size_bytes,
        "sha256": receipt.sha256,
        "url": receipt.browser_download_url,
    }


__all__ = ["PublishedReleaseAssets", "ReleaseAssetPublicationCoordinator"]

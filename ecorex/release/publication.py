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
from .publication_policy import (
    STABLE_PRIMARY_ONLY_POLICY,
    publication_receipt_policy,
    required_publication_sources,
)
from .replica import (
    HTTPSReadThroughReleaseMirror,
    HTTPSReleaseReplicaPublisher,
    ReleaseReplicaReceipt,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublishedReleaseAssets:
    release_id: str
    source_receipts: Mapping[str, tuple[Mapping[str, object], ...]]

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        source_receipts: Mapping[str, tuple[Mapping[str, object], ...]],
    ) -> "PublishedReleaseAssets":
        copied = {
            source_id: tuple(MappingProxyType(dict(item)) for item in receipts)
            for source_id, receipts in source_receipts.items()
        }
        return cls(
            release_id,
            MappingProxyType(copied),
        )


class ReleaseAssetPublicationCoordinator:
    """Publish immutable assets, then prove the channel-required sources.

    Managed mirrors keep their historical upload-first behavior. A GitHub
    read-through mirror is different: it is verified only after the GitHub
    release is public, because it has no independent upload surface.
    """

    def __init__(
        self,
        *,
        mirror: HTTPSReleaseReplicaPublisher | HTTPSReadThroughReleaseMirror,
        github: GitHubReleasePublisher | None = None,
        cdn: HTTPSReleaseReplicaPublisher | None = None,
    ) -> None:
        self.mirror = mirror
        self.github = github
        self.cdn = cdn

    def close(self) -> None:
        failed = False
        for resource in (self.cdn, self.github, self.mirror):
            if resource is None:
                continue
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
        ):
            raise ValueError("publisher identities do not match signed source order")

        read_through = isinstance(self.mirror, HTTPSReadThroughReleaseMirror) or (
            getattr(self.mirror, "read_through", False) is True
        )
        if publication_receipt_policy(manifest) == STABLE_PRIMARY_ONLY_POLICY:
            if read_through:
                if self.github is None:
                    raise ValueError(
                        "stable read-through mirror requires a GitHub publisher"
                    )
                if not publish_github:
                    raise ValueError(
                        "read-through mirror requires a public GitHub release"
                    )
                self._validate_github_source(
                    manifest=manifest,
                    github_base_url=github_source.base_url,
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
                self.github.publish(draft)
                verifier = self.mirror
                mirror_receipts = tuple(
                    verifier.verify_asset(  # type: ignore[union-attr]
                        base_url=mirror_source.base_url,
                        release_id=manifest.release_id,
                        path=path,
                        expected_sha256=expected_sha256[path.name],
                    )
                    for path in files
                )
                self._validate_replica_urls(mirror_source.base_url, mirror_receipts)
                return PublishedReleaseAssets.create(
                    release_id=manifest.release_id,
                    source_receipts={
                        mirror_source.source_id: tuple(
                            _replica_projection(item) for item in mirror_receipts
                        )
                    },
                )
            self._validate_primary_mirror(mirror_source.base_url)
            assert isinstance(self.mirror, HTTPSReleaseReplicaPublisher) or callable(
                getattr(self.mirror, "ensure_asset", None)
            )
            mirror_receipts = tuple(
                self.mirror.ensure_asset(  # type: ignore[union-attr]
                    release_id=manifest.release_id,
                    path=path,
                    expected_sha256=expected_sha256[path.name],
                )
                for path in files
            )
            self._validate_replica_urls(mirror_source.base_url, mirror_receipts)
            self.mirror.finalize(  # type: ignore[union-attr]
                release_id=manifest.release_id,
                manifest_sha256=expected_sha256["release-manifest.json"],
            )
            required = required_publication_sources(manifest)
            return PublishedReleaseAssets.create(
                release_id=manifest.release_id,
                source_receipts={
                    required[0].source_id: tuple(
                        _replica_projection(item) for item in mirror_receipts
                    )
                },
            )

        if self.github is None or self.cdn is None:
            raise ValueError("all-source publication requires GitHub and CDN publishers")
        if self.cdn.source_id != cdn_source.source_id:
            raise ValueError("publisher identities do not match signed source order")
        self._validate_publisher_sources(
            manifest=manifest,
            mirror_base_url=mirror_source.base_url,
            github_base_url=github_source.base_url,
            cdn_base_url=cdn_source.base_url,
        )
        if read_through and not publish_github:
            raise ValueError(
                "read-through mirror requires a public GitHub release"
            )

        mirror_receipts: tuple[ReleaseReplicaReceipt, ...] = ()
        if not read_through:
            assert isinstance(self.mirror, HTTPSReleaseReplicaPublisher) or callable(
                getattr(self.mirror, "ensure_asset", None)
            )
            mirror_receipts = tuple(
                self.mirror.ensure_asset(  # type: ignore[union-attr]
                    release_id=manifest.release_id,
                    path=path,
                    expected_sha256=expected_sha256[path.name],
                )
                for path in files
            )
            self._validate_replica_urls(mirror_source.base_url, mirror_receipts)
            self.mirror.finalize(  # type: ignore[union-attr]
                release_id=manifest.release_id,
                manifest_sha256=expected_sha256["release-manifest.json"],
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
        if publish_github:
            draft = self.github.publish(draft)
        if read_through:
            verifier = self.mirror
            mirror_receipts = tuple(
                verifier.verify_asset(  # type: ignore[union-attr]
                    base_url=mirror_source.base_url,
                    release_id=manifest.release_id,
                    path=path,
                    expected_sha256=expected_sha256[path.name],
                )
                for path in files
            )
            self._validate_replica_urls(mirror_source.base_url, mirror_receipts)
        receipts = {
            mirror_source.source_id: tuple(
                _replica_projection(item) for item in mirror_receipts
            ),
            github_source.source_id: tuple(
                _github_projection(item) for item in github_receipts
            ),
            cdn_source.source_id: tuple(
                _replica_projection(item) for item in cdn_receipts
            ),
        }
        return PublishedReleaseAssets.create(
            release_id=manifest.release_id,
            source_receipts=receipts,
        )

    def _validate_primary_mirror(self, mirror_base_url: str) -> None:
        """Fence the only Stable admission source before mutation."""

        self._validate_replica_base_url(
            mirror_base_url,
            public_hosts=self.mirror.public_hosts,
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

        self._validate_primary_mirror(mirror_base_url)
        assert self.cdn is not None and self.github is not None
        self._validate_replica_base_url(
            cdn_base_url,
            public_hosts=self.cdn.public_hosts,
        )
        self._validate_github_source(
            manifest=manifest,
            github_base_url=github_base_url,
        )

    def _validate_github_source(
        self,
        *,
        manifest: ReleaseManifest,
        github_base_url: str,
    ) -> None:
        assert self.github is not None
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

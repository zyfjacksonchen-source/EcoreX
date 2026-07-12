"""Typed contracts for deterministic EcoreX release construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import re

from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactKind(StrEnum):
    CORE = "core"
    BOOTSTRAP = "bootstrap"
    CAPABILITY_PACK = "capability-pack"


@dataclass(frozen=True, slots=True)
class WebBundleBuildInput:
    """The one production React dist tree bound into a release."""

    dist_dir: Path | str | os.PathLike[str]

    def __post_init__(self) -> None:
        try:
            dist_dir = Path(self.dist_dir)
        except TypeError as exc:
            raise ValueError("web dist_dir must be a filesystem path") from exc
        object.__setattr__(self, "dist_dir", dist_dir)


@dataclass(frozen=True, slots=True)
class ArtifactBuildInput:
    source_dir: Path | str | os.PathLike[str]
    kind: ArtifactKind
    platform: str
    architecture: str
    artifact_id: str | None = None
    file_name: str | None = None
    executable_paths: tuple[str, ...] = ()
    pack_id: str | None = None
    pack_tool_ids: tuple[str, ...] = ()
    pack_service_ids: tuple[str, ...] = ()
    runtime_api_version: str = "1.0.0"
    product_runtime: bool = False

    def __post_init__(self) -> None:
        try:
            source_dir = Path(self.source_dir)
        except TypeError as exc:
            raise ValueError("source_dir must be a filesystem path") from exc
        object.__setattr__(self, "source_dir", source_dir)
        if not isinstance(self.kind, ArtifactKind):
            try:
                object.__setattr__(self, "kind", ArtifactKind(self.kind))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported artifact kind: {self.kind!r}") from exc
        for label, value in (
            ("platform", self.platform),
            ("architecture", self.architecture),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be a non-empty string")
        for label, value in (
            ("artifact_id", self.artifact_id),
            ("file_name", self.file_name),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{label} must be null or a non-empty string")
        if isinstance(self.executable_paths, (str, bytes, bytearray)):
            raise ValueError("executable_paths must be a sequence of relative paths")
        try:
            executable_paths = tuple(self.executable_paths)
        except TypeError as exc:
            raise ValueError("executable_paths must be a sequence of relative paths") from exc
        if not all(isinstance(value, str) for value in executable_paths):
            raise ValueError("each executable path must be a string")
        object.__setattr__(self, "executable_paths", executable_paths)
        if not isinstance(self.product_runtime, bool):
            raise ValueError("product_runtime must be a boolean")
        if self.product_runtime and self.kind is not ArtifactKind.CORE:
            raise ValueError("only a Core artifact can be a product Runtime")
        if self.kind is ArtifactKind.CAPABILITY_PACK:
            if not isinstance(self.pack_id, str) or not self.pack_id:
                raise ValueError("capability-pack input requires pack_id")
            if isinstance(self.pack_tool_ids, (str, bytes, bytearray)) or not isinstance(
                self.pack_tool_ids, tuple
            ):
                raise ValueError(
                    "capability-pack tool IDs must be a sorted unique tuple"
                )
            if isinstance(
                self.pack_service_ids, (str, bytes, bytearray)
            ) or not isinstance(self.pack_service_ids, tuple):
                raise ValueError(
                    "capability-pack service IDs must be a sorted unique tuple"
                )
            for label, values in (
                ("tool", self.pack_tool_ids),
                ("service", self.pack_service_ids),
            ):
                if not all(isinstance(value, str) and value for value in values) or tuple(
                    sorted(set(values))
                ) != values:
                    raise ValueError(
                        f"capability-pack {label} IDs must be a sorted unique tuple"
                    )
            if not self.pack_tool_ids and not self.pack_service_ids:
                raise ValueError(
                    "capability-pack input requires a tool or service binding"
                )
        elif self.pack_id is not None or self.pack_tool_ids or self.pack_service_ids:
            raise ValueError("only capability-pack inputs may declare pack identity")


@dataclass(frozen=True, slots=True)
class CoreDeltaBuildInput:
    """One verified retained Core used to derive a signed target delta."""

    base_manifest: ReleaseManifest
    base_artifact: ReleaseArtifact
    base_package: Path | str | os.PathLike[str]

    def __post_init__(self) -> None:
        if not isinstance(self.base_manifest, ReleaseManifest):
            raise ValueError("delta base_manifest must be a ReleaseManifest")
        if not isinstance(self.base_artifact, ReleaseArtifact):
            raise ValueError("delta base_artifact must be a ReleaseArtifact")
        if self.base_manifest.artifact(self.base_artifact.artifact_id) != self.base_artifact:
            raise ValueError("delta base artifact is not bound to its manifest")
        if not self.base_artifact.artifact_id.startswith("core-"):
            raise ValueError("delta base artifact must be a Core")
        try:
            base_package = Path(self.base_package)
        except TypeError as exc:
            raise ValueError("delta base_package must be a filesystem path") from exc
        object.__setattr__(self, "base_package", base_package)


@dataclass(frozen=True, slots=True)
class ReleaseBuildSpec:
    channel: ReleaseChannel
    created_at: str
    sources: tuple[ReleaseSource, ...]
    artifacts: tuple[ArtifactBuildInput, ...]
    web_bundle: WebBundleBuildInput | None = None
    release_scoped_sources: bool = False
    dependency_lock_sha256: str | None = None
    core_delta_bases: tuple[CoreDeltaBuildInput, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.channel, ReleaseChannel):
            try:
                object.__setattr__(self, "channel", ReleaseChannel(self.channel))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported release channel: {self.channel!r}") from exc
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("created_at must be a non-empty string")
        try:
            sources = tuple(self.sources)
            artifacts = tuple(self.artifacts)
        except TypeError as exc:
            raise ValueError("sources and artifacts must be sequences") from exc
        if not all(isinstance(source, ReleaseSource) for source in sources):
            raise ValueError("every release source must be a ReleaseSource")
        if not all(isinstance(artifact, ArtifactBuildInput) for artifact in artifacts):
            raise ValueError("every release artifact must be an ArtifactBuildInput")
        if self.web_bundle is not None and not isinstance(
            self.web_bundle, WebBundleBuildInput
        ):
            raise ValueError("web_bundle must be one WebBundleBuildInput or null")
        if not isinstance(self.release_scoped_sources, bool):
            raise ValueError("release_scoped_sources must be a boolean")
        if self.dependency_lock_sha256 is not None and (
            not isinstance(self.dependency_lock_sha256, str)
            or _SHA256.fullmatch(self.dependency_lock_sha256) is None
        ):
            raise ValueError("dependency_lock_sha256 must be null or one SHA-256 digest")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "artifacts", artifacts)
        try:
            delta_bases = tuple(self.core_delta_bases)
        except TypeError as exc:
            raise ValueError("core_delta_bases must be a sequence") from exc
        if (
            len(delta_bases) > 3
            or not all(isinstance(item, CoreDeltaBuildInput) for item in delta_bases)
            or len(
                {
                    (item.base_artifact.platform, item.base_artifact.architecture)
                    for item in delta_bases
                }
            )
            != len(delta_bases)
        ):
            raise ValueError("core_delta_bases must contain unique supported targets")
        object.__setattr__(self, "core_delta_bases", delta_bases)
        if not artifacts and self.web_bundle is None:
            raise ValueError("at least one package or Web bundle artifact is required")


@dataclass(frozen=True, slots=True)
class BuiltRelease:
    output_dir: Path
    manifest: ReleaseManifest
    manifest_path: Path
    metadata_path: Path
    sbom_path: Path
    artifact_paths: Mapping[str, Path]

    @classmethod
    def create(
        cls,
        *,
        output_dir: Path,
        manifest: ReleaseManifest,
        manifest_path: Path,
        metadata_path: Path,
        sbom_path: Path,
        artifact_paths: Mapping[str, Path],
    ) -> "BuiltRelease":
        return cls(
            output_dir=output_dir,
            manifest=manifest,
            manifest_path=manifest_path,
            metadata_path=metadata_path,
            sbom_path=sbom_path,
            artifact_paths=MappingProxyType(dict(artifact_paths)),
        )

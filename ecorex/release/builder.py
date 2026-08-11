"""Deterministic, signed, atomic release artifact builder."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ecorex._version import __version__
from ecorex.capabilities import (
    CapabilityPackManifest,
    PackServiceBinding,
    PackToolBinding,
    builtin_capability_registry,
    builtin_pack_service_specs,
    tool_spec_digest,
)
from ecorex.update import (
    MAX_CAPABILITY_PACK_ARTIFACT_BYTES,
    MAX_CORE_ARTIFACT_BYTES,
    SOURCE_PRIORITY,
    ReleaseArtifact,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    CoreDeltaEndpoint,
    DeltaNotBeneficial,
    core_delta_artifact_id,
    core_delta_file_name,
    create_core_delta_archive,
)
from ecorex.update.manifest import (
    portable_path_segment_key,
    validate_portable_path_segment,
)

from .errors import ReleaseBuildError
from .macos_native_contract import (
    MACOS_NATIVE_COMPONENTS,
    MACOS_NATIVE_LICENSES,
    PYTHON_MACOS_DISTRIBUTION,
    PYTHON_MACOS_LICENSE,
)
from .identity import release_tag
from .models import (
    ArtifactBuildInput,
    ArtifactKind,
    BuiltRelease,
    ReleaseBuildSpec,
)
from .signing import ReleaseSigner, SigningError, sign_envelope
from .web_bundle import (
    WEB_MANIFEST_ARCHITECTURE,
    WEB_MANIFEST_ARTIFACT_ID,
    WEB_MANIFEST_FILE_NAME,
    WEB_MANIFEST_PLATFORM,
    ScannedWebBundle,
    create_signed_web_manifest,
    scan_web_bundle,
)


MAX_CORE_ARCHIVE_BYTES = MAX_CORE_ARTIFACT_BYTES
MAX_CORE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_CORE_BYTES = MAX_CORE_ARCHIVE_BYTES
MAX_BOOTSTRAP_BYTES = 10 * 1024 * 1024
MAX_CAPABILITY_PACK_BYTES = MAX_CAPABILITY_PACK_ARTIFACT_BYTES
MAX_RELEASE_METADATA_BYTES = 16 * 1024 * 1024
MAX_RELEASE_SBOM_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOURCE_MEMBERS = 50_000
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_RESERVED_RELEASE_FILE_NAMES = frozenset(
    {
        WEB_MANIFEST_FILE_NAME,
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
    }
)
SUPPORTED_TARGETS = frozenset(
    {("windows", "x64"), ("macos", "arm64"), ("macos", "x64")}
)


@dataclass(frozen=True, slots=True)
class _SourceEntry:
    relative: str
    path: Path
    is_directory: bool
    size: int
    mode: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _FileRecord:
    relative: str
    size: int
    mode: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative,
            "size_bytes": self.size,
            "mode": f"{self.mode:04o}",
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class _BuiltArtifact:
    kind: str
    platform: str
    architecture: str
    artifact_id: str
    file_name: str
    path: Path
    size_bytes: int
    sha256: str
    files: tuple[_FileRecord, ...]


class ReleaseBuilder:
    """Build one immutable release directory and publish it by one rename."""

    def __init__(
        self,
        signer: ReleaseSigner,
        *,
        max_core_bytes: int = MAX_CORE_BYTES,
        max_bootstrap_bytes: int = MAX_BOOTSTRAP_BYTES,
        max_capability_pack_bytes: int = MAX_CAPABILITY_PACK_BYTES,
    ) -> None:
        if not _statically_implements_signer(signer):
            raise TypeError("signer must implement the ReleaseSigner protocol")
        if (
            isinstance(max_core_bytes, bool)
            or not isinstance(max_core_bytes, int)
            or max_core_bytes <= 0
            or max_core_bytes > MAX_CORE_BYTES
        ):
            raise ValueError(f"max_core_bytes must be between 1 and {MAX_CORE_BYTES}")
        if (
            isinstance(max_bootstrap_bytes, bool)
            or not isinstance(max_bootstrap_bytes, int)
            or max_bootstrap_bytes <= 0
            or max_bootstrap_bytes > MAX_BOOTSTRAP_BYTES
        ):
            raise ValueError(
                f"max_bootstrap_bytes must be between 1 and {MAX_BOOTSTRAP_BYTES}"
            )
        if (
            isinstance(max_capability_pack_bytes, bool)
            or not isinstance(max_capability_pack_bytes, int)
            or max_capability_pack_bytes <= 0
            or max_capability_pack_bytes > MAX_CAPABILITY_PACK_BYTES
        ):
            raise ValueError(
                "max_capability_pack_bytes must be between 1 and "
                f"{MAX_CAPABILITY_PACK_BYTES}"
            )
        self.signer = signer
        self.max_core_bytes = max_core_bytes
        self.max_bootstrap_bytes = max_bootstrap_bytes
        self.max_capability_pack_bytes = max_capability_pack_bytes

    def build(
        self,
        spec: ReleaseBuildSpec,
        destination: str | os.PathLike[str],
    ) -> BuiltRelease:
        if not isinstance(spec, ReleaseBuildSpec):
            raise TypeError("spec must be a ReleaseBuildSpec")
        destination_path = Path(destination)
        if os.path.lexists(destination_path):
            raise ReleaseBuildError(
                f"release destination already exists: {destination_path}"
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        _require_real_directory(destination_path.parent, "release destination parent")
        self._validate_spec(spec, destination_path)

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination_path.name}.staging-",
                dir=destination_path.parent,
            )
        )
        published = False
        try:
            manifest, built_artifacts, sbom_path, metadata_path, manifest_path = (
                self._build_staged(spec, staging)
            )
            if os.path.lexists(destination_path):
                raise ReleaseBuildError(
                    f"release destination appeared during build: {destination_path}"
                )
            os.rename(staging, destination_path)
            _fsync_directory(destination_path.parent)
            published = True
            artifact_paths = {
                item.artifact_id: destination_path / item.file_name
                for item in built_artifacts
            }
            return BuiltRelease.create(
                output_dir=destination_path,
                manifest=manifest,
                manifest_path=destination_path / manifest_path.name,
                metadata_path=destination_path / metadata_path.name,
                sbom_path=destination_path / sbom_path.name,
                artifact_paths=artifact_paths,
            )
        except (ReleaseBuildError, SigningError):
            raise
        except Exception as exc:
            raise ReleaseBuildError(
                f"release build failed: {type(exc).__name__}"
            ) from exc
        finally:
            if not published and staging.exists():
                _remove_staging_tree(staging, destination_path.parent)

    def _validate_spec(self, spec: ReleaseBuildSpec, destination: Path) -> None:
        _validate_created_at(spec.created_at)
        source_kinds = tuple(source.kind for source in spec.sources)
        source_priorities = tuple(source.priority for source in spec.sources)
        if source_kinds != SOURCE_PRIORITY or source_priorities != tuple(
            range(len(SOURCE_PRIORITY))
        ):
            expected = ", ".join(kind.value for kind in SOURCE_PRIORITY)
            raise ReleaseBuildError(
                f"release sources must be ordered exactly as: {expected}"
            )
        source_ids = tuple(source.source_id for source in spec.sources)
        if len(source_ids) != len(set(source_ids)):
            raise ReleaseBuildError("release source IDs must be unique")
        seen_targets: set[tuple[ArtifactKind, str, str, str | None]] = set()
        seen_ids: set[str] = set()
        seen_names = {
            portable_path_segment_key(name) for name in _RESERVED_RELEASE_FILE_NAMES
        }
        destination_parent = destination.parent.resolve()
        for definition in spec.artifacts:
            target = (definition.platform, definition.architecture)
            if target not in SUPPORTED_TARGETS:
                raise ReleaseBuildError(
                    f"unsupported target: {definition.platform}/{definition.architecture}"
                )
            target_key = (
                definition.kind,
                *target,
                definition.pack_id
                if definition.kind is ArtifactKind.CAPABILITY_PACK
                else None,
            )
            if target_key in seen_targets:
                raise ReleaseBuildError(f"duplicate release target: {target_key!r}")
            seen_targets.add(target_key)
            artifact_id, file_name = _artifact_identity(definition)
            # Constructing the update contract is the canonical validation for
            # IDs and portable artifact names. The digest/signature are filled
            # after packaging.
            _validate_artifact_identity(artifact_id, file_name, definition)
            if artifact_id == WEB_MANIFEST_ARTIFACT_ID:
                raise ReleaseBuildError(
                    f"artifact id {WEB_MANIFEST_ARTIFACT_ID!r} is reserved for "
                    "the generated Web bundle manifest"
                )
            if artifact_id in seen_ids:
                raise ReleaseBuildError(f"duplicate artifact id: {artifact_id!r}")
            file_key = portable_path_segment_key(file_name)
            if file_key in seen_names:
                raise ReleaseBuildError(
                    f"reserved or colliding artifact file name: {file_name!r}"
                )
            seen_ids.add(artifact_id)
            seen_names.add(file_key)
            if definition.kind is ArtifactKind.CAPABILITY_PACK:
                sidecar_id, sidecar_name = _pack_manifest_identity(definition)
                if sidecar_id in seen_ids:
                    raise ReleaseBuildError(f"duplicate artifact id: {sidecar_id!r}")
                sidecar_key = portable_path_segment_key(sidecar_name)
                if sidecar_key in seen_names:
                    raise ReleaseBuildError(
                        f"reserved or colliding artifact file name: {sidecar_name!r}"
                    )
                seen_ids.add(sidecar_id)
                seen_names.add(sidecar_key)
            source = definition.source_dir
            _require_real_directory(source, "artifact source directory")
            source_resolved = source.resolve()
            try:
                destination_parent.relative_to(source_resolved)
            except ValueError:
                pass
            else:
                raise ReleaseBuildError(
                    "release destination parent cannot be inside an artifact source tree"
                )
        if spec.web_bundle is not None:
            web_root = spec.web_bundle.dist_dir
            _require_real_directory(web_root, "React dist directory")
            web_root_resolved = web_root.resolve()
            try:
                destination_parent.relative_to(web_root_resolved)
            except ValueError:
                pass
            else:
                raise ReleaseBuildError(
                    "release destination parent cannot be inside the React dist tree"
                )
        for definition in spec.artifacts:
            has_runtime_config = os.path.lexists(
                definition.source_dir / "runtime-config.json"
            )
            has_storage_migrations = os.path.lexists(
                definition.source_dir / "storage-migrations.json"
            )
            if definition.product_runtime and spec.web_bundle is None:
                raise ReleaseBuildError(
                    "product Runtime Core requires the signed React bundle"
                )
            if definition.product_runtime and spec.dependency_lock_sha256 is None:
                raise ReleaseBuildError(
                    "product Runtime Core requires the Python dependency lock digest"
                )
            if definition.product_runtime and not has_runtime_config:
                raise ReleaseBuildError(
                    "product Runtime Core is missing runtime-config.json"
                )
            if (
                definition.kind is ArtifactKind.CORE
                and has_runtime_config
                and not definition.product_runtime
            ):
                raise ReleaseBuildError(
                    "runtime-config.json requires an explicit product_runtime Core"
                )
            if has_storage_migrations and not definition.product_runtime:
                raise ReleaseBuildError(
                    "storage-migrations.json requires an explicit product_runtime Core"
                )
        core_targets = {
            (definition.platform, definition.architecture)
            for definition in spec.artifacts
            if definition.kind is ArtifactKind.CORE
        }
        for delta_base in spec.core_delta_bases:
            base = delta_base.base_artifact
            target = (base.platform, base.architecture)
            if target not in SUPPORTED_TARGETS or target not in core_targets:
                raise ReleaseBuildError(
                    "delta base does not have one matching target Core"
                )
            if _compare_semver(delta_base.base_manifest.version, __version__) >= 0:
                raise ReleaseBuildError(
                    "delta base must be an earlier immutable product version"
                )
            base_path = Path(delta_base.base_package)
            try:
                metadata = base_path.lstat()
            except OSError:
                raise ReleaseBuildError("delta base package is unavailable") from None
            if (
                _metadata_is_link_or_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != base.size_bytes
                or _sha256_file(base_path) != base.sha256
            ):
                raise ReleaseBuildError(
                    "delta base package differs from its signed identity"
                )
            delta_id = core_delta_artifact_id(
                platform=base.platform,
                architecture=base.architecture,
                base_artifact_sha256=base.sha256,
            )
            if delta_id in seen_ids:
                raise ReleaseBuildError("derived delta artifact identity collides")
            seen_ids.add(delta_id)

    def _build_staged(
        self,
        spec: ReleaseBuildSpec,
        staging: Path,
    ) -> tuple[
        ReleaseManifest,
        tuple[_BuiltArtifact, ...],
        Path,
        Path,
        Path,
    ]:
        scanned_web = (
            scan_web_bundle(spec.web_bundle) if spec.web_bundle is not None else None
        )
        product_migration_manifests = {
            _artifact_identity(definition)[0]: _product_migration_manifest_payload(
                definition
            )
            for definition in spec.artifacts
            if definition.product_runtime
        }
        built: list[_BuiltArtifact] = []
        for definition in sorted(
            spec.artifacts,
            key=lambda item: (item.kind.value, item.platform, item.architecture),
        ):
            artifact_id, file_name = _artifact_identity(definition)
            if definition.kind is ArtifactKind.CORE:
                limit = self.max_core_bytes
            elif definition.kind is ArtifactKind.BOOTSTRAP:
                limit = self.max_bootstrap_bytes
            else:
                limit = self.max_capability_pack_bytes
            artifact_path = staging / file_name
            files = _build_deterministic_zip(
                source=definition.source_dir,
                destination=artifact_path,
                executable_paths=definition.executable_paths,
                size_limit=limit,
                expanded_limit=(
                    MAX_CORE_EXPANDED_BYTES
                    if definition.kind is ArtifactKind.CORE
                    else MAX_UNPACKED_BYTES
                ),
            )
            size_bytes = artifact_path.stat().st_size
            if size_bytes > limit:
                raise ReleaseBuildError(
                    f"{definition.kind.value} artifact exceeds compressed size limit {limit}"
                )
            built.append(
                _BuiltArtifact(
                    kind=definition.kind.value,
                    platform=definition.platform,
                    architecture=definition.architecture,
                    artifact_id=artifact_id,
                    file_name=file_name,
                    path=artifact_path,
                    size_bytes=size_bytes,
                    sha256=_sha256_file(artifact_path),
                    files=files,
                )
            )
            if definition.kind is ArtifactKind.CAPABILITY_PACK:
                built.append(
                    self._build_capability_pack_manifest(
                        definition=definition,
                        artifact=built[-1],
                        staging=staging,
                    )
                )

        if (
            spec.web_bundle is not None
            and scan_web_bundle(spec.web_bundle) != scanned_web
        ):
            raise ReleaseBuildError(
                "React dist changed while core artifacts were built"
            )
        build_digest = _build_digest(
            spec, built, scanned_web, product_migration_manifests
        )
        release_id = f"release-{spec.channel.value}-{build_digest[:24]}"
        manifest_sources = _manifest_sources(spec, release_id=release_id)
        if scanned_web is not None:
            _web_manifest, web_manifest_payload = create_signed_web_manifest(
                scanned_web,
                release_id=release_id,
                version=__version__,
                build_digest=build_digest,
                signer=self.signer,
            )
            # A product Core is explicitly declared and contains one strict
            # runtime-config.json.
            # React files and their generated signed manifest are injected only
            # after build_digest exists, deliberately breaking the otherwise
            # circular dependency (web manifest -> build digest -> Core SHA).
            # The release manifest still signs the final rebuilt Core SHA, while
            # build_digest already binds the complete scanned Web inventory.
            for definition in sorted(
                spec.artifacts,
                key=lambda item: (
                    item.kind.value,
                    item.platform,
                    item.architecture,
                ),
            ):
                if definition.kind is not ArtifactKind.CORE:
                    continue
                if not definition.product_runtime:
                    continue
                artifact_index = next(
                    (
                        position
                        for position, candidate in enumerate(built)
                        if candidate.artifact_id == _artifact_identity(definition)[0]
                    ),
                    None,
                )
                if artifact_index is None:
                    raise ReleaseBuildError(
                        "product Core disappeared during Web composition"
                    )
                built[artifact_index] = _rebuild_product_core_with_web(
                    definition=definition,
                    artifact=built[artifact_index],
                    scanned_web=scanned_web,
                    web_manifest_payload=web_manifest_payload,
                    storage_migration_payload=product_migration_manifests[
                        built[artifact_index].artifact_id
                    ],
                    size_limit=self.max_core_bytes,
                    expanded_limit=MAX_CORE_EXPANDED_BYTES,
                    staging=staging,
                )
            web_manifest_path = staging / WEB_MANIFEST_FILE_NAME
            _atomic_write_bytes(web_manifest_path, web_manifest_payload)
            built.append(
                _BuiltArtifact(
                    kind="web_manifest",
                    platform=WEB_MANIFEST_PLATFORM,
                    architecture=WEB_MANIFEST_ARCHITECTURE,
                    artifact_id=WEB_MANIFEST_ARTIFACT_ID,
                    file_name=WEB_MANIFEST_FILE_NAME,
                    path=web_manifest_path,
                    size_bytes=len(web_manifest_payload),
                    sha256=hashlib.sha256(web_manifest_payload).hexdigest(),
                    files=(),
                )
            )
        for delta_base in sorted(
            spec.core_delta_bases,
            key=lambda item: (
                item.base_artifact.platform,
                item.base_artifact.architecture,
            ),
        ):
            base_artifact = delta_base.base_artifact
            target_item = next(
                (
                    item
                    for item in built
                    if item.kind == ArtifactKind.CORE.value
                    and item.platform == base_artifact.platform
                    and item.architecture == base_artifact.architecture
                ),
                None,
            )
            if target_item is None:
                raise ReleaseBuildError("matching target Core disappeared")
            target_endpoint = CoreDeltaEndpoint(
                release_id=release_id,
                version=__version__,
                build_digest=build_digest,
                artifact_id=target_item.artifact_id,
                artifact_sha256=target_item.sha256,
                artifact_size_bytes=target_item.size_bytes,
            )
            base_endpoint = CoreDeltaEndpoint.from_release(
                delta_base.base_manifest,
                base_artifact,
            )
            delta_id = core_delta_artifact_id(
                platform=target_item.platform,
                architecture=target_item.architecture,
                base_artifact_sha256=base_artifact.sha256,
            )
            delta_name = core_delta_file_name(
                platform=target_item.platform,
                architecture=target_item.architecture,
                base_artifact_sha256=base_artifact.sha256,
                target_artifact_sha256=target_item.sha256,
            )
            delta_path = staging / delta_name
            try:
                create_core_delta_archive(
                    base_package=Path(delta_base.base_package),
                    target_package=target_item.path,
                    base=base_endpoint,
                    target=target_endpoint,
                    destination=delta_path,
                )
            except DeltaNotBeneficial:
                continue
            built.append(
                _BuiltArtifact(
                    kind="core_delta",
                    platform=target_item.platform,
                    architecture=target_item.architecture,
                    artifact_id=delta_id,
                    file_name=delta_name,
                    path=delta_path,
                    size_bytes=delta_path.stat().st_size,
                    sha256=_sha256_file(delta_path),
                    files=(),
                )
            )
        artifacts: list[ReleaseArtifact] = []
        placeholder = SignatureEnvelope(
            "ed25519",
            "release-placeholder",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAA==",
        )
        for item in built:
            unsigned = ReleaseArtifact(
                artifact_id=item.artifact_id,
                platform=item.platform,
                architecture=item.architecture,
                file_name=item.file_name,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                signature=placeholder,
            )
            signature = sign_envelope(
                self.signer,
                unsigned.signed_payload(
                    release_id=release_id,
                    version=__version__,
                    build_digest=build_digest,
                ),
            )
            artifacts.append(replace(unsigned, signature=signature))

        unsigned_manifest = ReleaseManifest(
            schema_version=1,
            release_id=release_id,
            version=__version__,
            build_digest=build_digest,
            channel=spec.channel,
            created_at=spec.created_at,
            sources=manifest_sources,
            artifacts=tuple(artifacts),
            signature=placeholder,
        )
        manifest = replace(
            unsigned_manifest,
            signature=sign_envelope(self.signer, unsigned_manifest.canonical_payload()),
        )

        sbom_path = staging / "sbom.cdx.json"
        sbom_bytes = _pretty_json_bytes(
            _cyclonedx_sbom(spec, build_digest, built, scanned_web)
        )
        if len(sbom_bytes) > MAX_RELEASE_SBOM_BYTES:
            raise ReleaseBuildError("release SBOM exceeds its Bootstrap bound")
        _atomic_write_bytes(sbom_path, sbom_bytes)
        sbom_sha256 = hashlib.sha256(sbom_bytes).hexdigest()

        manifest_path = staging / "release-manifest.json"
        manifest_bytes = (
            manifest.to_json(include_signature=True, pretty=True) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(manifest_path, manifest_bytes)

        metadata_path = staging / "release-metadata.json"
        metadata = {
            "schema_version": 1,
            "release_id": release_id,
            "version": __version__,
            "channel": spec.channel.value,
            "created_at": spec.created_at,
            "build_digest": build_digest,
            "manifest": manifest_path.name,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_signature": manifest.signature.to_dict(),
            "sbom": sbom_path.name,
            "sbom_sha256": sbom_sha256,
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "kind": item.kind,
                    "platform": item.platform,
                    "architecture": item.architecture,
                    "file_name": item.file_name,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "signature": artifact.signature.to_dict(),
                }
                for item, artifact in zip(built, artifacts, strict=True)
            ],
        }
        if spec.dependency_lock_sha256 is not None:
            metadata["python_dependency_lock_sha256"] = spec.dependency_lock_sha256
        metadata_bytes = _pretty_json_bytes(metadata)
        if len(metadata_bytes) > MAX_RELEASE_METADATA_BYTES:
            raise ReleaseBuildError("release metadata exceeds its Bootstrap bound")
        _atomic_write_bytes(metadata_path, metadata_bytes)
        if (
            spec.web_bundle is not None
            and scan_web_bundle(spec.web_bundle) != scanned_web
        ):
            raise ReleaseBuildError("React dist changed during release construction")
        _fsync_directory(staging)
        return manifest, tuple(built), sbom_path, metadata_path, manifest_path

    def _build_capability_pack_manifest(
        self,
        *,
        definition: ArtifactBuildInput,
        artifact: _BuiltArtifact,
        staging: Path,
    ) -> _BuiltArtifact:
        registry = builtin_capability_registry()
        service_specs = builtin_pack_service_specs()
        bindings: list[PackToolBinding] = []
        for tool_id in definition.pack_tool_ids:
            try:
                spec = registry.get(tool_id)
            except Exception:
                raise ReleaseBuildError(
                    f"capability pack references unknown tool: {tool_id!r}"
                ) from None
            if definition.pack_id not in spec.required_packs:
                raise ReleaseBuildError(
                    f"tool {tool_id!r} does not require pack {definition.pack_id!r}"
                )
            bindings.append(
                PackToolBinding(
                    tool_id=spec.tool_id,
                    tool_version=spec.version,
                    spec_sha256=tool_spec_digest(spec),
                )
            )
        service_bindings: list[PackServiceBinding] = []
        for service_id in definition.pack_service_ids:
            try:
                service = service_specs[service_id]
            except KeyError:
                raise ReleaseBuildError(
                    f"capability pack references unknown service: {service_id!r}"
                ) from None
            service_bindings.append(
                PackServiceBinding(
                    service_id=service.service_id,
                    service_version=service.version,
                    contract_sha256=service.contract_sha256,
                )
            )
        placeholder = SignatureEnvelope(
            "ed25519",
            "capability-pack-placeholder",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAA==",
        )
        try:
            unsigned = CapabilityPackManifest(
                schema_version=2,
                pack_id=definition.pack_id or "",
                version=__version__,
                runtime_api_version=definition.runtime_api_version,
                platform=definition.platform,
                architecture=definition.architecture,
                artifact_file_name=artifact.file_name,
                artifact_size_bytes=artifact.size_bytes,
                artifact_sha256=artifact.sha256,
                tools=tuple(bindings),
                services=tuple(service_bindings),
                signature=placeholder,
            )
        except Exception as exc:
            raise ReleaseBuildError(
                f"capability-pack manifest is invalid: {type(exc).__name__}"
            ) from None
        manifest = replace(
            unsigned,
            signature=sign_envelope(self.signer, unsigned.canonical_payload()),
        )
        sidecar_id, sidecar_name = _pack_manifest_identity(definition)
        payload = manifest.to_bytes()
        if len(payload) > 256 * 1024:
            raise ReleaseBuildError("capability-pack manifest exceeds its size limit")
        path = staging / sidecar_name
        _atomic_write_bytes(path, payload)
        return _BuiltArtifact(
            kind="capability_pack_manifest",
            platform=definition.platform,
            architecture=definition.architecture,
            artifact_id=sidecar_id,
            file_name=sidecar_name,
            path=path,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            files=(),
        )


def _artifact_identity(definition: ArtifactBuildInput) -> tuple[str, str]:
    qualifier = (
        f"-{definition.pack_id}"
        if definition.kind is ArtifactKind.CAPABILITY_PACK
        else ""
    )
    artifact_id = definition.artifact_id or (
        f"{definition.kind.value}{qualifier}-{definition.platform}-{definition.architecture}"
    )
    file_name = definition.file_name or (
        f"ecorex-{definition.kind.value}{qualifier}-{definition.platform}-"
        f"{definition.architecture}-{__version__}.zip"
    )
    return artifact_id, file_name


def _pack_manifest_identity(definition: ArtifactBuildInput) -> tuple[str, str]:
    artifact_id, _file_name = _artifact_identity(definition)
    return (
        f"{artifact_id}-manifest",
        f"ecorex-capability-pack-{definition.pack_id}-{definition.platform}-"
        f"{definition.architecture}-{__version__}.json",
    )


def _statically_implements_signer(signer: object) -> bool:
    """Validate the signer shape without evaluating a backend property."""

    try:
        inspect.getattr_static(signer, "key_id")
        sign = inspect.getattr_static(signer, "sign")
    except AttributeError:
        return False
    return callable(sign)


def _validate_created_at(value: str) -> None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReleaseBuildError(
            "created_at must be an ISO-8601 timestamp with a UTC offset"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseBuildError(
            "created_at must be an ISO-8601 timestamp with a UTC offset"
        )


def _compare_semver(left: str, right: str) -> int:
    """Compare two already validated SemVer strings by SemVer precedence."""

    left_key = _semver_key(left)
    right_key = _semver_key(right)
    if left_key[:3] != right_key[:3]:
        return (left_key[:3] > right_key[:3]) - (left_key[:3] < right_key[:3])
    return _compare_prerelease(left_key[3], right_key[3])


def _semver_key(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    core_and_pre = value.split("+", 1)[0]
    core, separator, prerelease = core_and_pre.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return major, minor, patch, tuple(prerelease.split(".")) if separator else None


def _compare_prerelease(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
) -> int:
    if left is None or right is None:
        if left is right:
            return 0
        return 1 if left is None else -1
    for left_item, right_item in zip(left, right):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return (int(left_item) > int(right_item)) - (
                int(left_item) < int(right_item)
            )
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_item > right_item) - (left_item < right_item)
    return (len(left) > len(right)) - (len(left) < len(right))


def _validate_artifact_identity(
    artifact_id: str,
    file_name: str,
    definition: ArtifactBuildInput,
) -> None:
    try:
        ReleaseArtifact(
            artifact_id=artifact_id,
            platform=definition.platform,
            architecture=definition.architecture,
            file_name=file_name,
            size_bytes=1,
            sha256="0" * 64,
            signature=SignatureEnvelope(
                "ed25519",
                "validation-key",
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                "AAAAAAAAAAAAAAAAAAAAAA==",
            ),
        )
    except ValueError as exc:
        raise ReleaseBuildError(f"invalid artifact identity: {exc}") from exc


def _build_deterministic_zip(
    *,
    source: Path,
    destination: Path,
    executable_paths: Iterable[str],
    size_limit: int,
    expanded_limit: int,
) -> tuple[_FileRecord, ...]:
    entries = _scan_source_tree(source, executable_paths, expanded_limit)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    records: list[_FileRecord] = []
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for entry in entries:
                info = _zip_info(entry)
                if entry.is_directory:
                    archive.writestr(info, b"")
                else:
                    record = _write_zip_file(
                        archive, info, entry, temporary, size_limit
                    )
                    records.append(record)
                if temporary.stat().st_size > size_limit:
                    raise ReleaseBuildError(
                        f"artifact exceeds compressed size limit {size_limit}"
                    )
        if temporary.stat().st_size > size_limit:
            raise ReleaseBuildError(
                f"artifact exceeds compressed size limit {size_limit}"
            )
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return tuple(records)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _rebuild_product_core_with_web(
    *,
    definition: ArtifactBuildInput,
    artifact: _BuiltArtifact,
    scanned_web: ScannedWebBundle,
    web_manifest_payload: bytes,
    storage_migration_payload: bytes,
    size_limit: int,
    expanded_limit: int,
    staging: Path,
) -> _BuiltArtifact:
    """Inject the one signed React bundle into a platform Core deterministically."""

    # Import locally to avoid making the release data model import the ASGI
    # composition graph during normal module initialization.
    from ecorex.server.config import ProductRuntimeConfig

    config_path = definition.source_dir / "runtime-config.json"
    try:
        config_stat = config_path.lstat()
        if _metadata_is_link_or_reparse(config_stat) or not stat.S_ISREG(
            config_stat.st_mode
        ):
            raise ReleaseBuildError(
                "product runtime-config.json must be a regular non-link file"
            )
        with config_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            config_payload = stream.read(256 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = config_path.lstat()
        identity = (
            config_stat.st_dev,
            config_stat.st_ino,
            config_stat.st_size,
            config_stat.st_mtime_ns,
        )
        if (
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != identity
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != identity
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != identity
            or len(config_payload) != config_stat.st_size
        ):
            raise ReleaseBuildError(
                "product runtime-config.json changed while packaging"
            )
        runtime_config = ProductRuntimeConfig.from_bytes(config_payload)
    except ReleaseBuildError:
        raise
    except Exception as error:
        raise ReleaseBuildError(
            f"product runtime-config.json is invalid: {type(error).__name__}"
        ) from None
    if (
        runtime_config.identity.version != __version__
        or runtime_config.identity.platform != definition.platform
        or runtime_config.identity.architecture != definition.architecture
    ):
        raise ReleaseBuildError(
            "product runtime-config.json target does not match the Core"
        )

    overlay = Path(
        tempfile.mkdtemp(prefix=f".{artifact.artifact_id}.product-", dir=staging)
    )
    try:
        with zipfile.ZipFile(artifact.path, "r") as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                _validate_archive_path(relative)
                target = overlay.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)

        web_root = overlay.joinpath(*PurePosixPath(runtime_config.paths.web_root).parts)
        web_manifest_path = overlay.joinpath(
            *PurePosixPath(runtime_config.paths.web_manifest).parts
        )
        if os.path.lexists(web_root) or os.path.lexists(web_manifest_path):
            raise ReleaseBuildError(
                "product Core source collides with generated Web payload"
            )
        web_root.mkdir(parents=True)
        for record in scanned_web.files:
            source = scanned_web.dist_dir.joinpath(*PurePosixPath(record.path).parts)
            target = web_root.joinpath(*PurePosixPath(record.path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_verified_web_file(source, target, record)
        web_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(web_manifest_path):
            raise ReleaseBuildError(
                "product Web manifest path collides with generated payload"
            )
        with web_manifest_path.open("xb") as stream:
            stream.write(web_manifest_payload)
            stream.flush()
            os.fsync(stream.fileno())
        migration_path = overlay / "storage-migrations.json"
        if os.path.lexists(migration_path):
            try:
                migration_stat = migration_path.lstat()
                if (
                    _metadata_is_link_or_reparse(migration_stat)
                    or not stat.S_ISREG(migration_stat.st_mode)
                    or migration_path.read_bytes() != storage_migration_payload
                ):
                    raise ReleaseBuildError(
                        "product storage-migrations.json changed while packaging"
                    )
            except ReleaseBuildError:
                raise
            except OSError:
                raise ReleaseBuildError(
                    "product storage-migrations.json is unreadable"
                ) from None
        else:
            with migration_path.open("xb") as stream:
                stream.write(storage_migration_payload)
                stream.flush()
                os.fsync(stream.fileno())
        files = _build_deterministic_zip(
            source=overlay,
            destination=artifact.path,
            executable_paths=definition.executable_paths,
            size_limit=size_limit,
            expanded_limit=expanded_limit,
        )
        return _BuiltArtifact(
            kind=artifact.kind,
            platform=artifact.platform,
            architecture=artifact.architecture,
            artifact_id=artifact.artifact_id,
            file_name=artifact.file_name,
            path=artifact.path,
            size_bytes=artifact.path.stat().st_size,
            sha256=_sha256_file(artifact.path),
            files=files,
        )
    finally:
        if overlay.exists():
            shutil.rmtree(overlay)


def _copy_verified_web_file(
    source: Path,
    target: Path,
    record: Any,
) -> None:
    try:
        before = source.lstat()
        if (
            _metadata_is_link_or_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != record.size_bytes
        ):
            raise ReleaseBuildError(
                f"React file became unsafe while packaging: {record.path}"
            )
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, target.open("xb") as output_stream:
            opened = os.fstat(input_stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ReleaseBuildError(
                    f"React file changed while opening: {record.path}"
                )
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
        current = source.lstat()
    except ReleaseBuildError:
        raise
    except OSError:
        raise ReleaseBuildError(
            f"React file is unreadable while packaging: {record.path}"
        ) from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != identity
        or digest.hexdigest() != record.sha256
    ):
        raise ReleaseBuildError(f"React file changed while packaging: {record.path}")


def _scan_source_tree(
    source: Path,
    executable_paths: Iterable[str],
    max_unpacked_bytes: int,
) -> tuple[_SourceEntry, ...]:
    _require_real_directory(source, "artifact source directory")
    explicit_executables = {
        _normalize_relative_path(value) for value in executable_paths
    }
    pending: list[tuple[Path, PurePosixPath]] = [(source, PurePosixPath())]
    entries: list[_SourceEntry] = []
    seen: dict[str, tuple[str, bool]] = {}
    file_paths: set[str] = set()
    total_unpacked = 0
    while pending:
        directory, relative_root = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ReleaseBuildError(
                f"cannot read artifact source directory: {directory}"
            ) from exc
        for child in children:
            path = Path(child.path)
            metadata = path.lstat()
            if _metadata_is_link_or_reparse(metadata):
                raise ReleaseBuildError(
                    f"source contains a link or reparse point: {path}"
                )
            relative_path = relative_root / child.name
            relative = relative_path.as_posix()
            _validate_archive_path(relative_path)
            key = "/".join(
                portable_path_segment_key(part) for part in relative_path.parts
            )
            is_directory = child.is_dir(follow_symlinks=False)
            is_file = child.is_file(follow_symlinks=False)
            if not is_directory and not is_file:
                raise ReleaseBuildError(f"source contains a special file: {path}")
            if key in seen:
                raise ReleaseBuildError(
                    f"portable path collision: {relative!r} conflicts with {seen[key][0]!r}"
                )
            key_parts = key.split("/")
            for index in range(1, len(key_parts)):
                parent_key = "/".join(key_parts[:index])
                previous = seen.get(parent_key)
                if previous is not None and not previous[1]:
                    raise ReleaseBuildError(
                        f"file is used as a parent path: {relative!r}"
                    )
            if is_file and any(existing.startswith(key + "/") for existing in seen):
                raise ReleaseBuildError(
                    f"file conflicts with a directory path: {relative!r}"
                )
            seen[key] = (relative, is_directory)
            if is_directory:
                entries.append(
                    _SourceEntry(
                        relative=relative + "/",
                        path=path,
                        is_directory=True,
                        size=0,
                        mode=0o755,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        mtime_ns=metadata.st_mtime_ns,
                    )
                )
                if len(entries) > MAX_SOURCE_MEMBERS:
                    raise ReleaseBuildError(
                        f"source contains more than {MAX_SOURCE_MEMBERS} members"
                    )
                pending.append((path, relative_path))
                continue
            total_unpacked += metadata.st_size
            if total_unpacked > max_unpacked_bytes:
                raise ReleaseBuildError(
                    f"source expands above the {max_unpacked_bytes} byte hard limit"
                )
            # Source filesystem mode bits are intentionally ignored. They are
            # not stable between Windows and macOS checkout/build hosts.
            mode = 0o755 if relative in explicit_executables else 0o644
            entries.append(
                _SourceEntry(
                    relative=relative,
                    path=path,
                    is_directory=False,
                    size=metadata.st_size,
                    mode=mode,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    mtime_ns=metadata.st_mtime_ns,
                )
            )
            file_paths.add(relative)
            if len(entries) > MAX_SOURCE_MEMBERS:
                raise ReleaseBuildError(
                    f"source contains more than {MAX_SOURCE_MEMBERS} members"
                )
    missing_executables = explicit_executables - file_paths
    if missing_executables:
        raise ReleaseBuildError(
            f"executable_paths do not name packaged files: {sorted(missing_executables)!r}"
        )
    return tuple(sorted(entries, key=lambda item: item.relative))


def _zip_info(entry: _SourceEntry) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(entry.relative, date_time=FIXED_ZIP_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.flag_bits = 0
    info.extra = b""
    info.comment = b""
    if entry.is_directory:
        info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
    else:
        info.external_attr = (stat.S_IFREG | entry.mode) << 16
        info.file_size = entry.size
    return info


def _write_zip_file(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: _SourceEntry,
    temporary: Path,
    size_limit: int,
) -> _FileRecord:
    before = entry.path.lstat()
    _require_unchanged_entry(entry, before)
    digest = hashlib.sha256()
    written = 0
    require_lf_shell = entry.relative.casefold().endswith(".sh")
    with entry.path.open("rb") as source, archive.open(info, "w") as target:
        opened = os.fstat(source.fileno())
        _require_unchanged_entry(entry, opened)
        while chunk := source.read(64 * 1024):
            if require_lf_shell and b"\r" in chunk:
                raise ReleaseBuildError(
                    f"shell source must use LF line endings: {entry.relative}"
                )
            target.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if temporary.stat().st_size > size_limit:
                raise ReleaseBuildError(
                    f"artifact exceeds compressed size limit {size_limit}"
                )
        after = os.fstat(source.fileno())
    _require_unchanged_entry(entry, after)
    if written != entry.size:
        raise ReleaseBuildError(f"source file changed while packaging: {entry.path}")
    return _FileRecord(
        relative=entry.relative,
        size=written,
        mode=entry.mode,
        sha256=digest.hexdigest(),
    )


def _require_unchanged_entry(entry: _SourceEntry, metadata: os.stat_result) -> None:
    if (
        metadata.st_dev != entry.device
        or metadata.st_ino != entry.inode
        or metadata.st_size != entry.size
        or metadata.st_mtime_ns != entry.mtime_ns
        or _metadata_is_link_or_reparse(metadata)
    ):
        raise ReleaseBuildError(f"source changed while packaging: {entry.path}")


def _build_digest(
    spec: ReleaseBuildSpec,
    artifacts: Iterable[_BuiltArtifact],
    web_bundle: ScannedWebBundle | None,
    product_migration_manifests: Mapping[str, bytes],
) -> str:
    material: dict[str, Any] = {
        "version": __version__,
        "channel": spec.channel.value,
        "source_roots": [source.to_dict() for source in spec.sources],
        "release_scoped_sources": spec.release_scoped_sources,
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "kind": item.kind,
                "platform": item.platform,
                "architecture": item.architecture,
                "file_name": item.file_name,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "files": [record.to_dict() for record in item.files],
                "product_runtime": next(
                    (
                        definition.product_runtime
                        for definition in spec.artifacts
                        if _artifact_identity(definition)[0] == item.artifact_id
                    ),
                    False,
                ),
                "storage_migration_manifest_sha256": (
                    hashlib.sha256(
                        product_migration_manifests[item.artifact_id]
                    ).hexdigest()
                    if item.artifact_id in product_migration_manifests
                    else None
                ),
            }
            for item in artifacts
        ],
    }
    if spec.dependency_lock_sha256 is not None:
        material["python_dependency_lock_sha256"] = spec.dependency_lock_sha256
    if spec.core_delta_bases:
        material["core_delta_bases"] = [
            {
                "release_id": item.base_manifest.release_id,
                "version": item.base_manifest.version,
                "build_digest": item.base_manifest.build_digest,
                "artifact_id": item.base_artifact.artifact_id,
                "platform": item.base_artifact.platform,
                "architecture": item.base_artifact.architecture,
                "artifact_sha256": item.base_artifact.sha256,
                "artifact_size_bytes": item.base_artifact.size_bytes,
            }
            for item in sorted(
                spec.core_delta_bases,
                key=lambda candidate: (
                    candidate.base_artifact.platform,
                    candidate.base_artifact.architecture,
                ),
            )
        ]
    if web_bundle is not None:
        # The final web-manifest JSON contains build_digest and therefore
        # cannot itself be digest input. Its complete signed file inventory is
        # the non-circular source material instead.
        material["web_bundle"] = web_bundle.digest_material()
    payload = b"ecorex-build-v1\n" + _canonical_json_bytes(material) + b"\n"
    return hashlib.sha256(payload).hexdigest()


def _manifest_sources(
    spec: ReleaseBuildSpec, *, release_id: str
) -> tuple[ReleaseSource, ...]:
    if not spec.release_scoped_sources:
        return spec.sources
    resolved: list[ReleaseSource] = []
    for source in spec.sources:
        if source.kind is SourceKind.GITHUB_RELEASE or (
            source.kind is SourceKind.GITHUB_CN_MIRROR
            and source.base_url.endswith("/releases/download")
        ):
            if not source.base_url.endswith("/releases/download"):
                raise ReleaseBuildError(
                    "release-scoped GitHub source must end with /releases/download"
                )
            suffix = release_tag(__version__, spec.channel, release_id=release_id)
        else:
            suffix = release_id
        resolved.append(
            ReleaseSource(
                source_id=source.source_id,
                kind=source.kind,
                priority=source.priority,
                base_url=f"{source.base_url}/{suffix}",
            )
        )
    return tuple(resolved)


def _product_migration_manifest_payload(definition: ArtifactBuildInput) -> bytes:
    """Load or synthesize the bounded declarative plan embedded in a Core."""

    from ecorex.runtime.database import SCHEMA_VERSION
    from ecorex.runtime.storage_migrations import (
        MAX_STORAGE_MIGRATION_BYTES,
        StorageMigrationError,
        StorageMigrationManifest,
        current_storage_schema_sha256,
        product_storage_migration_manifest,
    )

    path = definition.source_dir / "storage-migrations.json"
    if not os.path.lexists(path):
        return product_storage_migration_manifest().to_bytes()
    try:
        before = path.lstat()
        if _metadata_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise ReleaseBuildError(
                "product storage-migrations.json must be a regular non-link file"
            )
        if not 1 <= before.st_size <= MAX_STORAGE_MIGRATION_BYTES:
            raise ReleaseBuildError("product storage-migrations.json size is invalid")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(MAX_STORAGE_MIGRATION_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            any(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
                != identity
                for metadata in (opened, after, current)
            )
            or len(payload) != before.st_size
        ):
            raise ReleaseBuildError(
                "product storage-migrations.json changed while packaging"
            )
        manifest = StorageMigrationManifest.from_bytes(payload)
    except ReleaseBuildError:
        raise
    except StorageMigrationError as error:
        raise ReleaseBuildError(
            f"product storage-migrations.json is invalid: {error}"
        ) from None
    except OSError:
        raise ReleaseBuildError(
            "product storage-migrations.json is unreadable"
        ) from None
    if manifest.target_schema_version != SCHEMA_VERSION:
        raise ReleaseBuildError(
            "product storage-migrations.json target does not match the Runtime schema"
        )
    if manifest.target_schema_sha256 != current_storage_schema_sha256():
        raise ReleaseBuildError(
            "product storage-migrations.json target schema digest does not match "
            "the compiled Runtime schema"
        )
    return payload


def _cyclonedx_sbom(
    spec: ReleaseBuildSpec,
    build_digest: str,
    artifacts: Iterable[_BuiltArtifact],
    web_bundle: ScannedWebBundle | None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if spec.dependency_lock_sha256 is not None:
        components.append(
            {
                "type": "file",
                "bom-ref": "dependency-lock:python",
                "name": "requirements/locks/manifest.json",
                "hashes": [{"alg": "SHA-256", "content": spec.dependency_lock_sha256}],
                "properties": [
                    {"name": "ecorex:dependency-lock", "value": "python-universal"},
                    {"name": "ecorex:python", "value": "3.11"},
                ],
            }
        )
    for artifact in artifacts:
        artifact_ref = f"artifact:{artifact.artifact_id}"
        components.append(
            {
                "type": "application",
                "bom-ref": artifact_ref,
                "name": artifact.file_name,
                "version": __version__,
                "hashes": [{"alg": "SHA-256", "content": artifact.sha256}],
                "properties": [
                    {"name": "ecorex:artifact-id", "value": artifact.artifact_id},
                    {"name": "ecorex:kind", "value": artifact.kind},
                    {"name": "ecorex:platform", "value": artifact.platform},
                    {
                        "name": "ecorex:architecture",
                        "value": artifact.architecture,
                    },
                    {"name": "ecorex:size-bytes", "value": str(artifact.size_bytes)},
                ],
            }
        )
        for record in artifact.files:
            components.append(
                {
                    "type": "file",
                    "bom-ref": f"{artifact_ref}:file:{record.relative}",
                    "name": record.relative,
                    "hashes": [{"alg": "SHA-256", "content": record.sha256}],
                    "properties": [
                        {"name": "ecorex:packaged-in", "value": artifact.artifact_id},
                        {"name": "ecorex:mode", "value": f"{record.mode:04o}"},
                        {"name": "ecorex:size-bytes", "value": str(record.size)},
                    ],
                }
            )
        components.extend(_macos_native_sbom_components(artifact))
    if web_bundle is not None:
        for record in web_bundle.files:
            components.append(
                {
                    "type": "file",
                    "bom-ref": f"web-bundle:file:{record.path}",
                    "name": record.path,
                    "hashes": [{"alg": "SHA-256", "content": record.sha256}],
                    "properties": [
                        {
                            "name": "ecorex:allowlisted-by",
                            "value": WEB_MANIFEST_ARTIFACT_ID,
                        },
                        {
                            "name": "ecorex:immutable",
                            "value": "true" if record.immutable else "false",
                        },
                        {
                            "name": "ecorex:size-bytes",
                            "value": str(record.size_bytes),
                        },
                    ],
                }
            )
    components.sort(key=lambda item: item["bom-ref"])
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ecorex:build:{build_digest}")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": spec.created_at,
            "component": {
                "type": "application",
                "bom-ref": "pkg:pypi/ecorex-agent-runtime",
                "name": "EcoreX",
                "version": __version__,
            },
            "properties": [
                {"name": "ecorex:build-digest", "value": build_digest},
                {"name": "ecorex:channel", "value": spec.channel.value},
            ]
            + (
                [
                    {
                        "name": "ecorex:python-dependency-lock-sha256",
                        "value": spec.dependency_lock_sha256,
                    }
                ]
                if spec.dependency_lock_sha256 is not None
                else []
            ),
        },
        "components": components,
    }


def _macos_native_sbom_components(
    artifact: _BuiltArtifact,
) -> list[dict[str, Any]]:
    member_name = "bin/pack-python/native-components.json"
    if artifact.kind != "core" or artifact.platform != "macos":
        return []
    records = {record.relative: record for record in artifact.files}
    inventory_record = records.get(member_name)
    if inventory_record is None:
        if not any(relative.startswith("bin/pack-python/") for relative in records):
            return []
        raise ReleaseBuildError("macOS Core native component inventory is missing")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            members = [
                item for item in archive.infolist() if item.filename == member_name
            ]
            if (
                len(members) != 1
                or members[0].is_dir()
                or members[0].file_size > 64 * 1024
            ):
                raise ReleaseBuildError(
                    "macOS Core native component inventory is invalid"
                )
            payload = archive.read(members[0])
        if hashlib.sha256(payload).hexdigest() != inventory_record.sha256:
            raise ReleaseBuildError(
                "macOS Core native component inventory digest mismatches"
            )
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_native_object
        )
    except ReleaseBuildError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile):
        raise ReleaseBuildError(
            "macOS Core native component inventory is invalid"
        ) from None
    expected_keys = {
        "architecture",
        "components",
        "distribution",
        "license_notice",
        "license_texts",
        "platform",
        "schema_version",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("platform") != "macos"
        or value.get("architecture") != artifact.architecture
        or value.get("distribution") != dict(PYTHON_MACOS_DISTRIBUTION)
        or not isinstance(value.get("components"), list)
    ):
        raise ReleaseBuildError("macOS Core native component inventory is invalid")
    native = value["components"]
    license_texts = value.get("license_texts")
    notice = value.get("license_notice")
    if (
        bool(native)
        and notice
        != {
            "path": PYTHON_MACOS_LICENSE["path"],
            "sha256": PYTHON_MACOS_LICENSE["sha256"],
            "size_bytes": PYTHON_MACOS_LICENSE["size_bytes"],
        }
    ) or (not native and notice is not None):
        raise ReleaseBuildError("macOS Core native component license notice is invalid")
    if isinstance(notice, dict):
        notice_record = records.get(f"bin/pack-python/{notice['path']}")
        if (
            notice_record is None
            or notice_record.sha256 != notice["sha256"]
            or notice_record.size != notice["size_bytes"]
        ):
            raise ReleaseBuildError(
                "macOS Core native component license notice is missing"
            )
    if not isinstance(license_texts, list):
        raise ReleaseBuildError("macOS Core native license text inventory is invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for component in native:
        keys = {
            "license",
            "license_text",
            "name",
            "path",
            "sha256",
            "source_sha256",
            "version",
        }
        if (
            not isinstance(component, dict)
            or set(component) != keys
            or any(
                not isinstance(component[key], str) or not component[key]
                for key in keys
            )
            or component["path"] in seen
        ):
            raise ReleaseBuildError("macOS Core native component inventory is invalid")
        seen.add(component["path"])
        contract = MACOS_NATIVE_COMPONENTS.get(PurePosixPath(component["path"]).name)
        if (
            contract is None
            or PurePosixPath(component["path"])
            != PurePosixPath("lib") / PurePosixPath(component["path"]).name
            or component["name"] != contract.name
            or component["version"] != contract.version
            or component["license"] != contract.license
            or component["license_text"]
            != MACOS_NATIVE_LICENSES[contract.license_text].archive_path
            or component["source_sha256"] != contract.source_sha256
        ):
            raise ReleaseBuildError(
                "macOS Core native component violates immutable contract"
            )
        packaged_path = f"bin/pack-python/{component['path']}"
        record = records.get(packaged_path)
        if record is None or record.sha256 != component["sha256"]:
            raise ReleaseBuildError(
                "macOS Core native component payload mismatches inventory"
            )
        license_value = component["license"]
        license_text = MACOS_NATIVE_LICENSES[contract.license_text]
        license_entry = (
            {"license": {"id": license_value}}
            if license_value in {"Apache-2.0", "TCL"}
            else {"license": {"name": license_value}}
        )
        result.append(
            {
                "type": "library",
                "bom-ref": (
                    f"native:{artifact.platform}:{artifact.architecture}:"
                    f"{component['path']}"
                ),
                "name": component["name"],
                "version": component["version"],
                "hashes": [{"alg": "SHA-256", "content": component["sha256"]}],
                "licenses": [license_entry],
                "externalReferences": [
                    {
                        "type": "distribution",
                        "url": PYTHON_MACOS_DISTRIBUTION["url"],
                        "hashes": [
                            {
                                "alg": "SHA-256",
                                "content": PYTHON_MACOS_DISTRIBUTION["sha256"],
                            }
                        ],
                    },
                    {
                        "type": "other",
                        "url": license_text.source_url,
                        "hashes": [
                            {
                                "alg": "SHA-256",
                                "content": license_text.source_archive_sha256,
                            }
                        ],
                    },
                ],
                "properties": [
                    {"name": "ecorex:native-path", "value": component["path"]},
                    {"name": "ecorex:packaged-in", "value": artifact.artifact_id},
                    {
                        "name": "ecorex:source-sha256",
                        "value": component["source_sha256"],
                    },
                    {
                        "name": "ecorex:distribution-size-bytes",
                        "value": str(PYTHON_MACOS_DISTRIBUTION["size_bytes"]),
                    },
                    {
                        "name": "ecorex:license-notice",
                        "value": notice["path"] if isinstance(notice, dict) else "",
                    },
                    {
                        "name": "ecorex:license-notice-sha256",
                        "value": notice["sha256"] if isinstance(notice, dict) else "",
                    },
                    {
                        "name": "ecorex:license-text",
                        "value": license_text.archive_path,
                    },
                    {
                        "name": "ecorex:license-text-sha256",
                        "value": license_text.sha256,
                    },
                    {
                        "name": "ecorex:license-source-internal-path",
                        "value": license_text.source_internal_path,
                    },
                ],
            }
        )
    expected_license_texts = [
        {
            "path": contract.archive_path,
            "provenance": contract.provenance,
            "sha256": contract.sha256,
            "size_bytes": contract.size_bytes,
            "source_archive_sha256": contract.source_archive_sha256,
            "source_internal_path": contract.source_internal_path,
            "source_url": contract.source_url,
        }
        for key, contract in sorted(MACOS_NATIVE_LICENSES.items())
        if key
        in {
            MACOS_NATIVE_COMPONENTS[PurePosixPath(item["path"]).name].license_text
            for item in native
        }
    ]
    if license_texts != expected_license_texts:
        raise ReleaseBuildError("macOS Core native license text inventory is invalid")
    for license_text in expected_license_texts:
        record = records.get(f"bin/pack-python/{license_text['path']}")
        if (
            record is None
            or record.sha256 != license_text["sha256"]
            or record.size != license_text["size_bytes"]
        ):
            raise ReleaseBuildError("macOS Core native license text is missing")
    return result


def _unique_native_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native inventory key")
        result[key] = value
    return result


def _normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReleaseBuildError(f"unsafe executable path: {value!r}")
    _validate_archive_path(path)
    return path.as_posix()


def _validate_archive_path(path: PurePosixPath) -> None:
    try:
        for part in path.parts:
            validate_portable_path_segment(part, label="release archive path")
    except ValueError as exc:
        raise ReleaseBuildError(
            f"release archive path is not portable: {path.as_posix()!r}"
        ) from exc


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is missing or unreadable: {path}") from exc
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseBuildError(f"{label} must not be a link or reparse point: {path}")


def _sha256_file(path: Path) -> str:
    before = path.lstat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReleaseBuildError(f"file changed while hashing: {path}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ReleaseBuildError(f"file changed while hashing: {path}")
    return digest.hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_staging_tree(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise ReleaseBuildError(
            "refusing to clean staging outside release parent"
        ) from exc
    shutil.rmtree(resolved)
    _fsync_directory(parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

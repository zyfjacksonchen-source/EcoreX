"""Atomic download, verification and slot projection for Capability Packs.

Core and the complete host Pack set are one activation unit. This module owns the
narrow supplemental-artifact workflow so ``InstallCoordinator`` remains the
state authority without learning Pack internals.  A signed manifest either has
no Capability Pack artifacts (explicit legacy/core-only compatibility) or the
exact product pack archive+sidecar set for the host. Partial sets are
always rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any, Callable, Mapping, Protocol

from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS

from .download_cache import VerifiedDownloadCache, VerifiedDownloadLease
from .fetching import ArtifactFetcher
from .manifest import (
    MAX_ARTIFACT_BYTES,
    SOURCE_PRIORITY,
    ReleaseArtifact,
    ReleaseManifest,
)
from .storage import atomic_write_json
from .verification import (
    SignatureVerifier,
    verify_artifact_file,
    verify_artifact_signature,
)


REQUIRED_PACK_IDS = REQUIRED_CAPABILITY_PACK_IDS
RUNTIME_API_VERSION = "1.0.0"
PACK_SET_MARKER_SCHEMA_VERSION = 1
PACK_INSTALL_PROGRESS_SCHEMA_VERSION = 1


class PackInstallError(RuntimeError):
    """Stable supplemental release failure."""


class IncompletePackSet(PackInstallError):
    pass


class PackDownloadFailed(PackInstallError):
    pass


class PackContentVerifier(Protocol):
    """Dependency-inverted verifier for the inner Capability Pack contract."""

    def __call__(
        self,
        sidecar_payload: bytes,
        artifact_path: Path,
        *,
        pack_id: str,
        release_version: str,
        platform: str,
        architecture: str,
        artifact: ReleaseArtifact,
        verifier: SignatureVerifier,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PackArtifactPair:
    pack_id: str
    artifact: ReleaseArtifact
    sidecar: ReleaseArtifact

    @property
    def relative_directory(self) -> PurePosixPath:
        return PurePosixPath("capability-packs") / self.pack_id

    @property
    def artifact_relative_path(self) -> PurePosixPath:
        return self.relative_directory / self.artifact.file_name

    @property
    def sidecar_relative_path(self) -> PurePosixPath:
        return self.relative_directory / self.sidecar.file_name


@dataclass(frozen=True, slots=True)
class ReleasePackSet:
    release_id: str
    version: str
    build_digest: str
    platform: str
    architecture: str
    pairs: tuple[PackArtifactPair, ...]

    @property
    def artifacts(self) -> tuple[ReleaseArtifact, ...]:
        result: list[ReleaseArtifact] = []
        for pair in self.pairs:
            result.extend((pair.artifact, pair.sidecar))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class PreparedPackSet:
    pack_set: ReleasePackSet
    package_paths: Mapping[str, Path]

    def payload_enricher(self, payload_root: Path) -> Mapping[str, Any]:
        return install_prepared_pack_set(payload_root, self)


def resolve_release_pack_set(
    manifest: ReleaseManifest,
    *,
    platform: str,
    architecture: str,
    verifier: SignatureVerifier,
) -> ReleasePackSet | None:
    all_pack_artifacts = tuple(
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id.startswith("capability-pack-")
    )
    if not all_pack_artifacts:
        # Explicit compatibility for existing signed Core-only manifests.  A
        # manifest that declares even one Pack switches to the strict all-three
        # product contract below; there is no silent partial fallback.
        return None
    target = {
        artifact.artifact_id: artifact
        for artifact in all_pack_artifacts
        if artifact.platform == platform and artifact.architecture == architecture
    }
    expected_ids = {
        identity
        for pack_id in REQUIRED_PACK_IDS
        for identity in (
            f"capability-pack-{pack_id}-{platform}-{architecture}",
            f"capability-pack-{pack_id}-{platform}-{architecture}-manifest",
        )
    }
    if set(target) != expected_ids:
        raise IncompletePackSet("signed release does not contain the exact host Pack set")
    pairs: list[PackArtifactPair] = []
    for pack_id in REQUIRED_PACK_IDS:
        artifact = target[f"capability-pack-{pack_id}-{platform}-{architecture}"]
        sidecar = target[
            f"capability-pack-{pack_id}-{platform}-{architecture}-manifest"
        ]
        if artifact.file_name != (
            f"ecorex-capability-pack-{pack_id}-{platform}-{architecture}-"
            f"{manifest.version}.zip"
        ) or sidecar.file_name != (
            f"ecorex-capability-pack-{pack_id}-{platform}-{architecture}-"
            f"{manifest.version}.json"
        ):
            raise IncompletePackSet("signed Pack file identity is not canonical")
        verify_artifact_signature(manifest, artifact, verifier)
        verify_artifact_signature(manifest, sidecar, verifier)
        pairs.append(PackArtifactPair(pack_id, artifact, sidecar))
    return ReleasePackSet(
        release_id=manifest.release_id,
        version=manifest.version,
        build_digest=manifest.build_digest,
        platform=platform,
        architecture=architecture,
        pairs=tuple(pairs),
    )


class PackSetDownloader:
    """Crash-resumable downloader for the six host Pack release artifacts."""

    def __init__(
        self,
        *,
        fetcher: ArtifactFetcher,
        verifier: SignatureVerifier,
        disk_free_provider: Callable[[Path], int],
        disk_reserve_bytes: int,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        pack_content_verifier: PackContentVerifier | None = None,
        download_cache: VerifiedDownloadCache | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.verifier = verifier
        self.disk_free_provider = disk_free_provider
        self.disk_reserve_bytes = disk_reserve_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.pack_content_verifier = pack_content_verifier
        self.download_cache = download_cache

    def prepare(
        self,
        manifest: ReleaseManifest,
        pack_set: ReleasePackSet,
        transaction_dir: Path,
    ) -> PreparedPackSet:
        root = _real_directory(transaction_dir)
        downloads = root / "capability-packs"
        downloads.mkdir(exist_ok=True)
        _reject_link(downloads)
        progress_path = root / "pack-install.json"
        progress = self._load_or_create_progress(progress_path, pack_set)
        package_paths: dict[str, Path] = {}
        for artifact in pack_set.artifacts:
            if artifact.size_bytes > self.max_artifact_bytes:
                raise PackInstallError("Capability Pack artifact exceeds size policy")
            # The inner signed CapabilityPackManifest binds the archive's
            # exact file name, so transaction files retain release names.
            path = downloads / artifact.file_name
            package_paths[artifact.artifact_id] = path
            state = progress["artifacts"][artifact.artifact_id]
            self._download_one(
                manifest,
                artifact,
                path,
                state=state,
                progress=progress,
                progress_path=progress_path,
            )
        prepared = PreparedPackSet(pack_set=pack_set, package_paths=package_paths)
        verify_prepared_pack_set(
            manifest,
            prepared,
            verifier=self.verifier,
            pack_content_verifier=self.pack_content_verifier,
        )
        progress["status"] = "verified"
        atomic_write_json(progress_path, progress)
        return prepared

    def _download_one(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        destination: Path,
        *,
        state: dict[str, Any],
        progress: dict[str, Any],
        progress_path: Path,
    ) -> None:
        verify_artifact_signature(manifest, artifact, self.verifier)
        if self.download_cache is None:
            return self._download_one_under_lease(
                manifest,
                artifact,
                destination,
                state=state,
                progress=progress,
                progress_path=progress_path,
                cache_lease=None,
            )
        with self.download_cache.acquire(manifest, artifact) as cache_lease:
            return self._download_one_under_lease(
                manifest,
                artifact,
                destination,
                state=state,
                progress=progress,
                progress_path=progress_path,
                cache_lease=cache_lease,
            )

    def _download_one_under_lease(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        destination: Path,
        *,
        state: dict[str, Any],
        progress: dict[str, Any],
        progress_path: Path,
        cache_lease: VerifiedDownloadLease | None,
    ) -> None:
        if os.path.lexists(destination):
            _reject_link(destination)
            if not destination.is_file():
                raise PackInstallError(
                    "Capability Pack partial download is not a regular file"
                )
            try:
                verify_artifact_file(destination, manifest, artifact, self.verifier)
            except Exception:
                _unlink_regular(destination)
            else:
                if cache_lease is not None:
                    cache_lease.admit(destination)
                state["status"] = "verified"
                atomic_write_json(progress_path, progress)
                return
        if cache_lease is not None and cache_lease.materialize(destination):
            state["status"] = "verified"
            atomic_write_json(progress_path, progress)
            return
        start_index = int(state.get("source_index", 0))
        last_error: BaseException | None = None
        for index in range(start_index, len(manifest.sources)):
            source = manifest.sources[index]
            state["source_index"] = index
            state["status"] = "downloading"
            atomic_write_json(progress_path, progress)
            if destination.exists() and destination.stat().st_size > artifact.size_bytes:
                _unlink_regular(destination)
            resume_from = destination.stat().st_size if destination.exists() else 0
            required = artifact.size_bytes - resume_from + self.disk_reserve_bytes
            if self.disk_free_provider(destination.parent) < required:
                raise PackDownloadFailed("insufficient disk space for Capability Packs")
            try:
                if resume_from < artifact.size_bytes:
                    self.fetcher.fetch(
                        source,
                        artifact,
                        destination,
                        resume_from=resume_from,
                        max_bytes=artifact.size_bytes,
                    )
                state["status"] = "verifying"
                atomic_write_json(progress_path, progress)
                verify_artifact_file(destination, manifest, artifact, self.verifier)
                if cache_lease is not None:
                    cache_lease.admit(destination)
            except Exception as error:
                last_error = error
                _unlink_regular(destination)
                state["source_index"] = index + 1
                state["status"] = "retrying"
                atomic_write_json(progress_path, progress)
                continue
            state["status"] = "verified"
            atomic_write_json(progress_path, progress)
            return
        raise PackDownloadFailed("all signed sources failed for a Capability Pack") from last_error

    @staticmethod
    def _load_or_create_progress(
        path: Path,
        pack_set: ReleasePackSet,
    ) -> dict[str, Any]:
        expected_identity = {
            "release_id": pack_set.release_id,
            "version": pack_set.version,
            "build_digest": pack_set.build_digest,
            "platform": pack_set.platform,
            "architecture": pack_set.architecture,
        }
        if path.exists():
            _reject_link(path)
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise PackInstallError("Capability Pack progress is unreadable") from None
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "schema_version",
                    "release_id",
                    "version",
                    "build_digest",
                    "platform",
                    "architecture",
                    "status",
                    "artifacts",
                }
                or value.get("schema_version") != PACK_INSTALL_PROGRESS_SCHEMA_VERSION
                or any(value.get(key) != item for key, item in expected_identity.items())
                or value.get("status") not in {"preparing", "verified"}
                or not isinstance(value.get("artifacts"), dict)
                or set(value["artifacts"])
                != {artifact.artifact_id for artifact in pack_set.artifacts}
            ):
                raise PackInstallError("Capability Pack progress identity changed")
            for state in value["artifacts"].values():
                if (
                    not isinstance(state, dict)
                    or set(state) != {"source_index", "status"}
                    or isinstance(state.get("source_index"), bool)
                    or not isinstance(state.get("source_index"), int)
                    or state["source_index"] < 0
                    or state["source_index"] > len(SOURCE_PRIORITY)
                    or state.get("status")
                    not in {"queued", "downloading", "verifying", "retrying", "verified"}
                ):
                    raise PackInstallError("Capability Pack progress is malformed")
            return value
        value = {
            "schema_version": PACK_INSTALL_PROGRESS_SCHEMA_VERSION,
            **expected_identity,
            "status": "preparing",
            "artifacts": {
                artifact.artifact_id: {"source_index": 0, "status": "queued"}
                for artifact in pack_set.artifacts
            },
        }
        atomic_write_json(path, value)
        return value


def verify_prepared_pack_set(
    manifest: ReleaseManifest,
    prepared: PreparedPackSet,
    *,
    verifier: SignatureVerifier,
    pack_content_verifier: PackContentVerifier | None,
) -> None:
    pack_set = prepared.pack_set
    if (
        pack_set.release_id != manifest.release_id
        or pack_set.version != manifest.version
        or pack_set.build_digest != manifest.build_digest
    ):
        raise PackInstallError("prepared Pack set belongs to another release")
    for pair in pack_set.pairs:
        artifact_path = prepared.package_paths.get(pair.artifact.artifact_id)
        sidecar_path = prepared.package_paths.get(pair.sidecar.artifact_id)
        if artifact_path is None or sidecar_path is None:
            raise IncompletePackSet("prepared Pack file set is incomplete")
        verify_artifact_file(artifact_path, manifest, pair.artifact, verifier)
        verify_artifact_file(sidecar_path, manifest, pair.sidecar, verifier)
        if pack_content_verifier is None:
            raise PackInstallError("Capability Pack content verifier is unavailable")
        sidecar_payload = _stable_bytes(sidecar_path, 256 * 1024)
        pack_content_verifier(
            sidecar_payload,
            artifact_path,
            pack_id=pair.pack_id,
            release_version=manifest.version,
            platform=pack_set.platform,
            architecture=pack_set.architecture,
            artifact=pair.artifact,
            verifier=verifier,
        )


def install_prepared_pack_set(
    payload_root: Path,
    prepared: PreparedPackSet,
) -> Mapping[str, Any]:
    root = _real_directory(payload_root)
    capability_root = root / "capability-packs"
    if os.path.lexists(capability_root):
        raise PackInstallError("Core payload collides with Capability Pack install root")
    capability_root.mkdir()
    records: list[dict[str, Any]] = []
    for pair in prepared.pack_set.pairs:
        directory = capability_root / pair.pack_id
        directory.mkdir()
        artifact_source = prepared.package_paths[pair.artifact.artifact_id]
        sidecar_source = prepared.package_paths[pair.sidecar.artifact_id]
        artifact_target = directory / pair.artifact.file_name
        sidecar_target = directory / pair.sidecar.file_name
        _transfer_stable(artifact_source, artifact_target)
        _transfer_stable(sidecar_source, sidecar_target)
        records.append(
            {
                "pack_id": pair.pack_id,
                "artifact_id": pair.artifact.artifact_id,
                "artifact_sha256": pair.artifact.sha256,
                "artifact_relative_path": pair.artifact_relative_path.as_posix(),
                "sidecar_artifact_id": pair.sidecar.artifact_id,
                "sidecar_sha256": pair.sidecar.sha256,
                "sidecar_relative_path": pair.sidecar_relative_path.as_posix(),
            }
        )
    _fsync_tree(capability_root)
    return {
        "schema_version": PACK_SET_MARKER_SCHEMA_VERSION,
        "kind": "ecorex-capability-pack-set",
        "release_id": prepared.pack_set.release_id,
        "version": prepared.pack_set.version,
        "build_digest": prepared.pack_set.build_digest,
        "platform": prepared.pack_set.platform,
        "architecture": prepared.pack_set.architecture,
        "packs": records,
    }


def validate_installed_pack_set(
    slot_path: Path,
    manifest: ReleaseManifest,
    *,
    verifier: SignatureVerifier,
    platform: str,
    architecture: str,
    pack_content_verifier: PackContentVerifier | None = None,
) -> ReleasePackSet | None:
    slot = _real_directory(slot_path)
    pack_set = resolve_release_pack_set(
        manifest,
        platform=platform,
        architecture=architecture,
        verifier=verifier,
    )
    marker = _read_json(slot / ".slot.json", maximum=256 * 1024)
    supplemental = marker.get("supplemental")
    if pack_set is None:
        if supplemental is not None:
            raise PackInstallError("Core-only release has an unexpected Pack projection")
        if os.path.lexists(slot / "payload" / "capability-packs"):
            raise PackInstallError("Core-only release contains an unverified Pack directory")
        return None
    expected_keys = {
        "schema_version",
        "kind",
        "release_id",
        "version",
        "build_digest",
        "platform",
        "architecture",
        "packs",
    }
    if (
        not isinstance(supplemental, Mapping)
        or set(supplemental) != expected_keys
        or supplemental.get("schema_version") != PACK_SET_MARKER_SCHEMA_VERSION
        or supplemental.get("kind") != "ecorex-capability-pack-set"
        or supplemental.get("release_id") != manifest.release_id
        or supplemental.get("version") != manifest.version
        or supplemental.get("build_digest") != manifest.build_digest
        or supplemental.get("platform") != platform
        or supplemental.get("architecture") != architecture
        or not isinstance(supplemental.get("packs"), list)
        or len(supplemental["packs"]) != len(REQUIRED_PACK_IDS)
    ):
        raise PackInstallError("installed Capability Pack marker is invalid")
    observed = {
        record.get("pack_id"): record
        for record in supplemental["packs"]
        if isinstance(record, Mapping)
    }
    if set(observed) != set(REQUIRED_PACK_IDS):
        raise IncompletePackSet("installed Capability Pack marker is incomplete")
    payload = _real_directory(slot / "payload")
    capability_root = _real_directory(payload / "capability-packs")
    if {path.name for path in capability_root.iterdir()} != set(REQUIRED_PACK_IDS):
        raise IncompletePackSet("installed Capability Pack directory set is incomplete")
    for pair in pack_set.pairs:
        record = observed[pair.pack_id]
        expected_record = {
            "pack_id": pair.pack_id,
            "artifact_id": pair.artifact.artifact_id,
            "artifact_sha256": pair.artifact.sha256,
            "artifact_relative_path": pair.artifact_relative_path.as_posix(),
            "sidecar_artifact_id": pair.sidecar.artifact_id,
            "sidecar_sha256": pair.sidecar.sha256,
            "sidecar_relative_path": pair.sidecar_relative_path.as_posix(),
        }
        if dict(record) != expected_record:
            raise PackInstallError("installed Capability Pack identity changed")
        directory = _real_directory(capability_root / pair.pack_id)
        if {path.name for path in directory.iterdir()} != {
            pair.artifact.file_name,
            pair.sidecar.file_name,
        }:
            raise PackInstallError("installed Capability Pack contains extra or missing files")
        artifact_path = _contained_file(payload, pair.artifact_relative_path)
        sidecar_path = _contained_file(payload, pair.sidecar_relative_path)
        prepared = PreparedPackSet(
            pack_set=ReleasePackSet(
                release_id=pack_set.release_id,
                version=pack_set.version,
                build_digest=pack_set.build_digest,
                platform=pack_set.platform,
                architecture=pack_set.architecture,
                pairs=(pair,),
            ),
            package_paths={
                pair.artifact.artifact_id: artifact_path,
                pair.sidecar.artifact_id: sidecar_path,
            },
        )
        verify_prepared_pack_set(
            manifest,
            prepared,
            verifier=verifier,
            pack_content_verifier=pack_content_verifier,
        )
    return pack_set


def _contained_file(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise PackInstallError("Capability Pack install path is unsafe")
    current = root
    for part in relative.parts:
        current = current / part
        _reject_link(current)
    try:
        current.absolute().relative_to(root.absolute())
    except ValueError:
        raise PackInstallError("Capability Pack install path escaped the slot") from None
    if not current.is_file():
        raise PackInstallError("Capability Pack install file is missing")
    return current


def _transfer_stable(source: Path, destination: Path) -> None:
    """Move one transaction-owned verified Pack into its atomic candidate slot."""

    _reject_link(source)
    try:
        before = source.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= MAX_ARTIFACT_BYTES
            or before.st_nlink != 1
        ):
            raise OSError
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            raise OSError
        if os.name == "nt":
            # MoveFile preserves the source DACL on a same-volume move.  Pack
            # files originate outside the pre-provisioned payload tree, so a
            # rename would bypass its inheritable Package-SID read grant.
            # Creating the destination in place makes Windows inherit the
            # payload DACL while the identity checks keep the verified source
            # stable for the duration of the bounded copy.
            with source.open("rb") as source_stream:
                opened = os.fstat(source_stream.fileno())
                with destination.open("xb") as destination_stream:
                    shutil.copyfileobj(
                        source_stream,
                        destination_stream,
                        length=1024 * 1024,
                    )
                    destination_stream.flush()
                    os.fsync(destination_stream.fileno())
                    copied = os.fstat(destination_stream.fileno())
                finished = os.fstat(source_stream.fileno())
            current = source.lstat()
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            if (
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != identity
                or (
                    finished.st_dev,
                    finished.st_ino,
                    finished.st_size,
                    finished.st_mtime_ns,
                )
                != identity
                or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                )
                != identity
                or not stat.S_ISREG(copied.st_mode)
                or copied.st_size != before.st_size
                or copied.st_nlink != 1
            ):
                raise OSError
            source.unlink()
            return
        os.replace(source, destination)
        _reject_link(destination)
        after = destination.lstat()
    except OSError:
        if os.name == "nt":
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise PackInstallError("Capability Pack file transfer failed") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != identity
        or after.st_nlink != 1
    ):
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise PackInstallError("Capability Pack file changed during transfer")
    if os.name != "nt":
        destination.chmod(0o600)


def _stable_bytes(path: Path, maximum: int) -> bytes:
    _reject_link(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
            raise OSError
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
    except OSError:
        raise PackInstallError("Capability Pack file is unreadable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity
        or len(payload) != before.st_size
    ):
        raise PackInstallError("Capability Pack file changed during verification")
    return payload


def _read_json(path: Path, *, maximum: int) -> Mapping[str, Any]:
    try:
        value = json.loads(_stable_bytes(path, maximum).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PackInstallError("Capability Pack JSON is invalid") from None
    if not isinstance(value, Mapping):
        raise PackInstallError("Capability Pack JSON must contain an object")
    return value


def _real_directory(path: Path) -> Path:
    _reject_link(path)
    try:
        absolute = Path(os.path.abspath(path))
        if not absolute.is_dir():
            raise OSError
    except OSError:
        raise PackInstallError("Capability Pack directory is invalid") from None
    return absolute


def _reject_link(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise PackInstallError("Capability Pack path is missing") from None
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise PackInstallError("Capability Pack path cannot be a link")


def _unlink_regular(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _reject_link(path)
    if not path.is_file():
        raise PackInstallError("Capability Pack partial download is not a file")
    path.unlink()


def _fsync_tree(root: Path) -> None:
    if os.name == "nt":
        return
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "IncompletePackSet",
    "PackArtifactPair",
    "PackDownloadFailed",
    "PackInstallError",
    "PackContentVerifier",
    "PackSetDownloader",
    "PreparedPackSet",
    "REQUIRED_PACK_IDS",
    "ReleasePackSet",
    "install_prepared_pack_set",
    "resolve_release_pack_set",
    "validate_installed_pack_set",
    "verify_prepared_pack_set",
]

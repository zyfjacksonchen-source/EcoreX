"""Attested encrypted-volume CAS for an explicit single-host deployment.

This backend is a formal alternative to S3 only when every API and worker
process runs on the same machine and ``replica_count`` is exactly one.  It
does not claim multi-host availability.  Encryption is supplied by the
mounted volume and is accepted only through a digest-fenced deployment
attestation plus live mount and machine-identity probes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Protocol, runtime_checkable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_RECORD_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ATTEMPT = re.compile(r"^\.[A-Za-z0-9._-]{1,255}\.(?:tmp|new)$")
_MAX_ATTESTATION_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 256 * 1024
_MAX_QUOTA_BYTES = 8 * 1024**4
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class AttestedLocalCASError(RuntimeError):
    """Stable failure raised at the single-host storage trust boundary."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code):
            code = "attested_local_cas_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MountObservation:
    mount_root: Path
    device_id: int
    is_mount: bool


@runtime_checkable
class CASFileSecurity(Protocol):
    def validate_directory(self, path: Path) -> None: ...

    def prepare_directory(self, path: Path) -> None: ...

    def validate_file(self, path: Path, *, allow_multiple_links: bool = False) -> os.stat_result: ...

    def prepare_file(self, descriptor: int) -> None: ...


class PosixGroupCASFileSecurity:
    """Group-shared POSIX permissions for separate API and worker accounts."""

    def __init__(self, *, owner_gid: int, directory_mode: int = 0o2770, file_mode: int = 0o660) -> None:
        if os.name == "nt" or not hasattr(os, "geteuid"):
            raise AttestedLocalCASError("attested_local_cas_posix_required")
        if (
            isinstance(owner_gid, bool)
            or not isinstance(owner_gid, int)
            or owner_gid < 0
            or directory_mode not in {0o2700, 0o2750, 0o2770}
            or file_mode not in {0o600, 0o640, 0o660}
            or (directory_mode & 0o002)
            or (file_mode & 0o002)
        ):
            raise AttestedLocalCASError("attested_local_cas_permission_config_invalid")
        groups = set(os.getgroups()) | {os.getegid()}
        if owner_gid not in groups:
            raise AttestedLocalCASError("attested_local_cas_process_group_invalid")
        self.owner_gid = owner_gid
        self.directory_mode = directory_mode
        self.file_mode = file_mode

    def validate_directory(self, path: Path) -> None:
        metadata = _lstat_directory(path)
        if (
            metadata.st_gid != self.owner_gid
            or stat.S_IMODE(metadata.st_mode) != self.directory_mode
        ):
            raise AttestedLocalCASError("attested_local_cas_directory_permission_invalid")

    def prepare_directory(self, path: Path) -> None:
        try:
            os.chmod(path, self.directory_mode)
        except OSError:
            raise AttestedLocalCASError("attested_local_cas_directory_permission_invalid") from None
        self.validate_directory(path)

    def validate_file(self, path: Path, *, allow_multiple_links: bool = False) -> os.stat_result:
        metadata = _lstat_regular(path)
        if (
            metadata.st_gid != self.owner_gid
            or stat.S_IMODE(metadata.st_mode) != self.file_mode
            or (not allow_multiple_links and metadata.st_nlink != 1)
        ):
            raise AttestedLocalCASError("attested_local_cas_file_permission_invalid")
        return metadata

    def prepare_file(self, descriptor: int) -> None:
        try:
            os.fchmod(descriptor, self.file_mode)
        except OSError:
            raise AttestedLocalCASError("attested_local_cas_file_permission_invalid") from None


@dataclass(frozen=True, slots=True)
class EncryptedVolumeAttestation:
    provider: str
    volume_id: str
    mount_root: Path
    evidence_reference: str
    evidence_sha256: str
    attestation_sha256: str


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    size_bytes: int


@dataclass(slots=True)
class VerifiedBlobRead:
    blob: StoredBlob
    handle: BinaryIO
    _closed: bool = False

    def read(self, amount: int = -1) -> bytes:
        if self._closed:
            raise AttestedLocalCASError("attested_local_cas_read_closed")
        try:
            return self.handle.read(amount)
        except OSError:
            raise AttestedLocalCASError("attested_local_cas_read_failed") from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.handle.close()
            except OSError:
                pass

    def __enter__(self) -> "VerifiedBlobRead":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class StoredRecord:
    payload: bytes
    version: str


class AttestedEncryptedLocalVolume:
    """Validate one encrypted mount and install one immutable CAS marker."""

    MARKER = ".ecorex-attested-local-cas.json"

    def __init__(
        self,
        *,
        cas_root: Path,
        attestation_path: Path,
        expected_attestation_sha256: str,
        expected_volume_id: str,
        expected_machine_id_sha256: str,
        replica_count: int,
        quota_bytes: int,
        minimum_free_bytes: int,
        security: CASFileSecurity | None = None,
        mount_probe: Callable[[Path], MountObservation] | None = None,
        machine_id_reader: Callable[[], bytes] | None = None,
        owner_gid: int | None = None,
    ) -> None:
        if replica_count != 1:
            raise AttestedLocalCASError("attested_local_cas_replica_count_invalid")
        if (
            _SHA256.fullmatch(str(expected_attestation_sha256)) is None
            or _SHA256.fullmatch(str(expected_machine_id_sha256)) is None
            or _SAFE_ID.fullmatch(str(expected_volume_id)) is None
            or isinstance(quota_bytes, bool)
            or not 1024 * 1024 <= quota_bytes <= _MAX_QUOTA_BYTES
            or isinstance(minimum_free_bytes, bool)
            or not 1024 * 1024 <= minimum_free_bytes <= quota_bytes
        ):
            raise AttestedLocalCASError("attested_local_cas_config_invalid")
        self.security = security or PosixGroupCASFileSecurity(
            owner_gid=owner_gid if owner_gid is not None else -1
        )
        self._mount_probe = mount_probe or _default_mount_probe
        self._machine_id_reader = machine_id_reader or _default_machine_id
        self.cas_root = _absolute(cas_root)
        self.attestation_path = _absolute(attestation_path)
        self.expected_attestation_sha256 = expected_attestation_sha256
        self.expected_machine_id_sha256 = expected_machine_id_sha256
        self.expected_volume_id = expected_volume_id
        self.replica_count = replica_count
        self.quota_bytes = quota_bytes
        self.minimum_free_bytes = minimum_free_bytes
        self.attestation = self._load_attestation()
        self.mount_root = self.attestation.mount_root
        if not _strictly_beneath(self.cas_root, self.mount_root):
            raise AttestedLocalCASError("attested_local_cas_root_outside_mount")
        self.validate()
        self._install_or_validate_marker()

    @property
    def attestation_sha256(self) -> str:
        return self.attestation.attestation_sha256

    @property
    def lock_path(self) -> Path:
        return self.cas_root / ".ecorex-attested-local-cas.lock"

    def validate(self) -> MountObservation:
        try:
            machine = self._machine_id_reader()
        except Exception:
            raise AttestedLocalCASError("attested_local_cas_machine_identity_unavailable") from None
        if (
            not isinstance(machine, bytes)
            or not machine
            or hashlib.sha256(machine.strip()).hexdigest()
            != self.expected_machine_id_sha256
        ):
            raise AttestedLocalCASError("attested_local_cas_machine_identity_mismatch")
        mount_metadata = _lstat_directory(self.mount_root)
        if os.name != "nt" and stat.S_IMODE(mount_metadata.st_mode) & 0o002:
            raise AttestedLocalCASError("attested_local_cas_mount_permission_invalid")
        self.security.validate_directory(self.cas_root)
        observation = self._mount_probe(self.mount_root)
        if (
            not isinstance(observation, MountObservation)
            or not observation.is_mount
            or _absolute(observation.mount_root) != self.mount_root
            or observation.device_id != self.mount_root.stat().st_dev
            or self.cas_root.stat().st_dev != observation.device_id
        ):
            raise AttestedLocalCASError("attested_local_cas_mount_identity_mismatch")
        if self._load_attestation().attestation_sha256 != self.attestation_sha256:
            raise AttestedLocalCASError("attested_local_cas_attestation_changed")
        return observation

    def marker_identity(self) -> Mapping[str, object]:
        return {
            "schema_version": 1,
            "backend": "attested-encrypted-local-cas",
            "availability_scope": "single-host",
            "multi_host_ha": False,
            "replica_count": 1,
            "volume_id": self.expected_volume_id,
            "mount_root": str(self.mount_root),
            "cas_root": str(self.cas_root),
            "attestation_sha256": self.attestation_sha256,
            "machine_id_sha256": self.expected_machine_id_sha256,
            "quota_bytes": self.quota_bytes,
            "minimum_free_bytes": self.minimum_free_bytes,
        }

    def _load_attestation(self) -> EncryptedVolumeAttestation:
        payload = _read_bounded_file(
            self.attestation_path,
            _MAX_ATTESTATION_BYTES,
            "attested_local_cas_attestation_invalid",
        )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != self.expected_attestation_sha256:
            raise AttestedLocalCASError("attested_local_cas_attestation_digest_mismatch")
        value = _strict_json(payload, "attested_local_cas_attestation_invalid")
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "provider",
            "volume_id",
            "mount_root",
            "encrypted",
            "evidence_reference",
            "evidence_sha256",
        }:
            raise AttestedLocalCASError("attested_local_cas_attestation_invalid")
        provider = value.get("provider")
        volume_id = value.get("volume_id")
        mount_root = value.get("mount_root")
        evidence = value.get("evidence_reference")
        evidence_sha = value.get("evidence_sha256")
        if (
            value.get("schema_version") != 1
            or provider not in {"luks2", "alibaba-cloud-kms"}
            or value.get("encrypted") is not True
            or volume_id != self.expected_volume_id
            or not isinstance(mount_root, str)
            or not Path(mount_root).is_absolute()
            or not isinstance(evidence, str)
            or not 3 <= len(evidence) <= 512
            or _SHA256.fullmatch(str(evidence_sha)) is None
        ):
            raise AttestedLocalCASError("attested_local_cas_attestation_invalid")
        return EncryptedVolumeAttestation(
            provider=str(provider),
            volume_id=str(volume_id),
            mount_root=_absolute(Path(mount_root)),
            evidence_reference=evidence,
            evidence_sha256=str(evidence_sha),
            attestation_sha256=digest,
        )

    def _install_or_validate_marker(self) -> None:
        expected = _canonical_json(self.marker_identity())
        marker = self.cas_root / self.MARKER
        with self.locked():
            if os.path.lexists(marker):
                actual = _read_bounded_file(
                    marker, 64 * 1024, "attested_local_cas_marker_invalid"
                )
                if actual != expected:
                    raise AttestedLocalCASError("attested_local_cas_marker_conflict")
                return
            _atomic_create(marker, expected, self.security)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.validate()
        path = self.lock_path
        key = str(path).casefold() if os.name == "nt" else str(path)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
            descriptor: int | None = None
            try:
                descriptor = os.open(path, flags, 0o600)
                self.security.prepare_file(descriptor)
                metadata = os.fstat(descriptor)
                if metadata.st_size < 1:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                self.security.validate_file(path)
                yield
            except AttestedLocalCASError:
                raise
            except OSError:
                raise AttestedLocalCASError("attested_local_cas_lock_failed") from None
            finally:
                if descriptor is not None:
                    try:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(descriptor)


class AttestedEncryptedLocalCAS:
    """One namespace in the volume-wide, quota-fenced local CAS."""

    deployment_scope = "shared"
    availability_scope = "single-host"
    replica_count = 1
    supports_multi_host_ha = False

    def __init__(
        self,
        volume: AttestedEncryptedLocalVolume,
        *,
        namespace: str,
        max_blob_bytes: int,
    ) -> None:
        if not isinstance(volume, AttestedEncryptedLocalVolume):
            raise TypeError("attested local CAS volume is invalid")
        if (
            _NAMESPACE.fullmatch(str(namespace)) is None
            or isinstance(max_blob_bytes, bool)
            or not 1 <= max_blob_bytes <= 512 * 1024 * 1024
        ):
            raise AttestedLocalCASError("attested_local_cas_namespace_invalid")
        self.volume = volume
        self.namespace = namespace
        self.max_blob_bytes = max_blob_bytes
        self.root = volume.cas_root / "namespaces" / namespace
        self.blob_root = self.root / "blobs" / "sha256"
        self.record_root = self.root / "records"
        with self.volume.locked():
            self._cleanup_temporary_files_locked()
            for path in (
                self.volume.cas_root / "namespaces",
                self.root,
                self.root / "blobs",
                self.blob_root,
                self.record_root,
            ):
                _ensure_directory(path, self.volume.security)

    @property
    def attestation_sha256(self) -> str:
        return self.volume.attestation_sha256

    def put(self, payload: bytes, *, expected_sha256: str | None = None) -> StoredBlob:
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= self.max_blob_bytes:
            raise AttestedLocalCASError("attested_local_cas_blob_invalid")
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and (
            _SHA256.fullmatch(str(expected_sha256)) is None
            or not secrets.compare_digest(digest, expected_sha256)
        ):
            raise AttestedLocalCASError("attested_local_cas_blob_digest_mismatch")
        target = self._blob_path(digest)
        with self.volume.locked():
            self._cleanup_temporary_files_locked()
            _ensure_descendants(self.blob_root, target.parent, self.volume.security)
            if os.path.lexists(target):
                self._verify_blob_locked(target, digest, len(payload))
                return StoredBlob(digest, len(payload))
            self._require_capacity_locked(len(payload))
            temporary = target.parent / f".cas-{secrets.token_hex(12)}.new"
            _write_temporary(temporary, payload, self.volume.security)
            try:
                try:
                    os.link(temporary, target)
                    _fsync_directory(target.parent)
                except FileExistsError:
                    pass
                self._verify_blob_locked(target, digest, len(payload), allow_multiple_links=True)
            finally:
                try:
                    temporary.unlink()
                    _fsync_directory(target.parent)
                except FileNotFoundError:
                    pass
            self._verify_blob_locked(target, digest, len(payload))
        return StoredBlob(digest, len(payload))

    def open_verified(self, sha256: str, *, expected_size: int | None = None) -> VerifiedBlobRead:
        _digest(sha256)
        path = self._blob_path(sha256)
        handle: BinaryIO | None = None
        descriptor: int | None = None
        try:
            before = self.volume.security.validate_file(path)
            if before.st_size > self.max_blob_bytes or (
                expected_size is not None and before.st_size != expected_size
            ):
                raise AttestedLocalCASError("attested_local_cas_blob_size_mismatch")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            handle = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            opened = os.fstat(handle.fileno())
            if _file_identity(before) != _file_identity(opened):
                raise AttestedLocalCASError("attested_local_cas_blob_identity_changed")
            digest = hashlib.sha256()
            observed = 0
            while chunk := handle.read(1024 * 1024):
                observed += len(chunk)
                if observed > self.max_blob_bytes:
                    raise AttestedLocalCASError("attested_local_cas_blob_size_mismatch")
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            current = self.volume.security.validate_file(path)
            if (
                _file_identity(after) != _file_identity(opened)
                or _file_identity(current) != _file_identity(opened)
                or observed != before.st_size
                or digest.hexdigest() != sha256
            ):
                raise AttestedLocalCASError("attested_local_cas_blob_integrity_failed")
            handle.seek(0)
            return VerifiedBlobRead(StoredBlob(sha256, observed), handle)
        except AttestedLocalCASError:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise AttestedLocalCASError("attested_local_cas_blob_unavailable") from None

    def read(self, sha256: str) -> bytes:
        with self.open_verified(sha256) as verified:
            payload = verified.read(self.max_blob_bytes + 1)
        if len(payload) != verified.blob.size_bytes:
            raise AttestedLocalCASError("attested_local_cas_blob_size_mismatch")
        return payload

    def delete(self, sha256: str, *, expected_size: int | None = None) -> bool:
        _digest(sha256)
        target = self._blob_path(sha256)
        with self.volume.locked():
            if not os.path.lexists(target):
                return False
            metadata = self._verify_blob_locked(target, sha256, expected_size)
            current = self.volume.security.validate_file(target)
            if _file_identity(metadata) != _file_identity(current):
                raise AttestedLocalCASError("attested_local_cas_blob_identity_changed")
            try:
                target.unlink()
                _fsync_directory(target.parent)
            except FileNotFoundError:
                return False
            except OSError:
                raise AttestedLocalCASError("attested_local_cas_blob_delete_failed") from None
            return True

    def read_record(self, kind: str, sha256: str) -> StoredRecord:
        path = self._record_path(kind, sha256)
        # Serialize reads with record replacement.  POSIX readers could safely
        # retain the previous inode, but Windows may deny or briefly hide the
        # destination while rename-over completes.  The shared lock also gives
        # callers one portable, linearizable record contract.
        with self.volume.locked():
            try:
                payload = _read_bounded_file(
                    path, _MAX_RECORD_BYTES, "attested_local_cas_record_unavailable"
                )
            except AttestedLocalCASError:
                raise AttestedLocalCASError(
                    "attested_local_cas_record_unavailable"
                ) from None
        return StoredRecord(payload, hashlib.sha256(payload).hexdigest())

    def compare_exchange_record(
        self,
        kind: str,
        sha256: str,
        payload: bytes,
        *,
        expected_version: str | None,
    ) -> StoredRecord:
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_RECORD_BYTES:
            raise AttestedLocalCASError("attested_local_cas_record_invalid")
        path = self._record_path(kind, sha256)
        with self.volume.locked():
            _ensure_descendants(self.record_root, path.parent, self.volume.security)
            exists = os.path.lexists(path)
            if expected_version is None:
                if exists:
                    raise AttestedLocalCASError("attested_local_cas_record_conflict")
            else:
                if _SHA256.fullmatch(str(expected_version)) is None or not exists:
                    raise AttestedLocalCASError("attested_local_cas_record_conflict")
                current = _read_bounded_file(
                    path, _MAX_RECORD_BYTES, "attested_local_cas_record_invalid"
                )
                if not secrets.compare_digest(
                    hashlib.sha256(current).hexdigest(), expected_version
                ):
                    raise AttestedLocalCASError("attested_local_cas_record_conflict")
            delta = len(payload)
            if exists:
                delta -= path.stat().st_size
            if delta > 0:
                self._require_capacity_locked(delta)
            if expected_version is None:
                _atomic_create(path, payload, self.volume.security)
            else:
                _atomic_replace(path, payload, self.volume.security)
            return StoredRecord(payload, hashlib.sha256(payload).hexdigest())

    def delete_record(self, kind: str, sha256: str, *, expected_version: str) -> bool:
        path = self._record_path(kind, sha256)
        if _SHA256.fullmatch(str(expected_version)) is None:
            raise AttestedLocalCASError("attested_local_cas_record_conflict")
        with self.volume.locked():
            if not os.path.lexists(path):
                return False
            current = _read_bounded_file(
                path, _MAX_RECORD_BYTES, "attested_local_cas_record_invalid"
            )
            if not secrets.compare_digest(hashlib.sha256(current).hexdigest(), expected_version):
                return False
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except FileNotFoundError:
                return False
            except OSError:
                raise AttestedLocalCASError("attested_local_cas_record_delete_failed") from None
            return True

    def list_blob_digests(self) -> tuple[str, ...]:
        with self.volume.locked():
            values: list[str] = []
            for path in self.blob_root.glob("*/*/*"):
                if not path.is_file() or _SHA256.fullmatch(path.name) is None:
                    raise AttestedLocalCASError("attested_local_cas_layout_invalid")
                self.volume.security.validate_file(path)
                values.append(path.name)
            return tuple(sorted(values))

    def health_probe(self, *, write_probe: bool, deep: bool = False) -> Mapping[str, object]:
        observation = self.volume.validate()
        probe_digest: str | None = None
        if write_probe:
            payload = b"ecorex-local-cas-health-v1\0" + secrets.token_bytes(32)
            stored = self.put(payload)
            probe_digest = stored.sha256
            if self.read(stored.sha256) != payload or not self.delete(
                stored.sha256, expected_size=len(payload)
            ):
                raise AttestedLocalCASError("attested_local_cas_health_probe_failed")
        with self.volume.locked():
            self._cleanup_temporary_files_locked()
            used, blobs, records = self._usage_locked(deep=deep)
            free = shutil.disk_usage(self.volume.cas_root).free
        if free < self.volume.minimum_free_bytes:
            raise AttestedLocalCASError("attested_local_cas_free_space_insufficient")
        return {
            "schema_version": 1,
            "status": "passed",
            "backend": "attested-encrypted-local-cas",
            "availability_scope": "single-host",
            "multi_host_ha": False,
            "replica_count": 1,
            "namespace": self.namespace,
            "volume_id": self.volume.expected_volume_id,
            "device_id": str(observation.device_id),
            "attestation_sha256": self.volume.attestation_sha256,
            "encryption_evidence_sha256": self.volume.attestation.evidence_sha256,
            "quota_bytes": self.volume.quota_bytes,
            "used_bytes": used,
            "free_bytes": free,
            "blob_count": blobs,
            "record_count": records,
            "write_probe_sha256": probe_digest,
        }

    def _blob_path(self, sha256: str) -> Path:
        _digest(sha256)
        return self.blob_root / sha256[:2] / sha256[2:4] / sha256

    def _record_path(self, kind: str, sha256: str) -> Path:
        if _RECORD_KIND.fullmatch(str(kind)) is None:
            raise AttestedLocalCASError("attested_local_cas_record_kind_invalid")
        _digest(sha256)
        return self.record_root / kind / sha256[:2] / sha256[2:4] / f"{sha256}.json"

    def _verify_blob_locked(
        self,
        path: Path,
        sha256: str,
        expected_size: int | None,
        *,
        allow_multiple_links: bool = False,
    ) -> os.stat_result:
        metadata = self.volume.security.validate_file(
            path, allow_multiple_links=allow_multiple_links
        )
        if metadata.st_size > self.max_blob_bytes or (
            expected_size is not None and metadata.st_size != expected_size
        ):
            raise AttestedLocalCASError("attested_local_cas_blob_size_mismatch")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
        except OSError:
            raise AttestedLocalCASError("attested_local_cas_blob_unavailable") from None
        if (
            _file_identity(opened) != _file_identity(metadata)
            or _file_identity(after) != _file_identity(metadata)
            or digest.hexdigest() != sha256
        ):
            raise AttestedLocalCASError("attested_local_cas_blob_integrity_failed")
        return metadata

    def _require_capacity_locked(self, additional: int) -> None:
        used, _blobs, _records = self._usage_locked(deep=False)
        free = shutil.disk_usage(self.volume.cas_root).free
        if (
            additional < 0
            or used + additional > self.volume.quota_bytes
            or free - additional < self.volume.minimum_free_bytes
        ):
            raise AttestedLocalCASError("attested_local_cas_quota_exceeded")

    def _usage_locked(self, *, deep: bool) -> tuple[int, int, int]:
        namespaces = self.volume.cas_root / "namespaces"
        used = 0
        blobs = 0
        records = 0
        if not namespaces.exists():
            return 0, 0, 0
        for current, directories, files in os.walk(namespaces, followlinks=False):
            root = Path(current)
            self.volume.security.validate_directory(root)
            for directory in directories:
                self.volume.security.validate_directory(root / directory)
            for name in files:
                path = root / name
                metadata = self.volume.security.validate_file(path)
                used += metadata.st_size
                if _SHA256.fullmatch(name):
                    blobs += 1
                    if deep:
                        self._verify_blob_locked(path, name, None)
                elif name.endswith(".json") and _SHA256.fullmatch(name[:-5]):
                    records += 1
                    if metadata.st_size > _MAX_RECORD_BYTES:
                        raise AttestedLocalCASError("attested_local_cas_record_invalid")
                else:
                    raise AttestedLocalCASError("attested_local_cas_layout_invalid")
        return used, blobs, records

    def _cleanup_temporary_files_locked(self) -> None:
        namespaces = self.volume.cas_root / "namespaces"
        if not namespaces.exists():
            return
        for current, directories, files in os.walk(namespaces, followlinks=False):
            root = Path(current)
            self.volume.security.validate_directory(root)
            for directory in directories:
                self.volume.security.validate_directory(root / directory)
            for name in files:
                if not _ATTEMPT.fullmatch(name):
                    continue
                path = root / name
                self.volume.security.validate_file(path, allow_multiple_links=True)
                try:
                    path.unlink()
                    _fsync_directory(root)
                except OSError:
                    raise AttestedLocalCASError(
                        "attested_local_cas_recovery_cleanup_failed"
                    ) from None


def _default_mount_probe(mount_root: Path) -> MountObservation:
    if os.name == "nt":
        raise AttestedLocalCASError("attested_local_cas_posix_mount_required")
    try:
        metadata = mount_root.lstat()
        return MountObservation(mount_root, metadata.st_dev, os.path.ismount(mount_root))
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_mount_probe_failed") from None


def _default_machine_id() -> bytes:
    return _read_bounded_file(
        Path("/etc/machine-id"), 4096, "attested_local_cas_machine_identity_unavailable"
    )


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path.expanduser())))
    except (OSError, TypeError):
        raise AttestedLocalCASError("attested_local_cas_path_invalid") from None


def _strictly_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _digest(value: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AttestedLocalCASError("attested_local_cas_digest_invalid")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise AttestedLocalCASError("attested_local_cas_json_invalid") from None


def _strict_json(payload: bytes, code: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(code)
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError(code)),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise AttestedLocalCASError(code) from None


def _read_bounded_file(path: Path, maximum: int, code: str) -> bytes:
    try:
        _reject_link_components(path)
        before = _lstat_regular(path)
        if not 1 <= before.st_size <= maximum:
            raise AttestedLocalCASError(code)
        chunks: list[bytes] = []
        total = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise AttestedLocalCASError(code)
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise AttestedLocalCASError(code)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
        current = _lstat_regular(path)
        if (
            total != before.st_size
            or _file_identity(after) != _file_identity(before)
            or _file_identity(current) != _file_identity(before)
        ):
            raise AttestedLocalCASError(code)
        return b"".join(chunks)
    except AttestedLocalCASError:
        raise
    except OSError:
        raise AttestedLocalCASError(code) from None


def _ensure_directory(path: Path, security: CASFileSecurity) -> None:
    if os.path.lexists(path):
        try:
            security.validate_directory(path)
        except AttestedLocalCASError as error:
            # mkdir and the following setgid chmod cannot be one syscall. A
            # crash, kill or restrictive transient sandbox can therefore
            # leave the exact empty umask-reduced directory behind. Repair
            # only that narrow identity and shape; a non-empty, differently
            # owned or otherwise malformed directory remains fail-closed.
            if (
                error.code != "attested_local_cas_directory_permission_invalid"
                or not isinstance(security, PosixGroupCASFileSecurity)
            ):
                raise
            metadata = _lstat_directory(path)
            reduced_mode = security.directory_mode & ~0o070
            try:
                empty = next(path.iterdir(), None) is None
            except OSError:
                raise error from None
            if (
                metadata.st_uid != os.geteuid()
                or metadata.st_gid != security.owner_gid
                or stat.S_IMODE(metadata.st_mode) != reduced_mode
                or not empty
            ):
                raise error
            security.prepare_directory(path)
            _fsync_directory(path.parent)
        return
    try:
        path.mkdir()
        security.prepare_directory(path)
        _fsync_directory(path.parent)
    except FileExistsError:
        security.validate_directory(path)
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_directory_create_failed") from None


def _ensure_descendants(base: Path, target: Path, security: CASFileSecurity) -> None:
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise AttestedLocalCASError("attested_local_cas_directory_outside_namespace") from None
    current = base
    security.validate_directory(current)
    for part in relative.parts:
        current /= part
        _ensure_directory(current, security)


def _write_temporary(path: Path, payload: bytes, security: CASFileSecurity) -> None:
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(path, flags, 0o600)
        security.prepare_file(descriptor)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        security.validate_file(path)
    except AttestedLocalCASError:
        raise
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_create(path: Path, payload: bytes, security: CASFileSecurity) -> None:
    temporary = path.parent / f".create-{secrets.token_hex(12)}.new"
    _write_temporary(temporary, payload, security)
    try:
        try:
            os.link(temporary, path)
            _fsync_directory(path.parent)
        except FileExistsError:
            raise AttestedLocalCASError("attested_local_cas_create_conflict") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    security.validate_file(path)


def _atomic_replace(path: Path, payload: bytes, security: CASFileSecurity) -> None:
    temporary = path.parent / f".replace-{secrets.token_hex(12)}.tmp"
    _write_temporary(temporary, payload, security)
    try:
        # POSIX rename-over is immediate even when a reader has the previous
        # inode open.  Windows can briefly reject the same atomic operation
        # while a verification reader is closing its handle.  Preserve the
        # atomic replace primitive and retry only that transient boundary.
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(min(0.0005 * (2 ** min(attempt, 6)), 0.02))
        _fsync_directory(path.parent)
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_replace_failed") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    security.validate_file(path)


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _linked(current.lstat()):
            raise AttestedLocalCASError("attested_local_cas_link_forbidden")


def _lstat_directory(path: Path) -> os.stat_result:
    try:
        _reject_link_components(path)
        value = path.lstat()
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_directory_unavailable") from None
    if not stat.S_ISDIR(value.st_mode) or _linked(value):
        raise AttestedLocalCASError("attested_local_cas_directory_unsafe")
    return value


def _lstat_regular(path: Path) -> os.stat_result:
    try:
        _reject_link_components(path)
        value = path.lstat()
    except OSError:
        raise AttestedLocalCASError("attested_local_cas_file_unavailable") from None
    if not stat.S_ISREG(value.st_mode) or _linked(value):
        raise AttestedLocalCASError("attested_local_cas_file_unsafe")
    return value


def _linked(value: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "AttestedEncryptedLocalCAS",
    "AttestedEncryptedLocalVolume",
    "AttestedLocalCASError",
    "CASFileSecurity",
    "EncryptedVolumeAttestation",
    "MountObservation",
    "PosixGroupCASFileSecurity",
    "StoredBlob",
    "StoredRecord",
    "VerifiedBlobRead",
]

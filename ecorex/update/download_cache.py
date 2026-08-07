"""Verified content-addressed cache for signed release downloads.

The transaction download remains the resumable network journal.  This cache is
the cross-transaction optimization boundary: bytes enter ``objects`` only
after manifest, artifact signature, size and SHA-256 verification.  Cache hits
are copied (never hard-linked) into the writable transaction and verified
again before the caller can consume them.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import time
from typing import Iterable

from .locking import LockUnavailable, ProductFileLock
from .manifest import ReleaseArtifact, ReleaseManifest
from .verification import (
    ContentVerificationError,
    SignatureVerifier,
    sha256_file,
    verify_artifact_file,
    verify_artifact_signature,
    verify_manifest_signature,
)


DEFAULT_DOWNLOAD_CACHE_MAX_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS = 7 * 24 * 60 * 60
_DIGEST_LENGTH = 64


class DownloadCacheError(RuntimeError):
    """The verified download cache could not preserve its trust boundary."""


@dataclass(frozen=True, slots=True)
class DownloadCacheCollection:
    retained_objects: int
    retained_bytes: int
    removed_objects: int
    removed_bytes: int
    quarantined_objects: int


class VerifiedDownloadCache:
    """Cross-process, digest-keyed cache for already verified release bytes."""

    def __init__(
        self,
        root: Path | str | os.PathLike[str],
        *,
        verifier: SignatureVerifier,
        max_bytes: int = DEFAULT_DOWNLOAD_CACHE_MAX_BYTES,
        max_age_seconds: float = DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS,
        quarantine_age_seconds: float = DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS,
        lock_timeout: float | None = None,
        create_storage: bool = True,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("download cache max_bytes must be a positive integer")
        if not 60 <= max_age_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("download cache max age is outside the supported range")
        if not 60 <= quarantine_age_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("download cache quarantine age is outside the supported range")
        self.root = Path(root)
        self.verifier = verifier
        self.max_bytes = max_bytes
        self.max_age_seconds = float(max_age_seconds)
        self.quarantine_age_seconds = float(quarantine_age_seconds)
        self.lock_timeout = lock_timeout
        self.objects = self.root / "objects"
        self.incoming = self.root / "incoming"
        self.quarantine = self.root / "quarantine"
        self.locks = self.root / "locks"
        self._converged = False
        if create_storage:
            self.converge_startup()

    def converge_startup(self) -> None:
        if self._converged:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        _require_real_directory(self.root, "download cache root")
        for path, label in (
            (self.objects, "download cache object root"),
            (self.incoming, "download cache incoming root"),
            (self.quarantine, "download cache quarantine root"),
            (self.locks, "download cache lock root"),
        ):
            path.mkdir(exist_ok=True)
            _require_real_directory(path, label)
        self._converged = True

    def acquire(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> "VerifiedDownloadLease":
        self.converge_startup()
        verify_manifest_signature(manifest, self.verifier)
        verify_artifact_signature(manifest, artifact, self.verifier)
        return VerifiedDownloadLease(self, manifest, artifact)

    def collect(
        self,
        *,
        keep_digests: Iterable[str] = (),
        now: float | None = None,
    ) -> DownloadCacheCollection:
        """Age/capacity GC with per-object exclusion and corruption isolation."""

        self.converge_startup()
        instant = time.time() if now is None else float(now)
        keep = {_normalized_digest(value) for value in keep_digests}
        removed_objects = 0
        removed_bytes = 0
        quarantined_objects = 0
        with ProductFileLock(
            self.locks / "gc.lock",
            timeout=self.lock_timeout,
        ):
            candidates: list[tuple[float, int, str, Path]] = []
            for path in _object_paths(self.objects):
                digest = path.name.casefold()
                try:
                    digest = _normalized_digest(digest)
                    with self._lock_for(digest, timeout=0.0):
                        metadata = _require_regular(path, "download cache object")
                        if sha256_file(path) != digest:
                            raise ContentVerificationError(
                                "download cache object does not match its digest key"
                            )
                except LockUnavailable:
                    # A fetch/materialization owns this digest. It will either
                    # publish verified bytes or remove its temporary file.
                    continue
                except Exception:
                    try:
                        normalized = _normalized_digest(digest)
                    except ValueError:
                        if self._quarantine_path(path, None):
                            quarantined_objects += 1
                    else:
                        try:
                            with self._lock_for(normalized, timeout=0.0):
                                if self._quarantine_path(path, normalized):
                                    quarantined_objects += 1
                        except LockUnavailable:
                            pass
                    continue
                candidates.append((metadata.st_mtime, metadata.st_size, digest, path))

            retained: list[tuple[float, int, str, Path]] = []
            for item in sorted(candidates, key=lambda value: (value[0], value[2])):
                modified, size, digest, path = item
                expired = instant - modified > self.max_age_seconds
                if digest not in keep and expired and self._remove_if_unlocked(path, digest):
                    removed_objects += 1
                    removed_bytes += size
                else:
                    retained.append(item)

            total = sum(item[1] for item in retained)
            kept_after_capacity: list[tuple[float, int, str, Path]] = []
            for item in retained:
                _modified, size, digest, path = item
                if (
                    total > self.max_bytes
                    and digest not in keep
                    and self._remove_if_unlocked(path, digest)
                ):
                    total -= size
                    removed_objects += 1
                    removed_bytes += size
                else:
                    kept_after_capacity.append(item)

            self._collect_incoming(instant)
            self._collect_quarantine(instant)
            return DownloadCacheCollection(
                retained_objects=len(kept_after_capacity),
                retained_bytes=sum(item[1] for item in kept_after_capacity),
                removed_objects=removed_objects,
                removed_bytes=removed_bytes,
                quarantined_objects=quarantined_objects,
            )

    def _object_path(self, digest: str) -> Path:
        normalized = _normalized_digest(digest)
        shard = self.objects / normalized[:2]
        shard.mkdir(exist_ok=True)
        _require_real_directory(shard, "download cache object shard")
        return shard / normalized

    def _lock_for(self, digest: str, *, timeout: float | None = None) -> ProductFileLock:
        return ProductFileLock(
            self.locks / f"{_normalized_digest(digest)}.lock",
            timeout=self.lock_timeout if timeout is None else timeout,
        )

    def _quarantine_path(self, path: Path, digest: str | None) -> bool:
        if not os.path.lexists(path):
            return False
        label = digest or "unknown"
        destination = self.quarantine / (
            f"{label}.{time.time_ns()}.{secrets.token_hex(6)}.corrupt"
        )
        try:
            os.replace(path, destination)
            os.utime(destination, None)
            _fsync_directory(path.parent)
            _fsync_directory(self.quarantine)
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise DownloadCacheError("corrupt cache object could not be quarantined") from error

    def _remove_if_unlocked(self, path: Path, digest: str) -> bool:
        try:
            with self._lock_for(digest, timeout=0.0):
                _unlink_regular(path)
            return True
        except LockUnavailable:
            return False

    def _collect_incoming(self, instant: float) -> None:
        for path in tuple(self.incoming.iterdir()):
            if not os.path.lexists(path):
                continue
            prefix = path.name.split(".", 1)[0].casefold()
            try:
                digest = _normalized_digest(prefix)
                metadata = _require_regular(path, "download cache incoming file")
            except Exception:
                self._quarantine_path(path, None)
                continue
            if instant - metadata.st_mtime < 60:
                continue
            try:
                with self._lock_for(digest, timeout=0.0):
                    _unlink_regular(path)
            except LockUnavailable:
                continue

    def _collect_quarantine(self, instant: float) -> None:
        for path in tuple(self.quarantine.iterdir()):
            try:
                metadata = _require_regular(path, "download cache quarantine file")
            except Exception as error:
                raise DownloadCacheError("download cache quarantine contains an unsafe entry") from error
            if instant - metadata.st_mtime > self.quarantine_age_seconds:
                _unlink_regular(path)


class VerifiedDownloadLease(AbstractContextManager["VerifiedDownloadLease"]):
    """One digest single-flight spanning cache lookup, fetch and admission."""

    def __init__(
        self,
        cache: VerifiedDownloadCache,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
    ) -> None:
        self.cache = cache
        self.manifest = manifest
        self.artifact = artifact
        self._lock = cache._lock_for(artifact.sha256)
        self._entered = False

    def __enter__(self) -> "VerifiedDownloadLease":
        self._lock.acquire()
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._entered = False
        self._lock.release()

    def materialize(self, destination: Path) -> bool:
        self._require_entered()
        object_path = self.cache._object_path(self.artifact.sha256)
        if not os.path.lexists(object_path):
            return False
        try:
            verify_artifact_file(
                object_path,
                self.manifest,
                self.artifact,
                self.cache.verifier,
            )
        except Exception:
            self.cache._quarantine_path(object_path, self.artifact.sha256)
            return False
        _atomic_verified_copy(
            object_path,
            destination,
            manifest=self.manifest,
            artifact=self.artifact,
            verifier=self.cache.verifier,
        )
        try:
            # The path was verified as a regular non-reparse file while this
            # digest lock is held. Windows does not implement follow_symlinks
            # for utime, so the plain call is safe at this point.
            os.utime(object_path, None)
        except OSError as error:
            raise DownloadCacheError("download cache access time could not be recorded") from error
        return True

    def admit(self, source: Path) -> Path | None:
        """Admit exact verified bytes; never publish a partial/unverified object."""

        self._require_entered()
        verify_manifest_signature(self.manifest, self.cache.verifier)
        verify_artifact_file(
            source,
            self.manifest,
            self.artifact,
            self.cache.verifier,
        )
        if self.artifact.size_bytes > self.cache.max_bytes:
            return None
        object_path = self.cache._object_path(self.artifact.sha256)
        if os.path.lexists(object_path):
            try:
                verify_artifact_file(
                    object_path,
                    self.manifest,
                    self.artifact,
                    self.cache.verifier,
                )
            except Exception:
                self.cache._quarantine_path(object_path, self.artifact.sha256)
            else:
                return object_path
        incoming = self.cache.incoming / (
            f"{self.artifact.sha256}.{secrets.token_hex(12)}.partial"
        )
        _copy_regular_stable(source, incoming)
        published = False
        try:
            verify_artifact_file(
                incoming,
                self.manifest,
                self.artifact,
                self.cache.verifier,
            )
            os.replace(incoming, object_path)
            published = True
            _fsync_directory(self.cache.incoming)
            _fsync_directory(object_path.parent)
            verify_artifact_file(
                object_path,
                self.manifest,
                self.artifact,
                self.cache.verifier,
            )
            return object_path
        except BaseException:
            if published and os.path.lexists(object_path):
                self.cache._quarantine_path(object_path, self.artifact.sha256)
            try:
                _unlink_regular(incoming)
            except (FileNotFoundError, DownloadCacheError):
                pass
            raise

    def _require_entered(self) -> None:
        if not self._entered:
            raise RuntimeError("download cache lease is not active")


def _atomic_verified_copy(
    source: Path,
    destination: Path,
    *,
    manifest: ReleaseManifest,
    artifact: ReleaseArtifact,
    verifier: SignatureVerifier,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _require_real_directory(destination.parent, "download transaction directory")
    temporary = destination.parent / (
        f".{destination.name}.{secrets.token_hex(12)}.cache-copy"
    )
    _copy_regular_stable(source, temporary)
    try:
        verify_artifact_file(temporary, manifest, artifact, verifier)
        if os.path.lexists(destination) and _is_link_or_reparse(destination):
            raise DownloadCacheError("download destination is a link or reparse point")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        verify_artifact_file(destination, manifest, artifact, verifier)
    finally:
        try:
            _unlink_regular(temporary)
        except (FileNotFoundError, DownloadCacheError):
            pass


def _copy_regular_stable(source: Path, destination: Path) -> None:
    before = _require_regular(source, "verified download source")
    if os.path.lexists(destination):
        raise DownloadCacheError("download cache temporary path already exists")
    try:
        cloned = _try_clone_regular(source, destination)
    except OSError:
        raise DownloadCacheError("verified download clone failed") from None
    if cloned:
        after = _require_regular(destination, "cloned verified download")
        current = _require_regular(source, "verified download source")
        if (
            after.st_size != before.st_size
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            or current.st_size != before.st_size
            or current.st_mtime_ns != before.st_mtime_ns
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            _unlink_regular(destination)
            raise DownloadCacheError("verified download changed while being cloned")
        try:
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
        except OSError:
            _unlink_regular(destination)
            raise DownloadCacheError("cloned verified download could not be synced") from None
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            opened = os.fstat(reader.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise DownloadCacheError("verified download changed while being opened")
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            if (
                after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise DownloadCacheError("verified download changed while being copied")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def copy_regular_cow(source: Path, destination: Path) -> None:
    """Create an independent stable copy, preferring APFS copy-on-write."""

    _copy_regular_stable(Path(source), Path(destination))


def _try_clone_regular(source: Path, destination: Path) -> bool:
    """Use APFS copy-on-write when available; callers retain verified fallback."""

    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        clonefile = libc.clonefile
    except AttributeError:
        return False
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clonefile.restype = ctypes.c_int
    if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
        return True
    error = ctypes.get_errno()
    if (
        error in {errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.EPERM}
        and not os.path.lexists(destination)
    ):
        return False
    raise OSError(error, os.strerror(error), destination)


def _object_paths(root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for shard in tuple(root.iterdir()):
        if _is_link_or_reparse(shard) or not shard.is_dir():
            raise DownloadCacheError("download cache object root contains an unsafe shard")
        if len(shard.name) != 2 or any(value not in "0123456789abcdef" for value in shard.name):
            raise DownloadCacheError("download cache object shard name is invalid")
        result.extend(tuple(shard.iterdir()))
    return tuple(result)


def _normalized_digest(value: str) -> str:
    normalized = str(value).casefold()
    if len(normalized) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("download cache digest is invalid")
    return normalized


def _require_regular(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DownloadCacheError(f"{label} is unavailable") from error
    if _is_link_or_reparse(path) or not stat.S_ISREG(metadata.st_mode):
        raise DownloadCacheError(f"{label} must be a regular file")
    return metadata


def _require_real_directory(path: Path, label: str) -> None:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise DownloadCacheError(f"{label} must be a real directory")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _unlink_regular(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _require_regular(path, "download cache removable file")
    try:
        path.unlink()
    except OSError as error:
        raise DownloadCacheError("download cache file could not be removed") from error
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DEFAULT_DOWNLOAD_CACHE_MAX_AGE_SECONDS",
    "DEFAULT_DOWNLOAD_CACHE_MAX_BYTES",
    "DEFAULT_DOWNLOAD_CACHE_QUARANTINE_AGE_SECONDS",
    "DownloadCacheCollection",
    "DownloadCacheError",
    "VerifiedDownloadCache",
    "VerifiedDownloadLease",
    "copy_regular_cow",
]

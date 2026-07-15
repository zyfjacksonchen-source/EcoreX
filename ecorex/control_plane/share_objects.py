"""Opaque content-addressed object storage for public share media.

The Control Plane database owns references and lifecycle state, while this
module owns immutable bytes.  Object keys are deliberately opaque to callers
and are never converted into public URLs or returned by the HTTP API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from typing import BinaryIO, Iterator, Protocol, runtime_checkable

from ecorex.artifacts.storage import ContentAddressedStore


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_LOCAL_OBJECT_KEY = re.compile(r"^sha256/[0-9a-f]{64}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_CHUNK_BYTES = 64 * 1024


class ShareObjectError(RuntimeError):
    """Object bytes are unavailable, corrupt, or conflict with metadata."""


class ShareObjectCapacityError(ShareObjectError):
    """The bounded verified-stream pool is temporarily saturated."""


@dataclass(frozen=True, slots=True)
class ShareStoredObject:
    object_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    etag: str

    def __post_init__(self) -> None:
        if (
            not _OBJECT_KEY.fullmatch(self.object_key)
            or "\\" in self.object_key
            or any(part in {"", ".", ".."} for part in self.object_key.split("/"))
            or not _DIGEST.fullmatch(self.sha256)
            or not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 1
            or not isinstance(self.mime_type, str)
            or not _MIME.fullmatch(self.mime_type)
            or self.etag != self.sha256
        ):
            raise ValueError("share object descriptor is invalid")


@dataclass(slots=True)
class ShareObjectRead:
    """Verified, descriptor-pinned object snapshot suitable for HTTP streaming.

    Local CAS reads hash a pinned file descriptor before the repository releases
    its authorization transaction, then stream from that same descriptor with
    constant memory.  A concurrent path replacement therefore cannot swap bytes
    between authorization and response streaming.
    """

    descriptor: ShareStoredObject
    _handle: BinaryIO = field(repr=False)
    _release: object | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def iter_range(
        self,
        start: int,
        end: int,
        *,
        chunk_bytes: int = _CHUNK_BYTES,
    ) -> Iterator[bytes]:
        if (
            self._closed
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not 0 <= start <= end < self.descriptor.size_bytes
            or not 1 <= chunk_bytes <= 1024 * 1024
        ):
            self.close()
            raise ShareObjectError("share object range is invalid")
        remaining = end - start + 1
        try:
            self._handle.seek(start)
            while remaining:
                chunk = self._handle.read(min(chunk_bytes, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise ShareObjectError("share object ended before its declared size")
                remaining -= len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._handle.close()
            except OSError:
                pass
            finally:
                release = self._release
                self._release = None
                if callable(release):
                    release()


@runtime_checkable
class ShareObjectStore(Protocol):
    """Minimal injectable CAS contract for local, S3, or compatible stores."""

    def put(
        self,
        content: bytes,
        *,
        sha256: str,
        mime_type: str,
    ) -> ShareStoredObject: ...

    def open(
        self,
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareObjectRead: ...

    def delete(self, object_key: str, *, sha256: str) -> bool: ...


class LocalShareObjectStore:
    """Atomic local CAS used by tests and single-node Control Plane installs."""

    def __init__(self, root: str | Path, *, max_open_streams: int = 32) -> None:
        if (
            not isinstance(max_open_streams, int)
            or isinstance(max_open_streams, bool)
            or not 1 <= max_open_streams <= 1024
        ):
            raise ValueError("share object stream limit is invalid")
        self.root = Path(root).expanduser().resolve()
        self._cas: ContentAddressedStore | None = None
        self._stream_slots = threading.BoundedSemaphore(max_open_streams)

    def _store(self, *, create: bool) -> ContentAddressedStore:
        if self._cas is not None:
            return self._cas
        if not create and not self.root.is_dir():
            raise ShareObjectError("share object store is unavailable")
        self._cas = ContentAddressedStore(self.root)
        return self._cas

    def put(
        self,
        content: bytes,
        *,
        sha256: str,
        mime_type: str,
    ) -> ShareStoredObject:
        if (
            not isinstance(content, bytes)
            or not _DIGEST.fullmatch(str(sha256))
            or hashlib.sha256(content).hexdigest() != sha256
            or not isinstance(mime_type, str)
            or not _MIME.fullmatch(mime_type)
        ):
            raise ShareObjectError("share object input is invalid")
        try:
            stored = self._store(create=True).put_bytes(content)
        except (OSError, RuntimeError, ValueError):
            raise ShareObjectError("share object could not be stored") from None
        return ShareStoredObject(
            object_key=f"sha256/{stored.sha256}",
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            mime_type=mime_type,
            etag=stored.sha256,
        )

    def open(
        self,
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareObjectRead:
        if object_key != f"sha256/{sha256}":
            raise ShareObjectError("share object identity is invalid")
        descriptor = self._descriptor(
            object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
        )
        if not self._stream_slots.acquire(blocking=False):
            raise ShareObjectCapacityError("share object stream capacity is busy")
        handle: BinaryIO | None = None
        descriptor_fd: int | None = None
        try:
            # Pin one descriptor, verify that descriptor, then stream from it.
            # This closes the verify/open TOCTOU window without copying as much
            # as 16 MiB per concurrent response into BytesIO.  The semaphore
            # bounds file descriptors as well as verification work.
            path = self._store(create=False).path_for(sha256)
            before = path.lstat()
            self._require_safe_regular(before)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor_fd = os.open(path, flags)
            handle = os.fdopen(descriptor_fd, "rb", closefd=True)
            descriptor_fd = None
            opened = os.fstat(handle.fileno())
            after = path.lstat()
            self._require_safe_regular(after)
            if (
                opened.st_size != size_bytes
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ShareObjectError("share object identity changed while opening")
            digest = hashlib.sha256()
            observed = 0
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                digest.update(chunk)
            if observed != size_bytes or digest.hexdigest() != sha256:
                raise ShareObjectError("share object size or digest does not match metadata")
            handle.seek(0)
        except ShareObjectError:
            if handle is not None:
                handle.close()
            elif descriptor_fd is not None:
                os.close(descriptor_fd)
            self._stream_slots.release()
            raise
        except (OSError, RuntimeError, ValueError):
            if handle is not None:
                handle.close()
            elif descriptor_fd is not None:
                os.close(descriptor_fd)
            self._stream_slots.release()
            raise ShareObjectError("share object is unavailable or corrupt") from None
        return ShareObjectRead(
            descriptor=descriptor,
            _handle=handle,
            _release=self._stream_slots.release,
        )

    def delete(self, object_key: str, *, sha256: str) -> bool:
        if not _LOCAL_OBJECT_KEY.fullmatch(str(object_key)) or object_key != f"sha256/{sha256}":
            raise ShareObjectError("share object identity is invalid")
        store = self._store(create=False)
        path = store.path_for(sha256)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise ShareObjectError("share object deletion could not be verified") from None
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ShareObjectError("share object is not a safe regular file")
        try:
            verified = self.open(
                object_key,
                sha256=sha256,
                size_bytes=int(metadata.st_size),
                mime_type="application/octet-stream",
            )
            verified.close()
            current = path.lstat()
            self._require_safe_regular(current)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ShareObjectError("share object identity changed before deletion")
            path.unlink()
        except FileNotFoundError:
            return False
        except ShareObjectError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise ShareObjectError("share object deletion failed") from None
        return True

    @staticmethod
    def _require_safe_regular(metadata: os.stat_result) -> None:
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ShareObjectError("share object is not a safe regular file")

    @staticmethod
    def _descriptor(
        object_key: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ShareStoredObject:
        try:
            return ShareStoredObject(
                object_key=object_key,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                etag=sha256,
            )
        except (TypeError, ValueError):
            raise ShareObjectError("share object metadata is invalid") from None


__all__ = [
    "LocalShareObjectStore",
    "ShareObjectCapacityError",
    "ShareObjectError",
    "ShareObjectRead",
    "ShareObjectStore",
    "ShareStoredObject",
]

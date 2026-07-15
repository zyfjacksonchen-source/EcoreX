"""Atomic content-addressed blob storage for artifact revisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import BinaryIO

from .errors import ContentIntegrityError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    path: Path


class ContentAddressedStore:
    """Store immutable blobs under SHA-256-only paths.

    User filenames are never part of a storage path.  Writes use a temporary
    sibling followed by ``os.replace``, so readers see either the prior full
    blob or the new full blob and never a partially written file.
    """

    def __init__(self, root: str | os.PathLike[str], *, create: bool = True) -> None:
        self.root = Path(root).resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path_for(self, sha256: str) -> Path:
        normalized = str(sha256).casefold()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return self.root / normalized[:2] / normalized[2:4] / normalized

    def put_bytes(self, content: bytes | bytearray | memoryview) -> StoredBlob:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("artifact content must be bytes-like")
        data = bytes(content)
        sha256 = self.digest(data)
        destination = self.path_for(sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)

        # The in-process lock is only a contention optimization. Correctness
        # comes from the create-if-absent hard-link commit below, which is also
        # atomic across independent store instances and processes.
        with self._write_lock:
            if destination.exists():
                self._verify(destination, sha256, len(data))
                return StoredBlob(sha256=sha256, size_bytes=len(data), path=destination)

            descriptor, temporary_name = tempfile.mkstemp(prefix=".ecorex-cas-", dir=destination.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Linking a fully flushed sibling to the final name is an
                    # atomic O_EXCL-style publish on NTFS/APFS/POSIX filesystems.
                    # Unlike os.replace it never tries to overwrite a winner,
                    # which Windows can reject with ERROR_ACCESS_DENIED.
                    os.link(temporary, destination)
                    self._fsync_directory(destination.parent)
                except FileExistsError:
                    # Another writer won. A digest path may only contain the
                    # same immutable bytes, so verify before accepting it.
                    self._verify(destination, sha256, len(data))
                except OSError:
                    # Some Windows filesystems surface a concurrent winner as
                    # ERROR_ACCESS_DENIED rather than ERROR_FILE_EXISTS.
                    if not destination.exists():
                        raise
                    self._verify(destination, sha256, len(data))
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

            self._verify(destination, sha256, len(data))
        return StoredBlob(sha256=sha256, size_bytes=len(data), path=destination)

    def put_file(self, source: str | os.PathLike[str]) -> StoredBlob:
        with Path(source).open("rb") as handle:
            return self.put_bytes(handle.read())

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def read_bytes(self, sha256: str, *, verify: bool = True) -> bytes:
        path = self.path_for(sha256)
        data = path.read_bytes()
        if verify and self.digest(data) != sha256.casefold():
            raise ContentIntegrityError(f"CAS blob {sha256} failed SHA-256 verification")
        return data

    def open(self, sha256: str) -> BinaryIO:
        # Return a detached verified stream. Verifying a path and then opening
        # it would leave a TOCTOU window in which tampered bytes could be read.
        return io.BytesIO(self.read_bytes(sha256, verify=True))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist a newly linked directory entry where the OS supports it."""

        if os.name == "nt":
            # CPython cannot open directories for fsync on Windows. The hard
            # link still provides atomic visibility; updater-level recovery is
            # responsible for power-loss durability on that platform.
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _verify(path: Path, expected_sha256: str, expected_size: int) -> None:
        try:
            stat = path.stat()
        except OSError as exc:
            raise ContentIntegrityError(f"CAS blob {expected_sha256} is missing after write") from exc
        if stat.st_size != expected_size:
            raise ContentIntegrityError(f"CAS blob {expected_sha256} has an unexpected size")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise ContentIntegrityError(f"CAS blob {expected_sha256} failed SHA-256 verification")

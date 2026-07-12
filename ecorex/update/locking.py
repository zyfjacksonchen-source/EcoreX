"""Cross-process single-flight lock for install and update operations."""

from __future__ import annotations

import os
import sys
import stat
import threading
import time
from pathlib import Path
from typing import BinaryIO, Protocol


class LockUnavailable(TimeoutError):
    pass


class _LockBackend(Protocol):
    name: str

    def try_acquire(self, stream: BinaryIO) -> bool:
        ...

    def release(self, stream: BinaryIO) -> None:
        ...


class _WindowsLockBackend:
    name = "windows-msvcrt"

    def __init__(self) -> None:
        import msvcrt

        self._msvcrt = msvcrt

    def try_acquire(self, stream: BinaryIO) -> bool:
        stream.seek(0)
        try:
            self._msvcrt.locking(stream.fileno(), self._msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def release(self, stream: BinaryIO) -> None:
        stream.seek(0)
        self._msvcrt.locking(stream.fileno(), self._msvcrt.LK_UNLCK, 1)


class _PosixLockBackend:
    name = "posix-flock"

    def __init__(self) -> None:
        import fcntl

        self._fcntl = fcntl

    def try_acquire(self, stream: BinaryIO) -> bool:
        try:
            self._fcntl.flock(
                stream.fileno(), self._fcntl.LOCK_EX | self._fcntl.LOCK_NB
            )
        except (BlockingIOError, OSError):
            return False
        return True

    def release(self, stream: BinaryIO) -> None:
        self._fcntl.flock(stream.fileno(), self._fcntl.LOCK_UN)


def _platform_backend() -> _LockBackend:
    if os.name == "nt":
        return _WindowsLockBackend()
    if os.name == "posix" and (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return _PosixLockBackend()
    raise RuntimeError(f"EcoreX update locking is unsupported on platform {sys.platform!r}")


class ProductFileLock:
    """A re-entrant-per-instance file lock shared by install and update flows.

    The lock file remains on disk after release.  Deleting lock files creates a
    split-brain race because waiters may still hold descriptors to the old
    inode, so cleanup is deliberately omitted.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float | None = 0.0,
        poll_interval: float = 0.05,
        backend: _LockBackend | None = None,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._backend = backend or _platform_backend()
        self._stream: BinaryIO | None = None
        self._depth = 0
        self._owner_thread: int | None = None
        self._guard = threading.Lock()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def acquired(self) -> bool:
        with self._guard:
            return self._depth > 0

    def acquire(self) -> "ProductFileLock":
        thread_id = threading.get_ident()
        with self._guard:
            if self._depth:
                if self._owner_thread != thread_id:
                    raise LockUnavailable("lock instance is already owned by another thread")
                self._depth += 1
                return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.path):
            metadata = self.path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
                raise LockUnavailable("product lock path cannot be a link or reparse point")
        stream = self.path.open("a+b", buffering=0)
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())

            started = time.monotonic()
            while not self._backend.try_acquire(stream):
                if self.timeout is not None and time.monotonic() - started >= self.timeout:
                    raise LockUnavailable(f"timed out acquiring product lock {self.path}")
                time.sleep(self.poll_interval)

            with self._guard:
                # No other thread can have acquired this instance because only
                # this call owns the newly opened descriptor.
                self._stream = stream
                self._depth = 1
                self._owner_thread = thread_id
            return self
        except BaseException:
            stream.close()
            raise

    def release(self) -> None:
        thread_id = threading.get_ident()
        with self._guard:
            if not self._depth or self._stream is None:
                raise RuntimeError("cannot release a product lock that is not acquired")
            if self._owner_thread != thread_id:
                raise RuntimeError("product lock can only be released by its owning thread")
            self._depth -= 1
            if self._depth:
                return
            stream = self._stream
            self._stream = None
            self._owner_thread = None
        try:
            self._backend.release(stream)
        finally:
            stream.close()

    def __enter__(self) -> "ProductFileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()

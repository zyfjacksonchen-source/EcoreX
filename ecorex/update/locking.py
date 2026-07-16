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
        self._guard = threading.Condition()
        # Reserving the instance while a thread opens and acquires the OS
        # descriptor prevents two threads from racing through the initial
        # ``_depth == 0`` check.  This is not ownership: only a successful OS
        # lock acquisition installs ``_owner_thread``.
        self._acquiring_thread: int | None = None

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def acquired(self) -> bool:
        with self._guard:
            return self._depth > 0

    def acquire(self) -> "ProductFileLock":
        thread_id = threading.get_ident()
        started = time.monotonic()
        with self._guard:
            while self._depth or self._acquiring_thread is not None:
                if self._depth and self._owner_thread == thread_id:
                    self._depth += 1
                    return self
                remaining = self._remaining_timeout(started)
                if remaining is not None and remaining <= 0:
                    raise LockUnavailable(
                        f"timed out acquiring product lock {self.path}"
                    )
                # A different thread never becomes a second/re-entrant owner.
                # It waits for the current instance owner (or in-flight OS
                # acquisition) and competes only after release is complete.
                self._guard.wait(timeout=remaining)
            self._acquiring_thread = thread_id

        stream: BinaryIO | None = None
        backend_acquired = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(self.path):
                metadata = self.path.lstat()
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
                    raise LockUnavailable(
                        "product lock path cannot be a link or reparse point"
                    )
            stream = self.path.open("a+b", buffering=0)
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())

            while not self._backend.try_acquire(stream):
                remaining = self._remaining_timeout(started)
                if remaining is not None and remaining <= 0:
                    raise LockUnavailable(f"timed out acquiring product lock {self.path}")
                time.sleep(
                    self.poll_interval
                    if remaining is None
                    else min(self.poll_interval, remaining)
                )
            backend_acquired = True

            with self._guard:
                if self._acquiring_thread != thread_id:
                    raise RuntimeError("product lock acquisition reservation was lost")
                self._stream = stream
                self._depth = 1
                self._owner_thread = thread_id
                self._acquiring_thread = None
                return self
        except BaseException:
            try:
                if stream is not None and backend_acquired:
                    try:
                        self._backend.release(stream)
                    except BaseException:
                        # Preserve the acquisition failure while still trying
                        # descriptor close, which is the final OS-lock fence.
                        pass
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        # Cleanup failures must not strand the in-process
                        # reservation or replace the causal acquire failure.
                        pass
            finally:
                with self._guard:
                    if self._acquiring_thread == thread_id:
                        self._acquiring_thread = None
                        self._guard.notify_all()
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
            # Keep ownership installed until both the OS unlock and descriptor
            # close finish.  A waiter cannot reserve this instance in the
            # small release boundary and become a second owner.
            release_error: BaseException | None = None
            try:
                self._backend.release(stream)
            except BaseException as error:
                release_error = error
            try:
                stream.close()
            except BaseException as error:
                if release_error is None:
                    release_error = error
            finally:
                self._stream = None
                self._owner_thread = None
                self._guard.notify_all()
            if release_error is not None:
                raise release_error

    def _remaining_timeout(self, started: float) -> float | None:
        if self.timeout is None:
            return None
        return self.timeout - (time.monotonic() - started)

    def __enter__(self) -> "ProductFileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()

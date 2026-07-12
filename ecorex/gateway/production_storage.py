"""Fail-closed single-process ownership for the SQLite Gateway provider."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import threading

from .schema import (
    GatewaySchemaError,
    GatewaySchemaManager,
    validate_gateway_wal_health,
)


_HELD_LOCKS: set[Path] = set()
_HELD_LOCKS_GUARD = threading.RLock()


class GatewayProductionStorageError(RuntimeError):
    """Gateway storage is unsafe, incompatible or owned by another process."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _regular_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise GatewayProductionStorageError(
            "gateway storage directory is unavailable"
        ) from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise GatewayProductionStorageError("gateway storage directory is unsafe")


def _regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise GatewayProductionStorageError("gateway storage file is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise GatewayProductionStorageError("gateway storage file is unsafe")


class GatewayInstanceLock:
    """Cross-platform, non-blocking lock held for the entire service lifetime."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_name(database_path.name + ".gateway.lock")
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        _regular_directory(self.path.parent)
        resolved = _absolute(self.path)
        with _HELD_LOCKS_GUARD:
            if resolved in _HELD_LOCKS:
                raise GatewayProductionStorageError(
                    "another gateway process owns the single-node database"
                )
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if os.path.lexists(self.path):
                # Empty lock files are valid before their first acquisition.
                try:
                    metadata = self.path.lstat()
                except OSError as error:
                    raise GatewayProductionStorageError(
                        "gateway process lock is unavailable"
                    ) from error
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise GatewayProductionStorageError("gateway process lock is unsafe")
            descriptor = os.open(str(self.path), flags, 0o600)
            try:
                descriptor_metadata = os.fstat(descriptor)
                path_metadata = self.path.lstat()
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    not stat.S_ISREG(descriptor_metadata.st_mode)
                    or stat.S_ISLNK(path_metadata.st_mode)
                    or bool(getattr(path_metadata, "st_file_attributes", 0) & reparse)
                    or (
                        descriptor_metadata.st_ino
                        and path_metadata.st_ino
                        and (
                            descriptor_metadata.st_ino != path_metadata.st_ino
                            or descriptor_metadata.st_dev != path_metadata.st_dev
                        )
                    )
                ):
                    raise GatewayProductionStorageError("gateway process lock is unsafe")
                if descriptor_metadata.st_size < 1:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, GatewayProductionStorageError) as error:
                os.close(descriptor)
                if isinstance(error, GatewayProductionStorageError):
                    raise
                raise GatewayProductionStorageError(
                    "another gateway process owns the single-node database"
                ) from None
            self._descriptor = descriptor
            _HELD_LOCKS.add(resolved)

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        resolved = _absolute(self.path)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            with _HELD_LOCKS_GUARD:
                _HELD_LOCKS.discard(resolved)

    def __enter__(self) -> "GatewayInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def validate_gateway_sqlite_health(database_path: Path, *, full: bool) -> None:
    """Validate schema authority and WAL mode without creating any object."""

    _regular_directory(database_path.parent)
    _regular_file(database_path)
    if full:
        try:
            GatewaySchemaManager(database_path).validate()
        except GatewaySchemaError as error:
            raise GatewayProductionStorageError(
                "gateway schema validation failed"
            ) from error
    try:
        validate_gateway_wal_health(database_path)
    except GatewaySchemaError:
        raise GatewayProductionStorageError(
            "gateway SQLite health is unavailable"
        ) from None


__all__ = [
    "GatewayInstanceLock",
    "GatewayProductionStorageError",
    "validate_gateway_sqlite_health",
]

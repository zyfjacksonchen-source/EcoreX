"""Single-node storage guardrails and verified SQLite backups.

This module deliberately does not make SQLite look like a distributed store.
One process lock, one persistent-volume identity and explicit migrations are
required.  A PostgreSQL/HA provider can implement the production dependency
protocol separately without weakening these single-node invariants.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
import uuid

from ecorex.ids import new_id
from .schema import ControlPlaneSchemaError, validate_control_plane_wal_health


_VOLUME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BACKUP_ID = re.compile(r"^cpb_[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HELD_LOCKS: set[Path] = set()
_HELD_LOCKS_GUARD = threading.RLock()


class ProductionStorageError(RuntimeError):
    """The configured production volume, lock or backup is unsafe/unavailable."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _regular_file(path: Path, *, nonempty: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProductionStorageError("production storage file is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or (nonempty and metadata.st_size <= 0)
    ):
        raise ProductionStorageError("production storage file is unsafe")
    return metadata


def _regular_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProductionStorageError("production storage directory is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ProductionStorageError("production storage directory is unsafe")
    return metadata


def _fsync_file(path: Path) -> None:
    # Windows rejects ``fsync`` on a read-only descriptor even when the file is
    # otherwise accessible.  The file is private and owned by this process, so
    # open a write-capable descriptor without modifying its content.
    descriptor = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ControlPlaneInstanceLock:
    """Cross-platform non-blocking process lock held for the entire service."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_name(database_path.name + ".instance.lock")
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
                raise ProductionStorageError(
                    "another Control Plane process owns the single-node storage"
                )
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if os.path.lexists(self.path):
                _regular_file(self.path)
            descriptor = os.open(str(self.path), flags, 0o600)
            try:
                metadata = os.fstat(descriptor)
                path_metadata = self.path.lstat()
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
                    or stat.S_ISLNK(path_metadata.st_mode)
                    or bool(
                        getattr(path_metadata, "st_file_attributes", 0) & reparse
                    )
                    or (
                        metadata.st_ino
                        and path_metadata.st_ino
                        and (
                            metadata.st_ino != path_metadata.st_ino
                            or metadata.st_dev != path_metadata.st_dev
                        )
                    )
                ):
                    raise ProductionStorageError("Control Plane process lock is unsafe")
                if metadata.st_size < 1:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, ProductionStorageError) as error:
                os.close(descriptor)
                if isinstance(error, ProductionStorageError):
                    raise
                raise ProductionStorageError(
                    "another Control Plane process owns the single-node storage"
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

    def __enter__(self) -> "ControlPlaneInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class PersistentVolumeGuard:
    MARKER_NAME = ".ecorex-control-plane-volume.json"

    def __init__(self, database_path: Path, *, volume_id: str) -> None:
        if _VOLUME_ID.fullmatch(volume_id) is None:
            raise ProductionStorageError("production volume identity is invalid")
        if not database_path.is_absolute():
            raise ProductionStorageError("production database path must be absolute")
        self.database_path = _absolute(database_path)
        self.root = self.database_path.parent
        self.volume_id = volume_id
        self.marker_path = self.root / self.MARKER_NAME

    def validate_directory(self) -> None:
        _regular_directory(self.root)
        if self.database_path.exists():
            _regular_file(self.database_path, nonempty=True)

    def install_or_validate(self) -> None:
        self.validate_directory()
        if self.marker_path.exists() or os.path.lexists(self.marker_path):
            self.validate_marker()
            return
        payload = {"schema_version": 1, "volume_id": self.volume_id}
        encoded_payload = _canonical(payload)
        envelope = {
            "payload": payload,
            "sha256": hashlib.sha256(encoded_payload).hexdigest(),
        }
        temporary = self.root / f".{self.MARKER_NAME}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            content = _canonical(envelope)
            os.write(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.marker_path)
            _fsync_directory(self.root)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self.validate_marker()

    def validate_marker(self) -> None:
        metadata = _regular_file(self.marker_path, nonempty=True)
        if metadata.st_size > 4096:
            raise ProductionStorageError("production volume marker is invalid")
        try:
            raw = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"payload", "sha256"}:
                raise ValueError
            payload = raw["payload"]
            if (
                not isinstance(payload, dict)
                or payload != {"schema_version": 1, "volume_id": self.volume_id}
                or raw["sha256"] != hashlib.sha256(_canonical(payload)).hexdigest()
                or self.marker_path.read_bytes() != _canonical(raw)
            ):
                raise ValueError
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise ProductionStorageError("production volume marker is invalid") from None

    def validate_wal(self) -> None:
        self.validate_marker()
        _regular_file(self.database_path, nonempty=True)
        try:
            validate_control_plane_wal_health(self.database_path)
        except ControlPlaneSchemaError as error:
            raise ProductionStorageError("production SQLite WAL is unhealthy") from error


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    schema_version: int
    backup_id: str
    database_sha256: str
    size_bytes: int
    created_at: str
    reason: str
    volume_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backup_id": self.backup_id,
            "database_sha256": self.database_sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "reason": self.reason,
            "volume_id": self.volume_id,
        }


class SQLiteBackupManager:
    """Online SQLite backup with digest/quick-check receipts and bounded retention."""

    def __init__(
        self,
        database_path: Path,
        backup_directory: Path,
        *,
        volume_id: str,
        retain_count: int = 14,
    ) -> None:
        if not database_path.is_absolute() or not backup_directory.is_absolute():
            raise ProductionStorageError("production backup paths must be absolute")
        if not 2 <= retain_count <= 365:
            raise ProductionStorageError("production backup retention is invalid")
        self.database_path = _absolute(database_path)
        self.backup_directory = _absolute(backup_directory)
        self.volume_id = volume_id
        self.retain_count = retain_count

    def validate_directory(self) -> None:
        _regular_directory(self.backup_directory)
        if self.backup_directory == self.database_path.parent:
            raise ProductionStorageError(
                "production backups require a distinct persistent directory"
            )

    def create(self, *, reason: str) -> BackupReceipt:
        if reason not in {"pre-migration", "post-migration", "scheduled", "operator"}:
            raise ProductionStorageError("production backup reason is invalid")
        self.validate_directory()
        _regular_file(self.database_path, nonempty=True)
        now = datetime.now(UTC)
        backup_id = new_id("cpb")
        database_copy = self.backup_directory / f"{backup_id}.sqlite3"
        manifest = self.backup_directory / f"{backup_id}.json"
        descriptor = os.open(
            str(database_copy),
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.close(descriptor)
        try:
            source = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode=ro&nofollow=1",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            destination = sqlite3.connect(str(database_copy), isolation_level=None)
            try:
                source.execute("PRAGMA query_only=ON")
                source.backup(destination, pages=256, sleep=0.01)
                quick = destination.execute("PRAGMA quick_check").fetchone()
                if quick is None or str(quick[0]).casefold() != "ok":
                    raise ProductionStorageError("production backup integrity check failed")
            finally:
                destination.close()
                source.close()
            _fsync_file(database_copy)
            digest = _sha256_file(database_copy)
            receipt = BackupReceipt(
                schema_version=1,
                backup_id=backup_id,
                database_sha256=digest,
                size_bytes=database_copy.stat().st_size,
                created_at=now.isoformat(),
                reason=reason,
                volume_id=self.volume_id,
            )
            _write_new_file(manifest, _canonical(receipt.to_dict()))
            _fsync_directory(self.backup_directory)
            self.validate(backup_id, full_digest=True)
            self._prune()
            return receipt
        except BaseException:
            for path in (manifest, database_copy):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def latest(self, *, full_digest: bool) -> BackupReceipt:
        self.validate_directory()
        candidates = sorted(
            (
                path.stem
                for path in self.backup_directory.glob("cpb_*.json")
                if _BACKUP_ID.fullmatch(path.stem)
            ),
            reverse=True,
        )
        if not candidates:
            raise ProductionStorageError("production backup is missing")
        return self.validate(candidates[0], full_digest=full_digest)

    def validate(self, backup_id: str, *, full_digest: bool) -> BackupReceipt:
        if _BACKUP_ID.fullmatch(backup_id) is None:
            raise ProductionStorageError("production backup identity is invalid")
        manifest = self.backup_directory / f"{backup_id}.json"
        database_copy = self.backup_directory / f"{backup_id}.sqlite3"
        manifest_meta = _regular_file(manifest, nonempty=True)
        database_meta = _regular_file(database_copy, nonempty=True)
        if manifest_meta.st_size > 8192:
            raise ProductionStorageError("production backup receipt is invalid")
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "backup_id",
                "database_sha256",
                "size_bytes",
                "created_at",
                "reason",
                "volume_id",
            }:
                raise ValueError
            receipt = BackupReceipt(**raw)
            created = datetime.fromisoformat(receipt.created_at)
            if (
                receipt.schema_version != 1
                or receipt.backup_id != backup_id
                or _DIGEST.fullmatch(receipt.database_sha256) is None
                or receipt.size_bytes != database_meta.st_size
                or receipt.size_bytes <= 0
                or created.tzinfo is None
                or receipt.reason
                not in {"pre-migration", "post-migration", "scheduled", "operator"}
                or receipt.volume_id != self.volume_id
                or manifest.read_bytes() != _canonical(receipt.to_dict())
            ):
                raise ValueError
            if full_digest and _sha256_file(database_copy) != receipt.database_sha256:
                raise ValueError
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ProductionStorageError("production backup receipt is invalid") from None
        if full_digest:
            connection = sqlite3.connect(
                f"{database_copy.as_uri()}?mode=ro&nofollow=1",
                uri=True,
                isolation_level=None,
            )
            try:
                quick = connection.execute("PRAGMA quick_check").fetchone()
                if quick is None or str(quick[0]).casefold() != "ok":
                    raise ProductionStorageError("production backup is corrupt")
            finally:
                connection.close()
        return receipt

    def restore(self, backup_id: str) -> None:
        """Restore only while the caller holds the single-node instance lock."""

        self.validate(backup_id, full_digest=True)
        source_path = self.backup_directory / f"{backup_id}.sqlite3"
        source = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro&nofollow=1",
            uri=True,
            isolation_level=None,
        )
        destination = sqlite3.connect(str(self.database_path), isolation_level=None)
        try:
            source.backup(destination, pages=256, sleep=0.01)
        finally:
            destination.close()
            source.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.database_path) + suffix)
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
        _fsync_file(self.database_path)
        _fsync_directory(self.database_path.parent)

    def _prune(self) -> None:
        candidates = sorted(
            (
                path.stem
                for path in self.backup_directory.glob("cpb_*.json")
                if _BACKUP_ID.fullmatch(path.stem)
            ),
            reverse=True,
        )
        for backup_id in candidates[self.retain_count :]:
            for suffix in (".json", ".sqlite3"):
                target = _absolute(self.backup_directory / f"{backup_id}{suffix}")
                if target.parent != self.backup_directory:
                    raise ProductionStorageError("production backup path escaped its root")
                _regular_file(target, nonempty=True)
                target.unlink()
        _fsync_directory(self.backup_directory)


def available_bytes(path: Path) -> int:
    _regular_directory(path)
    return int(shutil.disk_usage(path).free)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "BackupReceipt",
    "ControlPlaneInstanceLock",
    "PersistentVolumeGuard",
    "ProductionStorageError",
    "SQLiteBackupManager",
    "available_bytes",
]

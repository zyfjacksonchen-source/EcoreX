"""Bounded recovery for observability rows encrypted by an unavailable key.

Conversation, memory, projects and Artifact state are never part of this
recovery.  It only quarantines the derived audit/trace outbox after preserving
an exact SQLite backup, and is intentionally callable only for the one known
AES-GCM authentication failure emitted by :mod:`ecorex.observability.audit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
from typing import Final

from .audit import AuditIntegrityError


_UNREADABLE_PAYLOAD_ERROR: Final = "stored audit payload authentication failed"
_TABLES: Final = (
    "observability_audit_outbox",
    "observability_audit_daily",
    "observability_audit_cursors",
    "observability_trace_outbox",
    "observability_trace_segments",
    "observability_trace_cursors",
)
_SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class ObservabilityRecoveryReceipt:
    receipt_path: Path
    backup_path: Path
    removed_rows: dict[str, int]


def is_unreadable_observability_error(error: BaseException) -> bool:
    """Return true only for a known encrypted-payload authentication failure."""

    return isinstance(error, AuditIntegrityError) and str(error) == _UNREADABLE_PAYLOAD_ERROR


def quarantine_unreadable_observability(
    database_path: str | os.PathLike[str],
) -> ObservabilityRecoveryReceipt:
    """Back up and clear unreadable derived observability state once.

    The caller has already classified a fixed ``AuditIntegrityError``.  This
    function still refuses links, special files and insufficient disk space,
    commits the cleanup in one SQLite transaction, then verifies the live
    database before returning its durable receipt.
    """

    database = _require_database(database_path)
    state_root = database.parent
    backup_root = state_root / "observability-quarantine"
    _ensure_real_directory(backup_root, create=True)
    _require_disk_space(backup_root, database.stat().st_size)
    created_at = datetime.now(UTC).replace(microsecond=0)
    quarantine = backup_root / (
        "audit-key-mismatch-"
        + created_at.strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(6)
    )
    quarantine.mkdir(mode=0o700)
    _ensure_real_directory(quarantine, create=False)
    backup_path = quarantine / "runtime-before-observability-recovery.sqlite3"

    _backup_database(database, backup_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = _present_tables(connection)
        removed_rows = _row_counts(connection, tables)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                connection.execute(f'DELETE FROM "{table}"')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        remaining_rows = _row_counts(connection, tables)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if any(remaining_rows.values()) or integrity != "ok":
        raise RuntimeError("observability quarantine verification failed")

    receipt_path = quarantine / "recovery-receipt.json"
    _atomic_json(
        receipt_path,
        {
            "schema_version": _SCHEMA_VERSION,
            "reason": "audit_key_mismatch",
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "backup_sha256": _sha256_file(backup_path),
            "backup_size_bytes": backup_path.stat().st_size,
            "removed_rows": removed_rows,
            "remaining_rows": remaining_rows,
            "integrity": integrity,
        },
    )
    return ObservabilityRecoveryReceipt(
        receipt_path=receipt_path,
        backup_path=backup_path,
        removed_rows=removed_rows,
    )


def _require_database(value: str | os.PathLike[str]) -> Path:
    database = Path(value).expanduser().resolve(strict=True)
    _ensure_real_regular_file(database)
    _ensure_real_directory(database.parent, create=False)
    return database


def _ensure_real_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("observability recovery directory is unsafe")


def _ensure_real_regular_file(path: Path) -> None:
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("observability recovery database is unsafe")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )


def _require_disk_space(directory: Path, database_size: int) -> None:
    if database_size < 1:
        raise RuntimeError("observability recovery database is empty")
    # SQLite backup plus journal and receipt; leave a conservative small floor.
    required = database_size * 2 + 16 * 1024 * 1024
    if shutil.disk_usage(directory).free < required:
        raise RuntimeError("insufficient disk space for observability recovery")


def _backup_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    _ensure_real_regular_file(destination_path)
    if destination_path.stat().st_size < 1:
        raise RuntimeError("observability recovery backup is empty")
    verification = sqlite3.connect(f"file:{destination_path.as_posix()}?mode=ro", uri=True)
    try:
        if verification.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("observability recovery backup is invalid")
    finally:
        verification.close()


def _present_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    observed = {str(row[0]) for row in rows}
    return tuple(table for table in _TABLES if table in observed)


def _row_counts(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def _atomic_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "ObservabilityRecoveryReceipt",
    "is_unreadable_observability_error",
    "quarantine_unreadable_observability",
]

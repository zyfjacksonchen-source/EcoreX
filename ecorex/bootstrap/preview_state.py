"""Independent data checkpoint for side-by-side Runtime acceptance."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import sys
from typing import Any

from ecorex.runtime.storage_migrations import _copy_database_snapshot
from ecorex.observability.recovery import OBSERVABILITY_TABLES
from ecorex.update.download_cache import copy_regular_cow


MAX_SNAPSHOT_FILES = 100_000
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024 * 1024
_DATABASE_RELATIVE = Path("state/runtime.sqlite3")


class PreviewStateError(RuntimeError):
    """The acceptance checkpoint could not be created safely."""


def prepare_preview_state(
    source_install_root: str | os.PathLike[str],
    preview_install_root: str | os.PathLike[str],
) -> dict[str, Any]:
    source = _real_root(
        source_install_root,
        label="source install root",
        create=False,
    )
    preview = _real_root(
        preview_install_root,
        label="preview install root",
        create=True,
    )
    source_roots = tuple(source / name for name in ("state", "workspace"))
    preview_roots = tuple(preview / name for name in ("state", "workspace"))
    if any(
        _paths_overlap(source_root, preview_root)
        for source_root in source_roots
        for preview_root in preview_roots
    ):
        raise PreviewStateError("preview install root overlaps Runtime data")

    snapshot_id = secrets.token_hex(16)
    staging = preview / f".acceptance-snapshot-{snapshot_id}"
    if os.path.lexists(staging):
        raise PreviewStateError("acceptance snapshot staging path already exists")
    staging.mkdir(mode=0o700)
    counters = {"files": 0, "bytes": 0}
    try:
        for name in ("state", "workspace"):
            _copy_tree(
                source / name,
                staging / name,
                counters=counters,
                skip_database=name == "state",
            )
        source_database = source / _DATABASE_RELATIVE
        target_database = staging / _DATABASE_RELATIVE
        if source_database.exists():
            target_database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _copy_database_snapshot(source_database, target_database)
            observability_rows_removed = _clear_derived_observability(target_database)
            size = target_database.stat().st_size
            counters["files"] += 1
            counters["bytes"] += size
            _enforce_budget(counters)
            database_sha256 = _sha256_file(target_database)
        else:
            database_sha256 = hashlib.sha256(b"").hexdigest()
            observability_rows_removed = {}

        _replace_data_roots(preview, staging, snapshot_id)
        receipt = {
            "schema_version": 1,
            "status": "ready",
            "snapshot_id": snapshot_id,
            "database_sha256": database_sha256,
            "file_count": counters["files"],
            "size_bytes": counters["bytes"],
            "observability_rows_removed": observability_rows_removed,
            "created_at": (
                datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        }
        _atomic_json(preview / "acceptance-preview.json", receipt)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _clear_derived_observability(database_path: Path) -> dict[str, int]:
    """Keep preview from replaying live telemetry encrypted by the OS vault."""

    connection = sqlite3.connect(database_path)
    try:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        tables = tuple(
            table for table in OBSERVABILITY_TABLES if table in present
        )
        removed = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                connection.execute(f'DELETE FROM "{table}"')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PreviewStateError("Runtime acceptance database is invalid")
        return {table: count for table, count in removed.items() if count}
    finally:
        connection.close()


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    counters: dict[str, int],
    skip_database: bool,
) -> None:
    destination.mkdir(mode=0o700)
    if not source.exists():
        return
    _require_real_directory(source, "Runtime data directory")
    with os.scandir(source) as entries:
        for entry in sorted(entries, key=lambda value: value.name):
            source_path = Path(entry.path)
            target_path = destination / entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise PreviewStateError("Runtime data snapshot cannot contain links")
            if stat.S_ISDIR(metadata.st_mode):
                _copy_tree(
                    source_path,
                    target_path,
                    counters=counters,
                    skip_database=False,
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PreviewStateError("Runtime data snapshot contains a special file")
            if skip_database and entry.name in {
                _DATABASE_RELATIVE.name,
                _DATABASE_RELATIVE.name + "-wal",
                _DATABASE_RELATIVE.name + "-shm",
            }:
                continue
            counters["files"] += 1
            counters["bytes"] += metadata.st_size
            _enforce_budget(counters)
            copy_regular_cow(source_path, target_path)


def _replace_data_roots(preview: Path, staging: Path, snapshot_id: str) -> None:
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for name in ("state", "workspace"):
            current = preview / name
            replacement = staging / name
            backup = preview / f".{name}-before-{snapshot_id}"
            if os.path.lexists(current):
                _require_real_directory(current, f"preview {name}")
                os.replace(current, backup)
                backups.append((current, backup))
            os.replace(replacement, current)
            installed.append(current)
    except BaseException:
        for current in reversed(installed):
            if current.exists():
                shutil.rmtree(current)
        for current, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, current)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for _current, backup in backups:
        shutil.rmtree(backup)


def _real_root(
    value: str | os.PathLike[str],
    *,
    label: str,
    create: bool,
) -> Path:
    path = Path(os.path.abspath(value))
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(path, label)
    return path.resolve(strict=True)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PreviewStateError(f"{label} is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise PreviewStateError(f"{label} must be a real directory")


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _enforce_budget(counters: dict[str, int]) -> None:
    if counters["files"] > MAX_SNAPSHOT_FILES or counters["bytes"] > MAX_SNAPSHOT_BYTES:
        raise PreviewStateError("Runtime acceptance snapshot exceeds its safety budget")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecorex-bootstrap-preview-state")
    parser.add_argument("--source-install-root", required=True)
    parser.add_argument("--preview-install-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = prepare_preview_state(
            args.source_install_root,
            args.preview_install_root,
        )
    except Exception:
        print(
            "e-Mate could not create an isolated acceptance checkpoint.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "MAX_SNAPSHOT_FILES",
    "PreviewStateError",
    "prepare_preview_state",
]

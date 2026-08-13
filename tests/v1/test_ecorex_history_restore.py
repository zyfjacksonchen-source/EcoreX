from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from ecorex.migration.ecorex_history import (
    ECoreXHistoryMigrationError,
    default_ecorex_data_roots,
    restore_ecorex_history,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import RuntimeKernel
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.invariants import RuntimeInvariantAuditor


def _history(root: Path, *, title: str) -> tuple[Path, str]:
    database = root / "state/runtime.sqlite3"
    kernel = RuntimeKernel(database)
    thread = kernel.create_thread(CreateThreadRequest(title=title))
    kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input=f"{title} content", client_message_id=f"{title}-message"),
    )
    return database, thread.thread_id


def _downgrade_to_v1(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE knowledge_mutation_requests")
        connection.execute(
            "UPDATE runtime_meta SET value='1' WHERE key='storage_schema_version'"
        )
        connection.execute(
            "UPDATE runtime_meta SET value=? WHERE key='product_schema_sha256'",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()


def _family_digest(database: Path) -> str:
    digest = hashlib.sha256()
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _count(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def test_same_generation_history_is_late_merged_and_idempotent(tmp_path: Path) -> None:
    old_root = tmp_path / "Library/Application Support/ECoreX"
    target = tmp_path / ".emate"
    old_database, old_thread = _history(old_root, title="old ECoreX")
    _downgrade_to_v1(old_database)
    target_database, new_thread = _history(target, title="new e-Mate")
    before_source = _family_digest(old_database)

    result = restore_ecorex_history(target, source_roots=[old_root])

    assert result.migrated is True
    assert result.imported["threads"] == 1
    assert _family_digest(old_database) == before_source
    connection = sqlite3.connect(target_database)
    try:
        assert {row[0] for row in connection.execute("SELECT thread_id FROM threads")} == {
            old_thread,
            new_thread,
        }
    finally:
        connection.close()
    assert RuntimeInvariantAuditor(target_database).audit().ok
    counts = tuple(_count(target_database, table) for table in ("threads", "turns", "items", "events"))

    repeated = restore_ecorex_history(target, source_roots=[old_root])

    assert repeated.migrated is False
    assert tuple(_count(target_database, table) for table in ("threads", "turns", "items", "events")) == counts
    assert _family_digest(old_database) == before_source


def test_collision_fails_closed_without_changing_current_history(tmp_path: Path) -> None:
    old_root = tmp_path / "old/ECoreX"
    target = tmp_path / ".emate"
    old_database, old_thread = _history(old_root, title="old")
    _downgrade_to_v1(old_database)
    target_database, _ = _history(target, title="new")
    connection = sqlite3.connect(target_database)
    try:
        connection.execute(
            "INSERT INTO threads(thread_id,status,title,metadata_json,created_at,updated_at) "
            "VALUES (?, 'idle', 'collision', '{}', 'now', 'now')",
            (old_thread,),
        )
        connection.execute("INSERT INTO thread_heads(thread_id,last_seq) VALUES (?,0)", (old_thread,))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    before = _family_digest(target_database)

    with pytest.raises(ECoreXHistoryMigrationError, match="collides"):
        restore_ecorex_history(target, source_roots=[old_root])

    assert _family_digest(target_database) == before


def test_nonempty_wal_and_crash_after_publish_recover_idempotently(tmp_path: Path) -> None:
    old_root = tmp_path / "old/ECoreX"
    target = tmp_path / ".emate"
    old_database, _ = _history(old_root, title="old")
    _downgrade_to_v1(old_database)
    target_database, _ = _history(target, title="new")
    writer = sqlite3.connect(target_database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO memory_meta(key,value) VALUES ('wal-proof','retained')")
    writer.commit()
    assert Path(str(target_database) + "-wal").stat().st_size > 0

    def fault(phase: str) -> None:
        if phase == "after_publish":
            raise RuntimeError("simulated crash")

    with pytest.raises(ECoreXHistoryMigrationError):
        restore_ecorex_history(target, source_roots=[old_root], fault_hook=fault)
    writer.close()
    assert not (target / "migration/ecorex-history-v1.json").exists()

    result = restore_ecorex_history(target, source_roots=[old_root])

    assert result.source_found is True
    assert _count(target_database, "threads") == 2
    connection = sqlite3.connect(target_database)
    try:
        assert connection.execute(
            "SELECT value FROM memory_meta WHERE key='wal-proof'"
        ).fetchone() == ("retained",)
    finally:
        connection.close()
    assert RuntimeInvariantAuditor(target_database).audit().ok


def test_native_old_ecorex_roots_cover_macos_and_windows(tmp_path: Path) -> None:
    assert default_ecorex_data_roots(home=tmp_path, platform="darwin") == (
        tmp_path / "Library/Application Support/ECoreX",
    )
    assert default_ecorex_data_roots(
        home=tmp_path,
        platform="win32",
        environment={"APPDATA": "C:/Users/test/AppData/Roaming", "LOCALAPPDATA": "C:/Users/test/AppData/Local"},
    )[:2] == (
        Path("C:/Users/test/AppData/Roaming/ECoreX"),
        Path("C:/Users/test/AppData/Local/ECoreX"),
    )

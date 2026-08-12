from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.storage import MemoryChunk, MemoryStorage


def test_fts_damage_rebuild_preserves_shared_conversation_rows(tmp_path):
    db_path = tmp_path / "index.db"
    ConversationStore(db_path).append_messages(
        "shared-session",
        [
            {"role": "user", "content": "keep this prompt"},
            {"role": "assistant", "content": "keep this answer"},
        ],
        channel_type="web",
    )
    storage = MemoryStorage(db_path)
    storage.save_chunk(
        MemoryChunk(
            id="memory-1",
            user_id=None,
            scope="shared",
            source="memory",
            path="MEMORY.md",
            start_line=1,
            end_line=1,
            text="alpha beta",
            embedding=None,
            hash="hash-1",
        )
    )
    storage.close()

    with sqlite3.connect(db_path) as conn:
        conversation_rows = conn.execute(
            "SELECT session_id, CAST(content AS BLOB) FROM messages ORDER BY seq"
        ).fetchall()
        conn.execute("DELETE FROM chunks_fts_data")
        conn.commit()
        assert "fts5" in conn.execute("PRAGMA integrity_check").fetchone()[0]

    repaired = MemoryStorage(db_path)
    repaired.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT session_id, CAST(content AS BLOB) FROM messages ORDER BY seq"
        ).fetchall() == conversation_rows
        conn.execute(
            "INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)"
        )
        assert conn.execute(
            "SELECT id FROM chunks_fts WHERE chunks_fts MATCH 'alpha'"
        ).fetchall() == [("memory-1",)]
    assert not list(tmp_path.glob("index.db.corrupt-*"))


def test_unreadable_database_is_quarantined_instead_of_deleted(tmp_path):
    db_path = tmp_path / "index.db"
    original_bytes = b"not-a-sqlite-database"
    db_path.write_bytes(original_bytes)

    storage = MemoryStorage(db_path)
    storage.close()

    quarantined = list(tmp_path.glob("index.db.corrupt-*"))
    backups = [path for path in quarantined if not path.name.endswith(("-wal", "-shm"))]
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone() == (1,)


def test_quarantine_keeps_sqlite_sidecars_paired_with_backup(tmp_path):
    db_path = tmp_path / "index.db"
    originals = {
        db_path: b"database",
        Path(f"{db_path}-wal"): b"uncheckpointed-history",
        Path(f"{db_path}-shm"): b"shared-memory-state",
    }
    for path, content in originals.items():
        path.write_bytes(content)

    storage = MemoryStorage.__new__(MemoryStorage)
    storage.db_path = db_path
    storage.conn = None
    storage._quarantine_and_recreate()
    storage.conn.close()

    backups = [
        path
        for path in tmp_path.glob("index.db.corrupt-*")
        if not path.name.endswith(("-wal", "-shm"))
    ]
    assert len(backups) == 1
    assert backups[0].read_bytes() == originals[db_path]
    assert Path(f"{backups[0]}-wal").read_bytes() == originals[Path(f"{db_path}-wal")]
    assert Path(f"{backups[0]}-shm").read_bytes() == originals[Path(f"{db_path}-shm")]


def test_failed_quarantine_move_restores_original_set(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    originals = {
        db_path: b"database",
        Path(f"{db_path}-wal"): b"wal",
        Path(f"{db_path}-shm"): b"shm",
    }
    for path, content in originals.items():
        path.write_bytes(content)

    storage = MemoryStorage.__new__(MemoryStorage)
    storage.db_path = db_path
    storage.conn = None
    original_replace = os.replace

    def fail_wal(source, destination):
        if str(source).endswith("-wal"):
            raise OSError("simulated move failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_wal)

    with pytest.raises(OSError, match="simulated move failure"):
        storage._quarantine_and_recreate()

    for path, content in originals.items():
        assert path.read_bytes() == content
    assert not list(tmp_path.glob("index.db.corrupt-*"))


@pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
def test_transient_integrity_failure_never_quarantines_database(
    tmp_path, monkeypatch, message
):
    db_path = tmp_path / "index.db"
    original_bytes = b"conversation-history"
    db_path.write_bytes(original_bytes)

    class _TransientFailureConnection:
        def execute(self, _sql):
            raise sqlite3.OperationalError(message)

        def commit(self):
            pass

        def close(self):
            pass

    storage = MemoryStorage.__new__(MemoryStorage)
    storage.db_path = db_path
    storage.conn = _TransientFailureConnection()
    storage._fts5_needs_rebuild = False
    storage._trigram_needs_rebuild = False
    monkeypatch.setattr(
        storage,
        "_quarantine_and_recreate",
        lambda: pytest.fail("transient failures must not quarantine the shared DB"),
    )

    storage._check_integrity()

    assert db_path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("index.db.corrupt-*"))

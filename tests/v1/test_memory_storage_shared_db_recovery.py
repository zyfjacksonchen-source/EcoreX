from __future__ import annotations

import sqlite3

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.storage import MemoryChunk, MemoryStorage


def test_fts_damage_rebuild_preserves_shared_conversation_rows(tmp_path, monkeypatch):
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

    original_connect = sqlite3.connect
    with original_connect(db_path) as conn:
        conversation_rows = conn.execute(
            "SELECT session_id, CAST(content AS BLOB) FROM messages ORDER BY seq"
        ).fetchall()
        conn.execute(
            "DELETE FROM chunks_fts WHERE rowid = "
            "(SELECT rowid FROM chunks WHERE id = 'memory-1')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rank) "
                "VALUES('integrity-check', 1)"
            )

    class _IntegrityCursor:
        row = ("database disk image is malformed in fts5 table chunks_fts",)

        def fetchone(self):
            return self.row

        def fetchall(self):
            return [self.row]

    class _ConnectionProxy:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

        def execute(self, sql, *args, **kwargs):
            if sql.strip().lower() == "pragma integrity_check":
                return _IntegrityCursor()
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def connect(database, *args, **kwargs):
        conn = original_connect(database, *args, **kwargs)
        if str(database) == str(db_path):
            return _ConnectionProxy(conn)
        return conn

    with monkeypatch.context() as patcher:
        patcher.setattr(sqlite3, "connect", connect)
        repaired = MemoryStorage(db_path)
        repaired.close()

    with original_connect(db_path) as conn:
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
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original_bytes
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone() == (1,)


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

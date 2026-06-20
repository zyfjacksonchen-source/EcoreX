"""Durable run/job ledger for production agent runtime state.

The cancel registry is intentionally in-process; this ledger is the durable
counterpart used by Web/Desktop recovery surfaces to understand what happened
to a request after UI refreshes, SSE reconnects, worker exceptions, or runtime
restarts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger


RUN_STATUS_ACTIVE = {"queued", "running", "cancelling", "finalizing", "recovering"}
RUN_STATUS_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


_DDL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_id TEXT,
    run_type TEXT NOT NULL DEFAULT 'message',
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    terminal_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    model TEXT,
    provider TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL,
    terminal_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session_status
    ON agent_runs(session_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_updated_at
    ON agent_runs(updated_at);
"""


class RunLedger:
    """Small SQLite-backed ledger for request/job lifecycle state."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._init_db()

    def create_run(
        self,
        request_id: str,
        session_id: str,
        *,
        run_type: str = "message",
        parent_id: str = "",
        phase: str = "accepted",
        status: str = "running",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not request_id or not session_id:
            return
        now = time.time()
        payload = self._json(metadata or {})
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    request_id, session_id, parent_id, run_type, status, phase,
                    created_at, started_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    parent_id=excluded.parent_id,
                    run_type=excluded.run_type,
                    status=excluded.status,
                    phase=excluded.phase,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    request_id,
                    session_id,
                    parent_id or None,
                    run_type or "message",
                    status,
                    phase or "",
                    now,
                    now,
                    now,
                    payload,
                ),
            )
            conn.commit()

    def mark_phase(self, request_id: str, phase: str, *, status: Optional[str] = None) -> None:
        if not request_id or not phase:
            return
        now = time.time()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT terminal_at, phase, status FROM agent_runs WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row or row["terminal_at"] is not None:
                return
            next_status = status or row["status"] or "running"
            if row["phase"] == phase and row["status"] == next_status:
                return
            conn.execute(
                "UPDATE agent_runs SET phase=?, status=?, updated_at=? WHERE request_id=?",
                (phase, next_status, now, request_id),
            )
            conn.commit()

    def mark_cancelling(self, request_id: str) -> None:
        self.mark_phase(request_id, "cancelling", status="cancelling")

    def mark_terminal(
        self,
        request_id: str,
        status: str,
        *,
        reason: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if not request_id or status not in RUN_STATUS_TERMINAL:
            return
        now = time.time()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT terminal_at FROM agent_runs WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                return
            if row["terminal_at"] is not None:
                return
            conn.execute(
                """
                UPDATE agent_runs
                   SET status=?,
                       phase=?,
                       terminal_reason=?,
                       error_code=?,
                       error_message=?,
                       updated_at=?,
                       terminal_at=?
                 WHERE request_id=?
                """,
                (
                    status,
                    status,
                    reason or status,
                    error_code or None,
                    error_message or None,
                    now,
                    now,
                    request_id,
                ),
            )
            conn.commit()

    def get_run(self, request_id: str) -> Optional[Dict[str, Any]]:
        if not request_id:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE request_id=?",
                (request_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def active_snapshot(self, *, max_age_seconds: int = 60 * 60 * 24) -> List[Dict[str, Any]]:
        cutoff = time.time() - max(1, max_age_seconds)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_runs
                 WHERE terminal_at IS NULL
                   AND updated_at >= ?
                 ORDER BY updated_at ASC
                """,
                (cutoff,),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(_DDL)
            conn.commit()

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _json(value: Dict[str, Any]) -> str:
        try:
            return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            return "{}"

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        raw_metadata = item.pop("metadata_json", "") or "{}"
        try:
            item["metadata"] = json.loads(raw_metadata)
        except Exception:
            item["metadata"] = {}
        item["cancelled"] = item.get("status") in {"cancelling", "cancelled"}
        item["state"] = item.get("status") or ""
        now = time.time()
        created_at = float(item.get("created_at") or now)
        item["age_seconds"] = max(0, round(now - created_at, 3))
        return item


_ledger_instance: Optional[RunLedger] = None
_ledger_lock = threading.Lock()


def _default_db_path() -> Path:
    try:
        from agent.memory.config import get_default_memory_config

        return get_default_memory_config().get_db_path()
    except Exception:
        from common.utils import expand_path

        return Path(expand_path("~/cow")) / "memory" / "long-term" / "index.db"


def get_run_ledger() -> RunLedger:
    global _ledger_instance
    if _ledger_instance is not None:
        return _ledger_instance
    with _ledger_lock:
        if _ledger_instance is None:
            _ledger_instance = RunLedger(_default_db_path())
            logger.debug(f"[RunLedger] Using shared DB at: {_ledger_instance._db_path}")
        return _ledger_instance


def reset_run_ledger_for_tests(db_path: Optional[Path] = None) -> RunLedger:
    global _ledger_instance
    with _ledger_lock:
        _ledger_instance = RunLedger(Path(db_path) if db_path is not None else _default_db_path())
        return _ledger_instance

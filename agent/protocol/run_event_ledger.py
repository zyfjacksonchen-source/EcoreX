"""Durable append-only runtime event ledger.

This is the v0.2.2 counterpart to ``RunLedger``. ``RunLedger`` keeps the
current lifecycle snapshot; this module stores the replayable event stream that
can rebuild a running turn after refresh, reconnect, or process restart.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from common.ecorex_public_payload import mask_sensitive_text
from common.log import logger


_DDL = """
CREATE TABLE IF NOT EXISTS agent_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'runtime',
    created_at REAL NOT NULL,
    UNIQUE(idempotency_key),
    UNIQUE(request_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_events_request
    ON agent_run_events(request_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_agent_run_events_session
    ON agent_run_events(session_id, event_id);
CREATE INDEX IF NOT EXISTS idx_agent_run_events_type
    ON agent_run_events(event_type, created_at);
"""


_SENSITIVE_KEY_EXACT = {
    "access_token",
    "app_id",
    "appid",
    "api_key",
    "apikey",
    "authorization",
    "bot_open_id",
    "chat_id",
    "client_id",
    "client_secret",
    "client_secret",
    "cookie",
    "feishu_app_id",
    "feishu_home_channel",
    "home_channel",
    "id_token",
    "message_id",
    "open_chat_id",
    "open_id",
    "password",
    "private_key",
    "qr_image",
    "qr_url",
    "qrcode_url",
    "receive_id",
    "refresh_token",
    "secret",
    "session_token",
    "set_cookie",
    "tenant_access_token",
    "token",
    "union_id",
    "user_access_token",
    "verification_uri",
    "verification_url",
}

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "app_id",
    "authorization",
    "chat_id",
    "client_id",
    "cookie",
    "home_channel",
    "open_id",
    "password",
    "private_key",
    "qrcode",
    "qr_",
    "secret",
    "tenant_access",
    "verification_",
)

_SENSITIVE_VALUE_INDICATOR_KEYS = {"env", "key", "name", "variable"}
_SENSITIVE_VALUE_KEYS = {"value", "raw_value"}
_DEFAULT_SQLITE_TIMEOUT_SECONDS = 0.25
_TEXT_REDACTION_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{8,})\b"),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*=\s*)([^\s\"']+)"
    ),
    re.compile(
        r"(?i)(--(?:api[_-]?key|token|secret|password)\s+)([^\s\"']+)"
    ),
    re.compile(
        r"(?i)\b((?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[\"']?)([^\"'\s,;}{]+)"
    ),
    re.compile(r"\b(cli_[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"\b((?:ou|oc|om)_[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"(?i)(https://open\.feishu\.cn/[^\s\"')<>]+)"),
    re.compile(r"(?i)(data:image/(?:png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]{32,})"),
)


class RunEventLedger:
    """SQLite-backed append-only event stream for agent runtime events."""

    def __init__(self, db_path: Path, *, timeout_seconds: Optional[float] = None):
        self._db_path = Path(db_path)
        self._timeout_seconds = _coerce_timeout_seconds(timeout_seconds)
        self._lock = threading.RLock()
        self._init_db()

    def append_event(
        self,
        *,
        request_id: str,
        event_type: str,
        session_id: str = "",
        turn_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
        idempotency_key: str = "",
        source: str = "runtime",
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Append one event or return the existing row for its idempotency key."""
        request_id = str(request_id or "").strip()
        event_type = str(event_type or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        if not event_type:
            raise ValueError("event_type is required")
        session_id = str(session_id or "").strip()
        turn_id = str(turn_id or "").strip()
        source = str(source or "runtime").strip() or "runtime"
        now = float(created_at if created_at is not None else time.time())
        payload_json = self._json(payload or {})

        with self._lock, self._connection() as conn:
            final_key = idempotency_key
            try:
                conn.execute("BEGIN IMMEDIATE")
                owner_session_id = self._request_owner_session_id(conn, request_id)
                if owner_session_id and session_id and session_id != owner_session_id:
                    raise RunEventOwnerConflict()
                if owner_session_id and not session_id:
                    session_id = owner_session_id
                if idempotency_key:
                    existing = self._row_by_idempotency_key(conn, idempotency_key)
                    if existing:
                        existing_session_id = str(existing.get("session_id") or "").strip()
                        if existing_session_id and session_id and existing_session_id != session_id:
                            raise RunEventOwnerConflict()
                        if self._idempotency_conflict(existing, event_type, payload_json, source):
                            self._append_idempotency_conflict(
                                conn,
                                existing=existing,
                                request_id=request_id,
                                session_id=session_id,
                                turn_id=turn_id,
                                event_type=event_type,
                                payload_json=payload_json,
                                idempotency_key=idempotency_key,
                                source=source,
                                created_at=now,
                            )
                            existing["idempotency_conflict"] = True
                        conn.commit()
                        return existing

                next_seq = self._next_event_seq(conn, request_id)
                final_key = idempotency_key or f"{request_id}:{next_seq}:{event_type}"
                conn.execute(
                    """
                    INSERT INTO agent_run_events (
                        request_id, session_id, turn_id, event_seq, event_type,
                        payload_json, idempotency_key, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        turn_id,
                        next_seq,
                        event_type,
                        payload_json,
                        final_key,
                        source,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = self._row_by_idempotency_key(conn, final_key)
                if existing:
                    return existing
                raise
            except Exception:
                conn.rollback()
                raise

            row = conn.execute(
                "SELECT * FROM agent_run_events WHERE idempotency_key=?",
                (final_key,),
            ).fetchone()
            return self._row_to_dict(row)

    def list_events(
        self,
        *,
        after_event_id: int = 0,
        request_id: str = "",
        session_id: str = "",
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return ordered events after a global event cursor."""
        raw_limit = int(limit or 0)
        unlimited = raw_limit <= 0
        capped_limit = min(max(1, raw_limit), 5000) if not unlimited else 0
        clauses = ["event_id > ?"]
        params: List[Any] = [int(after_event_id or 0)]
        if request_id:
            clauses.append("request_id = ?")
            params.append(str(request_id))
        if session_id:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if not unlimited:
            params.append(capped_limit)
        sql = f"""
            SELECT * FROM agent_run_events
             WHERE {' AND '.join(clauses)}
             ORDER BY event_id ASC
        """
        if not unlimited:
            sql += " LIMIT ?"
        with self._lock, self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def events_for_request(self, request_id: str, *, limit: int = 5000) -> List[Dict[str, Any]]:
        return self.list_events(request_id=str(request_id or ""), limit=limit)

    def events_for_requests(self, request_ids: Iterable[str], *, limit: int = 0) -> Dict[str, List[Dict[str, Any]]]:
        """Return ordered events grouped by request id.

        This preserves ``events_for_request(..., limit=0)`` semantics while
        avoiding a query per request during session projection replay.
        """
        ordered_ids: List[str] = []
        seen = set()
        for value in request_ids or []:
            request_id = str(value or "").strip()
            if not request_id or request_id in seen:
                continue
            seen.add(request_id)
            ordered_ids.append(request_id)
        grouped: Dict[str, List[Dict[str, Any]]] = {request_id: [] for request_id in ordered_ids}
        if not ordered_ids:
            return grouped
        raw_limit = int(limit or 0)
        unlimited = raw_limit <= 0
        if not unlimited:
            for request_id in ordered_ids:
                grouped[request_id] = self.events_for_request(request_id, limit=raw_limit)
            return grouped
        with self._lock, self._connection() as conn:
            for start in range(0, len(ordered_ids), 400):
                chunk = ordered_ids[start:start + 400]
                placeholders = ",".join("?" for _ in chunk)
                params: List[Any] = list(chunk)
                sql = f"""
                    SELECT * FROM agent_run_events
                     WHERE request_id IN ({placeholders})
                     ORDER BY event_id ASC
                """
                rows = conn.execute(sql, tuple(params)).fetchall()
                for row in rows:
                    event = self._row_to_dict(row)
                    request_id = str(event.get("request_id") or "")
                    if request_id in grouped:
                        grouped[request_id].append(event)
        return grouped

    def owner_session_id_for_request(self, request_id: str) -> str:
        request_id = str(request_id or "").strip()
        if not request_id:
            return ""
        with self._lock, self._connection() as conn:
            return self._request_owner_session_id(conn, request_id)

    def latest_event_id_for_request(self, request_id: str) -> int:
        request_id = str(request_id or "").strip()
        if not request_id:
            return 0
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM agent_run_events WHERE request_id=?",
                (request_id,),
            ).fetchone()
            return int(row[0] or 0)

    def latest_event_id_for_session(self, session_id: str) -> int:
        session_id = str(session_id or "").strip()
        if not session_id:
            return 0
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM agent_run_events WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return int(row[0] or 0)

    def latest_event_for_image_job(self, job_id: str, *, limit: int = 1000) -> Optional[Dict[str, Any]]:
        """Return the latest image-job event row whose sanitized payload references job_id."""
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        with self._lock, self._connection() as conn:
            try:
                row = conn.execute(
                    """
                    SELECT * FROM agent_run_events
                     WHERE event_type LIKE 'image_job.%'
                       AND json_extract(payload_json, '$.job_id') = ?
                     ORDER BY event_id DESC
                     LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                return self._row_to_dict(row) if row else None
            except sqlite3.OperationalError:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_run_events
                     WHERE event_type LIKE 'image_job.%'
                       AND payload_json LIKE ?
                     ORDER BY event_id DESC
                    """,
                    (f"%{job_id}%",),
                ).fetchall()
        for row in rows:
            item = self._row_to_dict(row)
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if str(payload.get("job_id") or "") == job_id:
                return item
        return None

    def latest_event_id(self) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM agent_run_events").fetchone()
            return int(row[0] or 0)

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
        conn = sqlite3.connect(str(self._db_path), timeout=self._timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _next_event_seq(conn: sqlite3.Connection, request_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM agent_run_events WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return int(row[0] or 0) + 1

    @staticmethod
    def _request_owner_session_id(conn: sqlite3.Connection, request_id: str) -> str:
        try:
            row = conn.execute(
                "SELECT session_id FROM agent_runs WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row and str(row["session_id"] or "").strip():
                return str(row["session_id"] or "").strip()
        except sqlite3.OperationalError:
            pass
        row = conn.execute(
            """
            SELECT session_id FROM agent_run_events
             WHERE request_id=? AND session_id != ''
             ORDER BY event_id ASC
             LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        return str(row["session_id"] or "").strip() if row else ""

    @staticmethod
    def _row_by_idempotency_key(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM agent_run_events WHERE idempotency_key=?",
            (str(key),),
        ).fetchone()
        return RunEventLedger._row_to_dict(row) if row else None

    @staticmethod
    def _json(value: Dict[str, Any]) -> str:
        try:
            safe_value = RunEventLedger._json_safe(value or {})
            return json.dumps(safe_value or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            return "{}"

    @staticmethod
    def _json_safe(value: Any, *, key: str = "", depth: int = 0) -> Any:
        if _looks_sensitive_key(key):
            return "[redacted]"
        if depth > 12:
            return _redact_sensitive_text(str(value)[:500])
        if isinstance(value, dict):
            value_indicator_is_sensitive = any(
                _looks_sensitive_key(str(value.get(indicator_key) or ""))
                for indicator_key in _SENSITIVE_VALUE_INDICATOR_KEYS
            )
            return {
                str(item_key): (
                    "[redacted]"
                    if value_indicator_is_sensitive and str(item_key).lower() in _SENSITIVE_VALUE_KEYS
                    else RunEventLedger._json_safe(item_value, key=str(item_key), depth=depth + 1)
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [RunEventLedger._json_safe(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            return _redact_sensitive_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return _redact_sensitive_text(str(value))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        raw_payload = item.pop("payload_json", "") or "{}"
        try:
            item["payload"] = json.loads(raw_payload)
        except Exception:
            item["payload"] = {}
        return item

    @staticmethod
    def _idempotency_conflict(
        existing: Dict[str, Any],
        event_type: str,
        payload_json: str,
        source: str,
    ) -> bool:
        if str(existing.get("event_type") or "") != str(event_type or ""):
            return True
        if str(existing.get("source") or "") != str(source or "runtime"):
            return True
        try:
            existing_payload_json = json.dumps(existing.get("payload") or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            existing_payload_json = "{}"
        return existing_payload_json != payload_json

    def _append_idempotency_conflict(
        self,
        conn: sqlite3.Connection,
        *,
        existing: Dict[str, Any],
        request_id: str,
        session_id: str,
        turn_id: str,
        event_type: str,
        payload_json: str,
        idempotency_key: str,
        source: str,
        created_at: float,
    ) -> None:
        attempted_hash = hashlib.sha256(
            f"{event_type}|{source}|{payload_json}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        conflict_key = f"ledger-conflict:{idempotency_key}:{attempted_hash}"
        if self._row_by_idempotency_key(conn, conflict_key):
            return
        conflict_payload = {
            "conflicting_idempotency_key": idempotency_key,
            "existing_event_id": existing.get("event_id"),
            "existing_event_type": existing.get("event_type"),
            "existing_source": existing.get("source"),
            "attempted_event_type": event_type,
            "attempted_source": source,
            "attempted_payload_sha256": hashlib.sha256(
                payload_json.encode("utf-8", errors="replace")
            ).hexdigest(),
        }
        logger.warning(
            "[RunEventLedger] idempotency conflict for %s existing_event_id=%s attempted_type=%s",
            idempotency_key,
            existing.get("event_id"),
            event_type,
        )
        conn.execute(
            """
            INSERT INTO agent_run_events (
                request_id, session_id, turn_id, event_seq, event_type,
                payload_json, idempotency_key, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                str(session_id or existing.get("session_id") or ""),
                str(turn_id or existing.get("turn_id") or ""),
                self._next_event_seq(conn, request_id),
                "ledger.idempotency_conflict",
                self._json(conflict_payload),
                conflict_key,
                "run_event_ledger",
                created_at,
            ),
        )


class RunEventOwnerConflict(ValueError):
    code = "RUN_EVENT_OWNER_CONFLICT"
    reason = "request_owner_mismatch"

    def __init__(self):
        super().__init__("run event request owner mismatch")


def _looks_sensitive_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_").replace(".", "_")
    if normalized in _SENSITIVE_KEY_EXACT:
        return True
    if normalized.endswith("_token") or normalized.endswith("_secret") or normalized.endswith("_api_key"):
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    value = mask_sensitive_text(value, max_chars=max(512, len(value)))
    for pattern in _TEXT_REDACTION_PATTERNS:
        value = pattern.sub(
            lambda match: f"{match.group(1)}[redacted]" if (match.lastindex or 0) >= 2 else "[redacted]",
            value,
        )
    return value


def _coerce_timeout_seconds(value: Optional[float]) -> float:
    if value is None:
        raw = os.environ.get("ECOREX_RUN_EVENT_LEDGER_SQLITE_TIMEOUT_SECONDS", "")
        try:
            value = float(raw) if raw else _DEFAULT_SQLITE_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            value = _DEFAULT_SQLITE_TIMEOUT_SECONDS
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = _DEFAULT_SQLITE_TIMEOUT_SECONDS
    return max(0.05, min(parsed, 10.0))


_event_ledger_instance: Optional[RunEventLedger] = None
_event_ledger_lock = threading.Lock()


def _default_db_path() -> Path:
    try:
        from agent.memory.config import get_default_memory_config

        return get_default_memory_config().get_db_path()
    except Exception:
        from common.utils import expand_path

        return Path(expand_path("~/cow")) / "memory" / "long-term" / "index.db"


def get_run_event_ledger() -> RunEventLedger:
    global _event_ledger_instance
    if _event_ledger_instance is not None:
        return _event_ledger_instance
    with _event_ledger_lock:
        if _event_ledger_instance is None:
            _event_ledger_instance = RunEventLedger(_default_db_path())
            logger.debug(f"[RunEventLedger] Using shared DB at: {_event_ledger_instance._db_path}")
        return _event_ledger_instance


def reset_run_event_ledger_for_tests(db_path: Optional[Path] = None) -> RunEventLedger:
    global _event_ledger_instance
    with _event_ledger_lock:
        _event_ledger_instance = RunEventLedger(Path(db_path) if db_path is not None else _default_db_path())
        return _event_ledger_instance

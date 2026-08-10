"""Transactional learned-memory reset with an explicit undo boundary.

Factory knowledge is immutable product content and is never selected by this
service. User-learned and imported canonical records are tombstoned first, so
a reset is atomic, auditable, restart-safe and reversible until its deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from collections.abc import Callable

from ecorex.runtime.database import SQLiteDatabase, json_dumps
from ecorex.runtime.ids import new_id

from .errors import (
    MemoryConflict,
    MemoryContentNotFound,
    MemoryContentUnavailable,
    MemoryResetNotFound,
    MemoryUndoExpired,
)


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("memory timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MemoryResetProjection:
    reset_id: str
    status: str
    affected_records: int
    affected_files: int
    created_at: datetime
    undo_until: datetime
    updated_at: datetime
    can_undo: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "reset_id": self.reset_id,
            "status": self.status,
            "affected_records": self.affected_records,
            "affected_files": self.affected_files,
            "created_at": _time(self.created_at),
            "undo_until": _time(self.undo_until),
            "updated_at": _time(self.updated_at),
            "can_undo": self.can_undo,
        }


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    revision: int
    active_learned_records: int
    active_user_files: int
    factory_records: int
    tombstoned_records: int
    tombstoned_files: int
    latest_reset: MemoryResetProjection | None

    @property
    def resettable_count(self) -> int:
        return self.active_learned_records + self.active_user_files

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "active_learned_records": self.active_learned_records,
            "active_user_files": self.active_user_files,
            "factory_records": self.factory_records,
            "tombstoned_records": self.tombstoned_records,
            "tombstoned_files": self.tombstoned_files,
            "resettable_count": self.resettable_count,
            "latest_reset": self.latest_reset.to_dict() if self.latest_reset else None,
        }


@dataclass(frozen=True, slots=True)
class MemoryContentItem:
    item_id: str
    name: str
    path: str
    kind: str
    origin: str
    source: str
    size_bytes: int
    updated_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "origin": self.origin,
            "source": self.source,
            "size_bytes": self.size_bytes,
            "updated_at": _time(self.updated_at) if self.updated_at else None,
        }


@dataclass(frozen=True, slots=True)
class MemoryContentPage:
    view: str
    page: int
    page_size: int
    total: int
    items: tuple[MemoryContentItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class MemoryContentDocument:
    item: MemoryContentItem
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.item.to_dict(), "content": self.content}


class MemoryService:
    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        undo_window: timedelta = timedelta(hours=24),
        clock=_now,
        fault_hook: Callable[[str, str], None] | None = None,
        blob_loader: Callable[[str], bytes] | None = None,
        initialize: bool = True,
    ) -> None:
        if not timedelta(minutes=1) <= undo_window <= timedelta(days=30):
            raise ValueError("memory undo window must be between one minute and 30 days")
        self.database = database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        self.undo_window = undo_window
        self.clock = clock
        self.fault_hook = fault_hook or (lambda _phase, _reset_id: None)
        self.blob_loader = blob_loader
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        """Persist the revision sentinel during healthy startup convergence."""

        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_meta(key,value) VALUES('revision','0') "
                "ON CONFLICT(key) DO NOTHING"
            )

    def converge_startup(self) -> None:
        """Alias used by the healthy startup convergence coordinator."""

        self.initialize()

    @staticmethod
    def _request_fingerprint(operation: str, target: str) -> str:
        return hashlib.sha256(
            json_dumps({"operation": operation, "target": target, "confirmed": True}).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _validate_request_id(client_request_id: str) -> str:
        value = str(client_request_id or "").strip()
        if not _REQUEST_ID.fullmatch(value):
            raise ValueError("memory client_request_id is invalid")
        return value

    @staticmethod
    def _existing_request(
        connection: sqlite3.Connection,
        client_request_id: str,
        *,
        operation: str,
        fingerprint: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT operation,target_id,request_sha256 FROM memory_mutation_requests "
            "WHERE client_request_id=?",
            (client_request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_sha256"] != fingerprint:
            raise MemoryConflict("memory request ID was reused with different intent")
        return str(row["target_id"])

    @staticmethod
    def _revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM memory_meta WHERE key='revision'"
        ).fetchone()
        return int(row["value"] if row else 0)

    @classmethod
    def _advance_revision(cls, connection: sqlite3.Connection) -> int:
        revision = cls._revision(connection) + 1
        connection.execute(
            "INSERT INTO memory_meta(key,value) VALUES('revision',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(revision),),
        )
        return revision

    @staticmethod
    def _batch(row: sqlite3.Row, *, now: datetime) -> MemoryResetProjection:
        undo_until = _parse_time(str(row["undo_until"]))
        status = str(row["status"])
        return MemoryResetProjection(
            reset_id=str(row["reset_id"]),
            status=status,
            affected_records=int(row["affected_records"]),
            affected_files=int(row["affected_files"]),
            created_at=_parse_time(str(row["created_at"])),
            undo_until=undo_until,
            updated_at=_parse_time(str(row["updated_at"])),
            can_undo=status == "active" and now <= undo_until,
        )

    def snapshot(self) -> MemorySnapshot:
        now = self.clock()
        with self.database.reader() as connection:
            counts = connection.execute(
                "SELECT "
                "SUM(CASE WHEN memory_state='active' AND memory_origin!='factory' THEN 1 ELSE 0 END) learned,"
                "SUM(CASE WHEN memory_state='active' AND memory_origin='factory' THEN 1 ELSE 0 END) factory,"
                "SUM(CASE WHEN memory_state='tombstoned' THEN 1 ELSE 0 END) tombstoned "
                "FROM memory_canonical_records"
            ).fetchone()
            files = connection.execute(
                "SELECT "
                "SUM(CASE WHEN memory_state='active' AND memory_origin!='factory' THEN 1 ELSE 0 END) active_user,"
                "SUM(CASE WHEN memory_state='tombstoned' THEN 1 ELSE 0 END) tombstoned "
                "FROM memory_files"
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM memory_reset_batches ORDER BY created_at DESC,reset_id DESC LIMIT 1"
            ).fetchone()
            return MemorySnapshot(
                revision=self._revision(connection),
                active_learned_records=int(counts["learned"] or 0),
                active_user_files=int(files["active_user"] or 0),
                factory_records=int(counts["factory"] or 0),
                tombstoned_records=int(counts["tombstoned"] or 0),
                tombstoned_files=int(files["tombstoned"] or 0),
                latest_reset=self._batch(latest, now=now) if latest else None,
            )

    @staticmethod
    def _content_time(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if number <= 0:
                return None
            if number > 10_000_000_000:
                number /= 1000
            try:
                return datetime.fromtimestamp(number, UTC)
            except (OSError, OverflowError, ValueError):
                return None
        try:
            return _parse_time(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _content_text(content: bytes) -> str:
        if len(content) > 10 * 1024 * 1024:
            raise MemoryContentUnavailable("memory content exceeds its read boundary")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MemoryContentUnavailable("memory content is not UTF-8") from error
        if "\x00" in text:
            raise MemoryContentUnavailable("memory content contains NUL bytes")
        return text

    @staticmethod
    def _content_label(path: str, *, prefix: str) -> tuple[str, str]:
        parts = [
            part
            for part in str(path or "").replace("\\", "/").split("/")
            if part not in {"", ".", ".."}
        ]
        raw_name = parts[-1] if parts else "记忆.md"
        name = "".join(
            character for character in raw_name if character >= " " and character != "\x7f"
        )[:255] or "记忆.md"
        return name, f"{prefix}/{name}"

    @staticmethod
    def _file_item(row: sqlite3.Row) -> MemoryContentItem:
        authority_path = str(row["path"])
        name, path = MemoryService._content_label(authority_path, prefix="memory")
        return MemoryContentItem(
            item_id="memfile_" + hashlib.sha256(authority_path.encode("utf-8")).hexdigest(),
            name=name,
            path=path,
            kind="file",
            origin=str(row["memory_origin"]),
            source=str(row["memory_origin"]),
            size_bytes=int(row["size_bytes"]),
            updated_at=MemoryService._content_time(row["updated_at"] or row["mtime"]),
        )

    @staticmethod
    def _record_item(row: sqlite3.Row) -> MemoryContentItem:
        name, path = MemoryService._content_label(str(row["path"]), prefix="evolution")
        text = str(row["text"])
        return MemoryContentItem(
            item_id=str(row["record_id"]),
            name=name,
            path=path,
            kind="evolution",
            origin=str(row["memory_origin"]),
            source=str(row["memory_origin"]),
            size_bytes=len(text.encode("utf-8")),
            updated_at=MemoryService._content_time(row["updated_at"] or row["created_at"]),
        )

    def content_page(self, *, view: str, page: int, page_size: int = 10) -> MemoryContentPage:
        if view not in {"files", "evolution"}:
            raise ValueError("memory view is invalid")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 1_000_000:
            raise ValueError("memory page is invalid")
        if page_size != 10:
            raise ValueError("memory page size is fixed at 10")
        offset = (page - 1) * page_size
        with self.database.reader() as connection:
            if view == "files":
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_files WHERE memory_state='active'"
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT * FROM memory_files WHERE memory_state='active' "
                    "ORDER BY COALESCE(updated_at,mtime) DESC,path LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
                items = tuple(self._file_item(row) for row in rows)
            else:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_canonical_records WHERE memory_state='active'"
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT * FROM memory_canonical_records WHERE memory_state='active' "
                    "ORDER BY COALESCE(updated_at,created_at) DESC,record_id LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
                items = tuple(self._record_item(row) for row in rows)
        return MemoryContentPage(
            view=view,
            page=page,
            page_size=page_size,
            total=total,
            items=items,
        )

    def content_document(self, *, view: str, item_id: str) -> MemoryContentDocument:
        identity = str(item_id or "").strip()
        if view not in {"files", "evolution"} or not identity or len(identity) > 8192:
            raise ValueError("memory content identity is invalid")
        with self.database.reader() as connection:
            if view == "files":
                row = next(
                    (
                        candidate
                        for candidate in connection.execute(
                            "SELECT * FROM memory_files WHERE memory_state='active' ORDER BY path"
                        ).fetchall()
                        if hmac.compare_digest(self._file_item(candidate).item_id, identity)
                    ),
                    None,
                )
                if row is None:
                    raise MemoryContentNotFound("memory file does not exist")
                if (
                    row["availability"] != "stored"
                    or not row["blob_sha256"]
                    or self.blob_loader is None
                ):
                    raise MemoryContentUnavailable("memory file content is unavailable")
                try:
                    digest = str(row["blob_sha256"])
                    payload = self.blob_loader(digest)
                    if (
                        len(payload) != int(row["size_bytes"])
                        or hashlib.sha256(payload).hexdigest() != digest
                    ):
                        raise MemoryContentUnavailable("memory file content failed verification")
                    content = self._content_text(payload)
                except MemoryContentUnavailable:
                    raise
                except Exception as error:
                    raise MemoryContentUnavailable("memory file content is unavailable") from error
                item = self._file_item(row)
            else:
                row = connection.execute(
                    "SELECT * FROM memory_canonical_records "
                    "WHERE record_id=? AND memory_state='active'",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise MemoryContentNotFound("memory evolution record does not exist")
                content = self._content_text(str(row["text"]).encode("utf-8"))
                item = self._record_item(row)
        return MemoryContentDocument(item=item, content=content)

    def reset_learned(
        self,
        *,
        confirmed: bool,
        client_request_id: str,
    ) -> MemoryResetProjection:
        if confirmed is not True:
            raise ValueError("learned-memory reset requires explicit confirmation")
        request_id = self._validate_request_id(client_request_id)
        fingerprint = self._request_fingerprint("reset", "learned")
        now = self.clock()
        timestamp = _time(now)
        with self.database.transaction() as connection:
            existing_id = self._existing_request(
                connection, request_id, operation="reset", fingerprint=fingerprint
            )
            if existing_id is not None:
                row = connection.execute(
                    "SELECT * FROM memory_reset_batches WHERE reset_id=?", (existing_id,)
                ).fetchone()
                if row is None:
                    raise MemoryConflict("memory reset request references missing state")
                return self._batch(row, now=now)

            reset_id = new_id("memreset")
            undo_until = now + self.undo_window
            records = int(connection.execute(
                "SELECT COUNT(*) count FROM memory_canonical_records "
                "WHERE memory_state='active' AND memory_origin!='factory'"
            ).fetchone()["count"])
            files = int(connection.execute(
                "SELECT COUNT(*) count FROM memory_files "
                "WHERE memory_state='active' AND memory_origin!='factory'"
            ).fetchone()["count"])
            connection.execute(
                "INSERT INTO memory_reset_batches(reset_id,status,affected_records,affected_files,"
                "created_at,undo_until,updated_at) VALUES(?,'active',?,?,?,?,?)",
                (reset_id, records, files, timestamp, _time(undo_until), timestamp),
            )
            connection.execute(
                "UPDATE memory_canonical_records SET memory_state='tombstoned',reset_id=?,"
                "tombstoned_at=? WHERE memory_state='active' AND memory_origin!='factory'",
                (reset_id, timestamp),
            )
            self.fault_hook("after_records_tombstoned", reset_id)
            connection.execute(
                "UPDATE memory_files SET memory_state='tombstoned',reset_id=?,tombstoned_at=? "
                "WHERE memory_state='active' AND memory_origin!='factory'",
                (reset_id, timestamp),
            )
            self.fault_hook("after_files_tombstoned", reset_id)
            connection.execute(
                "INSERT INTO memory_mutation_requests(client_request_id,operation,target_id,"
                "request_sha256,created_at) VALUES(?,'reset',?,?,?)",
                (request_id, reset_id, fingerprint, timestamp),
            )
            revision = self._advance_revision(connection)
            connection.execute(
                "INSERT INTO memory_audit_events(event_id,event_type,reset_id,payload_json,created_at) "
                "VALUES(?, 'memory.learned_reset', ?, ?, ?)",
                (
                    new_id("memaudit"),
                    reset_id,
                    json_dumps({"affected_records": records, "affected_files": files, "revision": revision}),
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_reset_batches WHERE reset_id=?", (reset_id,)
            ).fetchone()
            assert row is not None
            return self._batch(row, now=now)

    def undo_reset(
        self,
        reset_id: str,
        *,
        confirmed: bool,
        client_request_id: str,
    ) -> MemoryResetProjection:
        if confirmed is not True:
            raise ValueError("memory reset undo requires explicit confirmation")
        reset_id = str(reset_id or "").strip()
        if not reset_id or len(reset_id) > 128:
            raise ValueError("memory reset ID is invalid")
        request_id = self._validate_request_id(client_request_id)
        fingerprint = self._request_fingerprint("undo", reset_id)
        now = self.clock()
        timestamp = _time(now)
        with self.database.transaction() as connection:
            existing_id = self._existing_request(
                connection, request_id, operation="undo", fingerprint=fingerprint
            )
            if existing_id is not None and existing_id != reset_id:
                raise MemoryConflict("memory undo request references another reset")
            row = connection.execute(
                "SELECT * FROM memory_reset_batches WHERE reset_id=?", (reset_id,)
            ).fetchone()
            if row is None:
                raise MemoryResetNotFound("memory reset does not exist")
            projection = self._batch(row, now=now)
            if existing_id is not None:
                return projection
            if projection.status == "purged" or now > projection.undo_until:
                raise MemoryUndoExpired("memory reset undo window has expired")
            if projection.status == "active":
                connection.execute(
                    "UPDATE memory_canonical_records SET memory_state='active',reset_id=NULL,"
                    "tombstoned_at=NULL WHERE memory_state='tombstoned' AND reset_id=?",
                    (reset_id,),
                )
                self.fault_hook("after_records_restored", reset_id)
                connection.execute(
                    "UPDATE memory_files SET memory_state='active',reset_id=NULL,tombstoned_at=NULL "
                    "WHERE memory_state='tombstoned' AND reset_id=?",
                    (reset_id,),
                )
                self.fault_hook("after_files_restored", reset_id)
                connection.execute(
                    "UPDATE memory_reset_batches SET status='undone',updated_at=? WHERE reset_id=?",
                    (timestamp, reset_id),
                )
                revision = self._advance_revision(connection)
                connection.execute(
                    "INSERT INTO memory_audit_events(event_id,event_type,reset_id,payload_json,created_at) "
                    "VALUES(?, 'memory.learned_reset_undone', ?, ?, ?)",
                    (new_id("memaudit"), reset_id, json_dumps({"revision": revision}), timestamp),
                )
            connection.execute(
                "INSERT INTO memory_mutation_requests(client_request_id,operation,target_id,"
                "request_sha256,created_at) VALUES(?,'undo',?,?,?)",
                (request_id, reset_id, fingerprint, timestamp),
            )
            updated = connection.execute(
                "SELECT * FROM memory_reset_batches WHERE reset_id=?", (reset_id,)
            ).fetchone()
            assert updated is not None
            return self._batch(updated, now=now)

    def purge_expired(self) -> int:
        now = self.clock()
        timestamp = _time(now)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT reset_id FROM memory_reset_batches "
                "WHERE status='active' AND undo_until < ? ORDER BY undo_until,reset_id",
                (timestamp,),
            ).fetchall()
            for row in rows:
                reset_id = str(row["reset_id"])
                connection.execute(
                    "DELETE FROM memory_canonical_records WHERE memory_state='tombstoned' AND reset_id=?",
                    (reset_id,),
                )
                connection.execute(
                    "DELETE FROM memory_files WHERE memory_state='tombstoned' AND reset_id=?",
                    (reset_id,),
                )
                connection.execute(
                    "UPDATE memory_reset_batches SET status='purged',updated_at=? WHERE reset_id=?",
                    (timestamp, reset_id),
                )
                revision = self._advance_revision(connection)
                connection.execute(
                    "INSERT INTO memory_audit_events(event_id,event_type,reset_id,payload_json,created_at) "
                    "VALUES(?, 'memory.learned_reset_purged', ?, ?, ?)",
                    (new_id("memaudit"), reset_id, json_dumps({"revision": revision}), timestamp),
                )
            return len(rows)


__all__ = [
    "MemoryContentDocument",
    "MemoryContentItem",
    "MemoryContentPage",
    "MemoryResetProjection",
    "MemoryService",
    "MemorySnapshot",
]

"""Read-only adapters for the real v0.3.0 SQLite and JSON layouts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from .errors import (
    DuplicateLegacyIdError,
    LegacyDatabaseError,
    LegacySchemaError,
    SourceChangedError,
    SourceLayoutError,
)
from .path_security import (
    secure_directory,
    secure_regular_file,
    stable_copy_file,
    stable_read_bytes,
    stable_sha256_file,
)


CONVERSATION_CANDIDATES = (
    "sessions/conversations.db",
    # Current v0.3.x reuses the long-term memory database for sessions/messages.
    "memory/long-term/index.db",
    "memory/conversations.db",
    "conversations.db",
)
MEMORY_INDEX_CANDIDATES = (
    "memory/long-term/index.db",
    "memory/index.db",
)
RELEASE_EVIDENCE_CANDIDATES = (
    ".ecorex/legacy-release.json",
    "release.json",
    "runtime-manifest.json",
    "runtime/runtime-manifest.json",
)
QUEUED_REQUESTS_DIRECTORY = ".ecorex/queued-requests"
SCHEDULER_TASKS_CANDIDATE = "scheduler/tasks.json"
V030_RELEASE_SCHEMA_COMMIT = "f0750d247bfe52ffb95c137cadc9983a03010690"
V030_LAST_HOTFIX_COMMIT = "9ac3b958a006e82bd53d8a26edf8e119110435d8"

_SAFE_LEGACY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_ACTIVE_RUN_STATUSES = {"queued", "running", "cancelling", "finalizing", "recovering"}


@dataclass(frozen=True, slots=True)
class LegacyWarning:
    code: str
    subject: str
    detail: str


@dataclass(frozen=True, slots=True)
class LegacyConversations:
    sessions: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    warnings: tuple[LegacyWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyMemory:
    chunks: tuple[dict[str, Any], ...]
    files: tuple[dict[str, Any], ...]
    warnings: tuple[LegacyWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyRuntimeLedger:
    runs: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    warnings: tuple[LegacyWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyQueuedRequests:
    records: tuple[dict[str, Any], ...]
    warnings: tuple[LegacyWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacySchedulerTasks:
    tasks: tuple[dict[str, Any], ...]
    warnings: tuple[LegacyWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyReleaseEvidence:
    evidence_level: str
    marker_label: str | None
    marker_sha256: str | None
    declared_version: str | None
    declared_commit: str | None
    package_sha256: str | None
    schema_fingerprint: str
    schema_tables: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_level": self.evidence_level,
            "marker_label": self.marker_label,
            "marker_sha256": self.marker_sha256,
            "declared_version": self.declared_version,
            "declared_commit": self.declared_commit,
            "package_sha256": self.package_sha256,
            "schema_fingerprint": self.schema_fingerprint,
            "schema_tables": list(self.schema_tables),
            "baseline_release_schema_commit": V030_RELEASE_SCHEMA_COMMIT,
            "baseline_last_hotfix_commit": V030_LAST_HOTFIX_COMMIT,
            "asset_attested": False,
        }


def discover_existing(root: Path, candidates: Iterable[str]) -> Path | None:
    for relative in candidates:
        candidate = root / relative
        if not os.path.lexists(candidate):
            continue
        try:
            return secure_regular_file(
                candidate,
                label=f"legacy source file {relative}",
                root=root,
            )
        except SourceLayoutError as error:
            raise LegacySchemaError(
                f"legacy source file {relative} is unsafe"
            ) from error
    return None


def _read_only_uri(path: Path) -> str:
    normalized = path.resolve(strict=True).as_posix()
    return f"file:{quote(normalized, safe='/:')}?mode=ro"


def snapshot_sqlite(source: Path, destination: Path, *, subject: str) -> None:
    """Create a consistent backup without ever opening the source with SQLite.

    SQLite's read-only mode may still create or touch a WAL shared-memory file.
    The migration boundary therefore copies the database and WAL bytes into the
    disposable staging directory, proves that both stayed unchanged during the
    copy, and runs the SQLite backup API only against that private copy.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    raw_snapshot = destination.with_name(f".{destination.name}.source-copy")
    raw_wal = Path(str(raw_snapshot) + "-wal")
    raw_shm = Path(str(raw_snapshot) + "-shm")
    source_wal = Path(str(source) + "-wal")

    def source_state() -> tuple[tuple[str, int, str], ...]:
        state: list[tuple[str, int, str]] = []
        for label, path in (("database", source), ("wal", source_wal)):
            if not os.path.lexists(path):
                continue
            try:
                digest, identity = stable_sha256_file(
                    path,
                    label=f"{subject} {label}",
                )
            except (SourceLayoutError, SourceChangedError) as error:
                raise LegacyDatabaseError(
                    f"{subject} {label} is not a stable regular file"
                ) from error
            state.append((label, identity.size, digest))
        return tuple(state)

    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        before = source_state()
        stable_copy_file(source, raw_snapshot, label=f"{subject} database")
        if os.path.lexists(source_wal):
            stable_copy_file(source_wal, raw_wal, label=f"{subject} wal")
        after = source_state()
        if before != after:
            raise SourceChangedError(f"{subject} changed while its read-only snapshot was copied")
        source_connection = sqlite3.connect(
            _read_only_uri(raw_snapshot), uri=True, timeout=30.0, isolation_level=None
        )
        source_connection.execute("PRAGMA query_only = ON")
        check = [str(row[0]) for row in source_connection.execute("PRAGMA integrity_check")]
        if check != ["ok"]:
            raise LegacyDatabaseError(f"{subject} failed SQLite integrity_check")
        target_connection = sqlite3.connect(str(destination), isolation_level=None)
        source_connection.backup(target_connection)
        copied_check = [str(row[0]) for row in target_connection.execute("PRAGMA integrity_check")]
        if copied_check != ["ok"]:
            raise LegacyDatabaseError(f"{subject} backup failed SQLite integrity_check")
    except LegacyDatabaseError:
        raise
    except sqlite3.Error as error:
        raise LegacyDatabaseError(f"{subject} is not a readable SQLite database") from error
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        raw_shm.unlink(missing_ok=True)
        raw_wal.unlink(missing_ok=True)
        raw_snapshot.unlink(missing_ok=True)


def _connect_snapshot(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _require_schema(
    connection: sqlite3.Connection,
    *,
    table: str,
    required_columns: set[str],
    subject: str,
) -> set[str]:
    if table not in _table_names(connection):
        raise LegacySchemaError(f"{subject} is missing table {table}")
    columns = _columns(connection, table)
    missing = required_columns - columns
    if missing:
        raise LegacySchemaError(
            f"{subject} is missing {table} columns: {', '.join(sorted(missing))}"
        )
    return columns


def _row_dict(row: sqlite3.Row, columns: set[str], defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    for key in columns:
        result[key] = row[key]
    return result


def _row_fingerprint(row: dict[str, Any]) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _strict_float(value: Any, *, subject: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LegacyDatabaseError(f"{subject} must be numeric") from error
    if not math.isfinite(result):
        raise LegacyDatabaseError(f"{subject} must be finite")
    return result


def read_conversations(snapshot: Path) -> LegacyConversations:
    connection = _connect_snapshot(snapshot)
    try:
        session_columns = _require_schema(
            connection,
            table="sessions",
            required_columns={"session_id", "created_at", "last_active"},
            subject="legacy conversation database",
        )
        message_columns = _require_schema(
            connection,
            table="messages",
            required_columns={"session_id", "seq", "role", "content", "created_at"},
            subject="legacy conversation database",
        )

        session_defaults = {
            "channel_type": "",
            "title": "",
            "title_locked": 0,
            "context_start_seq": 0,
            "project_id": "",
            "project_name": "",
            "project_path": "",
            "project_memory_path": "",
            "project_dreams_path": "",
            "metadata_json": "",
            "msg_count": None,
        }
        message_defaults = {"extras": ""}

        warnings: list[LegacyWarning] = []
        sessions_by_id: dict[str, dict[str, Any]] = {}
        for raw in connection.execute("SELECT * FROM sessions"):
            row = _row_dict(raw, session_columns, session_defaults)
            session_id = str(row.get("session_id") or "").strip()
            if not session_id:
                raise LegacyDatabaseError("legacy session_id must not be empty")
            existing = sessions_by_id.get(session_id)
            if existing is not None:
                if _row_fingerprint(existing) != _row_fingerprint(row):
                    raise DuplicateLegacyIdError(
                        f"legacy session_id {session_id!r} has conflicting rows"
                    )
                warnings.append(
                    LegacyWarning("duplicate_exact_session", session_id, "exact duplicate coalesced")
                )
                continue
            sessions_by_id[session_id] = row

        messages_by_id: dict[tuple[str, int], dict[str, Any]] = {}
        for raw in connection.execute("SELECT * FROM messages"):
            row = _row_dict(raw, message_columns, message_defaults)
            session_id = str(row.get("session_id") or "").strip()
            try:
                sequence = int(row.get("seq"))
            except (TypeError, ValueError) as error:
                raise LegacyDatabaseError("legacy message seq must be an integer") from error
            if not session_id or sequence < 0:
                raise LegacyDatabaseError("legacy message identity is invalid")
            if session_id not in sessions_by_id:
                raise LegacyDatabaseError(
                    f"legacy message {session_id}:{sequence} has no parent session"
                )
            identity = (session_id, sequence)
            existing = messages_by_id.get(identity)
            if existing is not None:
                if _row_fingerprint(existing) != _row_fingerprint(row):
                    raise DuplicateLegacyIdError(
                        f"legacy message {session_id}:{sequence} has conflicting rows"
                    )
                warnings.append(
                    LegacyWarning(
                        "duplicate_exact_message",
                        f"{session_id}:{sequence}",
                        "exact duplicate coalesced",
                    )
                )
                continue
            messages_by_id[identity] = row

        actual_counts: dict[str, int] = {}
        for session_id, _sequence in messages_by_id:
            actual_counts[session_id] = actual_counts.get(session_id, 0) + 1
        for session_id, session in sessions_by_id.items():
            declared = session.get("msg_count")
            if declared is not None and _safe_int(declared) != actual_counts.get(session_id, 0):
                warnings.append(
                    LegacyWarning(
                        "message_count_recomputed",
                        session_id,
                        "declared message count did not match canonical rows",
                    )
                )

        sessions = tuple(
            sorted(
                sessions_by_id.values(),
                key=lambda row: (_safe_int(row.get("created_at")), str(row["session_id"])),
            )
        )
        messages = tuple(
            sorted(
                messages_by_id.values(),
                key=lambda row: (
                    str(row["session_id"]),
                    int(row["seq"]),
                    _safe_int(row.get("created_at")),
                ),
            )
        )
        return LegacyConversations(sessions=sessions, messages=messages, warnings=tuple(warnings))
    finally:
        connection.close()


def read_memory(snapshot: Path) -> LegacyMemory:
    connection = _connect_snapshot(snapshot)
    try:
        chunk_columns = _require_schema(
            connection,
            table="chunks",
            required_columns={"id", "path", "start_line", "end_line", "text", "hash"},
            subject="legacy memory index",
        )
        file_columns = _require_schema(
            connection,
            table="files",
            required_columns={"path", "hash", "mtime", "size"},
            subject="legacy memory index",
        )
        chunk_defaults = {
            "user_id": None,
            "scope": "shared",
            "source": "memory",
            "metadata": None,
            "created_at": None,
            "updated_at": None,
        }
        file_defaults = {"source": "memory", "updated_at": None}
        warnings: list[LegacyWarning] = []

        chunks_by_id: dict[str, dict[str, Any]] = {}
        for raw in connection.execute("SELECT * FROM chunks"):
            row = _row_dict(raw, chunk_columns, chunk_defaults)
            legacy_id = str(row.get("id") or "").strip()
            if not legacy_id:
                raise LegacyDatabaseError("legacy memory chunk id must not be empty")
            existing = chunks_by_id.get(legacy_id)
            if existing is not None:
                if _row_fingerprint(existing) != _row_fingerprint(row):
                    raise DuplicateLegacyIdError(
                        f"legacy memory chunk id {legacy_id!r} has conflicting rows"
                    )
                warnings.append(
                    LegacyWarning("duplicate_exact_memory_chunk", legacy_id, "exact duplicate coalesced")
                )
                continue
            chunks_by_id[legacy_id] = row

        files_by_path: dict[str, dict[str, Any]] = {}
        for raw in connection.execute("SELECT * FROM files"):
            row = _row_dict(raw, file_columns, file_defaults)
            legacy_path = str(row.get("path") or "").strip()
            if not legacy_path:
                raise LegacyDatabaseError("legacy memory file path must not be empty")
            existing = files_by_path.get(legacy_path)
            if existing is not None:
                if _row_fingerprint(existing) != _row_fingerprint(row):
                    raise DuplicateLegacyIdError(
                        f"legacy memory file path {legacy_path!r} has conflicting rows"
                    )
                warnings.append(
                    LegacyWarning("duplicate_exact_memory_file", legacy_path, "exact duplicate coalesced")
                )
                continue
            files_by_path[legacy_path] = row

        return LegacyMemory(
            chunks=tuple(chunks_by_id[key] for key in sorted(chunks_by_id)),
            files=tuple(files_by_path[key] for key in sorted(files_by_path)),
            warnings=tuple(warnings),
        )
    finally:
        connection.close()


def _required_json_object(raw: Any, *, subject: str) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, str):
        raise LegacyDatabaseError(f"{subject} JSON value is not text")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise LegacyDatabaseError(f"{subject} contains invalid JSON") from error
    if not isinstance(value, dict):
        raise LegacyDatabaseError(f"{subject} JSON root must be an object")
    return value


def read_runtime_ledger(snapshot: Path) -> LegacyRuntimeLedger:
    """Read the v0.3 run snapshot/event ledger without activating old work."""

    connection = _connect_snapshot(snapshot)
    try:
        tables = _table_names(connection)
        has_runs = "agent_runs" in tables
        has_events = "agent_run_events" in tables
        if not has_runs and not has_events:
            return LegacyRuntimeLedger((), ())

        warnings: list[LegacyWarning] = []
        runs_by_id: dict[str, dict[str, Any]] = {}
        if has_runs:
            run_columns = _require_schema(
                connection,
                table="agent_runs",
                required_columns={
                    "request_id",
                    "session_id",
                    "run_type",
                    "status",
                    "phase",
                    "created_at",
                    "updated_at",
                    "metadata_json",
                },
                subject="legacy runtime ledger",
            )
            defaults = {
                "parent_id": None,
                "terminal_reason": None,
                "error_code": None,
                "error_message": None,
                "model": None,
                "provider": None,
                "started_at": None,
                "terminal_at": None,
                "lease_owner": None,
                "lease_expires_at": None,
            }
            known_statuses = _ACTIVE_RUN_STATUSES | {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "timeout",
            }
            for raw in connection.execute("SELECT * FROM agent_runs"):
                row = _row_dict(raw, run_columns, defaults)
                request_id = str(row.get("request_id") or "").strip()
                session_id = str(row.get("session_id") or "").strip()
                status = str(row.get("status") or "").strip().casefold()
                if not _SAFE_LEGACY_ID.fullmatch(request_id):
                    raise LegacyDatabaseError("legacy run request_id is invalid")
                if not session_id or "\x00" in session_id or len(session_id) > 1024:
                    raise LegacyDatabaseError("legacy run session_id is invalid")
                if status not in known_statuses:
                    raise LegacySchemaError(
                        f"legacy run {request_id!r} has unsupported status {status!r}"
                    )
                row["status"] = status
                row["created_at"] = _strict_float(
                    row.get("created_at"), subject=f"legacy run {request_id} created_at"
                )
                row["updated_at"] = _strict_float(
                    row.get("updated_at"), subject=f"legacy run {request_id} updated_at"
                )
                row["metadata"] = _required_json_object(
                    row.get("metadata_json"), subject=f"legacy run {request_id} metadata"
                )
                existing = runs_by_id.get(request_id)
                if existing is not None:
                    if _row_fingerprint(existing) != _row_fingerprint(row):
                        raise DuplicateLegacyIdError(
                            f"legacy run request_id {request_id!r} has conflicting rows"
                        )
                    warnings.append(
                        LegacyWarning(
                            "duplicate_exact_run", request_id, "exact duplicate coalesced"
                        )
                    )
                    continue
                runs_by_id[request_id] = row

        events_by_identity: dict[tuple[str, int], dict[str, Any]] = {}
        request_event_sessions: dict[str, str] = {}
        event_id_owners: dict[int, tuple[str, int]] = {}
        idempotency_owners: dict[str, tuple[str, int]] = {}
        if has_events:
            event_columns = _require_schema(
                connection,
                table="agent_run_events",
                required_columns={
                    "event_id",
                    "request_id",
                    "session_id",
                    "turn_id",
                    "event_seq",
                    "event_type",
                    "payload_json",
                    "idempotency_key",
                    "source",
                    "created_at",
                },
                subject="legacy runtime event ledger",
            )
            for raw in connection.execute("SELECT * FROM agent_run_events"):
                row = _row_dict(raw, event_columns, {})
                request_id = str(row.get("request_id") or "").strip()
                session_id = str(row.get("session_id") or "").strip()
                event_type = str(row.get("event_type") or "").strip()
                if not _SAFE_LEGACY_ID.fullmatch(request_id):
                    raise LegacyDatabaseError("legacy runtime event request_id is invalid")
                try:
                    event_seq = int(row.get("event_seq"))
                    event_id = int(row.get("event_id"))
                except (TypeError, ValueError, OverflowError) as error:
                    raise LegacyDatabaseError(
                        "legacy runtime event identity is not an integer"
                    ) from error
                if event_seq < 1 or event_id < 1 or not event_type or len(event_type) > 160:
                    raise LegacyDatabaseError("legacy runtime event identity is invalid")
                if "\x00" in session_id or len(session_id) > 1024:
                    raise LegacyDatabaseError("legacy runtime event session_id is invalid")
                row["event_seq"] = event_seq
                row["event_id"] = event_id
                idempotency_key = str(row.get("idempotency_key") or "").strip()
                if not idempotency_key or len(idempotency_key) > 512:
                    raise LegacyDatabaseError("legacy runtime event idempotency key is invalid")
                row["idempotency_key"] = idempotency_key
                row["created_at"] = _strict_float(
                    row.get("created_at"),
                    subject=f"legacy runtime event {request_id}:{event_seq} created_at",
                )
                row["payload"] = _required_json_object(
                    row.get("payload_json"),
                    subject=f"legacy runtime event {request_id}:{event_seq}",
                )
                established = request_event_sessions.get(request_id)
                if session_id:
                    if established and established != session_id:
                        raise LegacyDatabaseError(
                            f"legacy runtime events for {request_id!r} mix session owners"
                        )
                    request_event_sessions[request_id] = session_id
                run = runs_by_id.get(request_id)
                if run is not None and session_id and str(run["session_id"]) != session_id:
                    raise LegacyDatabaseError(
                        f"legacy runtime event {request_id}:{event_seq} has a different run owner"
                    )
                identity = (request_id, event_seq)
                event_id_owner = event_id_owners.get(event_id)
                if event_id_owner is not None and event_id_owner != identity:
                    raise DuplicateLegacyIdError(
                        f"legacy runtime source event_id {event_id} is reused"
                    )
                idempotency_owner = idempotency_owners.get(idempotency_key)
                if idempotency_owner is not None and idempotency_owner != identity:
                    raise DuplicateLegacyIdError(
                        "legacy runtime event idempotency key is reused"
                    )
                event_id_owners[event_id] = identity
                idempotency_owners[idempotency_key] = identity
                existing = events_by_identity.get(identity)
                if existing is not None:
                    if _row_fingerprint(existing) != _row_fingerprint(row):
                        raise DuplicateLegacyIdError(
                            f"legacy runtime event {request_id}:{event_seq} conflicts"
                        )
                    warnings.append(
                        LegacyWarning(
                            "duplicate_exact_run_event",
                            f"{request_id}:{event_seq}",
                            "exact duplicate coalesced",
                        )
                    )
                    continue
                events_by_identity[identity] = row

        orphan_request_ids = sorted(
            {request_id for request_id, _seq in events_by_identity if request_id not in runs_by_id}
        )
        for request_id in orphan_request_ids:
            warnings.append(
                LegacyWarning(
                    "orphan_runtime_event_stream",
                    request_id,
                    "event stream preserved as diagnostic history without executable run",
                )
            )
        return LegacyRuntimeLedger(
            runs=tuple(
                sorted(
                    runs_by_id.values(),
                    key=lambda row: (float(row.get("created_at") or 0), str(row["request_id"])),
                )
            ),
            events=tuple(
                events_by_identity[key]
                for key in sorted(
                    events_by_identity,
                    key=lambda item: (
                        float(events_by_identity[item].get("created_at") or 0),
                        int(events_by_identity[item]["event_id"]),
                    ),
                )
            ),
            warnings=tuple(warnings),
        )
    except sqlite3.Error as error:
        raise LegacyDatabaseError("legacy runtime ledger could not be read") from error
    finally:
        connection.close()


def read_queued_requests(root: Path) -> LegacyQueuedRequests:
    directory = root / QUEUED_REQUESTS_DIRECTORY
    if not os.path.lexists(directory):
        return LegacyQueuedRequests(())
    try:
        directory = secure_directory(
            directory,
            label="legacy queued-request store",
            root=root,
        )
    except SourceLayoutError as error:
        raise LegacySchemaError(
            "legacy queued-request store is not a regular directory"
        ) from error
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.suffix.casefold() != ".json":
            raise LegacySchemaError("legacy queued-request store contains an unsafe entry")
        try:
            path = secure_regular_file(
                path,
                label="legacy queued-request entry",
                root=root,
            )
        except SourceLayoutError as error:
            raise LegacySchemaError(
                "legacy queued-request store contains an unsafe entry"
            ) from error
        record = read_json_object(path, max_bytes=4 * 1024 * 1024)
        if int(record.get("schemaVersion") or 0) != 1:
            raise LegacySchemaError("legacy queued-request schema is unsupported")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise LegacySchemaError("legacy queued-request payload must be an object")
        request_id = str(record.get("request_id") or payload.get("request_id") or "").strip()
        session_id = str(record.get("session_id") or payload.get("session_id") or "").strip()
        if (
            not _SAFE_LEGACY_ID.fullmatch(request_id)
            or path.stem != request_id
            or str(payload.get("request_id") or "").strip() != request_id
            or not session_id
            or str(payload.get("session_id") or "").strip() != session_id
        ):
            raise LegacySchemaError("legacy queued-request identity is inconsistent")
        if request_id in seen:
            raise DuplicateLegacyIdError(
                f"legacy queued request {request_id!r} is duplicated"
            )
        seen.add(request_id)
        records.append(
            {
                "request_id": request_id,
                "session_id": session_id,
                "created_at": record.get("created_at"),
                "payload": payload,
                "source_relative_path": path.relative_to(root).as_posix(),
            }
        )
    return LegacyQueuedRequests(tuple(records))


def read_scheduler_tasks(root: Path) -> LegacySchedulerTasks:
    path = root / SCHEDULER_TASKS_CANDIDATE
    if not os.path.lexists(path):
        return LegacySchedulerTasks(())
    try:
        path = secure_regular_file(
            path,
            label="legacy scheduler task store",
            root=root,
        )
    except SourceLayoutError as error:
        raise LegacySchemaError("legacy scheduler task store is unsafe") from error
    payload = read_json_object(path)
    if int(payload.get("version") or 0) != 1:
        raise LegacySchemaError("legacy scheduler task schema is unsupported")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, dict):
        raise LegacySchemaError("legacy scheduler tasks must be an object")
    tasks: list[dict[str, Any]] = []
    warnings: list[LegacyWarning] = []
    for raw_id in sorted(raw_tasks):
        raw = raw_tasks[raw_id]
        if not isinstance(raw, dict):
            raise LegacySchemaError("legacy scheduler task must be an object")
        task_id = str(raw.get("id") or raw_id).strip()
        if not _SAFE_LEGACY_ID.fullmatch(task_id) or task_id != str(raw_id):
            raise LegacySchemaError("legacy scheduler task identity is inconsistent")
        name = str(raw.get("name") or "").strip()
        schedule = raw.get("schedule")
        action = raw.get("action")
        if not name or len(name) > 512 or not isinstance(schedule, dict) or not isinstance(action, dict):
            raise LegacySchemaError(f"legacy scheduler task {task_id!r} is malformed")
        schedule_type = str(schedule.get("type") or "").casefold()
        if schedule_type not in {"cron", "interval", "once"}:
            raise LegacySchemaError(
                f"legacy scheduler task {task_id!r} uses an unsupported schedule"
            )
        if schedule_type == "cron" and not str(schedule.get("expression") or "").strip():
            raise LegacySchemaError(f"legacy scheduler task {task_id!r} has no cron expression")
        if schedule_type == "interval":
            try:
                if int(schedule.get("seconds")) <= 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as error:
                raise LegacySchemaError(
                    f"legacy scheduler task {task_id!r} has an invalid interval"
                ) from error
        if schedule_type == "once" and not str(schedule.get("run_at") or "").strip():
            raise LegacySchemaError(f"legacy scheduler task {task_id!r} has no run time")
        action_type = str(action.get("type") or "").casefold()
        if action_type not in {"send_message", "agent_task", "tool_call", "skill_call"}:
            warnings.append(
                LegacyWarning(
                    "scheduler_action_unsupported",
                    task_id,
                    "task is preserved disabled because its action contract is unknown",
                )
            )
        tasks.append(dict(raw))
    return LegacySchedulerTasks(tuple(tasks), tuple(warnings))


def sqlite_schema_fingerprint(
    snapshots: Mapping[str, Path],
) -> tuple[str, tuple[str, ...]]:
    """Fingerprint only released canonical tables, excluding rebuildable FTS state."""

    canonical_tables = {
        "sessions",
        "messages",
        "chunks",
        "files",
        "agent_runs",
        "agent_run_events",
    }
    rows: list[dict[str, Any]] = []
    names: set[str] = set()
    seen_database: set[Path] = set()
    for _label, raw_path in sorted(snapshots.items()):
        path = raw_path.resolve(strict=True)
        if path in seen_database:
            continue
        seen_database.add(path)
        connection = _connect_snapshot(path)
        try:
            for table in sorted(_table_names(connection) & canonical_tables):
                names.add(table)
                columns = [
                    {
                        "name": str(row[1]),
                        "type": str(row[2] or "").upper(),
                        "not_null": int(row[3]),
                        "primary_key": int(row[5]),
                    }
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                rows.append({"table": table, "columns": columns})
        finally:
            connection.close()
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), tuple(sorted(names))


def read_release_evidence(
    root: Path,
    *,
    marker_override: Path | None,
    marker_label: str | None,
    schema_fingerprint: str,
    schema_tables: tuple[str, ...],
) -> LegacyReleaseEvidence:
    path = marker_override
    label = marker_label
    if path is None:
        for candidate in RELEASE_EVIDENCE_CANDIDATES:
            possible = root / candidate
            if not os.path.lexists(possible):
                continue
            try:
                path = secure_regular_file(
                    possible,
                    label=f"legacy release evidence {candidate}",
                    root=root,
                )
            except SourceLayoutError as error:
                raise LegacySchemaError("legacy release evidence is unsafe") from error
            label = candidate
            break
    if path is None:
        if not schema_tables:
            raise LegacySchemaError(
                "legacy source has neither release evidence nor a recognized v0.3 data schema"
            )
        return LegacyReleaseEvidence(
            evidence_level="release_schema_compatible_unattested",
            marker_label=None,
            marker_sha256=None,
            declared_version=None,
            declared_commit=None,
            package_sha256=None,
            schema_fingerprint=schema_fingerprint,
            schema_tables=schema_tables,
        )

    payload = read_json_object(path, max_bytes=1024 * 1024)
    declared_version = str(payload.get("version") or "").strip().lstrip("v")
    if declared_version != "0.3.0":
        raise LegacySchemaError("legacy release evidence does not declare version 0.3.0")
    declared_commit = str(
        payload.get("sourceCommit")
        or payload.get("source_commit")
        or payload.get("commit")
        or ""
    ).strip()
    if declared_commit and not _HEX_COMMIT.fullmatch(declared_commit):
        raise LegacySchemaError("legacy release evidence has an invalid source commit")
    package_sha256 = str(
        payload.get("packageSha256")
        or payload.get("package_sha256")
        or payload.get("sha256")
        or ""
    ).strip()
    if package_sha256 and not _HEX_SHA256.fullmatch(package_sha256):
        raise LegacySchemaError("legacy release evidence has an invalid package digest")
    marker_digest, _marker_identity = stable_sha256_file(
        path,
        label="legacy release evidence",
    )
    return LegacyReleaseEvidence(
        evidence_level="release_marker_and_schema" if schema_tables else "release_marker_only",
        marker_label=label,
        marker_sha256=marker_digest,
        declared_version=declared_version,
        declared_commit=declared_commit or None,
        package_sha256=package_sha256.lower() or None,
        schema_fingerprint=schema_fingerprint,
        schema_tables=schema_tables,
    )


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_json_object(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    try:
        payload = stable_read_bytes(
            path,
            label="legacy JSON file",
            maximum=max_bytes,
        )
        value = json.loads(
            payload.decode("utf-8-sig"), object_pairs_hook=_unique_object
        )
    except SourceLayoutError as error:
        raise ValueError("legacy JSON file is unsafe or exceeds its size limit") from error
    if not isinstance(value, dict):
        raise ValueError("legacy JSON root must be an object")
    return value

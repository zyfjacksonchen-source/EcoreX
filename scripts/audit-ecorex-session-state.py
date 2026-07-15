#!/usr/bin/env python3
"""Audit and repair EcoreX session UI state without exposing raw identifiers.

Default mode is dry-run. Apply only touches UI state metadata and always writes a
backup manifest that can be used for rollback with explicit target paths.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


TERMINAL_REQUEST_STATES = {
    "completed",
    "complete",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "aborted",
    "terminated",
}

UI_METADATA_COLLECTIONS = (
    "sessionProjects",
    "sessionProjectBindings",
    "sessionTitles",
    "pinnedSessions",
    "pinnedSessionTimes",
)

BACKUP_FILES = {
    "ui_state": "ui-state.json.bak",
    "conversation_db": "conversation-db.sqlite3.bak",
}


def _now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _salt(args: argparse.Namespace) -> bytes:
    value = args.salt or os.environ.get("ECOREX_AUDIT_HMAC_SALT") or secrets.token_hex(32)
    return str(value).encode("utf-8", errors="replace")


def _hmac(value: Any, salt: bytes, size: int = 16) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digest = hmac.new(salt, text.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:size]}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_default(value: Any) -> str:
    return str(value)


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_default)
        handle.write("\n")
    os.replace(tmp, path)


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback
    except Exception:
        return fallback


def _default_workspace() -> Path:
    return Path(os.environ.get("ECOREX_WORKSPACE") or os.getcwd()).resolve()


def _default_ui_state_path(workspace: Path) -> Path:
    return workspace / ".ecorex" / "ui-state.json"


def _default_conversation_db() -> Path:
    try:
        from agent.memory.config import get_default_memory_config

        return Path(get_default_memory_config().get_db_path())
    except Exception:
        return Path.home() / "cow" / "memory" / "long-term" / "index.db"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return bool(row)


def _integrity_check(db_path: Optional[Path]) -> Dict[str, Any]:
    if not db_path or not db_path.exists():
        return {"checked": False, "ok": True, "reason": "missing"}
    try:
        conn = _connect_readonly(db_path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            message = str(row[0] if row else "")
            return {"checked": True, "ok": message.lower() == "ok", "code": message[:64]}
        finally:
            conn.close()
    except Exception as exc:
        return {"checked": True, "ok": False, "code": type(exc).__name__}


def _load_backend_sessions(db_path: Optional[Path], max_sessions: int = 200000) -> Dict[str, Any]:
    if not db_path or not db_path.exists():
        return {
            "exists": False,
            "tableExists": False,
            "sessions": {},
            "rowCount": 0,
            "rowLimitExceeded": False,
            "legacyEmptyChannelCount": 0,
            "integrity": {"checked": False, "ok": True, "reason": "missing"},
        }
    conn = _connect_readonly(db_path)
    try:
        if not _table_exists(conn, "sessions"):
            return {
                "exists": True,
                "tableExists": False,
                "sessions": {},
                "rowCount": 0,
                "rowLimitExceeded": False,
                "legacyEmptyChannelCount": 0,
                "integrity": _integrity_check(db_path),
            }
        row_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] or 0)
        if row_count > max_sessions:
            return {
                "exists": True,
                "tableExists": True,
                "sessions": {},
                "rowCount": row_count,
                "rowLimitExceeded": True,
                "legacyEmptyChannelCount": 0,
                "integrity": _integrity_check(db_path),
            }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        exprs = {
            "session_id": "session_id" if "session_id" in columns else "'' AS session_id",
            "channel_type": "channel_type" if "channel_type" in columns else "'' AS channel_type",
            "project_id": "project_id" if "project_id" in columns else "'' AS project_id",
            "created_at": "created_at" if "created_at" in columns else "0 AS created_at",
            "last_active": "last_active" if "last_active" in columns else "0 AS last_active",
            "msg_count": "msg_count" if "msg_count" in columns else "0 AS msg_count",
        }
        query = "SELECT " + ", ".join(exprs.values()) + " FROM sessions"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    sessions: Dict[str, Dict[str, Any]] = {}
    legacy_empty = 0
    for session_id, channel_type, project_id, created_at, last_active, msg_count in rows:
        sid = str(session_id or "").strip()
        if not sid:
            continue
        channel = str(channel_type or "")
        if not channel:
            legacy_empty += 1
        project = str(project_id or "").strip()
        sessions[sid] = {
            "scope": "project" if project else "general",
            "project_id": project,
            "created_at": int(created_at or 0),
            "last_active": int(last_active or 0),
            "msg_count": int(msg_count or 0),
            "legacy_channel": not bool(channel),
        }
    return {
        "exists": True,
        "tableExists": True,
        "sessions": sessions,
        "rowCount": len(sessions),
        "rowLimitExceeded": False,
        "legacyEmptyChannelCount": legacy_empty,
        "integrity": _integrity_check(db_path),
    }


def _project_ids_from_state(state: Dict[str, Any]) -> Tuple[set[str], bool]:
    projects = state.get("projects")
    if not isinstance(projects, list):
        return set(), False
    ids = set()
    for item in projects:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item.get("id")).strip())
    return ids, True


def _project_id_from_binding(binding: Any) -> str:
    if not isinstance(binding, dict):
        return ""
    return str(binding.get("projectId") or binding.get("project_id") or "").strip()


def _local_project_for_session(state: Dict[str, Any], session_id: str) -> str:
    session_projects = state.get("sessionProjects") if isinstance(state.get("sessionProjects"), dict) else {}
    if session_projects.get(session_id):
        return str(session_projects.get(session_id) or "").strip()
    bindings = state.get("sessionProjectBindings") if isinstance(state.get("sessionProjectBindings"), dict) else {}
    return _project_id_from_binding(bindings.get(session_id))


def _ui_state_has_content(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    messages = value.get("messages")
    if isinstance(messages, list) and messages:
        return True
    for key in ("input", "composerText", "draft", "pendingAttachments", "attachments"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return True
        if isinstance(candidate, (list, dict)) and candidate:
            return True
    return False


def _active_requests(path: Optional[Path], salt: bytes) -> Dict[str, Any]:
    if not path:
        return {"provided": False, "valid": False, "count": None, "requestHashes": [], "reason": "missing_snapshot"}
    if not path.exists():
        return {"provided": True, "valid": False, "count": None, "requestHashes": [], "reason": "snapshot_not_found"}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception:
        return {"provided": True, "valid": False, "count": None, "requestHashes": [], "reason": "snapshot_unreadable"}
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("activeRequests", "requests", "runs"):
            if key in payload:
                items = payload.get(key)
                break
        else:
            return {"provided": True, "valid": False, "count": None, "requestHashes": [], "reason": "snapshot_missing_request_collection"}
    else:
        return {"provided": True, "valid": False, "count": None, "requestHashes": [], "reason": "snapshot_invalid_shape"}
    if not isinstance(items, list):
        return {"provided": True, "valid": False, "count": None, "requestHashes": [], "reason": "requests_invalid_shape"}
    active: List[Any] = []
    for item in items:
        if not isinstance(item, dict):
            active.append(item)
            continue
        status = str(item.get("status") or item.get("state") or "").lower()
        if status not in TERMINAL_REQUEST_STATES:
            active.append(item)
    hashes = []
    for item in active[:50]:
        if isinstance(item, dict):
            hashes.append(_hmac(item.get("request_id") or item.get("requestId") or item.get("id"), salt))
        else:
            hashes.append(_hmac(item, salt))
    return {"provided": True, "valid": True, "count": len(active), "requestHashes": [h for h in hashes if h]}


def _audit_state(state: Dict[str, Any], backend: Dict[str, Any], salt: bytes) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    sessions: Dict[str, Dict[str, Any]] = backend.get("sessions") or {}
    known_sessions = set(sessions)
    valid_projects, project_ids_known = _project_ids_from_state(state)
    issues: List[Dict[str, Any]] = []
    repairs: List[Dict[str, Any]] = []
    raw_repairs: List[Tuple[str, str]] = []
    seen_issue_keys: set[Tuple[str, str, str]] = set()
    seen_repairs: set[Tuple[str, str]] = set()

    def add_issue(issue_type: str, session_id: str = "", project_id: str = "", backend_project_id: str = "", collection: str = "") -> None:
        key = (issue_type, session_id, collection)
        if key in seen_issue_keys:
            return
        seen_issue_keys.add(key)
        item = {
            "type": issue_type,
            "collection": collection,
            "sessionIdHash": _hmac(session_id, salt),
        }
        if project_id:
            item["projectIdHash"] = _hmac(project_id, salt)
        if backend_project_id:
            item["backendProjectIdHash"] = _hmac(backend_project_id, salt)
        issues.append(item)

    def add_repair(action: str, collection: str, session_id: str) -> None:
        key = (collection, session_id)
        if key in seen_repairs:
            return
        seen_repairs.add(key)
        raw_repairs.append(key)
        repairs.append({
            "action": action,
            "collection": collection,
            "sessionIdHash": _hmac(session_id, salt),
        })

    for collection in UI_METADATA_COLLECTIONS:
        mapping = state.get(collection) if isinstance(state.get(collection), dict) else {}
        for session_id, value in mapping.items():
            sid = str(session_id or "").strip()
            if not sid:
                continue
            if sid.startswith("runtime-"):
                add_issue("runtime_fallback_id", sid, collection=collection)
                add_repair("remove_runtime_fallback_metadata", collection, sid)
            if sid not in known_sessions:
                add_issue(f"orphan_{collection}", sid, collection=collection)
                add_repair("remove_orphan_metadata", collection, sid)
                continue
            if collection == "sessionProjects":
                local_project = str(value or "").strip()
            elif collection == "sessionProjectBindings":
                local_project = _project_id_from_binding(value)
            else:
                local_project = ""
            if local_project and project_ids_known and local_project not in valid_projects:
                add_issue("dangling_project_binding", sid, project_id=local_project, collection=collection)
                add_repair("remove_dangling_project_metadata", collection, sid)

    ui_rows = state.get("sessionUiState") if isinstance(state.get("sessionUiState"), dict) else {}
    for session_id, value in ui_rows.items():
        sid = str(session_id or "").strip()
        if not sid:
            continue
        if sid.startswith("runtime-"):
            add_issue("runtime_fallback_id", sid, collection="sessionUiState")
            if not _ui_state_has_content(value):
                add_repair("remove_empty_runtime_fallback_row", "sessionUiState", sid)
        if sid not in known_sessions:
            add_issue("local_state_without_backend_session", sid, collection="sessionUiState")
            if not _ui_state_has_content(value):
                add_repair("remove_empty_local_state_without_backend", "sessionUiState", sid)

    session_ids_in_project_maps = set()
    for collection in ("sessionProjects", "sessionProjectBindings"):
        mapping = state.get(collection) if isinstance(state.get(collection), dict) else {}
        session_ids_in_project_maps.update(str(key or "").strip() for key in mapping if str(key or "").strip())
    for sid in session_ids_in_project_maps:
        backend_row = sessions.get(sid)
        if not backend_row:
            continue
        local_project = _local_project_for_session(state, sid)
        backend_project = str(backend_row.get("project_id") or "").strip()
        if not local_project:
            continue
        if not backend_project:
            add_issue("stale_local_project_for_backend_general", sid, project_id=local_project)
            add_repair("remove_stale_project_metadata", "sessionProjects", sid)
            add_repair("remove_stale_project_metadata", "sessionProjectBindings", sid)
        elif local_project != backend_project:
            add_issue("local_project_mismatch_backend", sid, project_id=local_project, backend_project_id=backend_project)
            add_repair("remove_project_metadata_overridden_by_backend", "sessionProjects", sid)
            add_repair("remove_project_metadata_overridden_by_backend", "sessionProjectBindings", sid)

    issue_counts: Dict[str, int] = {}
    for issue in issues:
        issue_counts[issue["type"]] = issue_counts.get(issue["type"], 0) + 1
    repair_counts: Dict[str, int] = {}
    for repair in repairs:
        key = f"{repair['action']}:{repair['collection']}"
        repair_counts[key] = repair_counts.get(key, 0) + 1

    summary = {
        "backendSessionCount": len(known_sessions),
        "backendSessionRowCount": int(backend.get("rowCount") or len(known_sessions)),
        "backendSessionRowLimitExceeded": bool(backend.get("rowLimitExceeded")),
        "legacyEmptyChannelCount": int(backend.get("legacyEmptyChannelCount") or 0),
        "uiStateSessionCount": len(ui_rows),
        "issueCount": len(issues),
        "repairActionCount": len(repairs),
        "issueTypes": dict(sorted(issue_counts.items())),
        "repairActions": dict(sorted(repair_counts.items())),
    }
    report = {
        "summary": summary,
        "issues": issues,
        "repairs": repairs,
    }
    return report, raw_repairs


def _apply_repairs(state: Dict[str, Any], raw_repairs: Iterable[Tuple[str, str]]) -> Dict[str, Any]:
    next_state = dict(state)
    for collection, session_id in raw_repairs:
        mapping = next_state.get(collection)
        if isinstance(mapping, dict):
            mapping = dict(mapping)
            mapping.pop(session_id, None)
            next_state[collection] = mapping
    next_state["updatedAt"] = int(time.time())
    next_state.setdefault("schemaVersion", 1)
    return next_state


def _backup_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _create_backup(ui_state: Path, conversation_db: Optional[Path], backup_dir: Path) -> Dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: List[Dict[str, Any]] = []
    if ui_state.exists():
        backup_name = BACKUP_FILES["ui_state"]
        backup_path = backup_dir / backup_name
        shutil.copy2(ui_state, backup_path)
        files.append({
            "role": "ui_state",
            "verificationSha256": _sha256_file(backup_path),
        })
    if conversation_db and conversation_db.exists():
        backup_name = BACKUP_FILES["conversation_db"]
        backup_path = backup_dir / backup_name
        _backup_sqlite(conversation_db, backup_path)
        files.append({
            "role": "conversation_db",
            "verificationSha256": _sha256_file(backup_path),
        })
    manifest = {
        "schemaVersion": 1,
        "kind": "ecorex-session-state-repair-backup",
        "createdAt": _now_iso(),
        "files": files,
    }
    manifest_path = backup_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return {
        "manifestPath": manifest_path,
        "manifest": manifest,
    }


def _rollback(manifest_path: Path, ui_state: Optional[Path], conversation_db: Optional[Path]) -> Dict[str, Any]:
    manifest = _load_json(manifest_path, {})
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise RuntimeError("invalid_backup_manifest")
    restored: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        backup_name = BACKUP_FILES.get(str(role or ""))
        backup_path = manifest_path.parent / str(backup_name or "")
        if not backup_path.exists():
            raise RuntimeError(f"missing_backup_{role}")
        if _sha256_file(backup_path) != item.get("verificationSha256"):
            raise RuntimeError(f"backup_hash_mismatch_{role}")
        if role == "ui_state":
            if not ui_state:
                raise RuntimeError("ui_state_target_required")
            ui_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, ui_state)
        elif role == "conversation_db":
            if not conversation_db:
                raise RuntimeError("conversation_db_target_required")
            conversation_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, conversation_db)
            integrity = _integrity_check(conversation_db)
            if not integrity.get("ok"):
                raise RuntimeError("conversation_db_integrity_failed_after_rollback")
        else:
            continue
        restored.append({"role": role})
    return {"restored": restored}


def _emit(payload: Dict[str, Any], output: Optional[Path]) -> None:
    if output:
        _write_json_atomic(output, payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=_json_default)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit or repair EcoreX session UI state")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--ui-state", type=Path, default=None)
    parser.add_argument("--conversation-db", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--active-requests-json", type=Path, default=None)
    parser.add_argument("--salt", default="")
    parser.add_argument("--max-backend-sessions", type=int, default=200000)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--rollback", type=Path, default=None)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    salt = _salt(args)
    workspace = (args.workspace or _default_workspace()).resolve()
    ui_state = (args.ui_state or _default_ui_state_path(workspace)).resolve()
    conversation_db = (args.conversation_db or _default_conversation_db()).resolve()

    if args.apply and args.rollback:
        parser.error("--apply and --rollback are mutually exclusive")

    if args.rollback:
        try:
            result = _rollback(args.rollback.resolve(), ui_state, conversation_db)
            payload = {
                "status": "success",
                "mode": "rollback",
                "generatedAt": _now_iso(),
                "result": result,
                "integrity": {"conversationDb": _integrity_check(conversation_db)},
                "privacy": {
                    "usesHmacHashes": True,
                    "rawIdentifiersIncluded": False,
                    "rawPathsIncluded": False,
                },
            }
            _emit(payload, args.output)
            return 0
        except Exception as exc:
            _emit({
                "status": "error",
                "mode": "rollback",
                "generatedAt": _now_iso(),
                "code": type(exc).__name__,
                "message": str(exc),
                "privacy": {"rawIdentifiersIncluded": False, "rawPathsIncluded": False},
            }, args.output)
            return 2

    active = _active_requests(args.active_requests_json, salt)
    if args.apply and not active.get("valid"):
        payload = {
            "status": "error",
            "mode": "apply",
            "generatedAt": _now_iso(),
            "code": "ACTIVE_REQUEST_SNAPSHOT_REQUIRED",
            "activeRequests": active,
            "privacy": {
                "usesHmacHashes": True,
                "rawIdentifiersIncluded": False,
                "rawPathsIncluded": False,
            },
        }
        _emit(payload, args.output)
        return 2
    if args.apply and int(active.get("count") or 0) > 0:
        payload = {
            "status": "error",
            "mode": "apply",
            "generatedAt": _now_iso(),
            "code": "ACTIVE_REQUESTS_PRESENT",
            "activeRequests": active,
            "privacy": {
                "usesHmacHashes": True,
                "rawIdentifiersIncluded": False,
                "rawPathsIncluded": False,
            },
        }
        _emit(payload, args.output)
        return 2

    state = _load_json(ui_state, {})
    if not isinstance(state, dict):
        state = {}
    backend = _load_backend_sessions(conversation_db, max_sessions=max(1, int(args.max_backend_sessions or 200000)))
    if args.apply and (not backend.get("exists") or not backend.get("tableExists")):
        payload = {
            "status": "error",
            "mode": "apply",
            "generatedAt": _now_iso(),
            "code": "CONVERSATION_DB_REQUIRED",
            "integrity": {"conversationDb": backend.get("integrity")},
            "privacy": {"rawIdentifiersIncluded": False, "rawPathsIncluded": False},
        }
        _emit(payload, args.output)
        return 2
    if args.apply and backend.get("rowLimitExceeded"):
        payload = {
            "status": "error",
            "mode": "apply",
            "generatedAt": _now_iso(),
            "code": "BACKEND_SESSION_ROW_LIMIT_EXCEEDED",
            "rowCount": int(backend.get("rowCount") or 0),
            "rowLimit": max(1, int(args.max_backend_sessions or 200000)),
            "integrity": {"conversationDb": backend.get("integrity")},
            "privacy": {"rawIdentifiersIncluded": False, "rawPathsIncluded": False},
        }
        _emit(payload, args.output)
        return 2
    report, raw_repairs = _audit_state(state, backend, salt)
    integrity_before = {"conversationDb": backend.get("integrity")}
    mode = "apply" if args.apply else "dry-run"

    backup_summary = None
    post_repair_summary = None
    if args.apply:
        if not integrity_before["conversationDb"].get("ok"):
            payload = {
                "status": "error",
                "mode": mode,
                "generatedAt": _now_iso(),
                "code": "CONVERSATION_DB_INTEGRITY_FAILED",
                "integrity": integrity_before,
                "privacy": {"rawIdentifiersIncluded": False, "rawPathsIncluded": False},
            }
            _emit(payload, args.output)
            return 2
        backup_dir = args.backup_dir
        if not backup_dir:
            backup_dir = ui_state.parent / "backups" / f"session-repair-{int(time.time())}"
        backup = _create_backup(ui_state, conversation_db, backup_dir.resolve())
        backup_summary = {
            "fileCount": len((backup.get("manifest") or {}).get("files") or []),
            "manifestIdHash": _hmac(str(backup.get("manifestPath") or ""), salt),
        }
        next_state = _apply_repairs(state, raw_repairs)
        _write_json_atomic(ui_state, next_state)
        backend_after = _load_backend_sessions(conversation_db, max_sessions=max(1, int(args.max_backend_sessions or 200000)))
        if not backend_after.get("integrity", {}).get("ok"):
            payload = {
                "status": "error",
                "mode": mode,
                "generatedAt": _now_iso(),
                "code": "CONVERSATION_DB_INTEGRITY_FAILED_AFTER_APPLY",
                "integrity": {
                    "conversationDbBefore": integrity_before.get("conversationDb"),
                    "conversationDbAfter": backend_after.get("integrity"),
                },
                "backup": backup_summary,
                "privacy": {"rawIdentifiersIncluded": False, "rawPathsIncluded": False},
            }
            _emit(payload, args.output)
            return 2
        post_report, _ = _audit_state(next_state, backend_after, salt)
        post_repair_summary = post_report["summary"]

    payload = {
        "status": "success",
        "mode": mode,
        "generatedAt": _now_iso(),
        "scope": {
            "uiStatePathHash": _hmac(str(ui_state), salt),
            "conversationDbPathHash": _hmac(str(conversation_db), salt),
        },
        "integrity": integrity_before,
        "postApplyIntegrity": {"conversationDb": backend_after.get("integrity")} if args.apply else None,
        "activeRequests": active,
        **report,
        "postRepairSummary": post_repair_summary,
        "backup": backup_summary,
        "privacy": {
            "usesHmacHashes": True,
            "rawIdentifiersIncluded": False,
            "rawPathsIncluded": False,
            "messageBodiesIncluded": False,
        },
    }
    _emit(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

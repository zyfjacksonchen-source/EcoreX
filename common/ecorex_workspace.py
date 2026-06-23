"""Shared EcoreX workspace state for desktop/Web co-installation."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger


INSTALLATION_SCHEMA_VERSION = 1
UI_STATE_SCHEMA_VERSION = 1
LOCK_STALE_SECONDS = 6 * 60 * 60


def _workspace_root(workspace: str) -> Path:
    from common.utils import expand_path

    return Path(expand_path(workspace)).resolve()


def _state_dir(workspace: str) -> Path:
    path = _workspace_root(workspace) / ".ecorex"
    path.mkdir(parents=True, exist_ok=True)
    return path


def installation_manifest_path(workspace: str) -> Path:
    return _state_dir(workspace) / "installations.json"


def ui_state_path(workspace: str) -> Path:
    return _state_dir(workspace) / "ui-state.json"


def locks_dir(workspace: str) -> Path:
    path = _state_dir(workspace) / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else fallback
    except FileNotFoundError:
        return fallback
    except Exception as exc:
        logger.warning(f"[EcoreXWorkspace] Failed reading {path}: {exc}")
        return fallback


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def _process_is_alive(pid: Any) -> Optional[bool]:
    try:
        normalized = int(pid)
    except Exception:
        return None
    if normalized <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(process_query_limited_information, False, normalized)
            if not handle:
                error = ctypes.get_last_error()
                if error in {5}:  # Access denied means the process exists.
                    return True
                if error in {87, 1168}:  # Invalid parameter / not found.
                    return False
                return None
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(normalized, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _session_lock_info(path: Path, now: int, stale_seconds: int = LOCK_STALE_SECONDS) -> Dict[str, Any]:
    owner = _read_json(path, {})
    try:
        age = max(0, now - int(path.stat().st_mtime))
    except Exception:
        age = 0
    owner_host = str(owner.get("host") or "").lower()
    current_host = socket.gethostname().lower()
    alive = _process_is_alive(owner.get("pid"))
    dead_owner = owner_host == current_host and alive is False
    stale = age >= stale_seconds
    removable_stale = stale and not (owner_host == current_host and alive is True)
    return {
        "path": str(path),
        "session_id": owner.get("sessionId") or "",
        "pid": owner.get("pid"),
        "host": owner.get("host") or "",
        "created_at": owner.get("createdAt") or 0,
        "age_seconds": age,
        "alive": alive,
        "dead_owner": dead_owner,
        "stale": stale,
        "removable": bool(dead_owner or removable_stale),
        "removed": False,
    }


def list_session_locks(workspace: str, cleanup: bool = False, stale_seconds: int = LOCK_STALE_SECONDS) -> list[Dict[str, Any]]:
    """Return session lock diagnostics and optionally remove safely removable locks."""
    now = int(time.time())
    result: list[Dict[str, Any]] = []
    try:
        paths = sorted(locks_dir(workspace).glob("session-*.lock"))
    except Exception as exc:
        logger.warning(f"[EcoreXWorkspace] Failed listing session locks: {exc}")
        return result
    for path in paths:
        info = _session_lock_info(path, now, stale_seconds=stale_seconds)
        if cleanup and info.get("removable"):
            try:
                path.unlink()
                info["removed"] = True
                logger.warning(
                    f"[EcoreXWorkspace] Removed stale/dead session lock: "
                    f"{path} pid={info.get('pid')} session={info.get('session_id')}"
                )
            except FileNotFoundError:
                info["removed"] = True
            except Exception as exc:
                info["remove_error"] = str(exc)
        result.append(info)
    return result


def cleanup_stale_session_locks(workspace: str, stale_seconds: int = LOCK_STALE_SECONDS) -> list[Dict[str, Any]]:
    return list_session_locks(workspace, cleanup=True, stale_seconds=stale_seconds)


def load_installation_manifest(workspace: str) -> Dict[str, Any]:
    path = installation_manifest_path(workspace)
    data = _read_json(path, {})
    if not data:
        data = {
            "schemaVersion": INSTALLATION_SCHEMA_VERSION,
            "workspaceId": str(uuid.uuid4()),
            "workspacePath": str(_workspace_root(workspace)),
            "surfaces": {},
            "createdAt": int(time.time()),
        }
    data.setdefault("schemaVersion", INSTALLATION_SCHEMA_VERSION)
    data.setdefault("workspaceId", str(uuid.uuid4()))
    data.setdefault("workspacePath", str(_workspace_root(workspace)))
    data.setdefault("surfaces", {})
    return data


def register_installation(workspace: str, surface: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    manifest = load_installation_manifest(workspace)
    surfaces = manifest.setdefault("surfaces", {})
    now = int(time.time())
    previous = surfaces.get(surface, {}) if isinstance(surfaces.get(surface), dict) else {}
    surfaces[surface] = {
        **previous,
        **(metadata or {}),
        "surface": surface,
        "host": socket.gethostname(),
        "lastSeenAt": now,
    }
    manifest["updatedAt"] = now
    _atomic_write_json(installation_manifest_path(workspace), manifest)
    return manifest


def load_ui_state(workspace: str) -> Dict[str, Any]:
    state = _read_json(ui_state_path(workspace), {})
    state.setdefault("schemaVersion", UI_STATE_SCHEMA_VERSION)
    state.setdefault("projects", [])
    state.setdefault("sessionProjects", {})
    state.setdefault("sessionTitles", {})
    state.setdefault("pinnedSessions", {})
    state.setdefault("pinnedProjects", {})
    state.setdefault("updatedAt", 0)
    return state


def _project_key(project: Dict[str, Any]) -> str:
    path_value = str(project.get("path") or "").strip()
    if path_value:
        try:
            return "path:" + os.path.normcase(os.path.realpath(os.path.expanduser(path_value))).replace("\\", "/")
        except Exception:
            return "path:" + path_value.replace("\\", "/").rstrip("/")
    return "id:" + str(project.get("id") or "").strip()


def _project_updated_at(project: Dict[str, Any]) -> str:
    return str(project.get("updatedAt") or project.get("updated_at") or "")


def _project_keys(source: Any) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()
    if not isinstance(source, list):
        return keys
    for item in source:
        if not isinstance(item, dict):
            continue
        key = _project_key(item)
        if not key or key == "id:" or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _merge_projects(current: Any, incoming: Any, replace: bool = False) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for source in (current, incoming):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            key = _project_key(item)
            if not key or key == "id:":
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(item)
                order.append(key)
                continue
            existing_time = _project_updated_at(existing)
            incoming_time = _project_updated_at(item)
            merged_item = {**existing, **item} if incoming_time >= existing_time else {**item, **existing}
            if key.startswith("path:") and existing.get("id") and item.get("id") and existing.get("id") != item.get("id"):
                # Keep the established id for the path so sessionProjects do not drift
                # when another client reports the same folder with a different hash.
                merged_item["id"] = existing.get("id")
            merged[key] = merged_item
    if replace and isinstance(incoming, list):
        replace_order = _project_keys(incoming)
        return [merged[key] for key in replace_order if key in merged]
    return [merged[key] for key in order if key in merged]


def _project_id_aliases(*sources: Any) -> Dict[str, str]:
    canonical_by_key: Dict[str, str] = {}
    aliases: Dict[str, str] = {}
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            key = _project_key(item)
            project_id = str(item.get("id") or "").strip()
            if not key or key == "id:" or not project_id:
                continue
            canonical_by_key.setdefault(key, project_id)
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            key = _project_key(item)
            project_id = str(item.get("id") or "").strip()
            canonical_id = canonical_by_key.get(key)
            if project_id and canonical_id:
                aliases[project_id] = canonical_id
    return aliases


def _project_ids(projects: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(projects, list):
        return ids
    for item in projects:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item.get("id")))
    return ids


def _normalize_session_project_mapping(
    mapping: Any,
    aliases: Dict[str, str],
    valid_project_ids: set[str],
    project_ids_known: bool = False,
) -> Dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    normalized: Dict[str, str] = {}
    for session_id, project_id in mapping.items():
        session_key = str(session_id or "").strip()
        project_key = str(project_id or "").strip()
        if not session_key or not project_key:
            continue
        canonical_id = aliases.get(project_key, project_key)
        if project_ids_known and canonical_id not in valid_project_ids:
            continue
        normalized[session_key] = canonical_id
    return normalized


def _normalize_project_keyed_mapping(
    mapping: Any,
    aliases: Dict[str, str],
    valid_project_ids: set[str],
    project_ids_known: bool = False,
) -> Dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for project_id, value in mapping.items():
        project_key = str(project_id or "").strip()
        if not project_key:
            continue
        canonical_id = aliases.get(project_key, project_key)
        if project_ids_known and canonical_id not in valid_project_ids:
            continue
        normalized[canonical_id] = value
    return normalized


def _merge_mapping(current: Any, incoming: Any, prefer_incoming: bool = False) -> Dict[str, Any]:
    current_map = current if isinstance(current, dict) else {}
    incoming_map = incoming if isinstance(incoming, dict) else {}
    if not incoming_map and current_map:
        return dict(current_map)
    if prefer_incoming:
        return {**current_map, **incoming_map}
    return {**incoming_map, **current_map}


def save_ui_state(workspace: str, incoming: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "activeProjectId",
        "activeSessionId",
        "lastActiveSessionId",
        "projects",
        "sessionProjects",
        "sessionTitles",
        "sessionUiState",
        "pinnedSessions",
        "pinnedProjects",
        "capabilityEnabled",
        "enabledCapabilityPacks",
        "skillDefaultsApplied",
        "theme",
        "savedAt",
    }
    current = load_ui_state(workspace)
    incoming = dict(incoming)
    replace_project_state = bool(
        incoming.get("replaceProjectState")
        or incoming.get("replace_project_state")
        or incoming.get("projectStateMode") == "replace"
    )
    raw_incoming_projects = incoming.get("projects")
    if "projects" in incoming:
        incoming_projects = incoming.get("projects")
        current_projects = current.get("projects")
        if isinstance(incoming_projects, list):
            if replace_project_state:
                incoming["projects"] = _merge_projects(current_projects, incoming_projects, replace=True)
            elif incoming_projects or not current_projects:
                incoming["projects"] = _merge_projects(current_projects, incoming_projects)
            else:
                incoming.pop("projects", None)
    effective_projects = incoming.get("projects") if "projects" in incoming else current.get("projects")
    id_aliases = _project_id_aliases(current.get("projects"), raw_incoming_projects, effective_projects)
    valid_project_ids = _project_ids(effective_projects)
    project_ids_known = isinstance(effective_projects, list)
    if "sessionProjects" in incoming:
        normalized_current = _normalize_session_project_mapping(current.get("sessionProjects"), id_aliases, valid_project_ids, project_ids_known)
        normalized_incoming = _normalize_session_project_mapping(incoming.get("sessionProjects"), id_aliases, valid_project_ids, project_ids_known)
        if replace_project_state:
            incoming["sessionProjects"] = normalized_incoming
        else:
            incoming["sessionProjects"] = _merge_mapping(normalized_current, normalized_incoming, prefer_incoming=False)
    elif id_aliases and current.get("sessionProjects"):
        normalized_current = _normalize_session_project_mapping(current.get("sessionProjects"), id_aliases, valid_project_ids, project_ids_known)
        if normalized_current != current.get("sessionProjects"):
            incoming["sessionProjects"] = normalized_current
    if "pinnedProjects" in incoming:
        normalized_current_pins = _normalize_project_keyed_mapping(current.get("pinnedProjects"), id_aliases, valid_project_ids, project_ids_known)
        normalized_incoming_pins = _normalize_project_keyed_mapping(incoming.get("pinnedProjects"), id_aliases, valid_project_ids, project_ids_known)
        if replace_project_state:
            incoming["pinnedProjects"] = normalized_incoming_pins
        else:
            incoming["pinnedProjects"] = _merge_mapping(normalized_current_pins, normalized_incoming_pins, prefer_incoming=False)
    elif id_aliases and current.get("pinnedProjects"):
        normalized_current_pins = _normalize_project_keyed_mapping(current.get("pinnedProjects"), id_aliases, valid_project_ids, project_ids_known)
        if normalized_current_pins != current.get("pinnedProjects"):
            incoming["pinnedProjects"] = normalized_current_pins
    for key in ("sessionTitles", "pinnedSessions"):
        if key in incoming:
            if replace_project_state:
                merged_map = incoming.get(key) if isinstance(incoming.get(key), dict) else {}
                incoming[key] = merged_map
                continue
            else:
                merged_map = _merge_mapping(current.get(key), incoming.get(key), prefer_incoming=False)
            if merged_map or not current.get(key):
                incoming[key] = merged_map
            else:
                incoming.pop(key, None)
    if "activeProjectId" in incoming:
        active_project_id = str(incoming.get("activeProjectId") or "").strip()
        if active_project_id:
            active_project_id = id_aliases.get(active_project_id, active_project_id)
            incoming["activeProjectId"] = active_project_id if not project_ids_known or active_project_id in valid_project_ids else None
        elif replace_project_state:
            incoming["activeProjectId"] = None
        elif current.get("activeProjectId"):
            incoming.pop("activeProjectId", None)
    elif replace_project_state and current.get("activeProjectId") and project_ids_known:
        active_project_id = id_aliases.get(str(current.get("activeProjectId")), str(current.get("activeProjectId")))
        if active_project_id not in valid_project_ids:
            incoming["activeProjectId"] = None
    for key in ("activeSessionId", "lastActiveSessionId"):
        if key in incoming and incoming.get(key) in ("", None) and current.get(key):
            incoming.pop(key, None)
    next_state = {
        **current,
        **{key: incoming[key] for key in allowed if key in incoming},
        "schemaVersion": UI_STATE_SCHEMA_VERSION,
        "updatedAt": int(time.time()),
    }
    _atomic_write_json(ui_state_path(workspace), next_state)
    return next_state


class SessionBusyError(RuntimeError):
    """Raised when another runtime is already producing a session response."""


class SessionLock(AbstractContextManager):
    """Simple cross-process lock for one session within a shared workspace."""

    def __init__(self, workspace: str, session_id: str, stale_seconds: int = LOCK_STALE_SECONDS):
        digest = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:32]
        self.path = locks_dir(workspace) / f"session-{digest}.lock"
        self.session_id = session_id
        self.stale_seconds = stale_seconds
        self.acquired = False

    def acquire(self) -> "SessionLock":
        now = int(time.time())
        payload = {
            "sessionId": self.session_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "createdAt": now,
        }
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                    handle.write("\n")
                self.acquired = True
                return self
            except FileExistsError:
                if self._remove_if_stale(now):
                    continue
                raise SessionBusyError(f"session is busy: {self.session_id}")

    def _remove_if_stale(self, now: int) -> bool:
        try:
            stat_result = self.path.stat()
            age = now - int(stat_result.st_mtime)
            owner = _read_json(self.path, {})
            owner_host = str(owner.get("host") or "").lower()
            current_host = socket.gethostname().lower()
            alive = _process_is_alive(owner.get("pid"))
            if owner_host == current_host and alive is False:
                self._unlink_lock()
                logger.warning(
                    f"[EcoreXWorkspace] Removed dead-pid session lock: "
                    f"{self.path} pid={owner.get('pid')}"
                )
                return True
            if owner_host == current_host and alive is True:
                return False
            if age < self.stale_seconds:
                return False
            self._unlink_lock()
            logger.warning(f"[EcoreXWorkspace] Removed stale session lock: {self.path}")
            return True
        except FileNotFoundError:
            return True
        except Exception:
            return False

    def _unlink_lock(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self._unlink_lock()
        finally:
            self.acquired = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

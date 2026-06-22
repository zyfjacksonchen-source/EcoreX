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
from typing import Any, Dict, Optional

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

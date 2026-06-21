# encoding:utf-8
"""Durable per-session state for the OpenAI Responses adapter."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from common.log import logger
from common.utils import expand_path
from config import conf
from models.openai.responses_adapter import ResponsesState


RESPONSES_STATE_SCHEMA_VERSION = 1
RESPONSES_STATE_FILE_NAME = "model-responses-state.json"
_STATE_LOCK = threading.RLock()


def responses_state_path(workspace: Optional[str] = None) -> Path:
    root_value = workspace or conf().get("agent_workspace") or "~/cow"
    root = Path(expand_path(root_value)).resolve()
    return root / ".ecorex" / RESPONSES_STATE_FILE_NAME


def responses_state_key(*, session_id: str, provider: str, model: str) -> str:
    raw = "\n".join([
        str(provider or "").strip().lower(),
        str(model or "").strip().lower(),
        str(session_id or "").strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_prompt_cache_key(*, session_id: str, provider: str, model: str) -> str:
    provider_digest = hashlib.sha256(str(provider or "").strip().lower().encode("utf-8")).hexdigest()[:8]
    model_digest = hashlib.sha256(str(model or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    session_digest = hashlib.sha256(str(session_id or "").strip().encode("utf-8")).hexdigest()[:24]
    return f"ecorex:{provider_digest}:{model_digest}:{session_digest}"


def load_responses_state(
    *,
    session_id: str,
    provider: str,
    model: str,
    workspace: Optional[str] = None,
) -> ResponsesState:
    if not session_id:
        return ResponsesState()
    with _STATE_LOCK:
        data = _read_state_file(responses_state_path(workspace))
        entry = _entries(data).get(responses_state_key(session_id=session_id, provider=provider, model=model))
    state_data = entry.get("state") if isinstance(entry, dict) else {}
    if not isinstance(state_data, dict):
        state_data = {}
    state_data.setdefault(
        "prompt_cache_key",
        default_prompt_cache_key(session_id=session_id, provider=provider, model=model),
    )
    return _state_from_dict(state_data)


def save_responses_state(
    *,
    session_id: str,
    provider: str,
    model: str,
    state: ResponsesState,
    workspace: Optional[str] = None,
) -> ResponsesState:
    if not session_id:
        return state
    next_state = _state_from_dict(state.to_dict() if isinstance(state, ResponsesState) else dict(state or {}))
    if not next_state.prompt_cache_key:
        next_state = _state_from_dict({
            **next_state.to_dict(),
            "prompt_cache_key": default_prompt_cache_key(session_id=session_id, provider=provider, model=model),
        })

    path = responses_state_path(workspace)
    key = responses_state_key(session_id=session_id, provider=provider, model=model)
    now = int(time.time())
    with _STATE_LOCK:
        data = _read_state_file(path)
        entries = data.get("sessions")
        if not isinstance(entries, dict):
            entries = {}
            data["sessions"] = entries
        entries[key] = {
            "provider": str(provider or ""),
            "model": str(model or ""),
            "sessionHash": hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest(),
            "updatedAt": now,
            "state": next_state.to_dict(),
        }
        data["schemaVersion"] = RESPONSES_STATE_SCHEMA_VERSION
        data["updatedAt"] = now
        _atomic_write_json(path, data)
    return next_state


def clear_responses_state(
    *,
    session_id: str,
    provider: str,
    model: str,
    workspace: Optional[str] = None,
) -> bool:
    if not session_id:
        return False
    path = responses_state_path(workspace)
    key = responses_state_key(session_id=session_id, provider=provider, model=model)
    with _STATE_LOCK:
        data = _read_state_file(path)
        entries = data.get("sessions")
        if not isinstance(entries, dict):
            entries = {}
            data["sessions"] = entries
        removed = entries.pop(key, None) is not None
        if removed:
            data["updatedAt"] = int(time.time())
            _atomic_write_json(path, data)
    return removed


def clear_responses_state_for_session(
    session_id: str,
    *,
    workspace: Optional[str] = None,
) -> int:
    if not session_id:
        return 0
    path = responses_state_path(workspace)
    session_hash = hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()
    with _STATE_LOCK:
        data = _read_state_file(path)
        entries = data.get("sessions")
        if not isinstance(entries, dict):
            entries = {}
            data["sessions"] = entries
        removed_keys = [
            key for key, entry in list(entries.items())
            if isinstance(entry, dict) and entry.get("sessionHash") == session_hash
        ]
        for key in removed_keys:
            entries.pop(key, None)
        if removed_keys:
            data["updatedAt"] = int(time.time())
            _atomic_write_json(path, data)
    return len(removed_keys)


def _entries(data: Dict[str, Any]) -> Dict[str, Any]:
    entries = data.get("sessions")
    return entries if isinstance(entries, dict) else {}


def _state_from_dict(data: Dict[str, Any]) -> ResponsesState:
    allowed = {
        "previous_response_id",
        "prompt_cache_key",
        "prompt_cache_retention",
        "service_tier",
        "truncation",
        "store",
        "compacted_input",
    }
    values = {key: data[key] for key in allowed if key in data}
    return ResponsesState(**values)


def _read_state_file(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("schemaVersion", RESPONSES_STATE_SCHEMA_VERSION)
            data.setdefault("sessions", {})
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"[ResponsesState] failed reading {path}: {exc}")
    return {
        "schemaVersion": RESPONSES_STATE_SCHEMA_VERSION,
        "sessions": {},
        "updatedAt": 0,
    }


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)

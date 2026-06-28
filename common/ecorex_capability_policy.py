"""Runtime-visible administrator capability policy helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from common.log import logger


_POLICY_MODES = {"ask", "preinstall", "disabled"}
_REDACTED_PACK_ID = "redacted-capability-pack"
_PACK_POLICY_ALIASES = {
    "feishu": "feishu-lark",
    "lark": "feishu-lark",
    "feishu-lark": "feishu-lark",
    "lark-feishu": "feishu-lark",
    "feishu-cli": "feishu-lark",
    "lark-cli": "feishu-lark",
    "tongxin": "tongxin-cli",
    "tongxin-cli": "tongxin-cli",
    "xin-agent": "tongxin-cli",
    "xin-agent-cli": "tongxin-cli",
    "tx-assistant": "tongxin-cli",
}


def load_capability_policy() -> Dict[str, Any]:
    path = _capability_policy_path()
    if not path:
        return _default_policy_payload("none")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning(f"[CapabilityPolicy] failed reading {path}: {exc}")
        return _default_policy_payload("unavailable")
    if not isinstance(data, dict):
        return _default_policy_payload("invalid")
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    capabilities = data.get("capabilities") if isinstance(data.get("capabilities"), list) else []
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in capabilities:
        if not isinstance(item, dict):
            continue
        pack_id = _safe_pack_id(item.get("id") or item.get("packId") or item.get("pack_id"))
        if not pack_id:
            continue
        pack_item = dict(item)
        by_id[pack_id] = pack_item
        canonical_pack_id = normalize_capability_pack_id(pack_id)
        if canonical_pack_id and canonical_pack_id not in by_id:
            by_id[canonical_pack_id] = pack_item
    return {
        "available": True,
        "source": "admin-cache",
        "path": str(path),
        "policy": dict(policy),
        "capabilities": by_id,
        "updatedAt": _safe_text(data.get("updatedAt") or policy.get("updatedAt"), 80),
    }


def policy_for_pack(pack_id: Any, *, pack_name: Any = "") -> Dict[str, Any]:
    safe_pack_id = normalize_capability_pack_id(pack_id)
    pack_id_redacted = bool(str(pack_id or "").strip()) and not safe_pack_id
    payload = load_capability_policy()
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    packs = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    pack_policy = packs.get(safe_pack_id) if safe_pack_id else {}
    if not isinstance(pack_policy, dict):
        pack_policy = {}

    global_mode = _normalize_mode(policy.get("mode"), "ask")
    mode = _normalize_mode(pack_policy.get("mode"), global_mode)
    updated_at = _safe_text(pack_policy.get("updatedAt") or policy.get("updatedAt") or payload.get("updatedAt"), 80)
    name = _safe_label(pack_policy.get("name") or pack_name, safe_pack_id or "this capability pack")
    blocked = mode == "disabled"
    return {
        "packId": safe_pack_id or (_REDACTED_PACK_ID if pack_id_redacted else ""),
        "packIdRedacted": pack_id_redacted,
        "policyMode": mode,
        "installAllowed": not blocked,
        "disabledReason": f"Administrator disabled self-service installation for {name}." if blocked else "",
        "policyStatus": _safe_text(pack_policy.get("status"), 80),
        "policyUpdatedAt": updated_at,
        "policySource": payload.get("source") or "runtime-default",
        "policyAvailable": bool(payload.get("available")),
        "mirrorConfigured": bool(_safe_url(policy.get("mirror"))),
        "offlineCacheConfigured": bool(_safe_text(policy.get("offlineCache") or policy.get("offline_cache"), 300)),
    }


def apply_policy_to_capability(item: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(item or {})
    pack_id = result.get("packId") or result.get("id")
    if not result.get("packId") and result.get("kind") != "capability-pack":
        return result
    policy = policy_for_pack(pack_id, pack_name=result.get("label") or result.get("name") or pack_id)
    result.update(policy)
    if policy.get("policyMode") == "disabled":
        result["agentCanInstall"] = False
        state = result.get("capabilityState") if isinstance(result.get("capabilityState"), dict) else {}
        if not state.get("installed"):
            result["enabled"] = False
    return result


def blocked_install_payload(pack_id: Any, *, pack_name: Any = "", action: str = "install") -> Optional[Dict[str, Any]]:
    policy = policy_for_pack(pack_id, pack_name=pack_name)
    if policy.get("installAllowed"):
        return None
    return {
        "status": "error",
        "errorType": "capability_policy_blocked",
        "action": action,
        "packId": policy.get("packId") or _REDACTED_PACK_ID,
        "packIdRedacted": bool(policy.get("packIdRedacted")),
        "message": policy.get("disabledReason") or "Administrator disabled self-service installation for this capability pack.",
        "policy": _public_policy(policy),
    }


def public_policy_fields(pack_id: Any, *, pack_name: Any = "") -> Dict[str, Any]:
    return _public_policy(policy_for_pack(pack_id, pack_name=pack_name))


def normalize_capability_pack_id(value: Any) -> str:
    safe_pack_id = _safe_pack_id(value)
    return _PACK_POLICY_ALIASES.get(safe_pack_id, safe_pack_id)


def _public_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "policyMode",
        "installAllowed",
        "disabledReason",
        "policyStatus",
        "policyUpdatedAt",
        "policySource",
        "policyAvailable",
        "mirrorConfigured",
        "offlineCacheConfigured",
        "packIdRedacted",
    )
    result: Dict[str, Any] = {}
    for key in keys:
        value = policy.get(key)
        if key == "packIdRedacted":
            if value:
                result[key] = True
            continue
        if value not in (None, ""):
            result[key] = value
    return result


def _capability_policy_path() -> Optional[Path]:
    explicit = os.environ.get("ECOREX_CAPABILITY_POLICY_FILE")
    if explicit:
        return Path(explicit).expanduser()
    for env_name in ("ECOREX_DESKTOP_USER_DATA", "ECOREX_USER_DATA"):
        base = os.environ.get(env_name)
        if base:
            candidate = Path(base).expanduser() / "capability-policy.json"
            if candidate.exists():
                return candidate
    candidates = []
    if os.name == "nt":
        for env_name in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "EcoreX" / "capability-policy.json")
    candidates.append(Path.home() / ".config" / "ecorex" / "capability-policy.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _default_policy_payload(reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "source": "runtime-default" if reason == "none" else f"runtime-default-{reason}",
        "policy": {"mode": "ask"},
        "capabilities": {},
        "updatedAt": "",
    }


def _normalize_mode(value: Any, default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _POLICY_MODES else default


def _safe_pack_id(value: Any) -> str:
    raw_original = str(value or "").strip().lower()
    if _looks_sensitive(raw_original):
        return ""
    raw = raw_original.replace("_", "-")
    if 1 <= len(raw) <= 96 and all(char in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in raw):
        if _looks_sensitive(raw):
            return ""
        return raw
    return ""


def _safe_label(value: Any, fallback: str) -> str:
    raw = _safe_text(value, 120)
    if raw and not _looks_sensitive(raw):
        return raw
    return fallback


def _looks_sensitive(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    markers = (
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "api-key",
        "apikey",
        "api_key",
        "client-secret",
        "private-key",
        "private_key",
        "bearer",
        "sk-",
        "xoxb-",
        "ghp_",
        "ghp-",
        "github_pat",
        "github-pat",
        "pat-",
    )
    return any(marker in text for marker in markers)


def _safe_text(value: Any, limit: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("\r", " ").replace("\n", " ")
    return raw[:limit]


def _safe_url(value: Any) -> str:
    raw = _safe_text(value, 300)
    if raw.startswith(("https://", "http://")):
        return raw
    return ""

"""Scheduler delivery target resolution.

The scheduler stores durable tasks, while External Connections owns the
platform delivery target projection.  This module keeps that boundary explicit:
it resolves a single concrete platform/receiver from task action metadata and
home-channel config without creating channels or touching platform SDKs.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Mapping, Optional

from channel.channel_catalog import CHANNEL_CATALOG, normalize_channel_name, parse_channel_list
from config import conf


WEB_CHANNEL = "web"
UNKNOWN_CHANNELS = {"", "unknown"}


def _hash_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _task_action(task: Any) -> Dict[str, Any]:
    if not isinstance(task, dict):
        return {}
    action = task.get("action")
    return action if isinstance(action, dict) else {}


def _platform_tokens(raw_value: Any) -> list:
    if isinstance(raw_value, (list, tuple, set)):
        return parse_channel_list(raw_value)
    raw = str(raw_value or "").strip()
    if not raw:
        return []
    if "," in raw:
        return parse_channel_list(raw)
    name = normalize_channel_name(raw)
    return [name] if name else []


def _home_channel(config: Mapping[str, Any], platform: str) -> Dict[str, Any]:
    name = normalize_channel_name(platform)
    if not name or name == WEB_CHANNEL:
        return {}
    channel_id = str(config.get(f"{name}_home_channel") or "").strip()
    if not channel_id:
        return {}
    label = str(config.get(f"{name}_home_channel_name") or "").strip()
    result = {"platform": name, "receiver": channel_id}
    if label:
        result["receiver_name"] = label
    return result


def _candidate_home_channels(config: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    ordered = []

    def add(platform: str) -> None:
        name = normalize_channel_name(platform)
        if not name or name == WEB_CHANNEL or name in ordered:
            return
        ordered.append(name)

    for platform in parse_channel_list(config.get("channel_type", "")):
        add(platform)
    for platform in CHANNEL_CATALOG.keys():
        add(platform)
    for platform in ordered:
        target = _home_channel(config, platform)
        if target:
            yield target


def _public_target(
    *,
    ok: bool,
    platform: str = "",
    receiver: str = "",
    receiver_name: str = "",
    source: str = "",
    reason: str = "",
    home_channel_required: bool = False,
    home_channel_configured: bool = False,
) -> Dict[str, Any]:
    normalized = normalize_channel_name(platform)
    return {
        "ok": bool(ok),
        "channel_type": normalized,
        "receiver": str(receiver or ""),
        "receiver_name": str(receiver_name or ""),
        "source": source or "context",
        "reason": reason,
        "home_channel_required": bool(home_channel_required),
        "home_channel_configured": bool(home_channel_configured),
        "receiver_hash": _hash_text(receiver),
    }


def resolve_scheduler_delivery_target(
    task: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
    prefer_home_channel: bool = False,
) -> Dict[str, Any]:
    """Resolve a scheduled task to one concrete platform receiver.

    ``prefer_home_channel`` is used at task creation time from Web/default
    contexts.  Execution keeps it false so old Web tasks stay Web-bound even if
    a user later configures an external home channel.
    """
    cfg = dict(config if config is not None else conf())
    action = _task_action(task)
    raw_platform = action.get("channel_type")
    tokens = _platform_tokens(raw_platform)
    receiver = str(action.get("receiver") or "").strip()
    receiver_name = str(action.get("receiver_name") or "").strip()
    source = str(action.get("delivery_target_source") or action.get("deliveryTargetSource") or "").strip()
    requested_home_platform = normalize_channel_name(
        action.get("home_channel_platform") or action.get("homeChannelPlatform") or ""
    )
    home_required = bool(action.get("home_channel_required") or action.get("homeChannelRequired") or source == "home_channel")

    if requested_home_platform:
        tokens = [requested_home_platform]
        home_required = True
        source = "home_channel"

    if source == "home_channel" and not requested_home_platform and len(tokens) == 1:
        requested_home_platform = tokens[0]

    if requested_home_platform:
        home = _home_channel(cfg, requested_home_platform)
        if not home:
            return _public_target(
                ok=False,
                platform=requested_home_platform,
                source="home_channel",
                reason="scheduler_home_channel_missing",
                home_channel_required=True,
                home_channel_configured=False,
            )
        return _public_target(
            ok=True,
            platform=home["platform"],
            receiver=home["receiver"],
            receiver_name=home.get("receiver_name", receiver_name),
            source="home_channel",
            reason="projection-target",
            home_channel_required=True,
            home_channel_configured=True,
        )

    if prefer_home_channel and (not tokens or WEB_CHANNEL in tokens or len(tokens) > 1):
        home = next(_candidate_home_channels(cfg), None)
        if home:
            return _public_target(
                ok=True,
                platform=home["platform"],
                receiver=home["receiver"],
                receiver_name=home.get("receiver_name", receiver_name),
                source="home_channel",
                reason="projection-target",
                home_channel_required=True,
                home_channel_configured=True,
            )

    if len(tokens) == 1:
        platform = tokens[0]
    else:
        platform = ""

    if platform in UNKNOWN_CHANNELS:
        return _public_target(
            ok=False,
            platform=platform or "unknown",
            receiver=receiver,
            receiver_name=receiver_name,
            source=source or "unknown",
            reason="scheduler_channel_unknown",
            home_channel_required=False,
        )

    if platform != WEB_CHANNEL and not receiver:
        home = _home_channel(cfg, platform)
        if home:
            return _public_target(
                ok=True,
                platform=home["platform"],
                receiver=home["receiver"],
                receiver_name=home.get("receiver_name", receiver_name),
                source="home_channel",
                reason="projection-target",
                home_channel_required=True,
                home_channel_configured=True,
            )
        return _public_target(
            ok=False,
            platform=platform,
            source=source or "context",
            reason="scheduler_receiver_missing",
            home_channel_required=False,
        )

    if platform == WEB_CHANNEL and not receiver:
        return _public_target(
            ok=False,
            platform=platform,
            source=source or "web_context",
            reason="scheduler_receiver_missing",
            home_channel_required=False,
        )

    return _public_target(
        ok=True,
        platform=platform,
        receiver=receiver,
        receiver_name=receiver_name,
        source=source or ("web_context" if platform == WEB_CHANNEL else "context"),
        reason="context-target",
        home_channel_required=home_required,
        home_channel_configured=bool(home_required and _home_channel(cfg, platform)),
    )


def apply_scheduler_delivery_target(task: Dict[str, Any], target: Mapping[str, Any]) -> None:
    """Mutate a task action with the resolved concrete target."""
    if not isinstance(task, dict):
        return
    action = task.get("action")
    if not isinstance(action, dict):
        action = {}
        task["action"] = action
    if target.get("channel_type"):
        action["channel_type"] = target.get("channel_type")
    if target.get("receiver"):
        action["receiver"] = target.get("receiver")
    if target.get("receiver_name"):
        action["receiver_name"] = target.get("receiver_name")
    if target.get("source"):
        action["delivery_target_source"] = target.get("source")
    if target.get("home_channel_required"):
        action["home_channel_required"] = True
        action["home_channel_platform"] = target.get("channel_type")


def project_scheduler_delivery_target(
    action_or_task: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    task = action_or_task if isinstance(action_or_task, dict) and "action" in action_or_task else {"action": action_or_task}
    target = resolve_scheduler_delivery_target(task, config=config, prefer_home_channel=False)
    return {
        "status": "ready" if target.get("ok") else "blocked",
        "channelType": target.get("channel_type") or "unknown",
        "source": target.get("source") or "context",
        "reason": target.get("reason") or "",
        "receiverHash": target.get("receiver_hash") or "",
        "homeChannelRequired": bool(target.get("home_channel_required")),
        "homeChannelConfigured": bool(target.get("home_channel_configured")),
    }

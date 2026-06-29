"""Shared channel catalog for runtime configuration and discovery.

The catalog is intentionally read-only: importing it must never start a
channel, import vendor SDKs, or touch user credentials.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set


ChannelDef = Dict[str, Any]


def _field(key: str, label: str, field_type: str = "text", default: Any = "") -> Dict[str, Any]:
    item: Dict[str, Any] = {"key": key, "label": label, "type": field_type}
    if default != "":
        item["default"] = default
    return item


CHANNEL_CATALOG: "OrderedDict[str, ChannelDef]" = OrderedDict([
    ("weixin", {
        "aliases": ["wx"],
        "label": {"zh": "\u5fae\u4fe1", "en": "WeChat"},
        "description": "WeChat personal assistant channel.",
        "icon": "fa-comment",
        "color": "emerald",
        "fields": [],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "qr_login",
            "channel_authorization": "qr_login",
            "auth_endpoint": "/api/weixin/qrlogin",
            "auth_endpoint_methods": ["GET", "POST"],
        },
    }),
    ("feishu", {
        "aliases": ["lark"],
        "label": {"zh": "\u98de\u4e66", "en": "Feishu / Lark"},
        "description": "Feishu/Lark bot channel using app credentials and websocket events.",
        "icon": "fa-paper-plane",
        "color": "blue",
        "fields": [
            _field("feishu_app_id", "App ID"),
            _field("feishu_app_secret", "App Secret", "secret"),
        ],
        "provides": ["channel", "chat", "feishu"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "legacy_auth_endpoint": "/api/feishu/register",
            "auth_endpoint": "",
            "auth_endpoint_methods": [],
            "status_probe": "credential_configured_only",
        },
        "agent": {
            "tool": "feishu_cli",
            "install_ability": "feishu-cli",
            "install_pack": "feishu-lark",
            "policy": "find-skill-first-on-demand-cli",
            "permission_gated": True,
            "status_action": {"tool": "feishu_cli", "action": "status"},
            "authorization_action": {"tool": "feishu_cli", "action": "config_init"},
        },
    }),
    ("dingtalk", {
        "aliases": [],
        "label": {"zh": "\u9489\u9489", "en": "DingTalk"},
        "description": "DingTalk bot channel.",
        "icon": "fa-comments",
        "color": "blue",
        "fields": [
            _field("dingtalk_client_id", "Client ID"),
            _field("dingtalk_client_secret", "Client Secret", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("wecom_bot", {
        "aliases": ["wecom"],
        "label": {"zh": "\u4f01\u5fae\u667a\u80fd\u673a\u5668\u4eba", "en": "WeCom Bot"},
        "description": "WeCom bot channel.",
        "icon": "fa-robot",
        "color": "emerald",
        "fields": [
            _field("wecom_bot_id", "Bot ID"),
            _field("wecom_bot_secret", "Secret", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("qq", {
        "aliases": [],
        "label": {"zh": "QQ \u673a\u5668\u4eba", "en": "QQ Bot"},
        "description": "QQ bot channel.",
        "icon": "fa-comment",
        "color": "blue",
        "fields": [
            _field("qq_app_id", "App ID"),
            _field("qq_app_secret", "App Secret", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("wechatcom_app", {
        "aliases": ["wecom_app", "wechatcom"],
        "label": {"zh": "\u4f01\u5fae\u81ea\u5efa\u5e94\u7528", "en": "WeCom App"},
        "description": "WeCom self-built app channel.",
        "icon": "fa-building",
        "color": "emerald",
        "fields": [
            _field("wechatcom_corp_id", "Corp ID"),
            _field("wechatcomapp_agent_id", "Agent ID"),
            _field("wechatcomapp_secret", "Secret", "secret"),
            _field("wechatcomapp_token", "Token", "secret"),
            _field("wechatcomapp_aes_key", "AES Key", "secret"),
            _field("wechatcomapp_port", "Port", "number", 9898),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("wechat_kf", {
        "aliases": [],
        "label": {"zh": "\u5fae\u4fe1\u5ba2\u670d", "en": "WeChat Customer Service"},
        "description": "WeChat customer service channel.",
        "icon": "fa-headset",
        "color": "emerald",
        "fields": [
            _field("wechat_kf_corp_id", "Corp ID"),
            _field("wechat_kf_secret", "Secret", "secret"),
            _field("wechat_kf_token", "Token", "secret"),
            _field("wechat_kf_aes_key", "AES Key", "secret"),
            _field("wechat_kf_port", "Port", "number", 9888),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("wechatmp", {
        "aliases": [],
        "label": {"zh": "\u516c\u4f17\u53f7", "en": "WeChat MP"},
        "description": "WeChat official account passive reply channel.",
        "icon": "fa-comment-dots",
        "color": "emerald",
        "fields": [
            _field("wechatmp_app_id", "App ID"),
            _field("wechatmp_app_secret", "App Secret", "secret"),
            _field("wechatmp_token", "Token", "secret"),
            _field("wechatmp_aes_key", "AES Key", "secret"),
            _field("wechatmp_port", "Port", "number", 8080),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("wechatmp_service", {
        "aliases": [],
        "label": {"zh": "\u516c\u4f17\u53f7\u5ba2\u670d", "en": "WeChat MP Service"},
        "description": "WeChat official account customer-service channel.",
        "icon": "fa-comment-dots",
        "color": "emerald",
        "fields": [
            _field("wechatmp_app_id", "App ID"),
            _field("wechatmp_app_secret", "App Secret", "secret"),
            _field("wechatmp_token", "Token", "secret"),
            _field("wechatmp_aes_key", "AES Key", "secret"),
            _field("wechatmp_port", "Port", "number", 8080),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_app_credentials",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("telegram", {
        "aliases": [],
        "label": {"zh": "Telegram", "en": "Telegram"},
        "description": "Telegram bot channel.",
        "icon": "fa-paper-plane",
        "color": "sky",
        "fields": [
            _field("telegram_token", "Bot Token", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_token",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("slack", {
        "aliases": [],
        "label": {"zh": "Slack", "en": "Slack"},
        "description": "Slack bot channel.",
        "icon": "fa-hashtag",
        "color": "purple",
        "fields": [
            _field("slack_bot_token", "Bot Token (xoxb-)", "secret"),
            _field("slack_app_token", "App Token (xapp-)", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_tokens",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
    ("discord", {
        "aliases": [],
        "label": {"zh": "Discord", "en": "Discord"},
        "description": "Discord bot channel.",
        "icon": "fa-discord",
        "color": "indigo",
        "fields": [
            _field("discord_token", "Bot Token", "secret"),
        ],
        "provides": ["channel", "chat"],
        "auth": {
            "mode": "bot_token",
            "channel_authorization": "app_credentials",
            "status_probe": "credential_configured_only",
        },
    }),
])


def normalize_channel_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    lowered = name.lower()
    for canonical, definition in CHANNEL_CATALOG.items():
        if lowered == canonical or lowered in {str(alias).lower() for alias in definition.get("aliases", [])}:
            return canonical
    return lowered


def parse_channel_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    elif isinstance(raw, Iterable):
        parts = raw
    else:
        parts = []
    seen: Set[str] = set()
    parsed: List[str] = []
    for part in parts:
        name = normalize_channel_name(part)
        if not name or name in seen:
            continue
        seen.add(name)
        parsed.append(name)
    return parsed


def active_channel_set(config: Mapping[str, Any]) -> Set[str]:
    return set(parse_channel_list(config.get("channel_type", "")))


def channel_has_config(config: Mapping[str, Any], channel_name: str) -> bool:
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name)
    if not definition:
        return False
    for field in definition.get("fields", []):
        key = str(field.get("key") or "")
        if key and config.get(key) not in ("", None):
            return True
    return False


def _field_is_required(field: Mapping[str, Any]) -> bool:
    if not field.get("key"):
        return False
    if field.get("required") is False:
        return False
    if field.get("type") == "number" and "default" in field:
        return False
    if field.get("type") == "bool" and "default" in field:
        return False
    return True


def channel_config_status(config: Mapping[str, Any], channel_name: str) -> Dict[str, Any]:
    """Return a secret-free configuration completeness summary for one channel."""
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name)
    if not definition:
        return {
            "state": "unknown",
            "requiredFields": [],
            "presentFields": [],
            "missingFields": [],
            "hasAnyConfig": False,
        }

    required = [
        str(field.get("key"))
        for field in definition.get("fields", [])
        if _field_is_required(field)
    ]
    present = [
        key for key in required
        if config.get(key) not in ("", None)
    ]
    missing = [key for key in required if key not in present]
    if not required:
        state = "not_required"
    elif not present:
        state = "missing"
    elif missing:
        state = "partial"
    else:
        state = "configured"
    return {
        "state": state,
        "requiredFields": required,
        "presentFields": present,
        "missingFields": missing,
        "hasAnyConfig": channel_has_config(config, name),
    }


def channel_requires_complete_config(channel_name: str) -> bool:
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    auth = definition.get("auth") if isinstance(definition.get("auth"), dict) else {}
    required_fields = [
        field for field in definition.get("fields", [])
        if isinstance(field, Mapping) and _field_is_required(field)
    ]
    return bool(required_fields and auth.get("status_probe") == "credential_configured_only")


def channel_requires_runtime_authorization(channel_name: str) -> bool:
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    auth = definition.get("auth") if isinstance(definition.get("auth"), dict) else {}
    return str(auth.get("mode") or "") in {"qr_login"}


def channel_auth_surface(config: Mapping[str, Any], channel_name: str) -> Dict[str, Any]:
    """Describe channel authorization without touching remote services."""
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    auth = definition.get("auth") if isinstance(definition.get("auth"), dict) else {}
    agent = definition.get("agent") if isinstance(definition.get("agent"), dict) else {}
    config_status = channel_config_status(config, name)
    mode = str(auth.get("mode") or ("static_credentials" if definition.get("fields") else "none"))
    endpoint = str(auth.get("auth_endpoint") or "")
    methods = auth.get("auth_endpoint_methods") if isinstance(auth.get("auth_endpoint_methods"), list) else []
    auth_supported = bool(endpoint or definition.get("fields") or mode not in {"", "none"})
    return {
        "mode": mode,
        "channelAuthorization": str(auth.get("channel_authorization") or mode),
        "channelAuthSupported": auth_supported,
        "authEndpoint": endpoint,
        "authEndpointMethods": [str(item) for item in methods],
        "statusProbe": str(auth.get("status_probe") or ""),
        "channelConfigState": config_status["state"],
        "requiredFields": config_status["requiredFields"],
        "presentFields": config_status["presentFields"],
        "missingFields": config_status["missingFields"],
        "agentAuthSupported": bool(agent.get("authorization_action")),
        "agentAuthorizationAction": agent.get("authorization_action") or None,
    }


def channel_agent_surface(
    channel_name: str,
    tool_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Describe whether a channel has an agent-visible callable surface."""
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    agent = definition.get("agent") if isinstance(definition.get("agent"), dict) else {}
    tool = str(agent.get("tool") or "")
    tool_set = {str(item).strip() for item in tool_names or [] if str(item).strip()}
    has_tool_snapshot = tool_names is not None
    schema_visible = bool(tool and tool in tool_set) if has_tool_snapshot else None
    schema_callable = bool(tool and schema_visible is True)
    requires_status_probe = bool(agent.get("status_action"))
    callable_now = False
    if not tool:
        status = "not_applicable"
    elif schema_visible is True:
        status = "schema_visible_unverified" if requires_status_probe else "schema_visible"
    elif has_tool_snapshot:
        status = "tool_not_loaded"
    else:
        status = "unknown"
    if not tool:
        readiness = "not_applicable"
        callable_reason = "no agent tool is declared for this channel"
    elif schema_visible is True and requires_status_probe:
        readiness = "unverified"
        callable_reason = "tool schema is visible, but CLI/auth readiness requires an explicit status probe"
    elif schema_visible is True:
        readiness = "schema_visible"
        callable_reason = "tool schema is visible; no remote readiness probe is declared"
    elif has_tool_snapshot:
        readiness = "tool_not_loaded"
        callable_reason = "declared tool schema is not loaded in the current agent snapshot"
    else:
        readiness = "unknown"
        callable_reason = "agent tool snapshot is not loaded for this read-only status query"
    return {
        "tool": tool,
        "declaredDiscoverable": bool(tool),
        "schemaVisible": schema_visible,
        "discoverable": bool(schema_visible) if has_tool_snapshot else bool(tool),
        "toolSchemaCallable": schema_callable,
        "callable": callable_now,
        "readiness": readiness,
        "callableReason": callable_reason,
        "requiresStatusProbe": requires_status_probe,
        "permissionGated": bool(agent.get("permission_gated", False)),
        "policy": str(agent.get("policy") or ""),
        "installAbility": str(agent.get("install_ability") or ""),
        "installPack": str(agent.get("install_pack") or ""),
        "statusAction": agent.get("status_action") or None,
        "authorizationAction": agent.get("authorization_action") or None,
        "status": status,
    }


def channel_observability(
    config: Mapping[str, Any],
    channel_name: str,
    *,
    running_channels: Optional[Iterable[str]] = None,
    runtime_state: Optional[Mapping[str, Any]] = None,
    tool_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Build the v0.2.2 channel state projection used by API/tests/UI."""
    name = normalize_channel_name(channel_name)
    active_channels = active_channel_set(config)
    running = {normalize_channel_name(item) for item in (running_channels or [])}
    runtime = dict(runtime_state or {})
    cfg_status = channel_config_status(config, name)
    runtime_status = str(runtime.get("status") or "")
    requires_complete_config = channel_requires_complete_config(name)
    requires_runtime_authorization = channel_requires_runtime_authorization(name)
    runtime_authorized = bool(
        requires_runtime_authorization
        and (name in running or runtime_status in {"active", "connected", "ready"})
    )
    if requires_complete_config and cfg_status["state"] in {"missing", "partial"}:
        configured = False
    elif requires_runtime_authorization and cfg_status["state"] == "not_required":
        configured = runtime_authorized
    elif cfg_status["state"] in {"configured", "not_required"}:
        configured = True
    elif name == "web" and name in active_channels:
        configured = True
    else:
        configured = name in active_channels or cfg_status["hasAnyConfig"]
    if requires_complete_config and not configured:
        if runtime_status == "error":
            status = "error"
        elif name in active_channels or runtime_status in {"active", "connected", "ready", "starting"}:
            status = "blocked"
        else:
            status = "available"
    elif requires_runtime_authorization and not configured:
        status = runtime_status if runtime_status == "error" else ("auth_required" if name in active_channels else "available")
    else:
        status = runtime_status or (
            "active" if name in active_channels else ("configured" if configured else "available")
        )
    running_state = name in running
    if (requires_complete_config or requires_runtime_authorization) and not configured:
        running_state = False
    config_state = cfg_status["state"]
    auth_surface = channel_auth_surface(config, name)
    if requires_runtime_authorization and not configured:
        config_state = "auth_required"
        auth_surface = {
            **auth_surface,
            "channelConfigState": config_state,
            "runtimeAuthorizationRequired": True,
            "runtimeAuthorized": False,
        }
    elif requires_runtime_authorization:
        auth_surface = {
            **auth_surface,
            "runtimeAuthorizationRequired": True,
            "runtimeAuthorized": True,
        }
    return {
        "active": name in active_channels,
        "configured": configured,
        "running": running_state,
        "status": status,
        "configState": config_state,
        "auth": auth_surface,
        "agentSurface": channel_agent_surface(name, tool_names),
    }


def channel_config_refs(channel_name: str) -> List[Dict[str, str]]:
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    refs = [{"path": "config.channel_type", "key": name}]
    for field in definition.get("fields", []):
        key = str(field.get("key") or "")
        if key:
            refs.append({"path": f"config.{key}", "key": key})
    return refs

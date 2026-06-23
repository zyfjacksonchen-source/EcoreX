"""Shared channel catalog for runtime configuration and discovery.

The catalog is intentionally read-only: importing it must never start a
channel, import vendor SDKs, or touch user credentials.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Set


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
    }),
    ("wecom_bot", {
        "aliases": [],
        "label": {"zh": "\u4f01\u5fae\u667a\u80fd\u673a\u5668\u4eba", "en": "WeCom Bot"},
        "description": "WeCom bot channel.",
        "icon": "fa-robot",
        "color": "emerald",
        "fields": [
            _field("wecom_bot_id", "Bot ID"),
            _field("wecom_bot_secret", "Secret", "secret"),
        ],
        "provides": ["channel", "chat"],
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
    }),
    ("wechatcom_app", {
        "aliases": [],
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


def channel_config_refs(channel_name: str) -> List[Dict[str, str]]:
    name = normalize_channel_name(channel_name)
    definition = CHANNEL_CATALOG.get(name, {})
    refs = [{"path": "config.channel_type", "key": name}]
    for field in definition.get("fields", []):
        key = str(field.get("key") or "")
        if key:
            refs.append({"path": f"config.{key}", "key": key})
    return refs

"""Runtime-safe projection of the predecessor messaging-channel catalog."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


def _field(
    key: str,
    label: str,
    field_type: str = "text",
    default: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"key": key, "label": label, "type": field_type}
    if default is not None:
        value["default"] = default
    return value


def _channel(
    label: str,
    description: str,
    icon: str,
    fields: list[dict[str, Any]],
    *,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "aliases": list(aliases),
        "label": {"zh": label},
        "description": description,
        "icon": icon,
        "fields": fields,
    }


# Keep this product projection self-contained: signed Runtime archives contain
# ``ecorex`` only. A focused test locks these fields to the verified baseline.
CHANNEL_CATALOG: OrderedDict[str, dict[str, Any]] = OrderedDict(
    [
        (
            "weixin",
            _channel(
                "微信",
                "WeChat personal assistant channel.",
                "fa-comment",
                [],
                aliases=("wx",),
            ),
        ),
        (
            "feishu",
            _channel(
                "飞书",
                "Feishu/Lark bot channel using app credentials and websocket events.",
                "fa-paper-plane",
                [
                    _field("feishu_app_id", "App ID"),
                    _field("feishu_app_secret", "App Secret", "secret"),
                ],
                aliases=("lark",),
            ),
        ),
        (
            "dingtalk",
            _channel(
                "钉钉",
                "DingTalk bot channel.",
                "fa-comments",
                [
                    _field("dingtalk_client_id", "Client ID"),
                    _field("dingtalk_client_secret", "Client Secret", "secret"),
                ],
            ),
        ),
        (
            "wecom_bot",
            _channel(
                "企微智能机器人",
                "WeCom bot channel.",
                "fa-robot",
                [
                    _field("wecom_bot_id", "Bot ID"),
                    _field("wecom_bot_secret", "Secret", "secret"),
                ],
                aliases=("wecom",),
            ),
        ),
        (
            "qq",
            _channel(
                "QQ 机器人",
                "QQ bot channel.",
                "fa-comment",
                [
                    _field("qq_app_id", "App ID"),
                    _field("qq_app_secret", "App Secret", "secret"),
                ],
            ),
        ),
        (
            "wechatcom_app",
            _channel(
                "企微自建应用",
                "WeCom self-built app channel.",
                "fa-building",
                [
                    _field("wechatcom_corp_id", "Corp ID"),
                    _field("wechatcomapp_agent_id", "Agent ID"),
                    _field("wechatcomapp_secret", "Secret", "secret"),
                    _field("wechatcomapp_token", "Token", "secret"),
                    _field("wechatcomapp_aes_key", "AES Key", "secret"),
                    _field("wechatcomapp_port", "Port", "number", 9898),
                ],
                aliases=("wecom_app", "wechatcom"),
            ),
        ),
        (
            "wechat_kf",
            _channel(
                "微信客服",
                "WeChat customer service channel.",
                "fa-headset",
                [
                    _field("wechat_kf_corp_id", "Corp ID"),
                    _field("wechat_kf_secret", "Secret", "secret"),
                    _field("wechat_kf_token", "Token", "secret"),
                    _field("wechat_kf_aes_key", "AES Key", "secret"),
                    _field("wechat_kf_port", "Port", "number", 9888),
                ],
            ),
        ),
        (
            "wechatmp",
            _channel(
                "公众号",
                "WeChat official account passive reply channel.",
                "fa-comment-dots",
                [
                    _field("wechatmp_app_id", "App ID"),
                    _field("wechatmp_app_secret", "App Secret", "secret"),
                    _field("wechatmp_token", "Token", "secret"),
                    _field("wechatmp_aes_key", "AES Key", "secret"),
                    _field("wechatmp_port", "Port", "number", 8080),
                ],
            ),
        ),
        (
            "wechatmp_service",
            _channel(
                "公众号客服",
                "WeChat official account customer-service channel.",
                "fa-comment-dots",
                [
                    _field("wechatmp_app_id", "App ID"),
                    _field("wechatmp_app_secret", "App Secret", "secret"),
                    _field("wechatmp_token", "Token", "secret"),
                    _field("wechatmp_aes_key", "AES Key", "secret"),
                    _field("wechatmp_port", "Port", "number", 8080),
                ],
            ),
        ),
        (
            "telegram",
            _channel(
                "Telegram",
                "Telegram bot channel.",
                "fa-paper-plane",
                [_field("telegram_token", "Bot Token", "secret")],
            ),
        ),
        (
            "slack",
            _channel(
                "Slack",
                "Slack bot channel.",
                "fa-hashtag",
                [
                    _field("slack_bot_token", "Bot Token (xoxb-)", "secret"),
                    _field("slack_app_token", "App Token (xapp-)", "secret"),
                ],
            ),
        ),
        (
            "discord",
            _channel(
                "Discord",
                "Discord bot channel.",
                "fa-discord",
                [_field("discord_token", "Bot Token", "secret")],
            ),
        ),
    ]
)


def normalize_channel_name(value: Any) -> str:
    name = str(value or "").strip().casefold()
    if not name:
        return ""
    for canonical, definition in CHANNEL_CATALOG.items():
        if name == canonical or name in definition["aliases"]:
            return canonical
    return name


__all__ = ["CHANNEL_CATALOG", "normalize_channel_name"]

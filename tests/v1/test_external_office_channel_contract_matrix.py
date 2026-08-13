from __future__ import annotations

import json
from pathlib import Path

from channel.channel_catalog import CHANNEL_CATALOG
from ecorex.connectors.builtin import builtin_connector_definitions
from ecorex.pack_catalog import COW_RUNTIME_SOURCE_ROOTS


ROOT = Path(__file__).resolve().parents[2]


def test_priority_office_channel_matrix_declares_only_real_capabilities() -> None:
    assert {
        channel_id: {
            "auth": CHANNEL_CATALOG[channel_id]["auth"]["mode"],
            "fields": tuple(
                field["key"] for field in CHANNEL_CATALOG[channel_id]["fields"]
            ),
        }
        for channel_id in ("feishu", "weixin", "dingtalk")
    } == {
        "feishu": {
            "auth": "bot_app_credentials",
            "fields": ("feishu_app_id", "feishu_app_secret"),
        },
        "weixin": {"auth": "qr_login", "fields": ()},
        "dingtalk": {
            "auth": "bot_app_credentials",
            "fields": ("dingtalk_client_id", "dingtalk_client_secret"),
        },
    }

    definitions = {
        definition.connector_id: definition
        for definition in builtin_connector_definitions()
    }
    assert tuple(action.action_id for action in definitions["feishu"].actions) == (
        "documents.read",
        "documents.write",
        "drive.search",
        "messages.send",
    )
    assert tuple(event.event_id for event in definitions["feishu"].events) == (
        "messages.received",
    )
    assert tuple(
        action.action_id for action in definitions["tencent-docs"].actions
    ) == ("documents.list", "documents.read", "documents.write")
    assert definitions["tencent-docs"].events == ()
    assert all(
        "documents.create" not in {action.action_id for action in definition.actions}
        for definition in (
            definitions["feishu"],
            definitions["tencent-docs"],
        )
    )


def test_all_other_declared_channels_keep_cow_send_and_callback_contract() -> None:
    other_channels = {
        "wecom_bot",
        "qq",
        "wechatcom_app",
        "wechat_kf",
        "wechatmp",
        "wechatmp_service",
        "telegram",
        "slack",
        "discord",
    }
    assert set(CHANNEL_CATALOG) == {"feishu", "weixin", "dingtalk"} | other_channels
    assert all(
        definition["provides"] == ["channel", "chat"]
        for channel_id, definition in CHANNEL_CATALOG.items()
        if channel_id != "feishu"
    )

    definitions = {
        definition.connector_id: definition
        for definition in builtin_connector_definitions()
    }
    for channel_id in other_channels | {"weixin", "dingtalk"}:
        assert tuple(action.action_id for action in definitions[channel_id].actions) == (
            "messages.send",
        )
        assert tuple(event.event_id for event in definitions[channel_id].events) == (
            "messages.received",
        )


def test_packaged_contract_keeps_channels_and_tencent_docs_on_cow_runtime_paths() -> None:
    packaged = json.loads(
        (ROOT / "release/capability-packs/channels/connector-contracts.json").read_text(
            encoding="utf-8"
        )
    )

    assert {
        item["connector_id"]: tuple(item["operations"])
        for item in packaged["connectors"]
    } == {
        "feishu": ("catalog", "describe", "read", "write", "health"),
        "tencent-docs": ("catalog", "describe", "read", "write", "health"),
    }
    assert {"agent", "bridge", "channel", "common"} <= set(COW_RUNTIME_SOURCE_ROOTS)

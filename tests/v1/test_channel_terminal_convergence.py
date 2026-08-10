from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Callable

import pytest

from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.dingtalk import _DingTalkStore
from ecorex.connectors.discord import _DiscordStore
from ecorex.connectors.feishu import _FeishuStore
from ecorex.connectors.qq import _QQStore
from ecorex.connectors.slack import _SlackStore
from ecorex.connectors.telegram import _TelegramStore
from ecorex.connectors.wechat_callback import _Store as _ManagedWechatStore
from ecorex.connectors.wecom_bot import _WeComStore
from ecorex.connectors.weixin import _WeixinStore


_OWNER = ChannelCredentialOwner("account-a", "organization-a")
_RAW_COLUMNS = {
    "binding_id",
    "conversation_id",
    "envelope_id",
    "lease_id",
    "marker",
    "message_id",
    "reply_message_id",
    "reply_url",
    "target_id",
    "text",
}


@dataclass(frozen=True)
class _Case:
    channel_id: str
    table: str
    store: Callable[[Path, ChannelCredentialOwner], Any]
    initial_state: str
    finish_name: str
    error_code: str
    pending_name: str


_CASES = (
    _Case("weixin", "weixin_pending", _WeixinStore, "pending", "finish_pending", "weixin_delivery_rejected", "pending"),
    _Case("feishu", "feishu_pending", _FeishuStore, "pending", "finish_pending", "feishu_bot_delivery_rejected", "pending"),
    _Case("telegram", "telegram_pending", _TelegramStore, "pending", "finish_pending", "telegram_delivery_rejected", "pending"),
    _Case("dingtalk", "dingtalk_events", _DingTalkStore, "outbound", "finish", "dingtalk_delivery_rejected", "outbound"),
    _Case("slack", "slack_events", _SlackStore, "outbound", "finish", "slack_delivery_rejected", "outbound"),
    _Case("discord", "discord_events", _DiscordStore, "outbound", "finish", "discord_delivery_rejected", "outbound"),
    _Case("wecom_bot", "wecom_bot_events", _WeComStore, "outbound", "finish", "wecom_bot_delivery_rejected", "outbound"),
    _Case("wechatcom_app", "managed_wechat_events", _ManagedWechatStore, "outbound", "finish", "managed_wechat_delivery_failed", "outbound"),
    _Case("wechat_kf", "managed_wechat_events", _ManagedWechatStore, "outbound", "finish", "managed_wechat_delivery_failed", "outbound"),
    _Case("wechatmp_service", "managed_wechat_events", _ManagedWechatStore, "outbound", "finish", "managed_wechat_delivery_failed", "outbound"),
    _Case("qq", "qq_events", _QQStore, "outbound", "finish", "qq_delivery_rejected", "outbound"),
)


def _insert_outbound(store: Any, case: _Case, turn_id: str, raw: str) -> None:
    store.terminal_error()
    with sqlite3.connect(store.path) as connection:
        columns = connection.execute(f"PRAGMA table_info({case.table})").fetchall()
        values: dict[str, Any] = {}
        for column in columns:
            name = str(column[1])
            declared_type = str(column[2]).upper()
            if name == "scope":
                value: Any = store.scope
            elif name == "state":
                value = case.initial_state
            elif name == "turn_id":
                value = turn_id
            elif name == "channel_id":
                value = case.channel_id
            elif name == "route":
                value = "c2c"
            elif name == "error_code":
                value = None
            elif name in _RAW_COLUMNS:
                value = raw
            elif name == "event_key":
                value = f"event-hash-{case.channel_id}"
            elif name == "event_id":
                value = f"event-id-{case.channel_id}"
            elif "INT" in declared_type:
                value = 1
            else:
                value = f"safe-{name}-{case.channel_id}"
            values[name] = value
        names = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO {case.table}({names}) VALUES({placeholders})",
            tuple(values.values()),
        )
        if case.channel_id == "weixin":
            connection.execute(
                "INSERT INTO weixin_context(scope,conversation_id,context_token) "
                "VALUES(?,?,?)",
                (store.scope, raw, raw),
            )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.channel_id)
@pytest.mark.parametrize(
    "terminal_state",
    ("completed", "failed", "uncertain"),
)
def test_all_channel_journals_converge_and_scrub_terminal_rows(
    tmp_path: Path,
    case: _Case,
    terminal_state: str,
) -> None:
    path = tmp_path / f"{case.channel_id}-{terminal_state}.db"
    store = case.store(path, _OWNER)
    turn_id = f"turn-{case.channel_id}-{terminal_state}"
    raw = f"raw-conversation-and-body-{case.channel_id}-{terminal_state}"
    _insert_outbound(store, case, turn_id, raw)

    finish = getattr(store, case.finish_name)
    finish(
        turn_id,
        terminal_state,
        None if terminal_state == "completed" else case.error_code,
    )

    assert getattr(store, case.pending_name)() == ()
    restarted = case.store(path, _OWNER)
    terminal = restarted.terminal_error()
    if terminal_state != "uncertain":
        assert terminal is None
    elif case.channel_id == "qq":
        assert terminal == case.error_code
    else:
        assert terminal == (case.error_code, True)
    assert raw.encode() not in path.read_bytes()

    if terminal_state == "uncertain":
        restarted.resolve_uncertain()
        assert case.store(path, _OWNER).terminal_error() is None
        assert getattr(case.store(path, _OWNER), case.pending_name)() == ()


_DELIVERY_STORES = tuple(
    case for case in _CASES if not case.channel_id.startswith("wechat")
)


@pytest.mark.parametrize(
    "case", _DELIVERY_STORES, ids=lambda case: case.channel_id
)
def test_direct_channel_delivery_restart_keeps_retryable_uncertain_and_failed_distinct(
    tmp_path: Path, case: _Case
) -> None:
    path = tmp_path / f"{case.channel_id}-delivery.db"
    store = case.store(path, _OWNER)

    assert store.claim_delivery("retryable") == "send"
    store.release_delivery("retryable")
    assert case.store(path, _OWNER).claim_delivery("retryable") == "send"

    assert store.claim_delivery("uncertain") == "send"
    restarted = case.store(path, _OWNER)
    terminal = restarted.terminal_error()
    if case.channel_id == "qq":
        assert terminal == "qq_delivery_uncertain"
    else:
        assert terminal is not None and terminal[1] is True
    assert restarted.claim_delivery("uncertain") == "uncertain"
    restarted.resolve_uncertain()

    assert store.claim_delivery("failed") == "send"
    store.mark_delivery("failed", "failed")
    restarted = case.store(path, _OWNER)
    assert restarted.claim_delivery("failed") == "failed"
    assert restarted.terminal_error() is None

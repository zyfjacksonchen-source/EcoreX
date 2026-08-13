from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bridge.context import Context
from bridge.reply import ReplyType
from ecorex.connectors.channel_runtime import (
    ChannelInboundMessage,
    ChannelOutboundReply,
    ChannelTurnReceipt,
)
from ecorex.connectors.cow_channel import CowChannelRuntimeBridge, CowChannelService
from ecorex.connectors.channel_self_service import (
    ChannelCredentialOwner,
    ChannelSelfService,
)
from ecorex.connectors.vault import InMemoryCredentialVault
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus


class _Manager:
    def __init__(self) -> None:
        self.started: list[tuple[list[str], bool]] = []
        self.stopped: list[str | None] = []
        self.restarted: list[str] = []
        self.added: list[str] = []
        self.removed: list[str] = []
        self.channels: dict[str, object] = {}

    def start(self, channel_names: list[str], first_start: bool = False) -> None:
        self.started.append((channel_names, first_start))

    def stop(self, channel_name: str | None = None) -> None:
        self.stopped.append(channel_name)

    def restart(self, channel_name: str) -> None:
        self.restarted.append(channel_name)

    def add_channel(self, channel_name: str) -> None:
        self.added.append(channel_name)

    def remove_channel(self, channel_name: str) -> None:
        self.removed.append(channel_name)

    def get_channel(self, channel_name: str):
        return self.channels.get(channel_name)


def test_cow_channel_service_starts_official_config_without_managed_session() -> None:
    manager = _Manager()
    service = CowChannelService(
        manager=manager,
        config={"channel_type": "telegram, feishu, web"},
    )

    service.start_sync()
    service.stop_sync()

    assert manager.started == [(["telegram", "feishu"], True)]
    assert manager.stopped == [None]


def test_native_weixin_exposes_qr_authorization_instead_of_hidden_terminal_login() -> None:
    service = CowChannelService(manager=_Manager(), config={})

    weixin = next(
        item for item in service.catalog()["items"] if item["channel_id"] == "weixin"
    )

    assert weixin["auth_kind"] == "device_code"
    assert weixin["actions"]["auth_begin"] is True
    assert weixin["actions"]["save"] is False


def test_native_weixin_device_actions_project_the_same_cow_login_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        login_status="waiting_scan",
        _current_qr_url="https://weixin.qq.com/q/cow-login",
    )
    manager = _Manager()
    manager.channels["weixin"] = channel
    native = CowChannelService(
        manager=manager,
        config={"channel_type": "weixin"},
    )
    native.started = True
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account", "organization"),
        vault=InMemoryCredentialVault(),
        native_service=native,
    )
    monkeypatch.setattr(
        "ecorex.connectors.cow_channel._qr_png_data_url",
        lambda _value: "data:image/png;base64,cWl4",
    )

    pending = service.begin_authorization("weixin", request_id="begin")
    channel.login_status = "scanned"
    scanned = service.poll_authorization(
        "weixin", pending["flow_id"], request_id="poll-scanned"
    )
    channel.login_status = "logged_in"
    channel._current_qr_url = ""
    confirmed = service.poll_authorization(
        "weixin", pending["flow_id"], request_id="poll-confirmed"
    )

    assert pending["status"] == "pending"
    assert pending["verification_url"] == "https://weixin.qq.com/q/cow-login"
    assert pending["qr_image_data_url"] == "data:image/png;base64,cWl4"
    assert scanned["status"] == "scanned"
    assert confirmed["status"] == "confirmed"
    assert confirmed["instance"]["state"] == "connected"
    assert confirmed["instance"]["health"] == "connected"


def test_native_weixin_cancel_and_refresh_control_the_same_cow_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        login_status="waiting_scan",
        _current_qr_url="https://weixin.qq.com/q/first",
    )
    manager = _Manager()
    manager.channels["weixin"] = channel
    native = CowChannelService(
        manager=manager,
        config={"channel_type": "weixin"},
    )
    native.started = True
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account", "organization"),
        vault=InMemoryCredentialVault(),
        native_service=native,
    )
    monkeypatch.setattr(
        "ecorex.connectors.cow_channel._qr_png_data_url",
        lambda value: "data:image/png;base64," + value.rsplit("/", 1)[-1],
    )

    first = service.begin_authorization("weixin", request_id="begin")
    channel._current_qr_url = "https://weixin.qq.com/q/refreshed"
    refreshed = service.refresh_authorization(
        "weixin", first["flow_id"], request_id="refresh"
    )
    cancelled = service.cancel_authorization(
        "weixin", first["flow_id"], request_id="cancel"
    )

    assert refreshed["flow_id"] == first["flow_id"]
    assert refreshed["verification_url"].endswith("/refreshed")
    assert manager.restarted == ["weixin"]
    assert cancelled["status"] == "cancelled"
    assert manager.removed == ["weixin"]


def test_cow_channel_ui_edits_the_live_config_and_manager(tmp_path: Path) -> None:
    manager = _Manager()
    config_path = tmp_path / "config.json"
    service = CowChannelService(manager=manager, config_path=config_path)
    service.started = True

    saved = service.save(
        "telegram",
        display_name="Telegram Bot",
        config={},
        secrets={"telegram_token": "token-one"},
    )
    enabled = service.enable("telegram")
    service.save(
        "telegram",
        display_name="Telegram Bot",
        config={},
        secrets={"telegram_token": "token-two"},
    )
    service.disable("telegram")
    service.remove("telegram")

    assert saved["enabled"] is False
    assert enabled["enabled"] is True
    assert manager.added == ["telegram"]
    assert manager.restarted == ["telegram"]
    assert manager.removed == ["telegram", "telegram"]
    assert json.loads(config_path.read_text(encoding="utf-8")) == {}


def test_scheduler_reply_uses_the_same_native_channel_for_text_and_file() -> None:
    sent: list[tuple[object, Context]] = []
    channel = SimpleNamespace(send=lambda reply, context: sent.append((reply, context)))
    manager = _Manager()
    manager.channels["telegram"] = channel
    service = CowChannelService(manager=manager, config={})

    service.send_outbound(
        "telegram",
        conversation_id="conversation-1",
        receiver="receiver-1",
        is_group=True,
        text="报告已生成",
        attachment={
            "file_type": "document",
            "path": "/tmp/report.pdf",
            "file_name": "report.pdf",
        },
    )

    assert [reply.type for reply, _context in sent] == [
        ReplyType.TEXT,
        ReplyType.FILE,
    ]
    assert sent[0][0].content == "报告已生成"
    assert sent[1][0].content == "file:///tmp/report.pdf"
    assert sent[1][0].file_name == "report.pdf"
    assert sent[0][1] is sent[1][1]
    assert sent[0][1].get("session_id") == "conversation-1"
    assert sent[0][1].get("receiver") == "receiver-1"
    assert sent[0][1].get("isgroup") is True
    assert sent[0][1].get("telegram_chat_id") == "receiver-1"


def test_all_public_cow_channels_import_and_construct_without_network(tmp_path: Path) -> None:
    script = r'''
import os
import socket
import sys
sys.path.insert(0, os.getcwd())
from config import conf
from channel.channel_catalog import CHANNEL_CATALOG
from channel.channel_factory import create_channel

def blocked(*_args, **_kwargs):
    raise AssertionError("channel constructor attempted network access")

socket.create_connection = blocked
socket.socket.connect = blocked
conf().update({
    "wechatcom_corp_id": "corp", "wechatcomapp_agent_id": "agent",
    "wechatcomapp_secret": "secret", "wechatcomapp_token": "token",
    "wechatcomapp_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    "wechat_kf_corp_id": "corp", "wechat_kf_secret": "secret",
    "wechat_kf_token": "token",
    "wechat_kf_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    "wechatmp_app_id": "app", "wechatmp_app_secret": "secret",
    "wechatmp_token": "token",
    "wechatmp_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
})
created = []
for name in CHANNEL_CATALOG:
    channel = create_channel(name)
    created.append(channel.channel_type)
    channel.stop()
assert created == list(CHANNEL_CATALOG)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**dict(__import__("os").environ), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class _Dispatcher:
    def __init__(self, reply: ChannelOutboundReply) -> None:
        self.reply = reply
        self.messages: list[ChannelInboundMessage] = []

    def dispatch(self, message: ChannelInboundMessage) -> ChannelTurnReceipt:
        self.messages.append(message)
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-1",
            turn_id="turn-1",
            client_message_id="message-1",
            conversation_sha256="conversation-hash",
        )

    def wait_for_reply(self, _receipt, *, timeout_seconds: float):
        assert timeout_seconds > 0
        return self.reply


def test_runtime_bridge_returns_file_to_the_current_cow_channel() -> None:
    dispatcher = _Dispatcher(
        ChannelOutboundReply(
            channel_id="telegram",
            turn_id="turn-1",
            item_id="message-item",
            text="报告已生成",
            attachment={
                "type": "file_to_send",
                "file_type": "document",
                "path": "/tmp/report.pdf",
                "file_name": "report.pdf",
            },
        )
    )
    bridge = CowChannelRuntimeBridge(dispatcher, timeout_seconds=1)
    context = Context()
    context.kwargs = {
        "channel_type": "telegram",
        "session_id": "chat-7",
        "receiver": "receiver-9",
        "isgroup": False,
        "msg": SimpleNamespace(msg_id="vendor-message-3"),
    }

    reply = bridge("生成报告", context)

    assert reply.type is ReplyType.FILE
    assert reply.content == "file:///tmp/report.pdf"
    assert reply.file_name == "report.pdf"
    assert reply.text_content == "报告已生成"
    assert dispatcher.messages == [
        ChannelInboundMessage(
            channel_id="telegram",
            conversation_id="chat-7",
            message_id="vendor-message-3",
            text="生成报告",
            receiver="receiver-9",
            is_group=False,
        )
    ]


def test_native_cow_channel_routes_the_current_reply_through_runtime() -> None:
    from channel.channel import Channel
    from channel.runtime_bridge import bind_runtime_bridge, unbind_runtime_bridge

    calls = []

    def runtime_bridge(query, context):
        calls.append((query, context.get("channel_type")))
        return SimpleNamespace(type=ReplyType.TEXT, content="runtime answer")

    channel = Channel()
    channel.channel_type = "telegram"
    context = Context(kwargs={})
    bind_runtime_bridge(runtime_bridge)
    try:
        reply = channel.build_reply_content("hello", context)
    finally:
        unbind_runtime_bridge(runtime_bridge)

    assert reply.content == "runtime answer"
    assert calls == [("hello", "telegram")]


def test_dispatcher_projects_a_file_only_cow_turn() -> None:
    from ecorex.connectors.channel_runtime import ChannelRuntimeDispatcher
    from ecorex.connectors.channel_self_service import ChannelCredentialOwner

    projection = SimpleNamespace(
        turns=[SimpleNamespace(turn_id="turn-1", status=TurnStatus.COMPLETED)],
        items=[
            SimpleNamespace(
                item_id="artifact-1",
                turn_id="turn-1",
                kind=ItemKind.ARTIFACT,
                status=ItemStatus.COMPLETED,
                content={
                    "type": "file_to_send",
                    "file_type": "document",
                    "path": "/tmp/report.pdf",
                },
            )
        ],
    )
    dispatcher = ChannelRuntimeDispatcher(
        owner=ChannelCredentialOwner("account", "organization"),
        composition=SimpleNamespace(),
        kernel=SimpleNamespace(projection=lambda _thread_id: projection),
        worker=SimpleNamespace(),
    )

    outbound = dispatcher.project_outbound_reply(
        ChannelTurnReceipt(
            channel_id="telegram",
            thread_id="thread-1",
            turn_id="turn-1",
            client_message_id="message-1",
            conversation_sha256="conversation-hash",
        )
    )

    assert outbound is not None
    assert outbound.text == ""
    assert outbound.attachment["path"] == "/tmp/report.pdf"


def test_channel_dispatcher_keeps_home_channel_context_for_cow_scheduler() -> None:
    created: dict[str, object] = {}

    class _Kernel:
        def create_thread(self, request):
            created["thread"] = request
            return SimpleNamespace(thread_id="thread-1")

        def create_turn(self, _thread_id, request, **_kwargs):
            created["turn"] = request
            return SimpleNamespace(
                turn=SimpleNamespace(turn_id="turn-1", status=TurnStatus.QUEUED)
            )

    class _Composition:
        permission_account_id = "account"

        @staticmethod
        def admit_turn(request, accept, *, thread_id=None):
            return accept(SimpleNamespace(request=request, snapshot_context=None))

    from ecorex.connectors.channel_runtime import ChannelRuntimeDispatcher
    from ecorex.connectors.channel_self_service import ChannelCredentialOwner

    dispatcher = ChannelRuntimeDispatcher(
        owner=ChannelCredentialOwner("account", "organization"),
        composition=_Composition(),
        kernel=_Kernel(),
        worker=SimpleNamespace(notify=lambda: None),
    )
    dispatcher.dispatch(
        ChannelInboundMessage(
            channel_id="telegram",
            conversation_id="conversation-1",
            message_id="message-1",
            text="稍后提醒我",
            receiver="receiver-1",
            is_group=True,
        )
    )

    expected = {
        "channel_id": "telegram",
        "conversation_id": "conversation-1",
        "receiver": "receiver-1",
        "is_group": True,
    }
    from ecorex.connectors.channel_runtime import (
        channel_context_for_turn,
        clear_channel_context_for_turn,
    )

    thread_channel = created["thread"].metadata["channel"]
    turn_channel = created["turn"].metadata["channel"]
    home_context = channel_context_for_turn("turn-1")
    clear_channel_context_for_turn("turn-1")
    assert home_context == expected
    assert thread_channel["channel_id"] == "telegram"
    assert turn_channel["channel_id"] == "telegram"
    assert "conversation_id" not in thread_channel
    assert "receiver" not in turn_channel


def test_agent_worker_attaches_the_same_cow_scheduler_to_home_channel() -> None:
    from ecorex.runtime.worker import AgentTurnWorker

    scheduler = SimpleNamespace(name="scheduler", current_context=None, config={})
    AgentTurnWorker._attach_scheduler_context(
        SimpleNamespace(tools=[scheduler]),
        "thread-1",
        {
            "channel_id": "telegram",
            "conversation_id": "conversation-1",
            "receiver": "receiver-1",
            "is_group": True,
        },
    )

    assert scheduler.current_context.get("channel_type") == "telegram"
    assert scheduler.current_context.get("thread_id") == "thread-1"
    assert scheduler.current_context.get("session_id") == "conversation-1"
    assert scheduler.current_context.get("receiver") == "receiver-1"
    assert scheduler.current_context.get("isgroup") is True
    assert scheduler.config["channel_type"] == "telegram"


def test_scheduler_task_keeps_kernel_thread_and_vendor_delivery_context(
    tmp_path: Path,
) -> None:
    from agent.tools.scheduler.scheduler_tool import SchedulerTool
    from agent.tools.scheduler.task_store import TaskStore

    store = TaskStore(str(tmp_path / "tasks.json"))
    tool = SchedulerTool({"channel_type": "telegram"})
    tool.task_store = store
    tool.current_context = Context(
        kwargs={
            "thread_id": "thread-1",
            "session_id": "conversation-1",
            "receiver": "receiver-1",
            "isgroup": True,
        }
    )

    result = tool.execute(
        {
            "action": "create",
            "name": "follow-up",
            "message": "hello later",
            "schedule_type": "once",
            "schedule_value": "+5m",
        }
    )

    assert result.status == "success"
    action = store.list_tasks()[0]["action"]
    assert action["thread_id"] == "thread-1"
    assert action["conversation_id"] == "conversation-1"
    assert action["receiver"] == "receiver-1"

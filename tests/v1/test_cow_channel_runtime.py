from __future__ import annotations

from types import SimpleNamespace

from bridge.context import Context
from bridge.reply import ReplyType
from ecorex.connectors.channel_runtime import (
    ChannelInboundMessage,
    ChannelOutboundReply,
    ChannelTurnReceipt,
)
from ecorex.connectors.cow_channel import CowChannelRuntimeBridge, CowChannelService
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus


class _Manager:
    def __init__(self) -> None:
        self.started: list[tuple[list[str], bool]] = []
        self.stopped = 0

    def start(self, channel_names: list[str], first_start: bool = False) -> None:
        self.started.append((channel_names, first_start))

    def stop(self) -> None:
        self.stopped += 1


def test_cow_channel_service_starts_official_config_without_managed_session() -> None:
    manager = _Manager()
    service = CowChannelService(
        manager=manager,
        config={"channel_type": "telegram, feishu, web"},
    )

    service.start_sync()
    service.stop_sync()

    assert manager.started == [(["telegram", "feishu"], True)]
    assert manager.stopped == 1


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
        {
            "channel_id": "telegram",
            "conversation_id": "conversation-1",
            "receiver": "receiver-1",
            "is_group": True,
        },
    )

    assert scheduler.current_context.get("channel_type") == "telegram"
    assert scheduler.current_context.get("session_id") == "conversation-1"
    assert scheduler.current_context.get("receiver") == "receiver-1"
    assert scheduler.current_context.get("isgroup") is True
    assert scheduler.config["channel_type"] == "telegram"

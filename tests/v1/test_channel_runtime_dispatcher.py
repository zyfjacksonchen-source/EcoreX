from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ecorex.connectors import (
    ChannelCredentialOwner,
    ChannelInboundMessage,
    ChannelRuntimeDispatcher,
    ChannelTurnTerminalFailure,
    ChannelTurnReceipt,
    DingTalkStreamAdapter,
    DiscordGatewayAdapter,
    FeishuMessageBotAdapter,
    QQBotGatewayAdapter,
    SlackSocketModeAdapter,
    TelegramBotAdapter,
    WeComBotLongConnectionAdapter,
)
from ecorex.connectors.qq import _JournalEvent as _QQJournalEvent
from ecorex.gateway import GatewayEvent
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus
from ecorex.runtime import RuntimeSettings, create_app


class _Gateway:
    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent.model_validate(
            {
                "seq": 1,
                "event_type": "output_text.delta",
                "response_id": f"response-{len(self.requests)}",
                "delta": f"answer-{len(self.requests)}",
            }
        )
        yield GatewayEvent.model_validate(
            {
                "seq": 2,
                "event_type": "response.completed",
                "response_id": f"response-{len(self.requests)}",
            }
        )

    async def aclose(self) -> None:
        return None


class _Transport:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self._delivered: set[str] = set()

    def send_text(self, **message: str) -> None:
        key = message["idempotency_key"]
        if key in self._delivered:
            return
        self._delivered.add(key)
        self.sent.append(message)


def _wait_for_reply(dispatcher, receipt):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        reply = dispatcher.project_outbound(receipt)
        if reply is not None:
            return reply
        time.sleep(0.01)
    raise TimeoutError("channel reply was not projected")


def test_channel_dispatcher_reuses_runtime_continuity_and_facts(tmp_path) -> None:
    gateway = _Gateway()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token="r" * 32,
            csrf_token="c" * 32,
            webui_origins=("http://testserver",),
            model_gateway=gateway,
            allow_unmanaged_model_gateway_for_testing=True,
            model_worker_concurrency=1,
            model_worker_poll_seconds=0.01,
            model_worker_shutdown_seconds=1,
        )
    )
    conversation_id = "external-chat-42"
    message_id = "external-message-1"

    with TestClient(app):
        dispatcher = ChannelRuntimeDispatcher(
            owner=ChannelCredentialOwner("account-a", "organization-a"),
            composition=app.state.runtime_composition,
            kernel=app.state.runtime,
            worker=app.state.model_worker_supervisor,
        )
        inbound = ChannelInboundMessage(
            channel_id="telegram",
            conversation_id=conversation_id,
            message_id=message_id,
            text="first",
        )
        first = dispatcher.dispatch(inbound)
        duplicate = dispatcher.dispatch(inbound)
        first_reply = _wait_for_reply(dispatcher, first)

        assert duplicate == first
        assert first_reply.text == "answer-1"
        assert len(gateway.requests) == 1

        second = dispatcher.dispatch(
            ChannelInboundMessage(
                channel_id="telegram",
                conversation_id=conversation_id,
                message_id="external-message-2",
                text="continue",
            )
        )
        second_reply = _wait_for_reply(dispatcher, second)

        assert second.thread_id == first.thread_id
        assert second.turn_id != first.turn_id
        assert second_reply.text == "answer-2"
        assert len(gateway.requests) == 2

        projection = app.state.runtime.projection(first.thread_id)
        assert conversation_id not in repr(projection)
        assert message_id not in repr(projection)
        assert projection.turns[0].metadata["channel"]["channel_id"] == "telegram"

        transport = _Transport()
        assert dispatcher.deliver(
            first,
            conversation_id=conversation_id,
            transport=transport,
        )
        assert dispatcher.deliver(
            first,
            conversation_id=conversation_id,
            transport=transport,
        )
        assert [item["text"] for item in transport.sent] == ["answer-1"]

        with pytest.raises(ValueError, match="does not match"):
            dispatcher.deliver(
                first,
                conversation_id="wrong-chat",
                transport=transport,
            )


@pytest.mark.parametrize(
    ("status", "sendable"),
    [
        (TurnStatus.COMPLETED, True),
        (TurnStatus.PARTIAL, True),
        (TurnStatus.FAILED, False),
        (TurnStatus.CANCELLED, False),
        (TurnStatus.INTERRUPTED, False),
        (TurnStatus.SUPERSEDED, False),
    ],
)
def test_channel_dispatcher_never_sends_old_text_for_unsuccessful_turns(
    status: TurnStatus,
    sendable: bool,
) -> None:
    receipt = ChannelTurnReceipt(
        channel_id="telegram",
        thread_id="thread-1",
        turn_id="turn-1",
        client_message_id="message-1",
        conversation_sha256="conversation-hash",
    )
    kernel = SimpleNamespace(
        projection=lambda _thread_id: SimpleNamespace(
            turns=[SimpleNamespace(turn_id="turn-1", status=status)],
            items=[
                SimpleNamespace(
                    turn_id="turn-1",
                    item_id="item-1",
                    kind=ItemKind.MESSAGE,
                    status=ItemStatus.COMPLETED,
                    content={"role": "assistant", "text": "旧助手文本"},
                )
            ],
        )
    )
    dispatcher = ChannelRuntimeDispatcher(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        composition=SimpleNamespace(permission_account_id="account-a"),
        kernel=kernel,
        worker=SimpleNamespace(),
    )

    if sendable:
        assert dispatcher.project_outbound(receipt) is not None
        return

    with pytest.raises(ChannelTurnTerminalFailure) as caught:
        dispatcher.project_outbound(receipt)
    assert caught.value.status is status
    assert caught.value.code == f"channel_turn_{status.value}"


class _TerminalDispatcher:
    def __init__(self, status: TurnStatus = TurnStatus.FAILED) -> None:
        self.status = status

    def deliver(self, *_args, **_kwargs) -> bool:
        raise ChannelTurnTerminalFailure(self.status)


@pytest.mark.parametrize(
    ("channel_id", "adapter_type", "mode", "table", "raw_columns"),
    [
        ("telegram", TelegramBotAdapter, "pending", "telegram_pending", ()),
        ("feishu", FeishuMessageBotAdapter, "pending", "feishu_pending", ()),
        (
            "slack",
            SlackSocketModeAdapter,
            "journal",
            "slack_events",
            ("conversation_id", "message_id", "text"),
        ),
        (
            "discord",
            DiscordGatewayAdapter,
            "journal",
            "discord_events",
            ("conversation_id", "message_id", "text"),
        ),
        (
            "dingtalk",
            DingTalkStreamAdapter,
            "journal",
            "dingtalk_events",
            ("conversation_id", "message_id", "text", "reply_url"),
        ),
        (
            "wecom_bot",
            WeComBotLongConnectionAdapter,
            "journal",
            "wecom_bot_events",
            ("conversation_id", "message_id", "text"),
        ),
        (
            "qq",
            QQBotGatewayAdapter,
            "journal",
            "qq_events",
            ("target_id", "reply_message_id", "marker", "text"),
        ),
    ],
)
def test_channel_adapters_terminalize_failed_turns_without_raw_replay(
    tmp_path,
    channel_id,
    adapter_type,
    mode,
    table,
    raw_columns,
) -> None:
    database_path = tmp_path / f"{channel_id}.db"
    adapter = adapter_type(database_path)
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _TerminalDispatcher(),
    )
    store = adapter._store
    receipt = ChannelTurnReceipt(
        channel_id=channel_id,
        thread_id="thread-1",
        turn_id="turn-1",
        client_message_id="message-1",
        conversation_sha256="conversation-hash",
    )

    if mode == "pending":
        if channel_id == "telegram":
            store.add_pending(receipt, "raw-conversation", 2)
        else:
            store.add_pending(receipt, "raw-conversation")
        adapter._drain_pending()
        assert store.pending() == ()
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                f"SELECT state, error_code, conversation_id FROM {table}"
            ).fetchone() == (
                "failed",
                f"{channel_id if channel_id != 'feishu' else 'feishu_bot'}_turn_failed",
                "",
            )
        return

    if channel_id == "slack":
        store.record(
            envelope_id="envelope-1",
            conversation_id="raw-conversation",
            message_id="raw-message",
            text="raw-text",
        )
    elif channel_id == "dingtalk":
        store.record(
            conversation_id="raw-conversation",
            message_id="raw-message",
            text="raw-text",
            reply_url="https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
        )
    elif channel_id == "qq":
        store.record(
            _QQJournalEvent(
                event_key="event-1",
                conversation_id="c2c:dXNlci0x",
                reply_message_id="raw-message",
                text="raw-text",
            ),
            route="c2c",
            target_id="user-1",
            marker="raw-marker",
            seq=1,
        )
    else:
        store.record(
            conversation_id="raw-conversation",
            message_id="raw-message",
            text="raw-text",
        )
    store.set_outbound(store.received()[0].event_key, receipt)
    adapter._drain_outbound()

    with sqlite3.connect(database_path) as connection:
        columns = ", ".join(("state", *raw_columns))
        row = connection.execute(f"SELECT {columns} FROM {table}").fetchone()
        assert row == ("failed", *("" for _ in raw_columns))
        if channel_id == "qq":
            assert connection.execute(
                "SELECT error_code FROM qq_events"
            ).fetchone()[0] == "qq_turn_failed"

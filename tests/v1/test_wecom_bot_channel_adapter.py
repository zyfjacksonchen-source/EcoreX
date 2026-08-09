from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any

from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.models import ConnectorHealth, ConnectorHealthResult
from ecorex.connectors.wecom_bot import WeComBotLongConnectionAdapter, _chunks


_BOT_ID = "bot-id-123456"
_BOT_SECRET = "bot-secret-value-123456"
_USER = "student-303550073"
_CHAT = "group-chat-123456"


def _message(
    *,
    message_id: str = "message-1",
    text: str = "@小芯 请整理本周进展",
    group: bool = True,
) -> str:
    body: dict[str, Any] = {
        "msgid": message_id,
        "aibotid": _BOT_ID,
        "chattype": "group" if group else "single",
        "from": {"userid": _USER},
        "msgtype": "text",
        "text": {"content": text},
    }
    if group:
        body["chatid"] = _CHAT
    return json.dumps(
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": f"callback-{message_id}"},
            "body": body,
        },
        ensure_ascii=False,
    )


def _disconnected() -> str:
    return json.dumps(
        {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "event-disconnected"},
            "body": {
                "msgtype": "event",
                "event": {"eventtype": "disconnected_event"},
            },
        }
    )


class _Socket:
    def __init__(
        self,
        frames: list[str],
        *,
        auth_rejected: bool = False,
        uncertain_send: bool = False,
        disconnect_when_empty: bool = False,
    ) -> None:
        self.frames: queue.Queue[str] = queue.Queue()
        for frame in frames:
            self.frames.put(frame)
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.disconnect_when_empty = disconnect_when_empty
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise OSError("socket closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            if self.disconnect_when_empty:
                self.disconnect_when_empty = False
                raise OSError("connection lost") from None
            raise TimeoutError from None

    def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        command = payload["cmd"]
        request_id = payload["headers"]["req_id"]
        if command == "aibot_subscribe":
            assert payload["body"] == {
                "bot_id": _BOT_ID,
                "secret": _BOT_SECRET,
            }
            self.frames.put(
                json.dumps(
                    {
                        "headers": {"req_id": request_id},
                        "errcode": 40001 if self.auth_rejected else 0,
                        "errmsg": "invalid credential" if self.auth_rejected else "ok",
                    }
                )
            )
        elif command == "ping":
            self.frames.put(
                json.dumps(
                    {
                        "headers": {"req_id": request_id},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
        elif command == "aibot_send_msg":
            if self.uncertain_send:
                raise OSError("write result unknown")
            self.frames.put(
                json.dumps(
                    {
                        "headers": {"req_id": request_id},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
        else:
            raise AssertionError(command)

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _SocketFactory:
    def __init__(self, *sockets: _Socket) -> None:
        self.planned = list(sockets)
        self.sockets: list[_Socket] = []
        self.urls: list[str] = []

    def __call__(self, url: str) -> _Socket:
        assert url == "wss://openws.work.weixin.qq.com"
        self.urls.append(url)
        socket = self.planned.pop(0) if self.planned else _Socket([])
        self.sockets.append(socket)
        return socket


class _Dispatcher:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.receipts: list[ChannelTurnReceipt] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        self.messages.append(message)
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-wecom",
            turn_id=f"turn-{len(self.receipts) + 1}",
            client_message_id=f"client-{message.message_id}",
            conversation_sha256="conversation-hash",
        )
        self.receipts.append(receipt)
        return receipt

    def deliver(self, receipt, *, conversation_id, transport) -> bool:
        transport.send_text(
            channel_id=receipt.channel_id,
            conversation_id=conversation_id,
            text="已完成整理",
            idempotency_key=f"delivery-{receipt.turn_id}",
        )
        return True


def _wait(predicate, *, seconds: float = 3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("WeCom adapter did not converge")


def _config() -> dict[str, str]:
    return {
        "wecom_bot_id": _BOT_ID,
        "wecom_bot_secret": _BOT_SECRET,
    }


def _adapter(
    path: Path,
    sockets: _SocketFactory,
    dispatcher: _Dispatcher,
) -> WeComBotLongConnectionAdapter:
    adapter = WeComBotLongConnectionAdapter(
        path,
        socket_factory=sockets,
        heartbeat_seconds=1,
        ack_timeout_seconds=0.2,
    )
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,  # type: ignore[arg-type]
    )
    return adapter


def test_wecom_bot_authenticates_journals_deduplicates_and_delivers_once(
    tmp_path: Path,
) -> None:
    probe_socket = _Socket([])
    probe_factory = _SocketFactory(probe_socket)
    probe = WeComBotLongConnectionAdapter(
        tmp_path / "probe.db",
        socket_factory=probe_factory,
        ack_timeout_seconds=0.2,
    )
    assert probe.test(_config()).health is ConnectorHealth.CONNECTED
    assert probe_socket.closed is True

    socket = _Socket([_message(), _message()])
    sockets = _SocketFactory(socket)
    dispatcher = _Dispatcher()
    state_path = tmp_path / "wecom.db"
    adapter = _adapter(state_path, sockets, dispatcher)

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(
        lambda: len(
            [item for item in socket.sent if item["cmd"] == "aibot_send_msg"]
        )
        == 1
    )
    assert len(dispatcher.messages) == 1
    assert dispatcher.messages[0].channel_id == "wecom_bot"
    assert dispatcher.messages[0].conversation_id == _CHAT
    assert dispatcher.messages[0].text == "请整理本周进展"

    sent = next(item for item in socket.sent if item["cmd"] == "aibot_send_msg")
    assert sent["body"] == {
        "chatid": _CHAT,
        "msgtype": "markdown",
        "markdown": {"content": "已完成整理"},
    }
    assert stat_mode(state_path) == 0o600
    raw_database = state_path.read_bytes()
    assert _BOT_ID.encode() not in raw_database
    assert _BOT_SECRET.encode() not in raw_database
    with sqlite3.connect(state_path) as connection:
        row = connection.execute(
            "SELECT state, conversation_id, message_id, text "
            "FROM wecom_bot_events"
        ).fetchone()
    assert row == ("completed", "", "", "")
    assert adapter.stop(1) is True
    assert adapter.health().health is ConnectorHealth.DISABLED
    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    assert adapter.stop(1) is True


def test_wecom_bot_reconnects_and_preserves_deduplication(tmp_path: Path) -> None:
    first = _Socket([_message()], disconnect_when_empty=True)
    second = _Socket([_message()])
    sockets = _SocketFactory(first, second)
    dispatcher = _Dispatcher()
    adapter = _adapter(tmp_path / "wecom.db", sockets, dispatcher)

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(sockets.sockets) == 2)
    _wait(lambda: len(dispatcher.messages) == 1)
    assert sum(
        item["cmd"] == "aibot_send_msg"
        for socket in sockets.sockets
        for item in socket.sent
    ) == 1
    assert adapter.stop(1) is True


def test_wecom_bot_marks_unknown_delivery_uncertain_without_blind_retry(
    tmp_path: Path,
) -> None:
    socket = _Socket([_message()], uncertain_send=True)
    sockets = _SocketFactory(socket)
    dispatcher = _Dispatcher()
    adapter = _adapter(tmp_path / "wecom.db", sockets, dispatcher)

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: adapter.health().health is ConnectorHealth.DEGRADED)
    time.sleep(0.3)
    assert sum(item["cmd"] == "aibot_send_msg" for item in socket.sent) == 1
    assert adapter.health().error_code == "wecom_bot_delivery_uncertain"
    assert adapter.stop(1) is True


def test_wecom_bot_rejects_bad_credentials_and_does_not_fight_new_connection(
    tmp_path: Path,
) -> None:
    rejected = _Socket([], auth_rejected=True)
    assert WeComBotLongConnectionAdapter(
        tmp_path / "rejected.db",
        socket_factory=_SocketFactory(rejected),
        ack_timeout_seconds=0.2,
    ).test(_config()) == ConnectorHealthResult(
        ConnectorHealth.ERROR, "wecom_bot_credentials_rejected"
    )
    assert rejected.closed is True

    socket = _Socket([_disconnected()])
    sockets = _SocketFactory(socket)
    adapter = _adapter(tmp_path / "wecom.db", sockets, _Dispatcher())
    assert adapter.start(_config()) == ConnectorHealthResult(
        ConnectorHealth.ERROR, "wecom_bot_connection_replaced"
    )
    time.sleep(0.2)
    assert len(sockets.sockets) == 1
    assert adapter.stop(1) is True


def test_wecom_bot_chunks_markdown_on_utf8_boundaries() -> None:
    text = "同学" * 10_001
    chunks = _chunks(text)
    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 20_000 for chunk in chunks)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import stat
import threading
import time
from typing import Any, Mapping

import httpx
import pytest

from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.models import ConnectorHealth
from ecorex.connectors.qq import QQBotGatewayAdapter


_APP_ID = "102123456"
_APP_SECRET = "QQ_SECRET_" + "S" * 24
_USER_OPENID = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
_GROUP_OPENID = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
_MESSAGE_ID = "ROBOT1.0_" + "M" * 48


def _ready(session_id: str = "session-qq-1", seq: int = 1) -> str:
    return json.dumps(
        {
            "op": 0,
            "s": seq,
            "t": "READY",
            "d": {"session_id": session_id, "user": {"bot": True}},
        }
    )


def _resumed(seq: int) -> str:
    return json.dumps({"op": 0, "s": seq, "t": "RESUMED", "d": ""})


def _message(
    event_type: str,
    *,
    seq: int,
    target_id: str,
    message_id: str = _MESSAGE_ID,
    content: str = "请整理本周进展",
) -> str:
    data: dict[str, Any] = {
        "id": message_id,
        "content": content,
        "message_type": 0,
        "message_scene": {"ext": [f"msg_idx=INDEX-{message_id[-8:]}"]},
    }
    if event_type == "C2C_MESSAGE_CREATE":
        data["author"] = {"id": target_id, "user_openid": target_id}
    else:
        data["group_openid"] = target_id
        data["author"] = {"member_openid": "MEMBER-1"}
    return json.dumps({"id": f"event-{seq}", "op": 0, "s": seq, "t": event_type, "d": data})


class _QQAPI:
    def __init__(
        self,
        *,
        auth_rejected: bool = False,
        uncertain_send: bool = False,
        uncertain_send_at: int | None = None,
    ) -> None:
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.uncertain_send_at = uncertain_send_at
        self.lock = threading.Lock()
        self.token_count = 0
        self.gateway_count = 0
        self.send_attempts = 0
        self.attempted: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.clients: list[_Client] = []

    def factory(self) -> "_Client":
        client = _Client(self)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, api: _QQAPI) -> None:
        self.api = api
        self.closed = False

    def get(self, path: str, *, headers: Mapping[str, str]) -> httpx.Response:
        assert path == "gateway"
        assert headers["Authorization"].startswith("QQBot ACCESS-")
        self.api.gateway_count += 1
        return httpx.Response(
            200,
            request=httpx.Request("GET", "https://api.bot.qq.com/gateway"),
            json={"url": "wss://api.sgroup.qq.com/websocket/"},
        )

    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response:
        request = httpx.Request("POST", f"https://api.bot.qq.com/{path}")
        if path == "app/getAppAccessToken":
            assert headers == {"Content-Type": "application/json"}
            assert json == {"appId": _APP_ID, "clientSecret": _APP_SECRET}
            if self.api.auth_rejected:
                return httpx.Response(401, request=request, json={"code": 11241})
            self.api.token_count += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "access_token": f"ACCESS-{self.api.token_count}-" + "T" * 24,
                    "expires_in": "7200",
                },
            )
        assert path.startswith(("v2/users/", "v2/groups/"))
        assert headers["Authorization"].startswith("QQBot ACCESS-")
        with self.api.lock:
            self.api.send_attempts += 1
            body = dict(json)
            self.api.attempted.append((path, body))
            if self.api.uncertain_send or (
                self.api.uncertain_send_at == self.api.send_attempts
            ):
                raise httpx.ReadTimeout("response was not observed", request=request)
            self.api.sent.append((path, body))
            index = len(self.api.sent)
        return httpx.Response(
            200,
            request=request,
            json={"id": f"sent-message-{index}", "timestamp": "2026-08-09T12:00:00+08:00"},
        )

    def close(self) -> None:
        self.closed = True


class _Socket:
    def __init__(self, frames: list[str]) -> None:
        self.frames: queue.Queue[str] = queue.Queue()
        self.frames.put(json.dumps({"op": 10, "d": {"heartbeat_interval": 60_000}}))
        for frame in frames:
            self.frames.put(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise OSError("closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def send(self, message: str) -> None:
        if self.closed:
            raise OSError("closed")
        self.sent.append(json.loads(message))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _SocketFactory:
    def __init__(self, sessions: list[list[str]]) -> None:
        self.sessions = sessions
        self.sockets: list[_Socket] = []

    def __call__(self, url: str) -> _Socket:
        assert url == "wss://api.sgroup.qq.com/websocket/"
        index = len(self.sockets)
        frames = self.sessions[index] if index < len(self.sessions) else [_resumed(index + 2)]
        socket = _Socket(list(frames))
        self.sockets.append(socket)
        return socket


class _Dispatcher:
    def __init__(self, reply_text: str = "已完成整理") -> None:
        self.messages: list[Any] = []
        self.deliveries = 0
        self.reply_text = reply_text

    def dispatch(self, message) -> ChannelTurnReceipt:
        self.messages.append(message)
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-qq",
            turn_id=f"turn-{len(self.messages)}",
            client_message_id=f"client-{message.message_id}",
            conversation_sha256="conversation-hash",
        )

    def deliver(self, receipt, *, conversation_id, transport) -> bool:
        self.deliveries += 1
        transport.send_text(
            channel_id=receipt.channel_id,
            conversation_id=conversation_id,
            text=self.reply_text,
            idempotency_key=f"delivery-{receipt.turn_id}",
        )
        return True


def _wait(predicate, *, seconds: float = 3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("QQ adapter did not converge")


def _adapter(
    path: Path,
    api: _QQAPI,
    sockets: _SocketFactory,
    dispatcher: _Dispatcher,
) -> QQBotGatewayAdapter:
    adapter = QQBotGatewayAdapter(
        path,
        client_factory=api.factory,
        socket_factory=sockets,
    )
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,  # type: ignore[arg-type]
    )
    return adapter


def _config() -> dict[str, str]:
    return {"qq_app_id": _APP_ID, "qq_app_secret": _APP_SECRET}


def test_qq_c2c_gateway_dispatches_and_passively_replies_once(tmp_path: Path) -> None:
    api = _QQAPI()
    sockets = _SocketFactory(
        [[_ready(), _message("C2C_MESSAGE_CREATE", seq=2, target_id=_USER_OPENID)]]
    )
    dispatcher = _Dispatcher()
    state_path = tmp_path / "qq.db"
    adapter = _adapter(state_path, api, sockets, dispatcher)

    assert adapter.test(_config()).health is ConnectorHealth.CONNECTED
    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1)

    identify = sockets.sockets[0].sent[0]
    assert identify["op"] == 2
    assert identify["d"]["intents"] == 1 << 25
    assert identify["d"]["shard"] == [0, 1]
    assert dispatcher.messages[0].channel_id == "qq"
    assert _USER_OPENID not in dispatcher.messages[0].conversation_id
    assert api.sent == [
        (
            f"v2/users/{_USER_OPENID}/messages",
            {
                "content": "已完成整理",
                "msg_type": 0,
                "msg_id": _MESSAGE_ID,
                "msg_seq": 1,
            },
        )
    ]
    assert adapter.stop(1) is True
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    database = state_path.read_bytes()
    assert _APP_SECRET.encode() not in database
    assert b"ACCESS-" not in database


def test_qq_gateway_resumes_and_deduplicates_group_replay(tmp_path: Path) -> None:
    group = _message("GROUP_AT_MESSAGE_CREATE", seq=2, target_id=_GROUP_OPENID)
    api = _QQAPI()
    sockets = _SocketFactory(
        [
            [_ready(), group, json.dumps({"op": 7})],
            [_resumed(2), _message("GROUP_AT_MESSAGE_CREATE", seq=3, target_id=_GROUP_OPENID)],
        ]
    )
    dispatcher = _Dispatcher()
    adapter = _adapter(tmp_path / "qq.db", api, sockets, dispatcher)

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(sockets.sockets) >= 2)
    _wait(lambda: len(api.sent) == 1)
    time.sleep(0.1)

    resume = sockets.sockets[1].sent[0]
    assert resume == {
        "op": 6,
        "d": {
            "token": resume["d"]["token"],
            "session_id": "session-qq-1",
            "seq": 2,
        },
    }
    assert resume["d"]["token"].startswith("QQBot ACCESS-")
    assert len(dispatcher.messages) == 1
    assert dispatcher.deliveries == 1
    assert api.sent[0][0] == f"v2/groups/{_GROUP_OPENID}/messages"
    assert adapter.stop(1) is True


def test_qq_uncertain_delivery_is_persisted_without_blind_retry(tmp_path: Path) -> None:
    api = _QQAPI(uncertain_send=True)
    sockets = _SocketFactory(
        [[_ready(), _message("C2C_MESSAGE_CREATE", seq=2, target_id=_USER_OPENID)]]
    )
    dispatcher = _Dispatcher()
    adapter = _adapter(tmp_path / "qq.db", api, sockets, dispatcher)

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: api.send_attempts == 1)
    _wait(lambda: adapter.health().error_code == "qq_delivery_uncertain")
    time.sleep(0.1)
    assert api.send_attempts == 1
    assert adapter.health().health is ConnectorHealth.DEGRADED
    assert adapter.stop(1) is True


def test_qq_long_text_is_chunked_with_stable_msg_sequences(tmp_path: Path) -> None:
    reply = "甲" * 5_000 + "乙"
    api = _QQAPI()
    sockets = _SocketFactory(
        [[_ready(), _message("C2C_MESSAGE_CREATE", seq=2, target_id=_USER_OPENID)]]
    )
    adapter = _adapter(
        tmp_path / "qq.db", api, sockets, _Dispatcher(reply_text=reply)
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 2)

    bodies = [body for _, body in api.sent]
    assert [body["msg_seq"] for body in bodies] == [1, 2]
    assert [len(body["content"]) for body in bodies] == [5_000, 1]
    assert "".join(str(body["content"]) for body in bodies) == reply
    assert all(body["msg_id"] == _MESSAGE_ID for body in bodies)
    assert adapter.stop(1) is True


def test_qq_chunk_limit_counts_utf16_units_without_splitting_astral_text(
    tmp_path: Path,
) -> None:
    reply = "甲" * 4_999 + "😀乙"
    api = _QQAPI()
    sockets = _SocketFactory(
        [[_ready(), _message("C2C_MESSAGE_CREATE", seq=2, target_id=_USER_OPENID)]]
    )
    adapter = _adapter(
        tmp_path / "qq.db", api, sockets, _Dispatcher(reply_text=reply)
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 2)
    chunks = [str(body["content"]) for _, body in api.sent]
    assert chunks == ["甲" * 4_999, "😀乙"]
    assert "".join(chunks) == reply
    assert adapter.stop(1) is True


@pytest.mark.parametrize(
    ("event_type", "target_id", "reply_limit"),
    [
        ("C2C_MESSAGE_CREATE", _USER_OPENID, 4),
        ("GROUP_AT_MESSAGE_CREATE", _GROUP_OPENID, 5),
    ],
)
def test_qq_passive_reply_capacity_exact_boundary(
    tmp_path: Path,
    event_type: str,
    target_id: str,
    reply_limit: int,
) -> None:
    reply = "界" * (5_000 * reply_limit)
    api = _QQAPI()
    sockets = _SocketFactory(
        [[_ready(), _message(event_type, seq=2, target_id=target_id)]]
    )
    adapter = _adapter(
        tmp_path / f"qq-boundary-{reply_limit}.db",
        api,
        sockets,
        _Dispatcher(reply_text=reply),
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == reply_limit)
    bodies = [body for _, body in api.sent]
    assert [body["msg_seq"] for body in bodies] == list(range(1, reply_limit + 1))
    assert all(len(str(body["content"])) == 5_000 for body in bodies)
    assert "".join(str(body["content"]) for body in bodies) == reply
    assert adapter.stop(1) is True


@pytest.mark.parametrize(
    ("event_type", "target_id", "reply_limit"),
    [
        ("C2C_MESSAGE_CREATE", _USER_OPENID, 4),
        ("GROUP_AT_MESSAGE_CREATE", _GROUP_OPENID, 5),
    ],
)
def test_qq_text_over_passive_reply_capacity_fails_closed(
    tmp_path: Path,
    event_type: str,
    target_id: str,
    reply_limit: int,
) -> None:
    api = _QQAPI()
    sockets = _SocketFactory(
        [[_ready(), _message(event_type, seq=2, target_id=target_id)]]
    )
    adapter = _adapter(
        tmp_path / f"qq-{reply_limit}.db",
        api,
        sockets,
        _Dispatcher(reply_text="长" * (5_000 * reply_limit + 1)),
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: adapter.health().error_code == "qq_delivery_too_large")
    assert api.send_attempts == 0
    assert adapter.stop(1) is True


def test_qq_second_chunk_uncertain_never_replays_first_chunk(tmp_path: Path) -> None:
    reply = "甲" * 5_000 + "乙"
    api = _QQAPI(uncertain_send_at=2)
    sockets = _SocketFactory(
        [[_ready(), _message("C2C_MESSAGE_CREATE", seq=2, target_id=_USER_OPENID)]]
    )
    adapter = _adapter(
        tmp_path / "qq.db", api, sockets, _Dispatcher(reply_text=reply)
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: api.send_attempts == 2)
    _wait(lambda: adapter.health().error_code == "qq_delivery_uncertain")
    time.sleep(0.1)

    assert api.send_attempts == 2
    assert [body["msg_seq"] for _, body in api.attempted] == [1, 2]
    assert [body["msg_seq"] for _, body in api.sent] == [1]
    assert api.sent[0][1]["content"] == "甲" * 5_000
    assert adapter.stop(1) is True

    restarted_api = _QQAPI()
    restarted_sockets = _SocketFactory([[_resumed(2)]])
    restarted = _adapter(
        tmp_path / "qq.db",
        restarted_api,
        restarted_sockets,
        _Dispatcher(reply_text=reply),
    )
    result = restarted.start(_config())
    assert result.health is ConnectorHealth.DEGRADED
    assert result.error_code == "qq_delivery_uncertain"
    time.sleep(0.1)
    assert restarted_api.send_attempts == 0
    assert restarted.stop(1) is True


def test_qq_rejects_bad_credentials_and_stops_within_fence(tmp_path: Path) -> None:
    rejected = QQBotGatewayAdapter(
        tmp_path / "rejected.db", client_factory=_QQAPI(auth_rejected=True).factory
    )
    result = rejected.test(_config())
    assert result.health is ConnectorHealth.ERROR
    assert result.error_code == "qq_auth_rejected"

    api = _QQAPI()
    sockets = _SocketFactory([[_ready()]])
    adapter = _adapter(tmp_path / "qq.db", api, sockets, _Dispatcher())
    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    started = time.monotonic()
    assert adapter.stop(0.5) is True
    assert time.monotonic() - started < 0.5
    assert sockets.sockets[0].closed is True
    assert all(client.closed for client in api.clients)

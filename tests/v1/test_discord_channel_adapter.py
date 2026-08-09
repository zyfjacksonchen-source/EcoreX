from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping

import httpx

from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.discord import DiscordGatewayAdapter
from ecorex.connectors.models import ConnectorHealth


_TOKEN = "D" * 24 + "." + "T" * 32
_BOT_USER = "100000000000000001"
_USER = "100000000000000002"
_CHANNEL = "100000000000000003"
_MESSAGE = "100000000000000004"


def _ready(sequence: int = 1) -> str:
    return json.dumps(
        {
            "op": 0,
            "s": sequence,
            "t": "READY",
            "d": {
                "v": 10,
                "user": {"id": _BOT_USER, "bot": True},
                "session_id": "discord-session-1",
                "resume_gateway_url": "wss://gateway.discord.gg",
                "guilds": [],
            },
        }
    )


def _message(
    *,
    sequence: int,
    message_id: str = _MESSAGE,
    guild: bool = True,
    content: str = "请整理本周进展",
) -> str:
    data: dict[str, Any] = {
        "id": message_id,
        "channel_id": _CHANNEL,
        "author": {"id": _USER, "username": "student", "bot": False},
        "content": f"<@{_BOT_USER}> {content}" if guild else content,
    }
    if guild:
        data["guild_id"] = "100000000000000005"
    return json.dumps({"op": 0, "s": sequence, "t": "MESSAGE_CREATE", "d": data})


class _DiscordAPI:
    def __init__(self, *, auth_rejected: bool = False, uncertain_send: bool = False):
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.username = "CowAgent"
        self.paths: list[tuple[str, str]] = []
        self.sent: list[dict[str, Any]] = []
        self.send_attempts = 0
        self.clients: list[_Client] = []
        self.lock = threading.Lock()

    def factory(self) -> "_Client":
        client = _Client(self)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, api: _DiscordAPI):
        self.api = api
        self.closed = False

    def get(self, path: str, *, headers: Mapping[str, str]) -> httpx.Response:
        request = httpx.Request("GET", f"https://discord.test/api/v10/{path}")
        self.api.paths.append(("GET", path))
        assert headers == {"Authorization": f"Bot {_TOKEN}"}
        if self.api.auth_rejected:
            return httpx.Response(401, request=request, json={"message": "401"})
        if path == "users/@me":
            return httpx.Response(
                200,
                request=request,
                json={"id": _BOT_USER, "username": self.api.username, "bot": True},
            )
        if path == "gateway/bot":
            return httpx.Response(
                200,
                request=request,
                json={
                    "url": "wss://gateway.discord.gg",
                    "shards": 1,
                    "session_start_limit": {
                        "total": 1000,
                        "remaining": 999,
                        "reset_after": 60_000,
                        "max_concurrency": 1,
                    },
                },
            )
        raise AssertionError(path)

    def patch(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response:
        request = httpx.Request("PATCH", f"https://discord.test/api/v10/{path}")
        self.api.paths.append(("PATCH", path))
        assert headers == {"Authorization": f"Bot {_TOKEN}"}
        assert path == "users/@me"
        assert json == {"username": "e-Mate"}
        self.api.username = "e-Mate"
        return httpx.Response(
            200,
            request=request,
            json={"id": _BOT_USER, "username": self.api.username, "bot": True},
        )

    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response:
        request = httpx.Request("POST", f"https://discord.test/api/v10/{path}")
        self.api.paths.append(("POST", path))
        assert headers == {"Authorization": f"Bot {_TOKEN}"}
        assert path == f"channels/{_CHANNEL}/messages"
        with self.api.lock:
            self.api.send_attempts += 1
            if self.api.uncertain_send:
                raise httpx.ReadTimeout("response was not observed", request=request)
            body = dict(json)
            self.api.sent.append(body)
            index = len(self.api.sent)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": str(200000000000000000 + index),
                "channel_id": _CHANNEL,
                "content": body["content"],
                "nonce": body["nonce"],
            },
        )

    def close(self) -> None:
        self.closed = True


class _Socket:
    def __init__(self, frames: list[str], *, heartbeat_interval: int = 60_000):
        self.frames: queue.Queue[str] = queue.Queue()
        self.frames.put(
            json.dumps({"op": 10, "d": {"heartbeat_interval": heartbeat_interval}})
        )
        for frame in frames:
            self.frames.put(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_codes: list[int] = []

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise OSError("socket closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        if payload["op"] == 1:
            self.frames.put(json.dumps({"op": 11, "d": None}))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_codes.append(code)
        self.closed = True


class _SocketFactory:
    def __init__(self, *frame_sets: list[str], heartbeat_interval: int = 60_000):
        self.frame_sets = list(frame_sets) or [[]]
        self.heartbeat_interval = heartbeat_interval
        self.sockets: list[_Socket] = []
        self.urls: list[str] = []

    def __call__(self, url: str) -> _Socket:
        assert url == "wss://gateway.discord.gg/?v=10&encoding=json"
        index = min(len(self.sockets), len(self.frame_sets) - 1)
        socket = _Socket(
            list(self.frame_sets[index]),
            heartbeat_interval=self.heartbeat_interval,
        )
        self.sockets.append(socket)
        self.urls.append(url)
        return socket


class _Dispatcher:
    def __init__(self):
        self.receipts: list[ChannelTurnReceipt] = []
        self.messages: list[Any] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        self.messages.append(message)
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-discord",
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
    raise TimeoutError("Discord adapter did not converge")


def _adapter(
    path: Path,
    api: _DiscordAPI,
    sockets: _SocketFactory,
    owner: ChannelCredentialOwner,
    dispatcher: _Dispatcher,
) -> DiscordGatewayAdapter:
    adapter = DiscordGatewayAdapter(
        path,
        client_factory=api.factory,
        socket_factory=sockets,
        heartbeat_jitter=lambda: 1,
    )
    adapter.bind_runtime(owner, dispatcher)  # type: ignore[arg-type]
    return adapter


def test_discord_gateway_journals_deduplicates_and_delivers_once(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "discord.db"
    api = _DiscordAPI()
    sockets = _SocketFactory([_ready(), _message(sequence=2), _message(sequence=3)])
    dispatcher = _Dispatcher()
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = _adapter(state_path, api, sockets, owner, dispatcher)

    assert adapter.test({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    assert api.paths == [("GET", "users/@me"), ("GET", "gateway/bot")]
    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    assert ("PATCH", "users/@me") in api.paths
    assert api.username == "e-Mate"
    _wait(lambda: len(api.sent) == 1)

    assert len(dispatcher.receipts) == 1
    assert dispatcher.messages[0].channel_id == "discord"
    assert dispatcher.messages[0].conversation_id == _CHANNEL
    assert dispatcher.messages[0].text == "请整理本周进展"
    identify = sockets.sockets[0].sent[0]
    assert identify["op"] == 2
    assert identify["d"]["intents"] == 4608
    assert api.sent == [
        {
            "content": "已完成整理",
            "nonce": api.sent[0]["nonce"],
            "enforce_nonce": True,
            "allowed_mentions": {"parse": []},
        }
    ]
    assert len(api.sent[0]["nonce"]) == 25
    assert adapter.stop(1) is True
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert _TOKEN.encode() not in state_path.read_bytes()

    repeated_sockets = _SocketFactory([_ready(), _message(sequence=2)])
    repeated_dispatcher = _Dispatcher()
    repeated = _adapter(
        state_path, api, repeated_sockets, owner, repeated_dispatcher
    )
    assert repeated.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(repeated_sockets.sockets[0].sent) == 1)
    time.sleep(0.1)
    assert repeated_dispatcher.receipts == []
    assert len(api.sent) == 1
    assert repeated.stop(1) is True


def test_discord_delivery_uncertainty_is_not_retried_and_tokens_are_redacted(
    tmp_path: Path,
) -> None:
    api = _DiscordAPI(uncertain_send=True)
    sockets = _SocketFactory([_ready(), _message(sequence=2)])
    adapter = _adapter(
        tmp_path / "discord.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),
    )

    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: api.send_attempts == 1)
    _wait(lambda: adapter.health().health is ConnectorHealth.DEGRADED)
    time.sleep(0.1)
    assert api.send_attempts == 1
    assert adapter.health().error_code == "discord_delivery_uncertain"
    assert _TOKEN not in repr(adapter.health())
    assert adapter.stop(1) is True

    rejected = DiscordGatewayAdapter(
        tmp_path / "rejected.db",
        client_factory=_DiscordAPI(auth_rejected=True).factory,
        socket_factory=_SocketFactory(),
    ).test({"discord_token": _TOKEN})
    assert rejected.error_code == "discord_auth_rejected"
    assert _TOKEN not in repr(rejected)


def test_discord_reconnect_resumes_session_and_stop_is_bounded(tmp_path: Path) -> None:
    api = _DiscordAPI()
    sockets = _SocketFactory(
        [_ready(sequence=10), json.dumps({"op": 7, "d": None})],
        [
            json.dumps({"op": 0, "s": 10, "t": "RESUMED", "d": {}}),
            _message(sequence=11, message_id="100000000000000006"),
        ],
    )
    adapter = _adapter(
        tmp_path / "discord.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),
    )

    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1)
    assert len(sockets.sockets) == 2
    resume = sockets.sockets[1].sent[0]
    assert resume == {
        "op": 6,
        "d": {
            "token": _TOKEN,
            "session_id": "discord-session-1",
            "seq": 10,
        },
    }
    assert 4000 in sockets.sockets[0].close_codes
    assert adapter.health().health is ConnectorHealth.CONNECTED
    started = time.monotonic()
    assert adapter.stop(1) is True
    assert time.monotonic() - started < 1
    assert 1000 in sockets.sockets[1].close_codes


def test_discord_journals_message_before_advancing_resume_sequence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _DiscordAPI()
    sockets = _SocketFactory(
        [_ready(sequence=10), _message(sequence=11)]
    )
    adapter = _adapter(
        tmp_path / "discord.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),
    )
    store = adapter._store
    assert store is not None
    original_record = store.record
    sequence_at_record: list[int | None] = []

    def record(**kwargs) -> None:
        session = store.session()
        sequence_at_record.append(session.sequence if session else None)
        original_record(**kwargs)

    monkeypatch.setattr(store, "record", record)
    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1)

    assert sequence_at_record == [10]
    assert store.session() is not None
    assert store.session().sequence == 11  # type: ignore[union-attr]
    assert adapter.stop(1) is True


def test_discord_ignores_bots_and_unmentioned_guild_messages(tmp_path: Path) -> None:
    bot = json.loads(_message(sequence=2))
    bot["d"]["author"] = {"id": "100000000000000007", "bot": True}
    unmentioned = json.loads(_message(sequence=3))
    unmentioned["d"]["content"] = "这条消息不是给小芯的"
    sockets = _SocketFactory([_ready(), json.dumps(bot), json.dumps(unmentioned)])
    dispatcher = _Dispatcher()
    adapter = _adapter(
        tmp_path / "discord.db",
        _DiscordAPI(),
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,
    )

    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(sockets.sockets[0].sent) == 1)
    time.sleep(0.1)
    assert dispatcher.receipts == []
    assert adapter.stop(1) is True


def test_discord_heartbeat_uses_latest_gateway_sequence(tmp_path: Path) -> None:
    api = _DiscordAPI()
    sockets = _SocketFactory([_ready()], heartbeat_interval=1_000)
    adapter = DiscordGatewayAdapter(
        tmp_path / "discord.db",
        client_factory=api.factory,
        socket_factory=sockets,
        heartbeat_jitter=lambda: 0,
    )
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )

    assert adapter.start({"discord_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(
        lambda: len(
            [message for message in sockets.sockets[0].sent if message["op"] == 1]
        )
        >= 2
    )
    heartbeats = [
        message for message in sockets.sockets[0].sent if message["op"] == 1
    ]
    assert heartbeats[0]["d"] is None
    assert heartbeats[-1]["d"] == 1
    assert adapter.stop(1) is True

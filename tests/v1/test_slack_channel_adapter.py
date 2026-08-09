from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping

import httpx
from fastapi.testclient import TestClient

from ecorex.connectors import InMemoryCredentialVault
from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.models import ConnectorHealth
from ecorex.connectors.slack import SlackSocketModeAdapter
from ecorex.gateway import GatewayEvent
from ecorex.runtime import RuntimeSettings, create_app


_BOT_TOKEN = "xox" + "b-" + "B" * 24
_APP_TOKEN = "xap" + "p-" + "A" * 24
_BOT_USER = "U123BOT99"
_USER = "U555USER9"
_CHANNEL = "C123CHAN9"


def _event_frame(
    envelope_id: str,
    *,
    timestamp: str = "1710000000.000001",
    thread_ts: str | None = None,
    text: str = "请整理本周进展",
) -> str:
    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": _CHANNEL,
        "user": _USER,
        "text": f"<@{_BOT_USER}> {text}",
        "ts": timestamp,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return json.dumps(
        {
            "type": "events_api",
            "envelope_id": envelope_id,
            "accepts_response_payload": False,
            "payload": {
                "type": "event_callback",
                "event_id": f"Ev{envelope_id}",
                "event": event,
            },
        }
    )


def test_slack_only_accepts_plain_messages_from_direct_messages(tmp_path: Path) -> None:
    adapter = SlackSocketModeAdapter(tmp_path / "slack.db")
    adapter._bot_user_id = _BOT_USER
    envelope = json.loads(_event_frame("env-channel"))
    event = envelope["payload"]["event"]
    event.update(
        {
            "type": "message",
            "channel_type": "channel",
            "text": "普通频道消息",
        }
    )

    assert adapter._event(envelope) is None

    event["channel_type"] = "im"
    assert adapter._event(envelope) is not None


class _SlackAPI:
    def __init__(self, *, auth_rejected: bool = False, uncertain_send: bool = False):
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.lock = threading.Lock()
        self.paths: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.send_attempts = 0
        self.open_count = 0
        self.clients: list[_Client] = []

    def factory(self) -> "_Client":
        client = _Client(self)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, api: _SlackAPI):
        self.api = api
        self.closed = False

    def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
    ) -> httpx.Response:
        request = httpx.Request("POST", f"https://slack.test/api/{path}")
        self.api.paths.append(path)
        authorization = headers.get("Authorization")
        if path == "auth.test":
            assert authorization == f"Bearer {_BOT_TOKEN}"
            if self.api.auth_rejected:
                return httpx.Response(
                    200,
                    request=request,
                    json={"ok": False, "error": "invalid_auth"},
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "ok": True,
                    "team_id": "T123TEAM9",
                    "user_id": _BOT_USER,
                    "bot_id": "B123BOT99",
                },
            )
        if path == "apps.connections.open":
            assert authorization == f"Bearer {_APP_TOKEN}"
            self.api.open_count += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "ok": True,
                    "url": f"wss://wss-primary.slack.com/link/?ticket={self.api.open_count}",
                },
            )
        if path == "chat.postMessage":
            assert authorization == f"Bearer {_BOT_TOKEN}"
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
                    "ok": True,
                    "channel": body["channel"],
                    "ts": f"1710000001.{index:06d}",
                },
            )
        raise AssertionError(path)

    def close(self) -> None:
        self.closed = True


class _Socket:
    def __init__(self, frames: list[str]):
        self.frames: queue.Queue[str] = queue.Queue()
        self.frames.put(json.dumps({"type": "hello"}))
        for frame in frames:
            self.frames.put(frame)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> str:
        if self.closed:
            raise RuntimeError("socket closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _SocketFactory:
    def __init__(
        self,
        first_frames: list[str],
        reconnect_frames: list[str] | None = None,
    ):
        self.first_frames = first_frames
        self.reconnect_frames = reconnect_frames or []
        self.sockets: list[_Socket] = []

    def __call__(self, url: str) -> _Socket:
        assert url.startswith("wss://wss-primary.slack.com/link/")
        frames = (
            self.first_frames
            if not self.sockets
            else (self.reconnect_frames if len(self.sockets) == 1 else [])
        )
        socket = _Socket(list(frames))
        self.sockets.append(socket)
        return socket


class _Dispatcher:
    def __init__(self, sockets: _SocketFactory):
        self.sockets = sockets
        self.receipts: list[ChannelTurnReceipt] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        assert any(
            socket.sent for socket in self.sockets.sockets
        ), "Slack envelope must be acknowledged first"
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-slack",
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


class _Gateway:
    async def stream(self, _request):
        yield GatewayEvent.model_validate(
            {
                "seq": 1,
                "event_type": "output_text.delta",
                "response_id": "slack-response",
                "delta": "已完成本周进展整理",
            }
        )
        yield GatewayEvent.model_validate(
            {
                "seq": 2,
                "event_type": "response.completed",
                "response_id": "slack-response",
            }
        )

    async def aclose(self) -> None:
        return None


def _wait(predicate, *, seconds: float = 2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("Slack adapter did not converge")


def _adapter(
    path: Path,
    api: _SlackAPI,
    sockets: _SocketFactory,
    owner: ChannelCredentialOwner,
    dispatcher: _Dispatcher,
) -> SlackSocketModeAdapter:
    adapter = SlackSocketModeAdapter(
        path,
        client_factory=api.factory,
        socket_factory=sockets,
    )
    adapter.bind_runtime(owner, dispatcher)  # type: ignore[arg-type]
    return adapter


def test_slack_socket_mode_acks_journals_deduplicates_and_delivers_once(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "slack.db"
    api = _SlackAPI()
    frames = [
        _event_frame("envelope-1", thread_ts="1709999999.000001"),
        _event_frame("envelope-2", thread_ts="1709999999.000001"),
    ]
    sockets = _SocketFactory(frames)
    dispatcher = _Dispatcher(sockets)
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = _adapter(state_path, api, sockets, owner, dispatcher)

    assert adapter.test(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    assert api.paths == ["auth.test", "apps.connections.open"]
    assert adapter.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(sockets.sockets[0].sent) == 2 and len(api.sent) == 1)

    assert len(dispatcher.receipts) == 1
    assert sockets.sockets[0].sent == [
        {"envelope_id": "envelope-1"},
        {"envelope_id": "envelope-2"},
    ]
    assert api.sent == [
        {
            "channel": _CHANNEL,
            "text": "已完成整理",
            "unfurl_links": False,
            "unfurl_media": False,
            "thread_ts": "1709999999.000001",
        }
    ]
    assert adapter.stop(1) is True
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    state_bytes = state_path.read_bytes()
    assert _BOT_TOKEN.encode() not in state_bytes
    assert _APP_TOKEN.encode() not in state_bytes

    repeated_sockets = _SocketFactory([_event_frame("envelope-3", thread_ts="1709999999.000001")])
    repeated_dispatcher = _Dispatcher(repeated_sockets)
    repeated = _adapter(
        state_path,
        api,
        repeated_sockets,
        owner,
        repeated_dispatcher,
    )
    assert repeated.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(repeated_sockets.sockets[0].sent))
    time.sleep(0.05)
    assert repeated_dispatcher.receipts == []
    assert len(api.sent) == 1
    assert repeated.stop(1) is True

    other_sockets = _SocketFactory([_event_frame("envelope-1", thread_ts="1709999999.000001")])
    other_dispatcher = _Dispatcher(other_sockets)
    other = _adapter(
        state_path,
        api,
        other_sockets,
        ChannelCredentialOwner("account-b", "organization-a"),
        other_dispatcher,
    )
    assert other.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 2)
    assert len(other_dispatcher.receipts) == 1
    assert other.stop(1) is True


def test_slack_delivery_uncertainty_is_not_retried_and_errors_hide_tokens(
    tmp_path: Path,
) -> None:
    api = _SlackAPI(uncertain_send=True)
    sockets = _SocketFactory([_event_frame("uncertain-envelope")])
    dispatcher = _Dispatcher(sockets)
    adapter = _adapter(
        tmp_path / "slack.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,
    )

    assert adapter.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    _wait(lambda: api.send_attempts == 1)
    _wait(lambda: adapter.health().health is ConnectorHealth.DEGRADED)
    time.sleep(0.1)
    assert api.send_attempts == 1
    assert adapter.health().error_code == "slack_delivery_uncertain"
    assert _BOT_TOKEN not in repr(adapter.health())
    assert _APP_TOKEN not in repr(adapter.health())
    assert adapter.stop(1) is True

    rejected = SlackSocketModeAdapter(
        tmp_path / "rejected.db",
        client_factory=_SlackAPI(auth_rejected=True).factory,
        socket_factory=_SocketFactory([]),
    ).test({"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN})
    assert rejected.error_code == "slack_auth_rejected"
    assert _BOT_TOKEN not in repr(rejected)
    assert _APP_TOKEN not in repr(rejected)


def test_slack_disconnect_opens_a_fresh_socket_and_stop_is_bounded(
    tmp_path: Path,
) -> None:
    api = _SlackAPI()
    sockets = _SocketFactory(
        [json.dumps({"type": "disconnect", "reason": "refresh_requested"})],
        [_event_frame("after-reconnect")],
    )
    dispatcher = _Dispatcher(sockets)
    adapter = _adapter(
        tmp_path / "slack.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,
    )

    assert adapter.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1, seconds=3)
    assert len(sockets.sockets) == 2
    assert api.open_count == 2
    assert adapter.health().health is ConnectorHealth.CONNECTED
    started = time.monotonic()
    assert adapter.stop(1) is True
    assert time.monotonic() - started < 1


def test_slack_start_opens_one_socket_and_closes_it_on_later_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _SlackAPI()
    sockets = _SocketFactory([])
    dispatcher = _Dispatcher(sockets)
    adapter = _adapter(
        tmp_path / "normal.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,
    )

    assert adapter.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    ).health is ConnectorHealth.CONNECTED
    assert api.open_count == 1
    assert len(sockets.sockets) == 1
    assert adapter.stop(1) is True

    failed_api = _SlackAPI()
    failed_sockets = _SocketFactory([])
    failed_dispatcher = _Dispatcher(failed_sockets)
    failed = _adapter(
        tmp_path / "failed.db",
        failed_api,
        failed_sockets,
        ChannelCredentialOwner("account-b", "organization-a"),
        failed_dispatcher,
    )
    store = failed._store
    assert store is not None
    monkeypatch.setattr(
        store,
        "has_uncertain",
        lambda: (_ for _ in ()).throw(RuntimeError("journal unavailable")),
    )

    result = failed.start(
        {"slack_bot_token": _BOT_TOKEN, "slack_app_token": _APP_TOKEN}
    )
    assert result.error_code == "slack_transport_unavailable"
    assert failed_api.open_count == 1
    assert len(failed_sockets.sockets) == 1
    assert failed_sockets.sockets[0].closed is True
    assert failed_api.clients[0].closed is True


def test_slack_product_adapter_uses_existing_runtime_end_to_end(tmp_path: Path) -> None:
    api = _SlackAPI()
    sockets = _SocketFactory([_event_frame("runtime-envelope")])
    adapter = SlackSocketModeAdapter(
        tmp_path / "slack.db",
        client_factory=api.factory,
        socket_factory=sockets,
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            model_gateway=_Gateway(),
            allow_unmanaged_model_gateway_for_testing=True,
            connector_vault=InMemoryCredentialVault(),
            channel_lifecycle_adapters={"slack": adapter},
            model_worker_poll_seconds=0.01,
            model_worker_shutdown_seconds=1,
        )
    )

    slack = next(
        item
        for item in app.state.channel_self_service.catalog()["items"]
        if item["channel_id"] == "slack"
    )
    assert slack["adapter_available"] is True
    assert app.state.channel_runtime_dispatcher is not None

    with TestClient(app):
        service = app.state.channel_self_service
        service.save(
            "slack",
            display_name="办公 Slack",
            config={},
            secrets={
                "slack_bot_token": _BOT_TOKEN,
                "slack_app_token": _APP_TOKEN,
            },
            request_id="slack-save",
        )
        assert service.enable("slack", request_id="slack-enable")["health"] == "connected"
        _wait(lambda: any(item["text"] == "已完成本周进展整理" for item in api.sent))

        threads, _ = app.state.runtime.list_threads()
        assert len([thread for thread in threads if thread.title == "Slack 会话"]) == 1
        assert _CHANNEL not in repr(threads)
        assert service.disable("slack", request_id="slack-disable")["health"] == "disabled"

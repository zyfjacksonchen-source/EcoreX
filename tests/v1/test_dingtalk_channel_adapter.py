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
from ecorex.connectors.dingtalk import DingTalkStreamAdapter
from ecorex.connectors.models import ConnectorHealth
from ecorex.gateway import GatewayEvent
from ecorex.runtime import RuntimeSettings, create_app


_CLIENT_ID = "ding-client-123456"
_CLIENT_SECRET = "client-secret-value-123456"
_CONVERSATION = "cid-group-123"
_REPLY_URL = "https://oapi.dingtalk.com/robot/sendBySession?session=reply-token"


def _callback(
    stream_message_id: str,
    *,
    provider_message_id: str = "provider-message-1",
    text: str = "请整理本周进展",
) -> str:
    return json.dumps(
        {
            "specVersion": "1.0",
            "type": "CALLBACK",
            "headers": {
                "messageId": stream_message_id,
                "contentType": "application/json",
                "topic": "/v1.0/im/bot/messages/get",
            },
            "data": json.dumps(
                {
                    "conversationId": _CONVERSATION,
                    "msgId": provider_message_id,
                    "msgtype": "text",
                    "text": {"content": text},
                    "sessionWebhook": _REPLY_URL,
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )


def _disconnect(message_id: str) -> str:
    return json.dumps(
        {
            "specVersion": "1.0",
            "type": "SYSTEM",
            "headers": {"messageId": message_id, "topic": "disconnect"},
            "data": json.dumps({"reason": "refresh"}),
        }
    )


class _DingTalkAPI:
    def __init__(self, *, auth_rejected: bool = False, uncertain_send: bool = False):
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.lock = threading.Lock()
        self.paths: list[str] = []
        self.open_count = 0
        self.send_attempts = 0
        self.sent: list[dict[str, Any]] = []
        self.clients: list[_Client] = []

    def factory(self) -> "_Client":
        client = _Client(self)
        self.clients.append(client)
        return client


class _Client:
    def __init__(self, api: _DingTalkAPI):
        self.api = api
        self.closed = False

    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response:
        url = path if path.startswith("https:") else f"https://api.dingtalk.test{path}"
        request = httpx.Request("POST", url)
        self.api.paths.append(path)
        if path == "/v1.0/gateway/connections/open":
            assert json["clientId"] == _CLIENT_ID
            assert json["clientSecret"] == _CLIENT_SECRET
            assert json["subscriptions"] == [
                {
                    "type": "CALLBACK",
                    "topic": "/v1.0/im/bot/messages/get",
                }
            ]
            if self.api.auth_rejected:
                return httpx.Response(401, request=request, json={"code": "InvalidAuthentication"})
            self.api.open_count += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "endpoint": "wss://wss-open-connection.dingtalk.com/connect",
                    "ticket": f"ticket-{self.api.open_count}",
                },
            )
        if path == _REPLY_URL:
            with self.api.lock:
                self.api.send_attempts += 1
                if self.api.uncertain_send:
                    raise httpx.ReadTimeout("response not observed", request=request)
                self.api.sent.append(dict(json))
            return httpx.Response(
                200,
                request=request,
                json={"errcode": 0, "errmsg": "ok"},
            )
        raise AssertionError(path)

    def close(self) -> None:
        self.closed = True


class _Socket:
    def __init__(self, frames: list[str]):
        self.frames: queue.Queue[str] = queue.Queue()
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
    ) -> None:
        self.first_frames = first_frames
        self.reconnect_frames = reconnect_frames or []
        self.sockets: list[_Socket] = []
        self.urls: list[str] = []

    def __call__(self, url: str) -> _Socket:
        assert url.startswith(
            "wss://wss-open-connection.dingtalk.com/connect?ticket=ticket-"
        )
        self.urls.append(url)
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
        ), "DingTalk callback must be acknowledged before Runtime dispatch"
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-dingtalk",
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
                "response_id": "dingtalk-response",
                "delta": "已完成本周进展整理",
            }
        )
        yield GatewayEvent.model_validate(
            {
                "seq": 2,
                "event_type": "response.completed",
                "response_id": "dingtalk-response",
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
    raise TimeoutError("DingTalk adapter did not converge")


def _adapter(
    path: Path,
    api: _DingTalkAPI,
    sockets: _SocketFactory,
    owner: ChannelCredentialOwner,
    dispatcher: _Dispatcher,
) -> DingTalkStreamAdapter:
    adapter = DingTalkStreamAdapter(
        path,
        client_factory=api.factory,
        socket_factory=sockets,
    )
    adapter.bind_runtime(owner, dispatcher)  # type: ignore[arg-type]
    return adapter


def _config() -> dict[str, str]:
    return {
        "dingtalk_client_id": _CLIENT_ID,
        "dingtalk_client_secret": _CLIENT_SECRET,
    }


def test_dingtalk_stream_acks_journals_deduplicates_and_delivers_once(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "dingtalk.db"
    api = _DingTalkAPI()
    sockets = _SocketFactory(
        [
            _callback("stream-message-1"),
            _callback("stream-message-2"),
        ]
    )
    dispatcher = _Dispatcher(sockets)
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = _adapter(state_path, api, sockets, owner, dispatcher)

    probe_sockets = _SocketFactory([])
    probe = DingTalkStreamAdapter(
        tmp_path / "probe.db",
        client_factory=api.factory,
        socket_factory=probe_sockets,
    )
    assert probe.test(_config()).health is ConnectorHealth.CONNECTED
    assert probe_sockets.sockets[0].closed is True
    assert api.clients[0].closed is True
    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(sockets.sockets[0].sent) == 2 and len(api.sent) == 1)

    assert len(dispatcher.receipts) == 1
    assert [ack["code"] for ack in sockets.sockets[0].sent] == [200, 200]
    assert [ack["headers"]["messageId"] for ack in sockets.sockets[0].sent] == [
        "stream-message-1",
        "stream-message-2",
    ]
    assert api.sent == [
        {"msgtype": "text", "text": {"content": "已完成整理"}}
    ]
    assert adapter.stop(1) is True
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    state_bytes = state_path.read_bytes()
    assert _CLIENT_ID.encode() not in state_bytes
    assert _CLIENT_SECRET.encode() not in state_bytes
    assert _REPLY_URL.encode() not in state_bytes
    assert _CONVERSATION.encode() not in state_bytes

    repeated_sockets = _SocketFactory(
        [_callback("stream-message-3")]
    )
    repeated_dispatcher = _Dispatcher(repeated_sockets)
    repeated = _adapter(
        state_path,
        api,
        repeated_sockets,
        owner,
        repeated_dispatcher,
    )
    assert repeated.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(repeated_sockets.sockets[0].sent))
    time.sleep(0.05)
    assert repeated_dispatcher.receipts == []
    assert len(api.sent) == 1
    assert repeated.stop(1) is True


def test_dingtalk_tenant_scope_and_errors_hide_credentials(tmp_path: Path) -> None:
    state_path = tmp_path / "dingtalk.db"
    api = _DingTalkAPI()
    first_sockets = _SocketFactory([_callback("stream-message-1")])
    first_dispatcher = _Dispatcher(first_sockets)
    first = _adapter(
        state_path,
        api,
        first_sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        first_dispatcher,
    )
    assert first.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1)
    assert first.stop(1) is True

    other_sockets = _SocketFactory([_callback("stream-message-1")])
    other_dispatcher = _Dispatcher(other_sockets)
    other = _adapter(
        state_path,
        api,
        other_sockets,
        ChannelCredentialOwner("account-b", "organization-a"),
        other_dispatcher,
    )
    assert other.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 2)
    assert len(other_dispatcher.receipts) == 1
    assert other.stop(1) is True

    rejected = DingTalkStreamAdapter(
        tmp_path / "rejected.db",
        client_factory=_DingTalkAPI(auth_rejected=True).factory,
        socket_factory=_SocketFactory([]),
    ).test(_config())
    assert rejected.error_code == "dingtalk_auth_rejected"
    assert _CLIENT_ID not in repr(rejected)
    assert _CLIENT_SECRET not in repr(rejected)


def test_dingtalk_delivery_uncertainty_is_not_retried(tmp_path: Path) -> None:
    api = _DingTalkAPI(uncertain_send=True)
    sockets = _SocketFactory([_callback("uncertain-stream")])
    dispatcher = _Dispatcher(sockets)
    state_path = tmp_path / "dingtalk.db"
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = _adapter(
        state_path,
        api,
        sockets,
        owner,
        dispatcher,
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: api.send_attempts == 1)
    _wait(lambda: adapter.health().health is ConnectorHealth.DEGRADED)
    time.sleep(0.1)
    assert api.send_attempts == 1
    assert adapter.health().error_code == "dingtalk_delivery_uncertain"
    assert adapter.stop(1) is True

    restarted_sockets = _SocketFactory([])
    restarted = _adapter(
        state_path,
        api,
        restarted_sockets,
        owner,
        _Dispatcher(restarted_sockets),
    )
    assert restarted.start(_config()).health is ConnectorHealth.DEGRADED
    time.sleep(0.1)
    assert api.send_attempts == 1
    assert restarted.stop(1) is True


def test_dingtalk_disconnect_reconnects_and_stop_is_bounded(tmp_path: Path) -> None:
    api = _DingTalkAPI()
    sockets = _SocketFactory(
        [_disconnect("system-message-1")],
        [_callback("stream-after-reconnect")],
    )
    dispatcher = _Dispatcher(sockets)
    adapter = _adapter(
        tmp_path / "dingtalk.db",
        api,
        sockets,
        ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,
    )

    assert adapter.start(_config()).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1, seconds=3)
    assert len(sockets.sockets) == 2
    assert api.open_count == 2
    assert sockets.sockets[0].sent[0]["headers"]["messageId"] == "system-message-1"
    assert adapter.health().health is ConnectorHealth.CONNECTED
    started = time.monotonic()
    assert adapter.stop(1) is True
    assert time.monotonic() - started < 1


def test_dingtalk_product_adapter_uses_existing_runtime_end_to_end(
    tmp_path: Path,
) -> None:
    api = _DingTalkAPI()
    sockets = _SocketFactory([_callback("runtime-stream-message")])
    adapter = DingTalkStreamAdapter(
        tmp_path / "dingtalk.db",
        client_factory=api.factory,
        socket_factory=sockets,
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            model_gateway=_Gateway(),
            allow_unmanaged_model_gateway_for_testing=True,
            connector_vault=InMemoryCredentialVault(),
            channel_lifecycle_adapters={"dingtalk": adapter},
            model_worker_poll_seconds=0.01,
            model_worker_shutdown_seconds=1,
        )
    )

    item = next(
        item
        for item in app.state.channel_self_service.catalog()["items"]
        if item["channel_id"] == "dingtalk"
    )
    assert item["adapter_available"] is True
    assert app.state.channel_runtime_dispatcher is not None

    with TestClient(app):
        service = app.state.channel_self_service
        service.save(
            "dingtalk",
            display_name="办公钉钉",
            config={"dingtalk_client_id": _CLIENT_ID},
            secrets={"dingtalk_client_secret": _CLIENT_SECRET},
            request_id="dingtalk-save",
        )
        assert (
            service.enable("dingtalk", request_id="dingtalk-enable")["health"]
            == "connected"
        )
        _wait(
            lambda: any(
                item["text"]["content"] == "已完成本周进展整理"
                for item in api.sent
            )
        )

        threads, _ = app.state.runtime.list_threads()
        assert len([thread for thread in threads if thread.title == "钉钉 会话"]) == 1
        assert _CONVERSATION not in repr(threads)
        assert (
            service.disable("dingtalk", request_id="dingtalk-disable")["health"]
            == "disabled"
        )

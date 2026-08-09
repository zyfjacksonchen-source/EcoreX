from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

import httpx

from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.feishu import FeishuMessageBotAdapter, _default_channel
from ecorex.connectors.models import ConnectorHealth


_APP_ID = "cli_feishu_test_01"
_APP_SECRET = "feishu-secret-value"


class _FeishuAPI:
    def __init__(self, *, auth_rejected: bool = False, uncertain_send: bool = False):
        self.auth_rejected = auth_rejected
        self.uncertain_send = uncertain_send
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def factory(self) -> "_FeishuAPI":
        return self

    def post(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request = httpx.Request("POST", "https://open.feishu.test" + path)
        if path.endswith("tenant_access_token/internal"):
            if self.auth_rejected:
                return httpx.Response(
                    200,
                    request=request,
                    json={"code": 10003, "msg": "invalid"},
                )
            assert json == {"app_id": _APP_ID, "app_secret": _APP_SECRET}
            return httpx.Response(
                200,
                request=request,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-access-token",
                    "expire": 7200,
                },
            )
        assert path == "/open-apis/im/v1/messages"
        assert params == {"receive_id_type": "chat_id"}
        assert headers == {"Authorization": "Bearer tenant-access-token"}
        self.sent.append(dict(json))
        if self.uncertain_send:
            raise httpx.ReadTimeout("uncertain", request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 0,
                "data": {"message_id": f"om_{len(self.sent)}"},
            },
        )

    def close(self) -> None:
        self.closed = True


class _Channel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.is_ready = False
        self.stopped = False
        self.start_calls = 0

    def on(self, name: str, handler):
        self.handlers[name] = handler
        return lambda: self.handlers.pop(name, None)

    async def start_background(self, *, timeout: float | None = 30.0) -> None:
        assert timeout is not None and timeout > 0
        self.start_calls += 1
        self.is_ready = True

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self.stopped = True
        self.is_ready = False

    def emit(self, message: Any) -> None:
        self.handlers["message"](message)


@dataclass
class _Message:
    message_id: str
    chat_id: str
    body_text: str


class _Dispatcher:
    def __init__(self) -> None:
        self.receipts: list[ChannelTurnReceipt] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-feishu",
            turn_id=f"turn-{message.message_id}",
            client_message_id=f"client-{message.message_id}",
            conversation_sha256="conversation-hash",
        )
        self.receipts.append(receipt)
        return receipt

    def deliver(self, receipt, *, conversation_id, transport) -> bool:
        transport.send_text(
            channel_id=receipt.channel_id,
            conversation_id=conversation_id,
            text="已完成飞书任务",
            idempotency_key=f"delivery-{receipt.turn_id}",
        )
        return True


def _wait(predicate, *, seconds: float = 2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("feishu adapter did not converge")


def _adapter(
    tmp_path: Path,
    api: _FeishuAPI,
    channel: _Channel,
    *,
    owner: ChannelCredentialOwner | None = None,
) -> tuple[FeishuMessageBotAdapter, _Dispatcher]:
    dispatcher = _Dispatcher()
    adapter = FeishuMessageBotAdapter(
        tmp_path / "feishu.db",
        client_factory=api.factory,
        channel_factory=lambda app_id, app_secret: channel,
    )
    adapter.bind_runtime(
        owner or ChannelCredentialOwner("account-a", "organization-a"),
        dispatcher,  # type: ignore[arg-type]
    )
    return adapter, dispatcher


def test_packaged_official_sdk_contract_is_reachable_and_strict() -> None:
    channel = _default_channel(_APP_ID, _APP_SECRET)
    try:
        assert type(channel).__module__.startswith("lark_channel.")
        assert channel._config.security.mode == "strict"
        assert channel._config.security.strict_content_text is True
        assert channel._config.transport.kind == "ws"
        assert channel._config.transport.auto_reconnect is True
        assert channel._config.transport.trust_env_proxy is False
        assert channel._config.inbound.include_raw is False
    finally:
        channel.stop()


def test_feishu_message_bot_validates_credentials_and_dispatches_once(
    tmp_path: Path,
) -> None:
    api = _FeishuAPI()
    channel = _Channel()
    adapter, dispatcher = _adapter(tmp_path, api, channel)
    credentials = {
        "feishu_app_id": _APP_ID,
        "feishu_app_secret": _APP_SECRET,
    }

    assert adapter.test(credentials).health is ConnectorHealth.CONNECTED
    assert adapter.start(credentials).health is ConnectorHealth.CONNECTED
    channel.emit(_Message("om_inbound_1", "oc_chat_1", "请汇总项目进度"))
    _wait(lambda: len(api.sent) == 1)

    assert dispatcher.receipts[0].channel_id == "feishu"
    assert api.sent[0]["receive_id"] == "oc_chat_1"
    assert api.sent[0]["msg_type"] == "text"
    assert _APP_SECRET not in repr(api.sent)
    assert adapter.health().health is ConnectorHealth.CONNECTED
    assert channel.start_calls == 1
    assert adapter.stop(1) is True
    assert channel.stopped is True
    assert os.stat(tmp_path / "feishu.db").st_mode & 0o777 == 0o600


def test_feishu_message_bot_health_tracks_reconnect_and_rejects_bad_secret(
    tmp_path: Path,
) -> None:
    rejected = FeishuMessageBotAdapter(
        tmp_path / "rejected.db",
        client_factory=_FeishuAPI(auth_rejected=True).factory,
        channel_factory=lambda app_id, app_secret: _Channel(),
    ).test({"feishu_app_id": _APP_ID, "feishu_app_secret": _APP_SECRET})
    assert rejected.error_code == "feishu_bot_auth_rejected"
    assert _APP_SECRET not in repr(rejected)

    api = _FeishuAPI()
    channel = _Channel()
    adapter, _ = _adapter(tmp_path, api, channel)
    assert adapter.start({
        "feishu_app_id": _APP_ID,
        "feishu_app_secret": _APP_SECRET,
    }).health is ConnectorHealth.CONNECTED
    channel.handlers["reconnecting"]()
    assert adapter.health().health is ConnectorHealth.DEGRADED
    assert adapter.health().error_code == "feishu_bot_reconnecting"
    channel.handlers["reconnected"]()
    assert adapter.health().health is ConnectorHealth.CONNECTED
    assert adapter.stop(1) is True


def test_feishu_message_bot_uncertain_delivery_is_never_resent(
    tmp_path: Path,
) -> None:
    api = _FeishuAPI(uncertain_send=True)
    adapter, _ = _adapter(tmp_path, api, _Channel())
    assert adapter.start({
        "feishu_app_id": _APP_ID,
        "feishu_app_secret": _APP_SECRET,
    }).health is ConnectorHealth.CONNECTED

    for _ in range(2):
        try:
            adapter.send_text(
                channel_id="feishu",
                conversation_id="oc_chat_1",
                text="只发一次",
                idempotency_key="delivery-stable",
            )
        except RuntimeError:
            pass
    assert len(api.sent) == 1
    assert adapter.stop(1) is True


def test_feishu_message_bot_delivery_journal_is_tenant_scoped(
    tmp_path: Path,
) -> None:
    api_a = _FeishuAPI()
    first, _ = _adapter(tmp_path, api_a, _Channel())
    assert first.start({
        "feishu_app_id": _APP_ID,
        "feishu_app_secret": _APP_SECRET,
    }).health is ConnectorHealth.CONNECTED
    first.send_text(
        channel_id="feishu",
        conversation_id="oc_chat_1",
        text="租户消息",
        idempotency_key="same-key",
    )
    assert first.stop(1) is True

    api_b = _FeishuAPI()
    second, _ = _adapter(
        tmp_path,
        api_b,
        _Channel(),
        owner=ChannelCredentialOwner("account-b", "organization-a"),
    )
    assert second.start({
        "feishu_app_id": _APP_ID,
        "feishu_app_secret": _APP_SECRET,
    }).health is ConnectorHealth.CONNECTED
    second.send_text(
        channel_id="feishu",
        conversation_id="oc_chat_1",
        text="租户消息",
        idempotency_key="same-key",
    )
    assert len(api_a.sent) == len(api_b.sent) == 1
    assert second.stop(1) is True

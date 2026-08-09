from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

import httpx
from fastapi.testclient import TestClient
import pytest

from ecorex.connectors import InMemoryCredentialVault
from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.models import ConnectorHealth
from ecorex.connectors.telegram import TelegramBotAdapter
from ecorex.gateway import GatewayEvent
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.session import Ed25519SessionLeaseVerifier, ManagedSessionService


_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"


class _TelegramAPI:
    def __init__(
        self,
        *,
        auth_rejected: bool = False,
        webhook_url: str = "",
    ) -> None:
        self.auth_rejected = auth_rejected
        self.webhook_url = webhook_url
        self.lock = threading.Lock()
        self.update_available = True
        self.offsets: list[int] = []
        self.sent: list[dict[str, Any]] = []
        self.names: list[str] = []
        self.paths: list[str] = []

    def factory(self, token: str) -> "_Client":
        assert token == _TOKEN
        return _Client(self)


class _Client:
    def __init__(self, api: _TelegramAPI) -> None:
        self.api = api
        self.closed = False

    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response:
        request = httpx.Request("POST", f"https://api.telegram.test/{path}")
        self.api.paths.append(path)
        if path == "getMe":
            if self.api.auth_rejected:
                return httpx.Response(
                    401,
                    request=request,
                    json={"ok": False, "error_code": 401},
                )
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"id": 99, "is_bot": True}},
            )
        if path == "getWebhookInfo":
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"url": self.api.webhook_url}},
            )
        if path == "setMyName":
            self.api.names.append(str(json.get("name")))
            return httpx.Response(200, request=request, json={"ok": True, "result": True})
        if path == "getUpdates":
            with self.api.lock:
                offset = int(json["offset"])
                self.api.offsets.append(offset)
                if self.api.update_available and offset <= 41:
                    self.api.update_available = False
                    result = [{
                        "update_id": 41,
                        "message": {
                            "message_id": 7,
                            "chat": {"id": -1000123},
                            "text": "请整理本周进展",
                        },
                    }]
                else:
                    result = []
            if not result:
                time.sleep(0.005)
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": result},
            )
        if path == "sendMessage":
            with self.api.lock:
                self.api.sent.append(dict(json))
                message_id = len(self.api.sent)
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"message_id": message_id}},
            )
        raise AssertionError(path)

    def close(self) -> None:
        self.closed = True


class _BlockingClient(_Client):
    def __init__(self, api: _TelegramAPI, entered: threading.Event) -> None:
        super().__init__(api)
        self.entered = entered

    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response:
        if path == "getUpdates":
            self.entered.set()
            time.sleep(0.2)
        return super().post(path, json=json)


class _Dispatcher:
    def __init__(self) -> None:
        self.receipts: list[ChannelTurnReceipt] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        receipt = ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-telegram",
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
            text="已完成整理",
            idempotency_key=f"delivery-{receipt.turn_id}",
        )
        return True


class _Gateway:
    async def stream(self, _request):
        yield GatewayEvent.model_validate({
            "seq": 1,
            "event_type": "output_text.delta",
            "response_id": "telegram-response",
            "delta": "已完成本周进展整理",
        })
        yield GatewayEvent.model_validate({
            "seq": 2,
            "event_type": "response.completed",
            "response_id": "telegram-response",
        })

    async def aclose(self) -> None:
        return None


def _wait(predicate, *, seconds: float = 2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("telegram adapter did not converge")


def test_telegram_long_poll_persists_offset_and_delivers_once(tmp_path: Path) -> None:
    api = _TelegramAPI()
    dispatcher = _Dispatcher()
    state_path = tmp_path / "telegram.db"
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = TelegramBotAdapter(state_path, client_factory=api.factory)
    adapter.bind_runtime(owner, dispatcher)  # type: ignore[arg-type]

    assert adapter.test({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    assert adapter.start({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: len(api.sent) == 1)

    assert dispatcher.receipts[0].channel_id == "telegram"
    assert api.sent == [{"chat_id": -1000123, "text": "已完成整理"}]
    assert adapter.health().health is ConnectorHealth.CONNECTED
    assert adapter.stop(1) is True
    assert stat_mode(state_path) == 0o600

    api.offsets.clear()
    assert adapter.start({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(api.offsets))
    assert api.offsets[0] == 42

    adapter.send_text(
        channel_id="telegram",
        conversation_id="-1000123",
        text="幂等消息",
        idempotency_key="stable-delivery",
    )
    adapter.send_text(
        channel_id="telegram",
        conversation_id="-1000123",
        text="幂等消息",
        idempotency_key="stable-delivery",
    )
    assert [item["text"] for item in api.sent].count("幂等消息") == 1
    assert adapter.stop(1) is True


def test_telegram_offset_and_errors_are_tenant_scoped_and_secret_free(tmp_path: Path) -> None:
    state_path = tmp_path / "telegram.db"
    first_api = _TelegramAPI()
    first = TelegramBotAdapter(state_path, client_factory=first_api.factory)
    first.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert first.start({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(first_api.sent))
    assert first.stop(1) is True

    second_api = _TelegramAPI()
    second_api.update_available = False
    second = TelegramBotAdapter(state_path, client_factory=second_api.factory)
    second.bind_runtime(
        ChannelCredentialOwner("account-b", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert second.start({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(second_api.offsets))
    assert second_api.offsets[0] == 0
    assert second.stop(1) is True

    rejected = TelegramBotAdapter(
        tmp_path / "rejected.db",
        client_factory=_TelegramAPI(auth_rejected=True).factory,
    ).test({"telegram_token": _TOKEN})
    assert rejected.error_code == "telegram_auth_rejected"
    assert _TOKEN not in repr(rejected)


def test_telegram_stop_completes_while_long_poll_is_active(tmp_path: Path) -> None:
    api = _TelegramAPI()
    api.update_available = False
    entered = threading.Event()
    adapter = TelegramBotAdapter(
        tmp_path / "telegram.db",
        client_factory=lambda token: _BlockingClient(api, entered),
    )
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert adapter.start({"telegram_token": _TOKEN}).health is ConnectorHealth.CONNECTED
    assert entered.wait(1)
    started = time.monotonic()
    assert adapter.stop(1) is True
    assert time.monotonic() - started < 1


def test_telegram_refuses_to_replace_an_existing_webhook(tmp_path: Path) -> None:
    api = _TelegramAPI(webhook_url="https://example.test/telegram")
    adapter = TelegramBotAdapter(tmp_path / "telegram.db", client_factory=api.factory)
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )

    assert (
        adapter.test({"telegram_token": _TOKEN}).error_code
        == "telegram_webhook_active"
    )
    assert (
        adapter.start({"telegram_token": _TOKEN}).error_code
        == "telegram_webhook_active"
    )
    assert api.paths == ["getMe", "getWebhookInfo", "getMe", "getWebhookInfo"]
    assert "getUpdates" not in api.paths


def test_telegram_product_adapter_uses_the_existing_runtime_end_to_end(
    tmp_path: Path,
) -> None:
    api = _TelegramAPI()
    adapter = TelegramBotAdapter(
        tmp_path / "telegram.db",
        client_factory=api.factory,
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            model_gateway=_Gateway(),
            allow_unmanaged_model_gateway_for_testing=True,
            connector_vault=InMemoryCredentialVault(),
            channel_lifecycle_adapters={"telegram": adapter},
            model_worker_poll_seconds=0.01,
            model_worker_shutdown_seconds=1,
        )
    )

    catalog = app.state.channel_self_service.catalog()["items"]
    telegram = next(item for item in catalog if item["channel_id"] == "telegram")
    assert telegram["adapter_available"] is True
    assert telegram["instance"] is None
    assert app.state.channel_runtime_dispatcher is not None
    assert adapter.health().health is ConnectorHealth.DISABLED

    with TestClient(app):
        service = app.state.channel_self_service
        saved = service.save(
            "telegram",
            display_name="办公机器人",
            config={},
            secrets={"telegram_token": _TOKEN},
            request_id="telegram-save",
        )
        assert saved["enabled"] is False
        assert (
            service.enable("telegram", request_id="telegram-enable")["health"]
            == "connected"
        )
        _wait(lambda: any(item["text"] == "已完成本周进展整理" for item in api.sent))
        assert api.names == ["e-Mate"]

        threads, _ = app.state.runtime.list_threads()
        assert len([thread for thread in threads if thread.title == "Telegram 会话"]) == 1
        assert "-1000123" not in repr(threads)
        assert (
            service.disable("telegram", request_id="telegram-disable")["health"]
            == "disabled"
        )


def test_telegram_cannot_bind_without_a_managed_agent_worker(tmp_path: Path) -> None:
    api = _TelegramAPI()
    api.update_available = False
    adapter = TelegramBotAdapter(tmp_path / "telegram.db", client_factory=api.factory)
    vault = InMemoryCredentialVault()
    session = ManagedSessionService(
        tmp_path / "runtime.db",
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier({"unused": bytes(range(32))}),
    )
    with pytest.raises(ValueError, match="require the Agent worker"):
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                managed_session_service=session,
                require_managed_session=True,
                model_gateway=_Gateway(),
                allow_unmanaged_model_gateway_for_testing=True,
                connector_vault=vault,
                channel_lifecycle_adapters={"telegram": adapter},
                model_worker_poll_seconds=0.01,
                model_worker_shutdown_seconds=1,
            )
        )
    assert api.paths == []


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777

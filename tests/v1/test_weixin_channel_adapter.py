from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import UTC, datetime, timedelta
import stat
import threading
import time
from typing import Any, Mapping

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex import __version__
from ecorex.connectors import InMemoryCredentialVault
from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import (
    ChannelCredentialOwner,
    ChannelDeviceAuthorization,
    ChannelSelfService,
    ChannelSelfServiceError,
    create_channel_self_service_router,
)
from ecorex.connectors.models import ConnectorHealth, ConnectorHealthResult
from ecorex.connectors.weixin import WeixinILinkAdapter


_TOKEN = "ilink-secret-bot-token"
_CONTEXT = "latest-user-context-token"
_CONFIG = {
    "weixin_base_url": "https://ilinkai.weixin.qq.com",
    "weixin_bot_id": "bot-a",
    "weixin_user_id": "user-a",
    "weixin_token": _TOKEN,
}


class _API:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.qr_generation = 0
        self.qr_statuses = ["scaned", "confirmed"]
        self.update_available = True
        self.cursors: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.expired = False

    def factory(self, base_url: str, token: str) -> "_Client":
        assert base_url == "https://ilinkai.weixin.qq.com"
        self.tokens.append(token)
        return _Client(self, token)


class _Client:
    def __init__(self, api: _API, token: str) -> None:
        self.api = api
        self.token = token
        self.closed = False

    def get(self, path: str, *, params: Mapping[str, Any]) -> httpx.Response:
        request = httpx.Request("GET", f"https://ilinkai.weixin.qq.com/{path}")
        if path == "ilink/bot/get_bot_qrcode":
            assert self.token == ""
            assert params == {"bot_type": "3"}
            self.api.qr_generation += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "qrcode": f"opaque-qr-{self.api.qr_generation}",
                    "qrcode_img_content": (
                        f"https://weixin.qq.com/q/{self.api.qr_generation}"
                    ),
                },
            )
        if path == "ilink/bot/get_qrcode_status":
            assert self.token == ""
            assert params["qrcode"].startswith("opaque-qr-")
            status = self.api.qr_statuses.pop(0)
            payload: dict[str, Any] = {"status": status}
            if status == "confirmed":
                payload.update(
                    {
                        "bot_token": _TOKEN,
                        "ilink_bot_id": "bot-a",
                        "ilink_user_id": "user-a",
                        "baseurl": "https://ilinkai.weixin.qq.com",
                    }
                )
            return httpx.Response(200, request=request, json=payload)
        raise AssertionError(path)

    def post(self, path: str, *, json: Mapping[str, Any]) -> httpx.Response:
        request = httpx.Request("POST", f"https://ilinkai.weixin.qq.com/{path}")
        assert json["base_info"] == {"channel_version": __version__}
        if path == "ilink/bot/getupdates":
            assert self.token == _TOKEN
            with self.api.lock:
                self.api.cursors.append(str(json["get_updates_buf"]))
                if self.api.expired:
                    payload = {"ret": -14}
                elif self.api.update_available:
                    self.api.update_available = False
                    payload = {
                        "ret": 0,
                        "get_updates_buf": "cursor-after-7",
                        "msgs": [
                            {
                                "message_type": 1,
                                "message_id": 7,
                                "from_user_id": "wx-user-7",
                                "context_token": _CONTEXT,
                                "item_list": [
                                    {
                                        "type": 1,
                                        "text_item": {"text": "请整理本周进展"},
                                    }
                                ],
                            }
                        ],
                    }
                else:
                    payload = {"ret": 0, "msgs": []}
            if not payload.get("msgs"):
                time.sleep(0.005)
            return httpx.Response(200, request=request, json=payload)
        if path == "ilink/bot/sendmessage":
            assert self.token == _TOKEN
            with self.api.lock:
                self.api.sent.append(dict(json["msg"]))
            return httpx.Response(200, request=request, json={"ret": 0})
        raise AssertionError(path)

    def close(self) -> None:
        self.closed = True


class _Dispatcher:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def dispatch(self, message) -> ChannelTurnReceipt:
        self.messages.append(message)
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-weixin",
            turn_id=f"turn-{message.message_id}",
            client_message_id=f"client-{message.message_id}",
            conversation_sha256="conversation-hash",
        )

    def deliver(self, receipt, *, conversation_id, transport) -> bool:
        transport.send_text(
            channel_id=receipt.channel_id,
            conversation_id=conversation_id,
            text="已完成整理",
            idempotency_key=f"delivery-{receipt.turn_id}",
        )
        return True


def _wait(predicate, *, seconds: float = 2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError("weixin adapter did not converge")


def test_weixin_device_flow_stores_token_only_in_vault_and_starts_runtime(
    tmp_path: Path,
) -> None:
    api = _API()
    vault = InMemoryCredentialVault()
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = WeixinILinkAdapter(
        tmp_path / "weixin.db", client_factory=api.factory
    )
    dispatcher = _Dispatcher()
    adapter.bind_runtime(owner, dispatcher)  # type: ignore[arg-type]
    service = ChannelSelfService(
        owner=owner, vault=vault, adapters={"weixin": adapter}
    )

    catalog = {item["channel_id"]: item for item in service.catalog()["items"]}
    assert catalog["weixin"]["adapter_available"] is True
    assert catalog["weixin"]["auth_kind"] == "device_code"
    assert catalog["weixin"]["actions"]["auth_begin"] is True
    assert catalog["weixin"]["actions"]["save"] is False

    pending = service.begin_authorization("weixin", request_id="weixin-begin")
    assert pending["status"] == "pending"
    assert pending["verification_url"] == "https://weixin.qq.com/q/1"
    assert pending["qr_image_data_url"].startswith("data:image/png;base64,")
    assert _TOKEN not in repr(pending)

    scanned = service.poll_authorization(
        "weixin", pending["flow_id"], request_id="weixin-poll-scan"
    )
    assert scanned["status"] == "scanned"
    assert scanned["verification_url"] == pending["verification_url"]

    confirmed = service.poll_authorization(
        "weixin", pending["flow_id"], request_id="weixin-poll-confirm"
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["verification_url"] is None
    assert confirmed["qr_image_data_url"] is None
    assert confirmed["instance"]["enabled"] is True
    assert confirmed["instance"]["health"] == "connected"
    assert _TOKEN not in repr(confirmed)
    _wait(lambda: len(api.sent) == 1)

    assert dispatcher.messages[0].channel_id == "weixin"
    assert dispatcher.messages[0].text == "请整理本周进展"
    assert api.sent[0]["to_user_id"] == "wx-user-7"
    assert api.sent[0]["context_token"] == _CONTEXT
    assert api.sent[0]["item_list"][0]["text_item"]["text"] == "已完成整理"
    assert _TOKEN not in (tmp_path / "weixin.db").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert stat.S_IMODE((tmp_path / "weixin.db").stat().st_mode) == 0o600

    repeated = service.poll_authorization(
        "weixin", pending["flow_id"], request_id="weixin-poll-repeat"
    )
    assert repeated["status"] == "confirmed"
    assert repeated["instance"]["instance_id"] == confirmed["instance"]["instance_id"]
    assert adapter.stop(1) is True


def test_weixin_device_router_cancel_refresh_and_no_secret_projection(
    tmp_path: Path,
) -> None:
    api = _API()
    adapter = WeixinILinkAdapter(tmp_path / "weixin.db", client_factory=api.factory)
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=InMemoryCredentialVault(),
        adapters={"weixin": adapter},
    )
    app = FastAPI()
    app.include_router(create_channel_self_service_router(service))

    with TestClient(app) as client:
        begun = client.post("/connectors/channels/weixin/auth/begin")
        assert begun.status_code == 200
        flow = begun.json()
        cancelled = client.post(
            f"/connectors/channels/weixin/auth/{flow['flow_id']}/cancel"
        )
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["verification_url"] is None
        assert cancelled.json()["qr_image_data_url"] is None
        refreshed = client.post(
            f"/connectors/channels/weixin/auth/{flow['flow_id']}/refresh"
        )
        assert refreshed.json()["status"] == "pending"
        assert refreshed.json()["verification_url"] == "https://weixin.qq.com/q/2"
        assert refreshed.json()["qr_image_data_url"].startswith("data:image/png;base64,")
        assert "qrcode" not in refreshed.json()
        assert _TOKEN not in repr(refreshed.json())


def test_weixin_cursor_and_context_are_tenant_scoped(tmp_path: Path) -> None:
    path = tmp_path / "weixin.db"
    first_api = _API()
    first = WeixinILinkAdapter(path, client_factory=first_api.factory)
    first.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert first.start(_CONFIG).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(first_api.sent))
    assert first.stop(1) is True

    second_api = _API()
    second_api.update_available = False
    second = WeixinILinkAdapter(path, client_factory=second_api.factory)
    second.bind_runtime(
        ChannelCredentialOwner("account-b", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert second.start(_CONFIG).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(second_api.cursors))
    assert second_api.cursors[0] == ""
    assert second.stop(1) is True

    first_again_api = _API()
    first_again_api.update_available = False
    first_again = WeixinILinkAdapter(path, client_factory=first_again_api.factory)
    first_again.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    assert first_again.start(_CONFIG).health is ConnectorHealth.CONNECTED
    _wait(lambda: bool(first_again_api.cursors))
    assert first_again_api.cursors[0] == "cursor-after-7"
    assert first_again.stop(1) is True


def test_weixin_minus_14_expires_session_and_requires_new_qr_login(
    tmp_path: Path,
) -> None:
    api = _API()
    api.update_available = False
    api.expired = True
    adapter = WeixinILinkAdapter(tmp_path / "weixin.db", client_factory=api.factory)
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )

    assert adapter.start(_CONFIG).health is ConnectorHealth.CONNECTED
    _wait(
        lambda: adapter.health().error_code == "weixin_reauthentication_required"
    )
    assert adapter.health().health is ConnectorHealth.ERROR
    assert adapter.health().error_code == "weixin_reauthentication_required"
    assert adapter.stop(1) is True


class _ReplacingDeviceAdapter:
    def __init__(self) -> None:
        self.token = "old-token"
        self.running_token: str | None = None
        self.stop_succeeds = True
        self.fail_start_token: str | None = None
        self.fail_consume = False
        self.crash_start_token: str | None = None
        self.fail_commit_token: str | None = None
        self.staged_token: str | None = None
        self.calls: list[str] = []

    def begin_authorization(self) -> ChannelDeviceAuthorization:
        return self._result("pending")

    def poll_authorization(self, _flow_id: str) -> ChannelDeviceAuthorization:
        return self._result("confirmed")

    def cancel_authorization(self, _flow_id: str) -> ChannelDeviceAuthorization:
        return self._result("cancelled")

    def refresh_authorization(self, _flow_id: str) -> ChannelDeviceAuthorization:
        return self._result("pending")

    def consume_authorization(self, _flow_id: str) -> None:
        self.calls.append("consume")
        if self.fail_consume:
            raise RuntimeError("consume failed")
        return None

    def start(self, config: Mapping[str, Any]):
        token = str(config["weixin_token"])
        self.calls.append(f"start:{token}")
        if token == self.crash_start_token:
            raise KeyboardInterrupt
        if token == self.fail_start_token:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "weixin_start_rejected"
            )
        self.running_token = token
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def prepare_replace(self, config: Mapping[str, Any]):
        token = str(config["weixin_token"])
        self.calls.append(f"prepare:{token}")
        if token == self.crash_start_token:
            raise KeyboardInterrupt
        if token == self.fail_start_token:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "weixin_start_rejected"
            )
        self.staged_token = token
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def commit_replace(self, _timeout: float):
        token = self.staged_token
        assert token is not None
        if token == self.fail_commit_token:
            return ConnectorHealthResult(
                ConnectorHealth.ERROR, "weixin_commit_rejected"
            )
        self.calls.append(f"switch:{self.running_token}->{token}")
        self.running_token = token
        self.staged_token = None
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def abort_replace(self) -> None:
        self.calls.append("abort")
        self.staged_token = None

    def test(self, _config):
        raise AssertionError

    def health(self):
        raise AssertionError

    def stop(self, _timeout: float) -> bool:
        self.calls.append("stop")
        if self.stop_succeeds:
            self.running_token = None
        return self.stop_succeeds

    def _result(self, status: str) -> ChannelDeviceAuthorization:
        active = status in {"pending", "scanned"}
        return ChannelDeviceAuthorization(
            flow_id="wxauth_0123456789abcdef0123456789abcdef",
            status=status,
            verification_url="https://weixin.qq.com/q/test" if active else None,
            qr_image_data_url="data:image/png;base64,AA==" if active else None,
            expires_at=datetime.now(UTC) + timedelta(minutes=8),
            config={
                "weixin_base_url": "https://ilinkai.weixin.qq.com",
                "weixin_bot_id": "bot-a",
                "weixin_user_id": "user-a",
            } if status == "confirmed" else None,
            secrets={"weixin_token": self.token} if status == "confirmed" else None,
        )


def test_weixin_reauthorization_commits_credentials_before_session_switch() -> None:
    adapter = _ReplacingDeviceAdapter()
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=InMemoryCredentialVault(),
        adapters={"weixin": adapter},  # type: ignore[dict-item]
    )
    flow = service.begin_authorization("weixin", request_id="begin-old")
    service.poll_authorization("weixin", flow["flow_id"], request_id="confirm-old")
    assert adapter.running_token == "old-token"

    adapter.token = "new-token"
    service.poll_authorization("weixin", flow["flow_id"], request_id="confirm-new")
    assert adapter.calls[-3:] == [
        "prepare:new-token",
        "consume",
        "switch:old-token->new-token",
    ]
    assert adapter.running_token == "new-token"

    adapter.token = "must-not-replace"
    adapter.fail_start_token = "must-not-replace"
    with pytest.raises(ChannelSelfServiceError, match="weixin_start_rejected"):
        service.poll_authorization("weixin", flow["flow_id"], request_id="confirm-blocked")
    assert adapter.running_token == "new-token"
    assert service._read("weixin").secrets["weixin_token"] == "new-token"


def test_weixin_reauthorization_restores_old_record_when_start_or_consume_fails() -> None:
    vault = InMemoryCredentialVault()
    adapter = _ReplacingDeviceAdapter()
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=vault,
        adapters={"weixin": adapter},  # type: ignore[dict-item]
    )
    flow = service.begin_authorization("weixin", request_id="begin")
    service.poll_authorization("weixin", flow["flow_id"], request_id="old")

    adapter.token = "start-fails"
    adapter.fail_start_token = "start-fails"
    with pytest.raises(ChannelSelfServiceError, match="weixin_start_rejected"):
        service.poll_authorization("weixin", flow["flow_id"], request_id="start-fails")
    assert adapter.running_token == "old-token"
    assert service._read("weixin").secrets["weixin_token"] == "old-token"

    adapter.fail_start_token = None
    adapter.token = "consume-fails"
    adapter.fail_consume = True
    with pytest.raises(ChannelSelfServiceError, match="channel_adapter_failed"):
        service.poll_authorization(
            "weixin", flow["flow_id"], request_id="consume-fails"
        )
    assert adapter.running_token == "old-token"
    assert service._read("weixin").secrets["weixin_token"] == "old-token"


def test_weixin_reauthorization_crash_marker_recovers_old_record_on_restart() -> None:
    vault = InMemoryCredentialVault()
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter = _ReplacingDeviceAdapter()
    service = ChannelSelfService(
        owner=owner,
        vault=vault,
        adapters={"weixin": adapter},  # type: ignore[dict-item]
    )
    flow = service.begin_authorization("weixin", request_id="begin")
    service.poll_authorization("weixin", flow["flow_id"], request_id="old")

    adapter.token = "crash-token"
    adapter.crash_start_token = "crash-token"
    with pytest.raises(KeyboardInterrupt):
        service.poll_authorization("weixin", flow["flow_id"], request_id="crash")
    assert service._read("weixin").secrets["weixin_token"] == "old-token"
    assert vault.get(service._transition_reference("weixin"))["state"] == "preparing"

    restored_adapter = _ReplacingDeviceAdapter()
    restored = ChannelSelfService(
        owner=owner,
        vault=vault,
        adapters={"weixin": restored_adapter},  # type: ignore[dict-item]
    )
    asyncio.run(restored.start())
    assert restored_adapter.running_token == "old-token"
    with pytest.raises(KeyError):
        vault.get(restored._transition_reference("weixin"))


class _RollbackFailVault(InMemoryCredentialVault):
    blocked_generation: str | None = None

    def put(self, reference: str, material: Mapping[str, str]) -> None:
        if (
            self.blocked_generation is not None
            and material.get("generation_reference") == self.blocked_generation
        ):
            raise RuntimeError("rollback write failed")
        super().put(reference, material)


def test_weixin_pointer_rollback_failure_stops_transport_fail_closed() -> None:
    vault = _RollbackFailVault()
    adapter = _ReplacingDeviceAdapter()
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=vault,
        adapters={"weixin": adapter},  # type: ignore[dict-item]
    )
    flow = service.begin_authorization("weixin", request_id="begin")
    service.poll_authorization("weixin", flow["flow_id"], request_id="old")
    pointer = vault.get(service._reference("weixin"))
    assert set(pointer) == {"schema_version", "channel_id", "generation_reference"}
    old_generation = pointer["generation_reference"]
    assert vault.get(old_generation)["secrets_json"] == '{"weixin_token":"old-token"}'

    vault.blocked_generation = old_generation
    adapter.token = "new-token"
    adapter.fail_commit_token = "new-token"
    with pytest.raises(ChannelSelfServiceError, match="channel_device_rollback_failed"):
        service.poll_authorization("weixin", flow["flow_id"], request_id="new")
    assert adapter.running_token is None
    assert vault.get(service._transition_reference("weixin"))["state"] == "preparing"


def test_weixin_concurrent_confirmed_polls_leave_one_readable_generation() -> None:
    vault = InMemoryCredentialVault()
    adapter = _ReplacingDeviceAdapter()
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=vault,
        adapters={"weixin": adapter},  # type: ignore[dict-item]
    )
    flow = service.begin_authorization("weixin", request_id="begin")
    barrier = threading.Barrier(3)
    failures: list[BaseException] = []

    def poll(request_id: str) -> None:
        barrier.wait()
        try:
            service.poll_authorization("weixin", flow["flow_id"], request_id=request_id)
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=poll, args=(f"poll-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert failures == []
    assert service._read("weixin").secrets["weixin_token"] == "old-token"
    pointer = vault.get(service._reference("weixin"))
    assert vault.get(pointer["generation_reference"])["channel_id"] == "weixin"
    with pytest.raises(KeyError):
        vault.get(service._transition_reference("weixin"))


def test_weixin_adapter_prepares_new_client_before_atomic_session_swap(
    tmp_path: Path,
) -> None:
    clients = []

    class Client:
        def __init__(self, token: str) -> None:
            self.token = token
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def factory(_base_url: str, token: str):
        client = Client(token)
        clients.append(client)
        return client

    adapter = WeixinILinkAdapter(tmp_path / "weixin.db", client_factory=factory)
    adapter.bind_runtime(
        ChannelCredentialOwner("account-a", "organization-a"),
        _Dispatcher(),  # type: ignore[arg-type]
    )
    adapter._run = lambda _client, stop, _initial=None: stop.wait(2)  # type: ignore[method-assign]
    cursors: list[str] = []

    def updates(client, cursor):
        cursors.append(cursor)
        if client.token == "bad-token":
            raise RuntimeError("prepare failed")
        return {"msgs": []}

    adapter._updates = updates  # type: ignore[method-assign]

    old_config = dict(_CONFIG)
    old_config["weixin_token"] = "old-token"
    assert adapter.start(old_config).health is ConnectorHealth.CONNECTED
    old_client = adapter._client

    new_config = dict(_CONFIG)
    new_config["weixin_token"] = "new-token"
    assert adapter.prepare_replace(new_config).health is ConnectorHealth.CONNECTED
    assert cursors[-1] == ""
    assert adapter._client is old_client
    assert old_client is not None and old_client.closed is False
    assert adapter.commit_replace(1).health is ConnectorHealth.CONNECTED
    assert old_client is not None and old_client.closed is True
    new_client = adapter._client
    assert new_client is not None and new_client.token == "new-token"

    bad_config = dict(_CONFIG)
    bad_config["weixin_token"] = "bad-token"
    assert adapter.prepare_replace(bad_config).health is ConnectorHealth.ERROR
    assert adapter._client is new_client
    assert new_client.closed is False
    assert clients[-1].closed is True
    assert adapter.stop(1) is True

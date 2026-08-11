from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ElementTree

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.connectors.channel_runtime import (
    ChannelRuntimeDispatcher,
    ChannelTurnReceipt,
)
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.wechat_callback import (
    ManagedWechatCallbackAdapter,
    ManagedWechatCallbackClient,
    _ManagedWechatError,
)
from ecorex.control_plane.audit import CloudAuditRepository
from ecorex.control_plane.audit_schema import CloudAuditSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.control_plane.wechat_callback_gateway import (
    BindingRequest,
    InboxMutationRequest,
    OutboundRequest,
    PullRequest,
    WechatCallbackError,
    WechatCallbackGateway,
    _WechatCrypto,
    _sha,
)
from ecorex.control_plane.wechat_callback_schema import WechatCallbackSchemaManager
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus


_AES_KEY = base64.b64encode(b"k" * 32).decode().rstrip("=")
_OWNER = ControlPrincipal(
    subject="user-a",
    client_id="desktop-a",
    account_id="account-a",
    organization_id="organization-a",
)


class _Provider:
    def __init__(self) -> None:
        self.sent = []

    async def aclose(self) -> None:
        return None

    async def send(self, channel_id, credentials, reply_context, text):
        self.sent.append((channel_id, dict(reply_context), text))
        return "provider-reply-1"


def _gateway(tmp_path) -> WechatCallbackGateway:
    database = tmp_path / "control.db"
    CloudAuditSchemaManager(database).migrate()
    WechatCallbackSchemaManager(database).migrate()
    return WechatCallbackGateway(
        database,
        encryption_key=b"w" * 32,
        audit_repository=CloudAuditRepository(
            database, encryption_key=b"a" * 32, integrity_key=b"i" * 32
        ),
        public_callback_base_url=(
            "https://dl.ecoremedia.net/api/v1/channels/wechat/callback"
        ),
        provider=_Provider(),
        passive_wait_seconds=0.2,
    )


def _binding(channel_id: str) -> BindingRequest:
    return BindingRequest(
        channel_id=channel_id,
        app_id="wx-app-a",
        app_secret="provider-secret",
        token="callback-token",
        encoding_aes_key=_AES_KEY,
    )


def test_wechat_callback_is_verified_durable_tenant_scoped_and_service_real(
    tmp_path,
) -> None:
    gateway = _gateway(tmp_path)
    binding = gateway.bind(_OWNER, _binding("wechatmp_service"))
    assert set(binding) == {
        "binding_id",
        "channel_id",
        "callback_url",
        "status",
        "external_display_name",
        "setup_requirement",
    }
    assert binding["external_display_name"] == "e-Mate"
    assert "provider-secret" not in repr(binding)

    plaintext = (
        b"<xml><ToUserName>wx-app-a</ToUserName>"
        b"<FromUserName>user-open-id</FromUserName>"
        b"<CreateTime>1786204800</CreateTime><MsgType>text</MsgType>"
        b"<Content>hello agent</Content><MsgId>provider-message-1</MsgId></xml>"
    )
    crypto = _WechatCrypto(
        token="callback-token", aes_key=_AES_KEY, receive_id="wx-app-a"
    )
    outer = crypto.encrypt(plaintext, timestamp="1786204800", nonce="nonce-a")
    fields = {child.tag: child.text or "" for child in ElementTree.fromstring(outer)}
    assert gateway.ingest_callback(
        binding["binding_id"],
        outer,
        signature=fields["MsgSignature"],
        timestamp="1786204800",
        nonce="nonce-a",
    ) is None
    assert gateway.ingest_callback(
        binding["binding_id"],
        outer,
        signature=fields["MsgSignature"],
        timestamp="1786204800",
        nonce="nonce-a",
    ) is None
    with sqlite3.connect(gateway.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM wechat_callback_inbox"
        ).fetchone() == (1,)

    other = ControlPrincipal("user-b", "desktop-b", "account-b", "organization-b")
    with pytest.raises(WechatCallbackError, match="binding_unavailable"):
        gateway.pull(
            other,
            PullRequest(
                binding_id=binding["binding_id"], lease_id="wxlease_1234567890abcdef"
            ),
        )
    pulled = gateway.pull(
        _OWNER,
        PullRequest(
            binding_id=binding["binding_id"], lease_id="wxlease_1234567890abcdef"
        ),
    )
    assert pulled["items"][0]["text"] == "hello agent"
    event_id = pulled["items"][0]["event_id"]
    gateway.acknowledge(
        _OWNER,
        InboxMutationRequest(
            binding_id=binding["binding_id"],
            event_id=event_id,
            lease_id=pulled["lease_id"],
        ),
    )
    result = asyncio.run(
        gateway.outbound(
            _OWNER,
            OutboundRequest(
                binding_id=binding["binding_id"],
                event_id=event_id,
                text="real final answer",
            ),
            "reply-1",
        )
    )
    assert result["state"] == "sent"
    assert gateway.provider.sent == [
        ("wechatmp_service", {"to_user": "user-open-id"}, "real final answer")
    ]
    with sqlite3.connect(gateway.database_path) as connection:
        inbox = connection.execute(
            "SELECT state,payload_envelope_json,reply_envelope_json "
            "FROM wechat_callback_inbox"
        ).fetchone()
        delivery = connection.execute(
            "SELECT state,payload_envelope_json FROM wechat_callback_deliveries"
        ).fetchone()
    assert inbox == ("acknowledged", None, None)
    assert delivery == ("sent", None)
    with pytest.raises(WechatCallbackError, match="callback_signature_invalid"):
        gateway.ingest_callback(
            binding["binding_id"],
            outer,
            signature="0" * 40,
            timestamp="1786204800",
            nonce="nonce-a",
        )


def test_passive_mp_retries_are_bounded_and_late_reply_is_retrieved_once(
    tmp_path,
) -> None:
    now = [datetime(2026, 8, 10, tzinfo=UTC)]
    database = tmp_path / "control.db"
    CloudAuditSchemaManager(database).migrate()
    WechatCallbackSchemaManager(database).migrate()
    gateway = WechatCallbackGateway(
        database,
        encryption_key=b"w" * 32,
        audit_repository=CloudAuditRepository(
            database, encryption_key=b"a" * 32, integrity_key=b"i" * 32
        ),
        public_callback_base_url=(
            "https://dl.ecoremedia.net/api/v1/channels/wechat/callback"
        ),
        provider=_Provider(),
        clock=lambda: now[0],
        passive_wait_seconds=4.5,
    )
    binding = gateway.bind(_OWNER, _binding("wechatmp"))
    crypto = _WechatCrypto(
        token="callback-token", aes_key=_AES_KEY, receive_id="wx-app-a"
    )

    def callback(message_id: str, content: str) -> tuple[bytes, dict[str, str]]:
        plaintext = (
            "<xml><ToUserName>wx-app-a</ToUserName>"
            "<FromUserName>user-open-id</FromUserName>"
            f"<CreateTime>{int(now[0].timestamp())}</CreateTime>"
            "<MsgType>text</MsgType>"
            f"<Content>{content}</Content><MsgId>{message_id}</MsgId></xml>"
        ).encode()
        outer = crypto.encrypt(
            plaintext, timestamp=str(int(now[0].timestamp())), nonce="nonce-a"
        )
        fields = {
            child.tag: child.text or "" for child in ElementTree.fromstring(outer)
        }
        return outer, fields

    first, fields = callback("provider-message-1", "slow\nquestion")
    event = gateway.ingest_callback(
        binding["binding_id"], first,
        signature=fields["MsgSignature"],
        timestamp=fields["TimeStamp"], nonce=fields["Nonce"],
    )
    assert event is not None
    pulled = gateway.pull(
        _OWNER,
        PullRequest(
            binding_id=binding["binding_id"], lease_id="wxlease_1234567890abcdef"
        ),
    )
    gateway.acknowledge(
        _OWNER,
        InboxMutationRequest(
            binding_id=binding["binding_id"], event_id=event["event_id"],
            lease_id=pulled["lease_id"],
        ),
    )

    deadlines = []
    for _attempt in range(3):
        with sqlite3.connect(database) as connection:
            deadlines.append(
                connection.execute(
                    "SELECT passive_deadline_at FROM wechat_callback_inbox "
                    "WHERE event_id=?", (event["event_id"],)
                ).fetchone()[0]
            )
        now[0] += timedelta(seconds=5)
        gateway.ingest_callback(
            binding["binding_id"], first,
            signature=fields["MsgSignature"],
            timestamp=fields["TimeStamp"], nonce=fields["Nonce"],
        )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT passive_attempts,passive_deadline_at,passive_hard_deadline_at "
            "FROM wechat_callback_inbox WHERE event_id=?", (event["event_id"],)
        ).fetchone()
    assert row[0] == 3
    assert row[1] == deadlines[-1]
    assert datetime.fromisoformat(row[2]) <= datetime(2026, 8, 10, tzinfo=UTC) + timedelta(seconds=15)
    app = FastAPI()
    app.include_router(gateway.create_router(principal_dependency=lambda: _OWNER))
    callback_url = f"/api/v1/channels/wechat/callback/{binding['binding_id']}"
    params = {
        "msg_signature": fields["MsgSignature"],
        "timestamp": fields["TimeStamp"],
        "nonce": fields["Nonce"],
    }
    with TestClient(app) as browser:
        response = browser.post(callback_url, params=params, content=first)
        assert response.status_code == 200
        encrypted_fields = {
            child.tag: child.text or ""
            for child in ElementTree.fromstring(response.content)
        }
        reply = crypto.decrypt(
            encrypted_fields["Encrypt"],
            signature=encrypted_fields["MsgSignature"],
            timestamp=encrypted_fields["TimeStamp"],
            nonce=encrypted_fields["Nonce"],
        )
        assert ElementTree.fromstring(reply).findtext("Content") == (
            "回复任意文字以获取稍后完成的回复"
        )
        assert browser.post(callback_url, params=params, content=first).text == "success"

    late_answer = "第一行\n\t" + "答" * 1000 + "\n结束"
    result = asyncio.run(
        gateway.outbound(
            _OWNER,
            OutboundRequest(
                binding_id=binding["binding_id"], event_id=event["event_id"],
                text=late_answer,
            ),
            "reply-late-1",
        )
    )
    assert result == {"state": "ready", "error_code": None}
    assert asyncio.run(
        gateway.outbound(
            _OWNER,
            OutboundRequest(
                binding_id=binding["binding_id"], event_id=event["event_id"],
                text=late_answer,
            ),
            "reply-late-1",
        )
    ) == result
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO wechat_callback_inbox("
            "event_id,binding_id,provider_message_sha256,payload_envelope_json,"
            "reply_envelope_json,state,created_at,acknowledged_at,"
            "conversation_sha256,passive_attempts,passive_hint_sent) "
            "VALUES('wxevt_historical',?,?,NULL,NULL,'acknowledged',?,?,?,0,0)",
            (
                binding["binding_id"],
                _sha("provider-message-historical"),
                now[0].isoformat(),
                now[0].isoformat(),
                _sha("user-open-id"),
            ),
        )
        connection.commit()
    other = ControlPrincipal("user-b", "desktop-b", "account-b", "organization-b")
    other_binding = gateway.bind(
        other,
        BindingRequest(
            channel_id="wechatmp",
            app_id="wx-app-b",
            app_secret="provider-secret",
            token="callback-token",
            encoding_aes_key=_AES_KEY,
        ),
    )
    other_credentials = gateway._public_credentials(other_binding["binding_id"])
    try:
        assert gateway._claim_deferred_reply(
            other_binding["binding_id"],
            other_credentials,
            provider_message_id="other-message",
            conversation_sha256=_sha("user-open-id"),
        ) is None
    finally:
        other_credentials.clear()
    with TestClient(app) as browser:
        assert browser.post(callback_url, params=params, content=first).text == "success"
        historical, historical_fields = callback(
            "provider-message-historical", "historical replay"
        )
        assert browser.post(
            callback_url,
            params={
                "msg_signature": historical_fields["MsgSignature"],
                "timestamp": historical_fields["TimeStamp"],
                "nonce": historical_fields["Nonce"],
            },
            content=historical,
        ).text == "success"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state FROM wechat_callback_deliveries WHERE binding_id=? "
            "AND idempotency_key='reply-late-1'",
            (binding["binding_id"],),
        ).fetchone() == ("ready",)

    second, second_fields = callback("provider-message-2", "取回回复")
    with TestClient(app) as browser:
        response = browser.post(
            callback_url,
            params={
                "msg_signature": second_fields["MsgSignature"],
                "timestamp": second_fields["TimeStamp"],
                "nonce": second_fields["Nonce"],
            },
            content=second,
        )
    encrypted_fields = {
        child.tag: child.text or ""
        for child in ElementTree.fromstring(response.content)
    }
    reply = crypto.decrypt(
        encrypted_fields["Encrypt"],
        signature=encrypted_fields["MsgSignature"],
        timestamp=encrypted_fields["TimeStamp"],
        nonce=encrypted_fields["Nonce"],
    )
    first_part = ElementTree.fromstring(reply).findtext("Content") or ""
    assert len(first_part.encode("utf-8")) <= 2048
    assert first_part.endswith("\n【未完待续，回复任意文字以继续】")
    third, third_fields = callback("provider-message-3", "继续")
    with TestClient(app) as browser:
        response = browser.post(
            callback_url,
            params={
                "msg_signature": third_fields["MsgSignature"],
                "timestamp": third_fields["TimeStamp"],
                "nonce": third_fields["Nonce"],
            },
            content=third,
        )
    encrypted_fields = {
        child.tag: child.text or ""
        for child in ElementTree.fromstring(response.content)
    }
    reply = crypto.decrypt(
        encrypted_fields["Encrypt"],
        signature=encrypted_fields["MsgSignature"],
        timestamp=encrypted_fields["TimeStamp"],
        nonce=encrypted_fields["Nonce"],
    )
    second_part = ElementTree.fromstring(reply).findtext("Content") or ""
    assert first_part.removesuffix("\n【未完待续，回复任意文字以继续】") + second_part == late_answer
    with sqlite3.connect(database) as connection:
        delivery = connection.execute(
            "SELECT state,payload_envelope_json FROM wechat_callback_deliveries "
            "WHERE binding_id=? AND idempotency_key='reply-late-1'",
            (binding["binding_id"],),
        ).fetchone()
        inbox = connection.execute(
            "SELECT state,payload_envelope_json,reply_envelope_json "
            "FROM wechat_callback_inbox WHERE event_id=?", (event["event_id"],)
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM wechat_callback_inbox WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone() == (4,)
    assert delivery == ("sent", None)
    assert inbox == ("acknowledged", None, None)


def test_passive_http_forces_two_provider_retries_then_returns_hint(tmp_path) -> None:
    now = [datetime(2026, 8, 10, tzinfo=UTC)]
    database = tmp_path / "control.db"
    CloudAuditSchemaManager(database).migrate()
    WechatCallbackSchemaManager(database).migrate()
    gateway = WechatCallbackGateway(
        database,
        encryption_key=b"w" * 32,
        audit_repository=CloudAuditRepository(
            database, encryption_key=b"a" * 32, integrity_key=b"i" * 32
        ),
        public_callback_base_url=(
            "https://dl.ecoremedia.net/api/v1/channels/wechat/callback"
        ),
        provider=_Provider(),
        clock=lambda: now[0],
        passive_wait_seconds=0.1,
        provider_callback_timeout_seconds=0.2,
    )
    binding = gateway.bind(_OWNER, _binding("wechatmp"))
    crypto = _WechatCrypto(
        token="callback-token", aes_key=_AES_KEY, receive_id="wx-app-a"
    )
    plaintext = (
        b"<xml><ToUserName>wx-app-a</ToUserName>"
        b"<FromUserName>user-open-id</FromUserName>"
        b"<CreateTime>1786204800</CreateTime><MsgType>text</MsgType>"
        b"<Content>slow question</Content><MsgId>provider-message-1</MsgId></xml>"
    )
    body = crypto.encrypt(plaintext, timestamp="1786204800", nonce="nonce-a")
    fields = {
        child.tag: child.text or "" for child in ElementTree.fromstring(body)
    }
    params = {
        "msg_signature": fields["MsgSignature"],
        "timestamp": fields["TimeStamp"],
        "nonce": fields["Nonce"],
    }
    app = FastAPI()
    app.include_router(gateway.create_router(principal_dependency=lambda: _OWNER))
    url = f"/api/v1/channels/wechat/callback/{binding['binding_id']}"

    with TestClient(app) as browser:
        for expected_attempts in (1, 2):
            started = time.monotonic()
            response = browser.post(url, params=params, content=body)
            assert time.monotonic() - started >= 0.27
            assert response.text == "success"
            with sqlite3.connect(database) as connection:
                assert connection.execute(
                    "SELECT passive_attempts FROM wechat_callback_inbox "
                    "WHERE binding_id=?",
                    (binding["binding_id"],),
                ).fetchone() == (expected_attempts,)
            now[0] += timedelta(seconds=0.2)

        started = time.monotonic()
        response = browser.post(url, params=params, content=body)
        assert time.monotonic() - started < 0.27
    encrypted = {
        child.tag: child.text or ""
        for child in ElementTree.fromstring(response.content)
    }
    reply = crypto.decrypt(
        encrypted["Encrypt"],
        signature=encrypted["MsgSignature"],
        timestamp=encrypted["TimeStamp"],
        nonce=encrypted["Nonce"],
    )
    assert ElementTree.fromstring(reply).findtext("Content") == (
        "回复任意文字以获取稍后完成的回复"
    )


def test_two_new_messages_atomically_claim_one_deferred_reply(tmp_path) -> None:
    gateway = _gateway(tmp_path)
    binding = gateway.bind(_OWNER, _binding("wechatmp"))
    event_id = "wxevt_original"
    key = "reply-concurrent"
    answer = "答" * 1800
    envelope = gateway.cipher.encrypt(
        answer,
        associated_data=f"wechat-delivery:{binding['binding_id']}:{key}",
    )
    with sqlite3.connect(gateway.database_path) as connection:
        connection.execute(
            "INSERT INTO wechat_callback_inbox("
            "event_id,binding_id,provider_message_sha256,payload_envelope_json,"
            "reply_envelope_json,state,created_at,acknowledged_at,"
            "conversation_sha256,passive_attempts,passive_hint_sent,"
            "passive_hard_deadline_at) VALUES(?,?,?,NULL,NULL,'acknowledged',"
            "?,?,?,1,0,'2099-01-01T00:00:00+00:00')",
            (
                event_id,
                binding["binding_id"],
                _sha("original-message"),
                "2026-08-10T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00",
                _sha("user-open-id"),
            ),
        )
        connection.execute(
            "INSERT INTO wechat_callback_deliveries("
            "binding_id,idempotency_key,event_id,request_sha256,"
            "payload_envelope_json,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'ready',?,?)",
            (
                binding["binding_id"],
                key,
                event_id,
                _sha("request"),
                envelope,
                "2026-08-10T00:00:00+00:00",
                "2026-08-10T00:00:00+00:00",
            ),
        )
        connection.commit()

    first_part = gateway._take_passive_reply(
        binding["binding_id"], event_id, allow_hint=False
    )
    assert first_part is not None and len(first_part.encode("utf-8")) <= 2048
    middle_credentials = gateway._public_credentials(binding["binding_id"])
    try:
        middle_claim = gateway._claim_deferred_reply(
            binding["binding_id"],
            middle_credentials,
            provider_message_id="new-message-middle",
            conversation_sha256=_sha("user-open-id"),
        )
    finally:
        middle_credentials.clear()
    assert middle_claim is not None and middle_claim[1] is not None
    middle_part = middle_claim[1]
    with sqlite3.connect(gateway.database_path) as connection:
        remainder_before_replay = connection.execute(
            "SELECT payload_envelope_json FROM wechat_callback_deliveries "
            "WHERE binding_id=? AND idempotency_key=?",
            (binding["binding_id"], key),
        ).fetchone()[0]
    original_credentials = gateway._public_credentials(binding["binding_id"])
    try:
        assert gateway._claim_deferred_reply(
            binding["binding_id"],
            original_credentials,
            provider_message_id="original-message",
            conversation_sha256=_sha("user-open-id"),
        ) == ("", None)
    finally:
        original_credentials.clear()
    with sqlite3.connect(gateway.database_path) as connection:
        assert connection.execute(
            "SELECT payload_envelope_json FROM wechat_callback_deliveries "
            "WHERE binding_id=? AND idempotency_key=?",
            (binding["binding_id"], key),
        ).fetchone() == (remainder_before_replay,)

    barrier = threading.Barrier(2)

    def claim(message_id: str):
        credentials = gateway._public_credentials(binding["binding_id"])
        try:
            barrier.wait()
            return gateway._claim_deferred_reply(
                binding["binding_id"],
                credentials,
                provider_message_id=message_id,
                conversation_sha256=_sha("user-open-id"),
            )
        finally:
            credentials.clear()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("new-message-1", "new-message-2")))
    assert sum(result is not None for result in results) == 1
    claimed = next(result for result in results if result is not None)
    assert claimed[0] == event_id
    assert (
        first_part.removesuffix("\n【未完待续，回复任意文字以继续】")
        + middle_part.removesuffix("\n【未完待续，回复任意文字以继续】")
        + str(claimed[1])
        == answer
    )
    with sqlite3.connect(gateway.database_path) as connection:
        assert connection.execute(
            "SELECT state,payload_envelope_json FROM wechat_callback_deliveries "
            "WHERE binding_id=? AND idempotency_key=?",
            (binding["binding_id"], key),
        ).fetchone() == ("sent", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM wechat_callback_inbox WHERE binding_id=?",
            (binding["binding_id"],),
        ).fetchone() == (3,)


class _Client:
    def __init__(self, channel_id="wechatmp_service") -> None:
        self.channel_id = channel_id
        self.session = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                account_id="account-a",
                organization_id="organization-a",
                generation=1,
            )
        )
        self.acks = []
        self.sent = []

    def pull(self, binding_id, lease_id):
        return {
            "items": [
                {
                    "event_id": "wxevt_1",
                    "channel_id": self.channel_id,
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                    "text": "hello",
                    "created_at": "2026-08-10T00:00:00+00:00",
                }
            ]
            if not self.acks
            else []
        }

    def ack(self, *values):
        self.acks.append(values)

    def send(self, *values):
        self.sent.append(values)

    def abandon(self, *values):
        raise AssertionError(values)


class _Dispatcher:
    def __init__(self) -> None:
        self.runtime = ChannelRuntimeDispatcher(
            owner=ChannelCredentialOwner("account-a", "organization-a"),
            composition=SimpleNamespace(permission_account_id="account-a"),
            kernel=SimpleNamespace(
                projection=lambda _thread_id: SimpleNamespace(
                    turns=[SimpleNamespace(turn_id="turn-1", status=TurnStatus.COMPLETED)],
                    items=[
                        SimpleNamespace(
                            turn_id="turn-1",
                            item_id="item-1",
                            kind=ItemKind.MESSAGE,
                            status=ItemStatus.COMPLETED,
                            content={"role": "assistant", "text": "answer"},
                        )
                    ],
                )
            ),
            worker=SimpleNamespace(),
        )

    def dispatch(self, message):
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-1",
            turn_id="turn-1",
            client_message_id="client-1",
            conversation_sha256=self.runtime._digest(
                "conversation", message.channel_id, message.conversation_id
            ),
        )

    def deliver(self, receipt, *, conversation_id, transport):
        return self.runtime.deliver(
            receipt,
            conversation_id=conversation_id,
            transport=transport,
        )


@pytest.mark.parametrize("channel_id", ["wechatmp", "wechatmp_service"])
def test_managed_wechat_adapter_uses_only_bound_owner_and_dispatcher(
    tmp_path, channel_id
) -> None:
    client = _Client(channel_id)
    adapter = ManagedWechatCallbackAdapter(
        channel_id, tmp_path / "product.db", client=client
    )
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter.bind_runtime(owner, _Dispatcher())
    adapter._binding_id = "wxbind_" + "a" * 43
    adapter._cycle()

    assert len(client.acks) >= 1
    assert client.sent[0][2] == "answer"
    assert client.sent[0][3].startswith("channel-delivery-")
    with sqlite3.connect(tmp_path / "product.db") as connection:
        row = connection.execute(
            "SELECT state,conversation_id,message_id,text FROM managed_wechat_events"
        ).fetchone()
    assert row == ("completed", "", "", "")

    mismatched = ManagedWechatCallbackAdapter(
        channel_id, tmp_path / "other.db", client=client
    )
    with pytest.raises(RuntimeError, match="does not match"):
        mismatched.bind_runtime(
            ChannelCredentialOwner("account-b", "organization-b"), _Dispatcher()
        )


class _ManagedSession:
    def snapshot(self):
        return SimpleNamespace(
            account_id="account-a",
            organization_id="organization-a",
            generation=1,
        )

    def bearer_token(self) -> str:
        return "m" * 32


def _managed_client(handler) -> ManagedWechatCallbackClient:
    return ManagedWechatCallbackClient(
        connector_endpoint="https://api.example.test/api/v1/connectors",
        allowed_hosts=frozenset({"api.example.test"}),
        session=_ManagedSession(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("channel_id", ["wechatmp", "wechatmp_service"])
def test_managed_mp_bind_uses_catalog_app_secret(channel_id: str) -> None:
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "binding_id": "wxbind_a",
                "channel_id": channel_id,
                "callback_url": "https://api.example.test/callback/wxbind_a",
                "status": "enabled",
                "external_display_name": "e-Mate",
                "setup_requirement": "set e-Mate",
            },
        )

    _managed_client(handler).bind(
        channel_id,
        {
            "wechatmp_app_id": "wx-app-a",
            "wechatmp_app_secret": "provider-secret",
            "wechatmp_token": "callback-token",
            "wechatmp_aes_key": _AES_KEY,
        },
    )
    assert requests == [
        {
            "channel_id": channel_id,
            "app_id": "wx-app-a",
            "agent_id": None,
            "app_secret": "provider-secret",
            "token": "callback-token",
            "encoding_aes_key": _AES_KEY,
        }
    ]


@pytest.mark.parametrize("status_code", [200, 204, 299])
def test_managed_wechat_ack_accepts_any_2xx_without_parsing(status_code: int) -> None:
    client = _managed_client(
        lambda _request: httpx.Response(status_code, content=b"not-json")
    )
    client.ack("wxbind_a", "wxevt_a", "wxlease_a")
    client.abandon("wxbind_a", "wxevt_a", "wxlease_a")


@pytest.mark.parametrize("operation", ["bind", "pull"])
def test_managed_wechat_malformed_success_is_retryable(operation: str) -> None:
    client = _managed_client(
        lambda _request: httpx.Response(200, json={"unexpected": True})
    )
    with pytest.raises(_ManagedWechatError) as caught:
        if operation == "bind":
            client.bind("wechatmp_service", {})
        else:
            client.pull("wxbind_a", "wxlease_a")
    assert caught.value.retryable is True
    assert caught.value.uncertain is False


def test_managed_wechat_pull_rejects_more_than_twenty_exact_items() -> None:
    item = {
        "event_id": "wxevt_a",
        "channel_id": "wechatmp_service",
        "conversation_id": "conversation-a",
        "message_id": "message-a",
        "text": "hello",
        "created_at": "2026-08-10T00:00:00+00:00",
    }
    client = _managed_client(
        lambda _request: httpx.Response(
            200,
            json={
                "binding_id": "wxbind_a",
                "lease_id": "wxlease_a",
                "items": [item] * 21,
            },
        )
    )
    with pytest.raises(_ManagedWechatError) as caught:
        client.pull("wxbind_a", "wxlease_a")
    assert caught.value.retryable is True


def test_managed_wechat_response_body_is_hard_capped() -> None:
    client = _managed_client(
        lambda _request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
    )
    with pytest.raises(_ManagedWechatError, match="response_too_large") as caught:
        client.pull("wxbind_a", "wxlease_a")
    assert caught.value.retryable is True


def test_managed_wechat_response_json_depth_is_bounded() -> None:
    nested: object = "leaf"
    for _ in range(30):
        nested = [nested]
    client = _managed_client(
        lambda _request: httpx.Response(200, json={"value": nested})
    )
    with pytest.raises(_ManagedWechatError, match="response_invalid") as caught:
        client.pull("wxbind_a", "wxlease_a")
    assert caught.value.retryable is True

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import sqlite3
from types import SimpleNamespace
import xml.etree.ElementTree as ElementTree

import pytest

from ecorex.connectors.channel_runtime import ChannelTurnReceipt
from ecorex.connectors.channel_self_service import ChannelCredentialOwner
from ecorex.connectors.wechat_callback import ManagedWechatCallbackAdapter
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
)
from ecorex.control_plane.wechat_callback_schema import WechatCallbackSchemaManager


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
    with pytest.raises(WechatCallbackError, match="passive_runtime_unavailable"):
        gateway.bind(_OWNER, _binding("wechatmp"))
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


class _Client:
    def __init__(self) -> None:
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
                    "channel_id": "wechatmp_service",
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                    "text": "hello",
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
    def dispatch(self, message):
        return ChannelTurnReceipt(
            channel_id=message.channel_id,
            thread_id="thread-1",
            turn_id="turn-1",
            client_message_id="client-1",
            conversation_sha256="conversation-hash",
        )

    def deliver(self, receipt, *, conversation_id, transport):
        transport.send_text(
            conversation_id=conversation_id,
            text="answer",
            idempotency_key="delivery-1",
        )
        return True


def test_managed_wechat_adapter_uses_only_bound_owner_and_dispatcher(tmp_path) -> None:
    client = _Client()
    adapter = ManagedWechatCallbackAdapter(
        "wechatmp_service", tmp_path / "product.db", client=client
    )
    owner = ChannelCredentialOwner("account-a", "organization-a")
    adapter.bind_runtime(owner, _Dispatcher())
    adapter._binding_id = "wxbind_" + "a" * 43
    adapter._cycle()

    assert len(client.acks) >= 1
    assert client.sent[0][2:] == ("answer", "delivery-1")
    with sqlite3.connect(tmp_path / "product.db") as connection:
        row = connection.execute(
            "SELECT state,conversation_id,message_id,text FROM managed_wechat_events"
        ).fetchone()
    assert row == ("completed", "", "", "")

    mismatched = ManagedWechatCallbackAdapter(
        "wechatmp_service", tmp_path / "other.db", client=client
    )
    with pytest.raises(RuntimeError, match="does not match"):
        mismatched.bind_runtime(
            ChannelCredentialOwner("account-b", "organization-b"), _Dispatcher()
        )

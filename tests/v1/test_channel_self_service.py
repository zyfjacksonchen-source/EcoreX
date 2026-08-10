from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from channel.channel_catalog import CHANNEL_CATALOG as COW_CHANNEL_CATALOG
from ecorex.connectors.channel_catalog import CHANNEL_CATALOG
from ecorex.connectors.channel_self_service import (
    ChannelAuditEvent,
    ChannelCredentialOwner,
    ChannelSelfService,
    ChannelSelfServiceError,
    channel_audit_outbox_event,
)
from ecorex.connectors.models import ConnectorHealth, ConnectorHealthResult
from ecorex.connectors.vault import (
    InMemoryCredentialVault,
    SerializedCredentialVault,
)


class _Adapter:
    def __init__(self, *, stop_succeeds: bool = True) -> None:
        self.calls: list[tuple[str, object]] = []
        self.stop_succeeds = stop_succeeds

    def test(self, config: Mapping[str, object]) -> ConnectorHealthResult:
        self.calls.append(("test", dict(config)))
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def start(self, config: Mapping[str, object]) -> ConnectorHealthResult:
        self.calls.append(("start", dict(config)))
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def health(self) -> ConnectorHealthResult:
        self.calls.append(("health", None))
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    def stop(self, timeout_seconds: float) -> bool:
        self.calls.append(("stop", timeout_seconds))
        return self.stop_succeeds


class _MissingBackend:
    def put(self, reference: str, payload: bytes) -> None:
        raise AssertionError((reference, payload))

    def get(self, reference: str) -> bytes:
        raise KeyError(reference)

    def delete(self, reference: str) -> None:
        del reference


def _service(
    vault: InMemoryCredentialVault,
    *,
    owner: ChannelCredentialOwner | None = None,
    adapter: _Adapter | None = None,
    audit: list[dict[str, object]] | None = None,
) -> ChannelSelfService:
    return ChannelSelfService(
        owner=owner or ChannelCredentialOwner("account-a", "organization-a"),
        vault=vault,
        adapters={"telegram": adapter} if adapter else {},
        audit_sink=(lambda event: audit.append(event.to_dict())) if audit is not None else None,
    )


def test_product_channel_catalog_matches_cow_215_contract() -> None:
    assert tuple(CHANNEL_CATALOG) == tuple(COW_CHANNEL_CATALOG)
    for channel_id, definition in CHANNEL_CATALOG.items():
        legacy = COW_CHANNEL_CATALOG[channel_id]
        assert definition["aliases"] == legacy["aliases"]
        assert definition["label"]["zh"] == legacy["label"]["zh"]
        assert definition["description"] == legacy["description"]
        assert definition["icon"] == legacy["icon"]
        assert definition["fields"] == legacy["fields"]


def test_catalog_is_typed_secret_free_and_fail_closed_without_pack() -> None:
    service = _service(InMemoryCredentialVault())

    catalog = service.catalog()
    telegram = next(item for item in catalog["items"] if item["channel_id"] == "telegram")
    feishu = next(item for item in catalog["items"] if item["channel_id"] == "feishu")
    wechatmp = next(item for item in catalog["items"] if item["channel_id"] == "wechatmp")

    assert catalog["contract_version"] == "channel-self-service-v1"
    assert telegram["auth_kind"] == "api_token"
    assert telegram["adapter_available"] is False
    assert telegram["unavailable_reason"] == "adapter_not_packaged"
    assert telegram["actions"]["test"] is False
    assert wechatmp["adapter_available"] is False
    assert wechatmp["unavailable_reason"] == "passive_runtime_unavailable"
    assert telegram["fields"] == [
        {
            "key": "telegram_token",
            "label": "Bot Token",
            "type": "secret",
            "required": True,
            "secret": True,
            "configured": False,
        }
    ]
    assert feishu["auth_kind"] == "app_credentials"
    assert [field["key"] for field in feishu["fields"]] == [
        "feishu_app_id",
        "feishu_app_secret",
    ]
    assert feishu["actions"]["save"] is False
    assert feishu["actions"]["auth_begin"] is False


def test_feishu_message_bot_is_not_misclassified_as_document_oauth() -> None:
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=InMemoryCredentialVault(),
        adapters={"feishu": _Adapter()},
    )

    saved = service.save(
        "feishu",
        display_name="飞书消息 Bot",
        config={"feishu_app_id": "cli_test_value"},
        secrets={"feishu_app_secret": "write-only-secret"},
        request_id="save-feishu-bot",
    )
    feishu = next(
        item for item in service.catalog()["items"] if item["channel_id"] == "feishu"
    )

    assert saved["configured_fields"] == [
        "feishu_app_id",
        "feishu_app_secret",
    ]
    assert feishu["auth_kind"] == "app_credentials"
    assert feishu["actions"]["save"] is True
    assert feishu["actions"]["auth_begin"] is False
    assert "write-only-secret" not in repr(feishu)


def test_secret_is_one_way_tenant_scoped_and_lifecycle_is_real() -> None:
    vault = InMemoryCredentialVault()
    adapter = _Adapter()
    audit: list[dict[str, object]] = []
    service = _service(vault, adapter=adapter, audit=audit)
    secret = "telegram-secret-value"

    saved = service.save(
        "telegram",
        display_name="通知机器人",
        config={},
        secrets={"telegram_token": secret},
        request_id="save-1",
    )
    tested = service.test("telegram", request_id="test-1")
    enabled = service.enable("telegram", request_id="enable-1")
    health = service.health("telegram", request_id="health-1")
    disabled = service.disable("telegram", request_id="disable-1")

    assert saved["configured_fields"] == ["telegram_token"]
    assert saved["missing_fields"] == []
    assert enabled["state"] == "connected"
    assert enabled["health"] == "connected"
    assert health["health"] == "connected"
    assert disabled["state"] == "stopped"
    assert disabled["health"] == "disabled"
    assert [call[0] for call in adapter.calls] == [
        "test",
        "start",
        "health",
        "stop",
    ]
    assert adapter.calls[0][1] == {"telegram_token": secret}
    public = {"saved": saved, "tested": tested, "enabled": enabled, "audit": audit}
    assert secret not in repr(public)
    assert audit[0]["field_names"] == ["telegram_token"]

    other = _service(
        vault,
        owner=ChannelCredentialOwner("account-b", "organization-a"),
        adapter=_Adapter(),
    )
    assert next(
        item for item in other.catalog()["items"] if item["channel_id"] == "telegram"
    )["instance"] is None


def test_missing_pack_rejects_credentials_instead_of_faking_health() -> None:
    service = _service(InMemoryCredentialVault())

    with pytest.raises(ChannelSelfServiceError, match="channel_adapter_unavailable"):
        service.save(
            "slack",
            display_name="Slack",
            config={},
            secrets={
                "slack_bot_token": "xoxb-test",
                "slack_app_token": "xapp-test",
            },
            request_id="save-slack",
        )
    slack = next(
        item for item in service.catalog()["items"] if item["channel_id"] == "slack"
    )
    assert slack["instance"] is None
    assert slack["actions"]["save"] is False


def test_disconnect_refuses_to_drop_vault_while_adapter_will_not_stop() -> None:
    vault = InMemoryCredentialVault()
    adapter = _Adapter(stop_succeeds=False)
    service = _service(vault, adapter=adapter)
    service.save(
        "telegram",
        display_name="Telegram",
        config={},
        secrets={"telegram_token": "still-needed-by-running-adapter"},
        request_id="save-2",
    )

    with pytest.raises(ChannelSelfServiceError, match="channel_stop_timeout"):
        service.disconnect("telegram", request_id="disconnect-2")

    telegram = next(
        item for item in service.catalog()["items"] if item["channel_id"] == "telegram"
    )
    assert telegram["instance"]["state"] == "error"
    assert telegram["instance"]["last_error_code"] == "channel_stop_timeout"


def test_running_channel_must_stop_before_configuration_changes() -> None:
    vault = InMemoryCredentialVault()
    service = _service(vault, adapter=_Adapter())
    service.save(
        "telegram",
        display_name="Telegram",
        config={},
        secrets={"telegram_token": "first-secret"},
        request_id="save-running",
    )
    service.enable("telegram", request_id="enable-running")

    with pytest.raises(ChannelSelfServiceError, match="channel_must_be_disabled"):
        service.save(
            "telegram",
            display_name="Telegram",
            config={},
            secrets={"telegram_token": "replacement-secret"},
            request_id="replace-running",
        )


def test_text_configuration_rejects_structured_values() -> None:
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=InMemoryCredentialVault(),
        adapters={"dingtalk": _Adapter()},
    )

    with pytest.raises(ChannelSelfServiceError, match="channel_config_value_invalid"):
        service.save(
            "dingtalk",
            display_name="DingTalk",
            config={"dingtalk_client_id": {"unexpected": "object"}},
            secrets={"dingtalk_client_secret": "secret"},
            request_id="invalid-text",
        )


def test_serialized_vault_preserves_missing_item_identity() -> None:
    vault = SerializedCredentialVault(_MissingBackend())
    with pytest.raises(KeyError):
        vault.get("ecorex/channel-instances/missing")


def test_enabled_channel_restores_and_audit_bridge_stays_secret_free() -> None:
    vault = InMemoryCredentialVault()
    first_adapter = _Adapter()
    service = _service(vault, adapter=first_adapter)
    service.save(
        "telegram",
        display_name="Telegram",
        config={},
        secrets={"telegram_token": "not-an-audit-field"},
        request_id="save-restore",
    )
    service.enable("telegram", request_id="enable-restore")

    restored_adapter = _Adapter()
    restored = _service(vault, adapter=restored_adapter)
    asyncio.run(restored.start())
    asyncio.run(restored.stop())

    assert [call[0] for call in restored_adapter.calls] == ["start", "stop"]
    event = channel_audit_outbox_event(
        ChannelAuditEvent(
            account_id="account-a",
            organization_id="organization-a",
            channel_id="telegram",
            action="enable",
            outcome="succeeded",
            request_id="enable-restore",
            field_names=("telegram_token",),
            error_code=None,
            created_at=datetime.now(UTC),
        )
    )
    assert event.event_type == "connector.channel.enable"
    assert event.payload["connector_id"] == "telegram"
    assert "not-an-audit-field" not in repr(event)
    assert "telegram_token" not in repr(event)

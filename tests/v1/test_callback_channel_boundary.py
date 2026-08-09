from __future__ import annotations

import pytest

from ecorex.connectors.channel_self_service import (
    ChannelCredentialOwner,
    ChannelSelfService,
    ChannelSelfServiceError,
)


_CALLBACK_CHANNELS = {
    "wechatcom_app": "app_credentials",
    "wechat_kf": "app_credentials",
    "wechatmp": "app_credentials",
    "wechatmp_service": "app_credentials",
}


class _NoWriteVault:
    def get(self, reference: str) -> dict[str, str]:
        raise KeyError(reference)

    def put(self, reference: str, material: dict[str, str]) -> None:
        raise AssertionError((reference, material))

    def delete(self, reference: str) -> None:
        raise AssertionError(reference)


def test_unpackaged_callback_channels_and_weixin_qr_stay_fail_closed() -> None:
    vault = _NoWriteVault()
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=vault,
    )
    catalog = {
        item["channel_id"]: item for item in service.catalog()["items"]
    }

    unavailable = {"weixin": "device_code", **_CALLBACK_CHANNELS}
    for channel_id, auth_kind in unavailable.items():
        item = catalog[channel_id]
        assert item["auth_kind"] == auth_kind
        assert item["adapter_available"] is False
        assert item["unavailable_reason"] == "adapter_not_packaged"
        assert item["instance"] is None
        assert not any(item["actions"].values())

        expected = (
            "channel_device_authorization_required"
            if channel_id == "weixin"
            else "channel_adapter_unavailable"
        )
        with pytest.raises(ChannelSelfServiceError, match=expected):
            service.save(
                channel_id,
                display_name=item["label"],
                config={},
                secrets={},
                request_id=f"blocked-{channel_id}",
            )

        assert next(
            item
            for item in service.catalog()["items"]
            if item["channel_id"] == channel_id
        )["instance"] is None


def test_managed_callback_catalog_keeps_passive_mp_explicitly_unavailable() -> None:
    adapters = {
        channel_id: object()
        for channel_id in ("wechatcom_app", "wechat_kf", "wechatmp_service")
    }
    service = ChannelSelfService(
        owner=ChannelCredentialOwner("account-a", "organization-a"),
        vault=_NoWriteVault(),
        adapters=adapters,
    )
    catalog = {item["channel_id"]: item for item in service.catalog()["items"]}

    assert catalog["wechatmp"]["adapter_available"] is False
    assert catalog["wechatmp"]["unavailable_reason"] == "adapter_not_packaged"
    assert not any(catalog["wechatmp"]["actions"].values())
    for channel_id in adapters:
        assert catalog[channel_id]["adapter_available"] is True

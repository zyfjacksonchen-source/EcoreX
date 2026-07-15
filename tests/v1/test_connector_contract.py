from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthError,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorIdempotencyConflict,
    ConnectorIdempotencyRequired,
    ConnectorPermissionDenied,
    ConnectorRegistry,
    ConnectorService,
    ConnectorTier,
    ConnectorUnavailable,
    InMemoryCredentialVault,
    builtin_connector_definitions,
)


_ALLOWED_RETURN_URIS = frozenset({"http://127.0.0.1:37211/auth/callback"})


class FakeAdapter:
    def __init__(self, connector_id: str, scopes: frozenset[str]) -> None:
        self.connector_id = connector_id
        self.scopes = scopes
        self.invocations: list[tuple[str, Mapping[str, Any], str | None]] = []

    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        assert return_uri == "http://127.0.0.1:37211/auth/callback"
        assert code_challenge
        assert code_challenge_method == "S256"
        return AuthChallenge(
            flow_id=flow_id,
            connector_id=self.connector_id,
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example/connect"
                f"?state={state}&code_challenge={code_challenge}"
                "&code_challenge_method=S256"
            ),
        )

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        assert flow_id.startswith("connflow_")
        assert response["state"] == private_state["state"]
        return AuthGrant(
            account_subject="acct-opaque",
            account_display_name="产品团队",
            granted_scopes=self.scopes,
            credential_material={"access_token": "TOP-SECRET-TOKEN"},
        )

    async def check_health(self, credentials: Mapping[str, str]) -> ConnectorHealthResult:
        assert credentials["access_token"] == "TOP-SECRET-TOKEN"
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        assert credentials["access_token"] == "TOP-SECRET-TOKEN"
        self.invocations.append((action_id, inputs, idempotency_key))
        return {"action_id": action_id, "ok": True, "title": inputs.get("title")}

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        assert credentials["access_token"] == "TOP-SECRET-TOKEN"
        assert idempotency_key.startswith("ecorex-disconnect:")
        return True


def _registry(*, with_feishu: bool = True) -> tuple[ConnectorRegistry, FakeAdapter]:
    registry = ConnectorRegistry()
    adapter = FakeAdapter(
        "feishu",
        frozenset(
            {
                "docx:document:readonly",
                "docx:document",
                "drive:drive:readonly",
                "im:message",
            }
        ),
    )
    for definition in builtin_connector_definitions():
        registry.register(
            definition,
            adapter if with_feishu and definition.connector_id == "feishu" else None,
        )
    return registry, adapter


def _connect(service: ConnectorService, adapter: FakeAdapter):
    challenge = asyncio.run(
        service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri="http://127.0.0.1:37211/auth/callback",
        )
    )
    state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
    instance = asyncio.run(service.complete_connect(challenge.flow_id, {"state": state}))
    return challenge, instance


def test_builtin_catalog_has_stable_document_connectors_and_beta_channels() -> None:
    definitions = builtin_connector_definitions()
    by_id = {definition.connector_id: definition for definition in definitions}

    assert by_id["feishu"].tier is ConnectorTier.STABLE
    assert by_id["tencent-docs"].tier is ConnectorTier.STABLE
    assert {
        connector_id
        for connector_id, definition in by_id.items()
        if definition.tier is ConnectorTier.BETA
    } == {
        "dingtalk",
        "wecom_bot",
        "wechatcom_app",
        "wechat_kf",
        "wechatmp",
        "wechatmp_service",
        "weixin",
        "qq",
        "telegram",
        "slack",
        "discord",
    }
    assert by_id["feishu"].action("documents.write").requires_idempotency_key is True
    assert by_id["tencent-docs"].action("documents.read").requires_idempotency_key is False
    for definition in definitions:
        for action in definition.actions:
            assert action.input_schema["type"] == "object"
            assert action.input_schema["additionalProperties"] is False


def test_catalog_projection_is_generated_by_backend_and_never_contains_secrets() -> None:
    registry, adapter = _registry()
    vault = InMemoryCredentialVault()
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=vault,
    )
    _challenge, instance = _connect(service, adapter)

    encoded = json.dumps(
        [item.to_dict() for item in service.catalog()],
        ensure_ascii=False,
        sort_keys=True,
    )

    assert "TOP-SECRET-TOKEN" not in encoded
    assert instance.credential_ref not in encoded
    assert "acct-opaque" not in encoded
    feishu = next(item for item in service.catalog() if item.definition.connector_id == "feishu")
    assert feishu.adapter_available is True
    assert feishu.instances[0].health is ConnectorHealth.CONNECTED
    assert "documents.write" in feishu.instances[0].available_actions
    tencent = next(
        item for item in service.catalog() if item.definition.connector_id == "tencent-docs"
    )
    assert tencent.adapter_available is False
    assert tencent.unavailable_reason == "adapter_not_installed"


def test_secret_bearing_non_oauth_auth_is_explicitly_unavailable() -> None:
    dingtalk = next(
        definition
        for definition in builtin_connector_definitions()
        if definition.connector_id == "dingtalk"
    )
    registry = ConnectorRegistry()
    registry.register(
        dingtalk,
        FakeAdapter("dingtalk", frozenset({"messages.send"})),
    )
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=InMemoryCredentialVault(),
    )
    item = service.catalog()[0]
    assert item.adapter_available is True
    assert item.unavailable_reason == "secure_credential_submission_unavailable"
    with pytest.raises(ConnectorUnavailable, match="one-time credential submission"):
        asyncio.run(
            service.begin_connect(
                "dingtalk",
                auth_kind=ConnectorAuthKind.APP_CREDENTIALS,
                return_uri="http://127.0.0.1:37211/auth/callback",
                client_request_id="dingtalk-secret-auth",
            )
        )


def test_connector_write_requires_scope_idempotency_and_admin_policy() -> None:
    registry, adapter = _registry()
    audit = []
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=InMemoryCredentialVault(),
        audit_sink=audit.append,
    )
    _challenge, instance = _connect(service, adapter)

    with pytest.raises(ConnectorIdempotencyRequired):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "计划"},
            )
        )
    result = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "计划"},
            idempotency_key="turn_1:connector_1",
        )
    )
    replay = asyncio.run(
        service.invoke(
            instance.instance_id,
            "documents.write",
            {"title": "计划"},
            idempotency_key="turn_1:connector_1",
        )
    )
    assert replay == result
    assert len(adapter.invocations) == 1
    assert len(audit) == 1
    assert audit[0].input_sha256
    assert audit[0].idempotency_key != "turn_1:connector_1"
    assert len(audit[0].idempotency_key) == 64
    with pytest.raises(ConnectorIdempotencyConflict):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "另一个计划"},
                idempotency_key="turn_1:connector_1",
            )
        )
    with pytest.raises(ConnectorPermissionDenied):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "计划"},
                idempotency_key="turn_1:connector_2",
                admin_hard_denies=frozenset({"documents.write"}),
            )
        )


def test_missing_grant_scope_and_missing_adapter_fail_closed() -> None:
    registry = ConnectorRegistry()
    feishu = next(
        definition
        for definition in builtin_connector_definitions()
        if definition.connector_id == "feishu"
    )
    limited = FakeAdapter("feishu", frozenset({"docx:document:readonly"}))
    registry.register(feishu, limited)
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=InMemoryCredentialVault(),
    )
    _challenge, instance = _connect(service, limited)
    with pytest.raises(ConnectorPermissionDenied):
        asyncio.run(
            service.invoke(
                instance.instance_id,
                "documents.write",
                {"title": "计划"},
                idempotency_key="key",
            )
        )

    unavailable_registry, _ = _registry(with_feishu=False)
    unavailable = ConnectorService(
        unavailable_registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=InMemoryCredentialVault(),
    )
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(
            unavailable.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="http://127.0.0.1:37211/auth/callback",
            )
        )


def test_default_vault_rejects_credentials_instead_of_falling_back_to_plaintext() -> None:
    registry, _adapter = _registry()
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
    )
    with pytest.raises(RuntimeError, match="credential vault"):
        asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="http://127.0.0.1:37211/auth/callback",
            )
        )


def test_oauth_return_uri_is_runtime_allowlisted_not_client_controlled() -> None:
    registry, _adapter = _registry()
    service = ConnectorService(
        registry,
        allowed_return_uris=_ALLOWED_RETURN_URIS,
        vault=InMemoryCredentialVault(),
    )
    with pytest.raises(ConnectorAuthError, match="not allowed"):
        asyncio.run(
            service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="https://attacker.example/steal-code",
            )
        )
    with pytest.raises(ValueError, match="loopback"):
        ConnectorService(
            registry,
            allowed_return_uris=frozenset({"https://attacker.example/callback"}),
            vault=InMemoryCredentialVault(),
        )

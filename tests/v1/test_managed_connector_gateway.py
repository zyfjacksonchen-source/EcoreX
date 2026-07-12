from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from ecorex.connectors import (
    ConnectorAuthError,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorUnavailable,
    ManagedConnectorGatewayAdapter,
    ManagedConnectorTransportError,
)


class Session:
    def __init__(self) -> None:
        self.generation = 1
        self.change_on_bearer = False

    def snapshot(self):
        return SimpleNamespace(
            account_id="account-a",
            lease_digest="lease-a",
            generation=self.generation,
        )

    def bearer_token(self) -> str:
        if self.change_on_bearer:
            self.generation += 1
        return "managed-connector-bearer"


def test_gateway_session_reads_do_not_block_network_event_loop() -> None:
    class SlowSession(Session):
        def snapshot(self):
            time.sleep(0.1)
            return super().snapshot()

    async def scenario():
        async with _client(
            lambda _request: httpx.Response(
                200,
                json={"health": "connected", "error_code": None},
                headers={"content-type": "application/json"},
            )
        ) as client:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://connectors.example/api/v1/connectors",
                allowed_hosts=frozenset({"connectors.example"}),
                session=SlowSession(),
                client=client,
            )
            started = asyncio.get_running_loop().time()
            pending = asyncio.create_task(
                adapter.check_health({"managed_grant": "grant"})
            )
            await asyncio.sleep(0.02)
            loop_delay = asyncio.get_running_loop().time() - started
            result = await pending
            return loop_delay, result

    loop_delay, result = asyncio.run(scenario())
    assert loop_delay < 0.1
    assert result.health is ConnectorHealth.CONNECTED


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_gateway_auth_health_action_and_revoke_contracts() -> None:
    observed: list[tuple[str, dict, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        observed.append((request.url.path, payload, dict(request.headers)))
        assert request.headers["authorization"] == "Bearer managed-connector-bearer"
        assert request.headers["accept-encoding"] == "identity"
        suffix = request.url.path.rsplit("/feishu/", 1)[1]
        if suffix == "auth/begin":
            query = urlencode(
                {
                    "state": payload["state"],
                    "code_challenge": payload["code_challenge"],
                    "code_challenge_method": "S256",
                }
            )
            body = {
                "flow_id": payload["flow_id"],
                "connector_id": "feishu",
                "auth_kind": "oauth2",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "authorization_url": f"https://open.feishu.cn/auth?{query}",
                "user_code": None,
                "verification_url": None,
            }
        elif suffix == "auth/complete":
            body = {
                "account_subject": "open-id-a",
                "account_display_name": "协作账号",
                "granted_scopes": ["docx:document:readonly"],
                "managed_grant": "opaque-provider-grant-value",
            }
        elif suffix == "health":
            body = {"health": "connected", "error_code": None}
        elif suffix == "actions/documents.read":
            assert request.headers["idempotency-key"] == "read-once"
            body = {
                "ok": True,
                "action_id": "documents.read",
                "title": "产品文档",
                "document_id": "doc-a",
            }
        elif suffix == "revoke":
            assert request.headers["idempotency-key"] == "revoke-once"
            body = {"revoked": True}
        else:  # pragma: no cover - makes unexpected route failures explicit
            raise AssertionError(suffix)
        return httpx.Response(
            200,
            json=body,
            headers={"content-type": "application/json"},
        )

    async def scenario() -> None:
        async with _client(handler) as client:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://connectors.example/api/v1/connectors",
                allowed_hosts=frozenset({"connectors.example"}),
                session=Session(),
                client=client,
            )
            challenge = await adapter.begin_auth(
                flow_id="flow-a",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri="http://127.0.0.1:8765/api/v1/connectors/oauth/callback",
                state="state-a",
                code_challenge="pkce-a",
                code_challenge_method="S256",
            )
            assert challenge.flow_id == "flow-a"
            grant = await adapter.complete_auth(
                flow_id="flow-a",
                response={"code": "one-time-code", "state": "state-a"},
                private_state={"pkce_verifier": "private-pkce"},
            )
            assert grant.credential_material == {
                "managed_grant": "opaque-provider-grant-value"
            }
            health = await adapter.check_health(grant.credential_material)
            assert health.health is ConnectorHealth.CONNECTED
            result = await adapter.invoke(
                action_id="documents.read",
                inputs={"document_id": "doc-a"},
                credentials=grant.credential_material,
                idempotency_key="read-once",
            )
            assert result["document_id"] == "doc-a"
            assert await adapter.revoke(
                credentials=grant.credential_material,
                idempotency_key="revoke-once",
            )

    asyncio.run(scenario())
    assert [path for path, _, _ in observed] == [
        "/api/v1/connectors/feishu/auth/begin",
        "/api/v1/connectors/feishu/auth/complete",
        "/api/v1/connectors/feishu/health",
        "/api/v1/connectors/feishu/actions/documents.read",
        "/api/v1/connectors/feishu/revoke",
    ]
    assert "managed-connector-bearer" not in repr([body for _, body, _ in observed])


@pytest.mark.parametrize(
    "endpoint,hosts",
    [
        ("http://connectors.example/api/v1/connectors", {"connectors.example"}),
        ("https://evil.example/api/v1/connectors", {"connectors.example"}),
        ("https://connectors.example:8443/api/v1/connectors", {"connectors.example"}),
        ("https://connectors.example/api/v1/free-form", {"connectors.example"}),
        ("https://user:pass@connectors.example/api/v1/connectors", {"connectors.example"}),
    ],
)
def test_gateway_root_is_fixed_and_allowlisted(endpoint: str, hosts: set[str]) -> None:
    with pytest.raises(ValueError):
        ManagedConnectorGatewayAdapter(
            connector_id="feishu",
            endpoint=endpoint,
            allowed_hosts=frozenset(hosts),
            session=Session(),
        )


def test_session_change_redirect_and_duplicate_json_fail_closed() -> None:
    session = Session()
    session.change_on_bearer = True

    async def changed_session() -> None:
        async with _client(
            lambda _request: httpx.Response(
                200, json={}, headers={"content-type": "application/json"}
            )
        ) as client:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://connectors.example/api/v1/connectors",
                allowed_hosts=frozenset({"connectors.example"}),
                session=session,
                client=client,
            )
            with pytest.raises(ConnectorUnavailable):
                await adapter.check_health({"managed_grant": "grant"})

    asyncio.run(changed_session())

    async def malformed(handler, expected) -> None:
        async with _client(handler) as client:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://connectors.example/api/v1/connectors",
                allowed_hosts=frozenset({"connectors.example"}),
                session=Session(),
                client=client,
            )
            with pytest.raises(expected):
                await adapter.begin_auth(
                    flow_id="flow-a",
                    auth_kind=ConnectorAuthKind.OAUTH2,
                    return_uri="http://127.0.0.1:8765/callback",
                    state="state-a",
                    code_challenge="pkce-a",
                    code_challenge_method="S256",
                )

    asyncio.run(
        malformed(
            lambda _request: httpx.Response(
                302,
                headers={
                    "location": "https://evil.example",
                    "content-type": "application/json",
                },
            ),
            ConnectorAuthError,
        )
    )
    asyncio.run(
        malformed(
            lambda _request: httpx.Response(
                200,
                content=b'{"flow_id":"a","flow_id":"b"}',
                headers={"content-type": "application/json"},
            ),
            ConnectorAuthError,
        )
    )


def test_low_level_transport_marks_retryable_remote_failure_without_body_leak() -> None:
    async def scenario() -> None:
        async with _client(
            lambda _request: httpx.Response(
                503,
                json={"secret": "provider-internal-secret"},
                headers={"content-type": "application/json"},
            )
        ) as client:
            adapter = ManagedConnectorGatewayAdapter(
                connector_id="feishu",
                endpoint="https://connectors.example/api/v1/connectors",
                allowed_hosts=frozenset({"connectors.example"}),
                session=Session(),
                client=client,
            )
            with pytest.raises(ManagedConnectorTransportError) as failure:
                await adapter._request("health", {"managed_grant": "grant"})
            assert failure.value.retryable is True
            assert "provider-internal-secret" not in str(failure.value)

    asyncio.run(scenario())

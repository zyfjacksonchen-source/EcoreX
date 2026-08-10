from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ecorex.connectors import InMemoryCredentialVault
from ecorex.extensions import (
    MCPOAuthError,
    MCPOAuthRegistration,
    MCPOAuthService,
    MCPTransportError,
    ManagedHTTPMCPTransport,
)


def test_mcp_oauth_pkce_refresh_tenant_isolation_and_clear() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url == httpx.URL(
            "https://mcp.example.test/.well-known/oauth-protected-resource"
        ):
            return httpx.Response(
                200,
                json={
                    "authorization_servers": ["https://auth.example.test"],
                    "required_scopes": ["mcp.read", "mcp.write"],
                },
            )
        if request.url == httpx.URL(
            "https://auth.example.test/.well-known/oauth-authorization-server"
        ):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.test",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "revocation_endpoint": "https://auth.example.test/revoke",
                },
            )
        if request.url == httpx.URL("https://auth.example.test/register"):
            payload = json.loads(request.content)
            assert payload["token_endpoint_auth_method"] == "none"
            assert payload["redirect_uris"] == [
                "http://127.0.0.1:8765/api/v1/mcp/oauth/callback"
            ]
            return httpx.Response(201, json={"client_id": "public-client"})
        if request.url == httpx.URL("https://auth.example.test/token"):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"][0]
                assert form["resource"] == ["https://mcp.example.test/session"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-one",
                        "refresh_token": "refresh-one",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-two",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url == httpx.URL("https://auth.example.test/revoke"):
            assert parse_qs(request.content.decode())["token"] == ["refresh-one"]
            return httpx.Response(204)
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/session",
        expected_host="mcp.example.test",
        authorization_hosts=frozenset({"auth.example.test"}),
    )
    service = MCPOAuthService(
        (registration,),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=InMemoryCredentialVault(),
        client=client,
    )

    async def scenario() -> None:
        challenge = await service.begin("remote.mcp", "tenant_one")
        parsed = urlsplit(str(challenge["authorization_url"]))
        parameters = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parameters["code_challenge_method"] == ["S256"]
        assert parameters["state"][0]
        assert parameters["resource"] == ["https://mcp.example.test/session"]
        assert parameters["scope"] == ["mcp.read mcp.write"]

        with pytest.raises(MCPOAuthError, match="mcp_oauth_state_invalid"):
            await service.complete(state="wrong", code="code-one")
        authorized = await service.complete(
            state=parameters["state"][0],
            code="code-one",
        )
        assert authorized.state == "authorized"
        assert "access-one" not in json.dumps(authorized.to_dict())
        assert await service.access_token("remote.mcp", "tenant_one") == "access-one"
        assert (
            await service.status("remote.mcp", "tenant_two")
        ).state == "authorization_required"
        assert await service.refresh("remote.mcp", "tenant_one") == "access-two"
        await service.clear("remote.mcp", "tenant_one")
        assert (
            await service.status("remote.mcp", "tenant_one")
        ).state == "authorization_required"
        await client.aclose()

    asyncio.run(scenario())
    assert sum(request.url.path == "/register" for request in observed) == 1
    assert sum(request.url.path == "/revoke" for request in observed) == 1


def test_managed_http_mcp_oauth_retries_one_refresh_only() -> None:
    class Provider:
        token = "expired"
        refreshes = 0

        async def access_token(self) -> str:
            return self.token

        async def refresh_after_unauthorized(self) -> str:
            self.refreshes += 1
            self.token = "fresh"
            return self.token

    provider = Provider()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("authorization") == "Bearer expired":
            return httpx.Response(401)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": "one", "result": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ManagedHTTPMCPTransport(
        "https://mcp.example.test/session",
        expected_host="mcp.example.test",
        client=client,
    )
    transport.bind_oauth(provider)

    async def scenario() -> None:
        response = await transport.exchange(
            {"jsonrpc": "2.0", "id": "one", "method": "initialize"},
            timeout_seconds=5,
            max_response_bytes=4096,
        )
        assert response["result"] == {}
        await client.aclose()

    asyncio.run(scenario())
    assert provider.refreshes == 1
    assert [request.headers.get("authorization") for request in requests] == [
        "Bearer expired",
        "Bearer fresh",
    ]


def test_mcp_oauth_rejects_non_loopback_callback_and_unapproved_auth_host() -> None:
    registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/session",
        expected_host="mcp.example.test",
    )
    with pytest.raises(ValueError, match="loopback"):
        MCPOAuthService(
            (registration,),
            redirect_uri="https://public.example.test/callback",
            vault=InMemoryCredentialVault(),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"authorization_servers": ["https://unapproved.example.test"]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MCPOAuthService(
        (registration,),
        redirect_uri="http://localhost:8765/api/v1/mcp/oauth/callback",
        vault=InMemoryCredentialVault(),
        client=client,
    )

    async def scenario() -> None:
        with pytest.raises(MCPOAuthError, match="mcp_oauth_endpoint_invalid"):
            await service.begin("remote.mcp", "tenant")
        await client.aclose()

    asyncio.run(scenario())


def test_mcp_oauth_rejects_structurally_unbounded_json() -> None:
    content = (b'{"nested":' * 40) + b"0" + (b"}" * 40)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=content,
                headers={"content-type": "application/json"},
            )
        )
    )
    service = MCPOAuthService(
        (
            MCPOAuthRegistration(
                service_id="remote.mcp",
                resource_url="https://mcp.example.test/session",
                expected_host="mcp.example.test",
            ),
        ),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=InMemoryCredentialVault(),
        client=client,
    )

    async def scenario() -> None:
        with pytest.raises(MCPOAuthError, match="mcp_oauth_response_invalid"):
            await service._request_json(
                "GET",
                "https://mcp.example.test/metadata",
                frozenset({"mcp.example.test"}),
                expected_statuses={200},
            )
        await client.aclose()

    asyncio.run(scenario())


def test_oauth_registration_generation_never_reuses_tokens_after_source_change() -> (
    None
):
    vault = InMemoryCredentialVault()
    old_registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/old",
        expected_host="mcp.example.test",
        client_id="client",
    )
    new_registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/new",
        expected_host="mcp.example.test",
        client_id="client",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))
    old_service = MCPOAuthService(
        (old_registration,),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=vault,
        client=client,
    )
    new_service = MCPOAuthService(
        (new_registration,),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=vault,
        client=client,
    )

    async def scenario() -> None:
        await old_service._save_record(
            old_registration,
            "tenant",
            {"access_token": "old-token", "expires_at": "4102444800"},
        )
        assert await old_service.access_token("remote.mcp", "tenant") == "old-token"
        assert await new_service.access_token("remote.mcp", "tenant") is None
        assert old_service._reference(
            old_registration, "tenant"
        ) != new_service._reference(new_registration, "tenant")
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("refresh_succeeds", [True, False])
def test_concurrent_refresh_is_single_flight_and_cannot_clobber_its_result(
    refresh_succeeds: bool,
) -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        if not refresh_succeeds:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/session",
        expected_host="mcp.example.test",
        client_id="client",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MCPOAuthService(
        (registration,),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=InMemoryCredentialVault(),
        client=client,
    )

    async def scenario() -> None:
        await service._save_record(
            registration,
            "tenant",
            {
                "access_token": "stale-token",
                "refresh_token": "refresh-token",
                "token_endpoint": "https://mcp.example.test/token",
                "client_id": "client",
            },
        )
        first = asyncio.create_task(service.refresh("remote.mcp", "tenant"))
        await started.wait()
        second = asyncio.create_task(service.refresh("remote.mcp", "tenant"))
        await asyncio.sleep(0)
        release.set()
        expected = "fresh-token" if refresh_succeeds else None
        assert await asyncio.gather(first, second) == [expected, expected]
        assert calls == 1
        assert await service.access_token("remote.mcp", "tenant") == expected
        await client.aclose()

    asyncio.run(scenario())


def test_clear_waits_for_inflight_refresh_and_prevents_token_resurrection() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    vault = InMemoryCredentialVault()
    registration = MCPOAuthRegistration(
        service_id="remote.mcp",
        resource_url="https://mcp.example.test/session",
        expected_host="mcp.example.test",
        client_id="client",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MCPOAuthService(
        (registration,),
        redirect_uri="http://127.0.0.1:8765/api/v1/mcp/oauth/callback",
        vault=vault,
        client=client,
    )

    async def scenario() -> None:
        await service._save_record(
            registration,
            "tenant",
            {
                "access_token": "stale-token",
                "refresh_token": "refresh-token",
                "token_endpoint": "https://mcp.example.test/token",
                "client_id": "client",
            },
        )
        refresh = asyncio.create_task(service.refresh("remote.mcp", "tenant"))
        await started.wait()
        clear = asyncio.create_task(service.clear("remote.mcp", "tenant"))
        await asyncio.sleep(0)
        release.set()
        assert await refresh == "fresh-token"
        await clear
        assert await service.access_token("remote.mcp", "tenant") is None
        with pytest.raises(KeyError):
            vault.get(service._reference(registration, "tenant"))
        await client.aclose()

    asyncio.run(scenario())

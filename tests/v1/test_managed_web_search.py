from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sqlite3

import httpx
from fastapi.testclient import TestClient

from agent.tools.base_tool import ToolResult
from agent.tools.web_search import web_search
from agent.tools.web_search.web_search import (
    WebSearch,
    bind_managed_web_search_executor,
    reset_managed_web_search_executor,
)
from common.ecorex_tool_permissions import (
    bind_cow_direct_tools,
    reset_cow_direct_tools,
)
from ecorex.control_plane.management import AdminManagementRepository
from ecorex.control_plane.management_models import CreateAdminUserRequest
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal
from ecorex.gateway import (
    GatewayPrincipal,
    GatewaySchemaManager,
    ManagedHTTPSResponsesProvider,
    ManagedModelGatewayClient,
    SQLiteGatewayStore,
    create_managed_gateway_app,
)
from ecorex.gateway.models import GatewayWebSearchRequest, GatewayWebSearchResponse
from ecorex.gateway.production import AdminManagementGatewayUsageAccountant


SESSION_TOKEN = "managed-session-" + "s" * 32
PROVIDER_TOKEN = "provider-secret-" + "p" * 32


class _Authenticator:
    def authenticate(self, token: str) -> GatewayPrincipal:
        if token != SESSION_TOKEN:
            raise PermissionError
        return GatewayPrincipal(
            subject="user-1",
            account_id="account-1",
            allowed_model_ids=frozenset({"ecorex-chat"}),
            quota_period="2026-08",
            request_limit=100,
        )


def _response(query: str = "CowAgent 2.1.5") -> GatewayWebSearchResponse:
    return GatewayWebSearchResponse(
        query=query,
        total=1,
        count=1,
        results=[
            {
                "title": "CowAgent",
                "url": "https://example.com/cowagent",
                "snippet": "Grounded result with a real URL citation.",
            }
        ],
        usage={"input_tokens": 8, "output_tokens": 5, "total_tokens": 13},
        provider_created_at=datetime.now(timezone.utc),
    )


def test_cow_web_search_prefers_managed_account_without_user_key(monkeypatch) -> None:
    monkeypatch.setattr(web_search, "configured_providers", lambda: [])
    calls: list[tuple[dict, str | None]] = []

    def managed(arguments: dict, tool_call_id: str | None) -> ToolResult:
        calls.append((arguments, tool_call_id))
        return ToolResult.success(
            _response(arguments["query"]).model_dump(
                mode="json",
                exclude={"schema_version", "usage", "provider_created_at"},
            )
        )

    permission_token = bind_cow_direct_tools()
    managed_token = bind_managed_web_search_executor(managed)
    try:
        tool = WebSearch()
        tool.tool_call_id = "call-1"
        result = tool.execute({"query": "CowAgent 2.1.5", "count": 3})
    finally:
        reset_managed_web_search_executor(managed_token)
        reset_cow_direct_tools(permission_token)

    assert result.status == "success"
    assert result.result["results"][0]["url"] == "https://example.com/cowagent"
    assert calls == [
        (
            {
                "query": "CowAgent 2.1.5",
                "count": 3,
                "freshness": "noLimit",
                "summary": False,
            },
            "call-1",
        )
    ]
    schema = WebSearch.get_json_schema()["parameters"]
    assert set(schema["properties"]) == {"query", "count", "freshness", "summary"}
    assert schema["required"] == ["query"]
    assert "api_key" not in json.dumps(calls, ensure_ascii=False)

    permission_token = bind_cow_direct_tools()
    try:
        failure = WebSearch().execute({"query": "CowAgent"})
    finally:
        reset_cow_direct_tools(permission_token)
    assert failure.status == "error"
    assert "No search provider configured" in str(failure.result)


def test_explicit_user_provider_remains_compatible(monkeypatch) -> None:
    monkeypatch.setattr(web_search, "configured_providers", lambda: ["bocha"])
    monkeypatch.setattr(
        WebSearch,
        "_search_bocha",
        lambda _self, query, _count, _freshness, _summary: ToolResult.success(
            {"query": query, "backend": "bocha", "results": []}
        ),
    )
    managed_calls: list[dict] = []
    permission_token = bind_cow_direct_tools()
    managed_token = bind_managed_web_search_executor(
        lambda arguments, _call_id: (
            managed_calls.append(arguments)
            or ToolResult.success({"backend": "managed", "results": []})
        )
    )
    try:
        default = WebSearch().execute({"query": "default"})
        explicit = WebSearch().execute({"query": "byok", "provider": "bocha"})
    finally:
        reset_managed_web_search_executor(managed_token)
        reset_cow_direct_tools(permission_token)
    assert default.result["backend"] == "managed"
    assert explicit.result["backend"] == "bocha"
    assert [call["query"] for call in managed_calls] == ["default"]


def test_responses_provider_runs_native_search_and_returns_citations() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "id": "resp_search_1",
                "status": "completed",
                "created_at": 1_786_464_000,
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "type": "search",
                            "sources": [
                                {
                                    "type": "url",
                                    "title": "CowAgent release",
                                    "url": "https://example.com/releases/2.1.5",
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "CowAgent 2.1.5 is available.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "CowAgent release",
                                        "url": "https://example.com/releases/2.1.5",
                                    }
                                ],
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        provider = ManagedHTTPSResponsesProvider(
            origin="https://provider.ecorex.invalid",
            allowed_origins=frozenset({"https://provider.ecorex.invalid"}),
            model_mapping={"ecorex-chat": "gpt-5.6-luna"},
            bearer_token=lambda: PROVIDER_TOKEN,
            client=client,
        )
        principal = _Authenticator().authenticate(SESSION_TOKEN)
        result = await provider.search(
            GatewayWebSearchRequest(
                request_id="search-1",
                model_id="ecorex-chat",
                query="CowAgent 2.1.5",
                count=5,
            ),
            principal,
        )
        assert result.results[0].url == "https://example.com/releases/2.1.5"
        await client.aclose()

    asyncio.run(scenario())
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert requests[0].headers["authorization"] == f"Bearer {PROVIDER_TOKEN}"
    assert PROVIDER_TOKEN not in json.dumps(_response().model_dump(mode="json"))


def test_managed_client_uses_login_session_without_exposing_it() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=_response().model_dump(mode="json"),
        )

    class Credentials:
        def bearer_token(self) -> str:
            return SESSION_TOKEN

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = ManagedModelGatewayClient(
            "https://gateway.ecorex.invalid/api/v1/model/stream",
            credentials=Credentials(),
            allowed_hosts=frozenset({"gateway.ecorex.invalid"}),
            client=http,
        )
        result = await client.search(
            GatewayWebSearchRequest(
                request_id="search-client-1",
                model_id="ecorex-chat",
                query="CowAgent 2.1.5",
            )
        )
        assert result.results[0].url == "https://example.com/cowagent"
        await http.aclose()

    asyncio.run(scenario())
    assert str(requests[0].url) == "https://gateway.ecorex.invalid/api/v1/web-search"
    assert requests[0].headers["authorization"] == f"Bearer {SESSION_TOKEN}"
    assert SESSION_TOKEN not in requests[0].content.decode("utf-8")


def test_gateway_search_settles_usage_and_audit_once_and_fails_closed(tmp_path) -> None:
    class Provider:
        async def search(self, request, principal):
            assert principal.account_id == "account-1"
            assert "api_key" not in request.model_dump(mode="json")
            return _response(request.query)

        async def stream(self, request, principal):
            del request, principal
            if False:
                yield None

    database = tmp_path / "gateway.db"
    GatewaySchemaManager(database).migrate()
    management_database = tmp_path / "management.db"
    AdminManagementSchemaManager(management_database).migrate()
    repository = AdminManagementRepository(
        management_database,
        encryption_key=b"w" * 32,
    )
    repository.create_user(
        CreateAdminUserRequest(
            account_id="account-1",
            display_name="Managed search user",
            email="search@example.com",
            password="managed-search-password-1",
            token_limit=1000,
            client_request_id="create-managed-search-user",
        ),
        actor=ControlPrincipal(
            subject="administrator",
            client_id="admin-web",
            account_id="admin",
            roles=frozenset({"platform_admin", "release_admin"}),
        ),
    )
    accountant = AdminManagementGatewayUsageAccountant(repository)
    app = create_managed_gateway_app(
        SQLiteGatewayStore(database),
        authenticator=_Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
        usage_accountant=accountant,
    )
    with TestClient(app) as client:
        request = GatewayWebSearchRequest(
            request_id="search-1",
            model_id="ecorex-chat",
            query="CowAgent 2.1.5",
        ).model_dump(mode="json")
        response = client.post(
            "/api/v1/web-search",
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
            json=request,
        )
    assert response.status_code == 200
    assert response.json()["results"][0]["url"].startswith("https://")
    assert repository.get_user("account-1").tokens_used == 13
    with sqlite3.connect(management_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM admin_ops_provider_usage_facts "
            "WHERE source_id='search-1'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM admin_ops_audit "
            "WHERE action='usage.provider.settled'"
        ).fetchone()[0] == 1

    closed = create_managed_gateway_app(
        SQLiteGatewayStore(database),
        authenticator=_Authenticator(),
        provider=Provider(),
        allowed_model_ids=frozenset({"ecorex-chat"}),
    )
    with TestClient(closed) as client:
        unavailable = client.post(
            "/api/v1/web-search",
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
            json={
                "request_id": "search-2",
                "model_id": "ecorex-chat",
                "query": "CowAgent",
            },
        )
    assert unavailable.status_code == 503
    assert PROVIDER_TOKEN not in unavailable.text

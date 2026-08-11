from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import ValidationError

from ecorex.connectors import InMemoryCredentialVault
from ecorex.extensions import (
    MCPClientSupervisor,
    SQLiteExtensionRepository,
    ExtensionService,
)
from ecorex.extensions.user_mcp import (
    UserMCPServerRequest,
    UserMCPService,
    create_user_mcp_router,
    mcp_tenant_namespace,
)
from ecorex.runtime import RuntimeSettings, create_app


def _request(**changes) -> UserMCPServerRequest:
    return UserMCPServerRequest(
        display_name=changes.get("display_name", "My MCP"),
        endpoint=changes.get("endpoint", "https://mcp.example.com/v1"),
        auth_kind=changes.get("auth_kind", "bearer"),
        credential=changes.get("credential", "TOKEN-ONLY-IN-VAULT"),
        oauth_client_id=changes.get("oauth_client_id"),
        oauth_scope=changes.get("oauth_scope", ""),
        authorization_hosts=changes.get("authorization_hosts", []),
    )


def _service(tmp_path, *, organization_id="org-a", vault=None, client=None, reloads=None):
    return UserMCPService(
        tmp_path / "user-mcp.db",
        account_id="account-a",
        organization_id=organization_id,
        vault=vault or InMemoryCredentialVault(),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
        reload_requester=(
            (lambda reason: reloads.append(reason) or True)
            if reloads is not None
            else None
        ),
        http_client=client,
        host_resolver=lambda _host: ("93.184.216.34",),
    )


class _FaultVault(InMemoryCredentialVault):
    def __init__(self) -> None:
        super().__init__()
        self.references: set[str] = set()
        self.put_references: list[str] = []
        self.fail_delete: set[str] = set()

    def put(self, reference, material) -> None:
        super().put(reference, material)
        self.references.add(reference)
        self.put_references.append(reference)

    def delete(self, reference) -> None:
        if reference in self.fail_delete:
            self.fail_delete.remove(reference)
            raise RuntimeError("injected vault delete failure")
        super().delete(reference)
        self.references.discard(reference)


def test_crud_is_tenant_scoped_and_never_projects_or_persists_secret(tmp_path) -> None:
    vault = InMemoryCredentialVault()
    service = _service(tmp_path, vault=vault)
    created = service.create(_request())
    projection = created.projection()
    assert projection["credential_configured"] is True
    assert "credential" not in projection
    assert "TOKEN-ONLY-IN-VAULT" not in (tmp_path / "user-mcp.db").read_text(
        "utf-8", errors="ignore"
    )

    other_org = _service(tmp_path, organization_id="org-b", vault=vault)
    assert other_org.list() == ()
    with pytest.raises(Exception, match="mcp_server_not_found"):
        other_org.get(created.server_id)

    updated = service.update(
        created.server_id,
        _request(display_name="Renamed MCP", credential=None),
    )
    assert updated.display_name == "Renamed MCP"
    assert vault.get(updated.credential_ref or "")["bearer_token"] == "TOKEN-ONLY-IN-VAULT"
    asyncio.run(service.remove(created.server_id))
    assert service.list() == ()
    with pytest.raises(KeyError):
        vault.get(updated.credential_ref or "")


def test_credential_swap_rolls_back_new_generation_when_database_commit_fails(
    tmp_path, monkeypatch
) -> None:
    vault = _FaultVault()
    service = _service(tmp_path, vault=vault)
    created = service.create(_request(credential="old-token"))
    original_connect = service._connect
    connections = 0
    failed_ref: list[str] = []

    @contextmanager
    def fail_after_write():
        nonlocal connections
        connections += 1
        with original_connect() as connection:
            yield connection
            if connections == 2:
                failed_ref.append(vault.put_references[-1])
                vault.fail_delete.add(failed_ref[0])
                raise sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(service, "_connect", fail_after_write)
    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        service._save(
            created,
            created.server_id,
            _request(credential="new-token", expected_revision=created.revision),
        )

    assert vault.references == {created.credential_ref, failed_ref[0]}
    assert vault.get(created.credential_ref or "")["bearer_token"] == "old-token"
    _service(tmp_path, vault=vault)
    assert vault.references == {created.credential_ref}


def test_committed_swap_and_delete_cleanup_converge_after_restart(tmp_path) -> None:
    vault = _FaultVault()
    service = _service(tmp_path, vault=vault)
    created = service.create(_request(credential="old-token"))
    old_ref = created.credential_ref or ""
    vault.fail_delete.add(old_ref)

    updated = service.update(
        created.server_id,
        _request(credential="new-token", expected_revision=created.revision),
    )
    assert updated.credential_ref != old_ref
    assert old_ref in vault.references
    assert vault.get(updated.credential_ref or "")["bearer_token"] == "new-token"

    _service(tmp_path, vault=vault)
    assert old_ref not in vault.references

    vault.fail_delete.add(updated.credential_ref or "")
    asyncio.run(service.remove(updated.server_id))
    assert service.list() == ()
    assert updated.credential_ref in vault.references

    _service(tmp_path, vault=vault)
    assert vault.references == set()
    assert sqlite3.connect(tmp_path / "user-mcp.db").execute(
        "SELECT COUNT(*) FROM user_mcp_vault_cleanup"
    ).fetchone() == (0,)


def test_expected_revision_fences_concurrent_credential_swaps(tmp_path) -> None:
    barrier = threading.Barrier(2)

    class BarrierVault(_FaultVault):
        def put(self, reference, material) -> None:
            super().put(reference, material)
            if material.get("bearer_token") != "initial-token":
                barrier.wait(timeout=5)

    vault = BarrierVault()
    first = _service(tmp_path, vault=vault)
    created = first.create(_request(credential="initial-token"))
    second = _service(tmp_path, vault=vault)

    def update(service: UserMCPService, token: str):
        try:
            return service.update(
                created.server_id,
                _request(credential=token, expected_revision=created.revision),
            )
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda pair: update(*pair),
                ((first, "token-one"), (second, "token-two")),
            )
        )

    winner = next(item for item in results if not isinstance(item, Exception))
    loser = next(item for item in results if isinstance(item, Exception))
    assert "mcp_server_revision_conflict" in str(loser)
    assert first.get(created.server_id).revision == created.revision + 1
    assert vault.get(winner.credential_ref or "")["bearer_token"] in {
        "token-one",
        "token-two",
    }
    assert vault.references == {winner.credential_ref}

    with pytest.raises(Exception, match="mcp_bearer_credential_required"):
        first.update(
            created.server_id,
            _request(
                endpoint="https://other.example.com/v1",
                credential=None,
                expected_revision=winner.revision,
            ),
        )


def test_mcp_endpoint_accepts_cow_local_and_http_configuration(tmp_path) -> None:
    for endpoint in (
        "http://mcp.example.com/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.2/v1",
        "https://mcp.example.com/v1?token=secret",
        "https://user:pass@mcp.example.com/v1",
    ):
        assert _request(endpoint=endpoint).endpoint == endpoint

    for endpoint in ("file:///tmp/mcp", "ftp://mcp.example.com/v1", "http://:bad"):
        with pytest.raises(ValidationError):
            _request(endpoint=endpoint)

    service = UserMCPService(
        tmp_path / "rebound.db",
        account_id="account-a",
        organization_id="org-a",
        vault=InMemoryCredentialVault(),
        runtime_api_version="1.0.0",
        platform="darwin",
        architecture="arm64",
    )
    configured = service.create(
        UserMCPServerRequest(
            display_name="Local MCP",
            endpoint="http://127.0.0.1:8123/mcp",
        )
    )
    assert configured.endpoint == "http://127.0.0.1:8123/mcp"


def test_real_managed_transport_test_catalog_and_supervisor_execution(tmp_path) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.method == "DELETE":
            return httpx.Response(405)
        assert request.headers.get("authorization") == "Bearer TOKEN-ONLY-IN-VAULT"
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202, headers={"MCP-Session-Id": "session-1"})
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1.0.0"},
            }
        elif payload["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Look up one record.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                        "outputSchema": {"type": "object"},
                    }
                ]
            }
        elif payload["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            raise AssertionError(payload)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "MCP-Session-Id": "session-1"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = _service(tmp_path, client=client)
        configured = service.create(_request())
        tested = await service.test(configured.server_id, oauth_service=None)
        assert tested.projection()["tool_names"] == ["lookup"]
        enabled = service.set_enabled(configured.server_id, True)
        binding = service.runtime_bindings()[0]
        extensions = ExtensionService(
            SQLiteExtensionRepository(tmp_path / "extensions.db"),
            runtime_api_version="1.0.0",
            platform="darwin",
            architecture="arm64",
        )
        extensions.register_runtime_bound(binding.verified_manifest)
        supervisor = MCPClientSupervisor(extensions, (binding,))
        result = await supervisor.call(
            extensions.snapshot().snapshot_id,
            binding,
            enabled.tools[0],
            {"query": "record"},
            tenant_id=service.tenant_namespace,
        )
        await supervisor.close()
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result["content"][0]["text"] == "ok"
    assert any(json.loads(request.content).get("method") == "tools/call" for request in observed if request.content)


def test_test_result_cannot_overwrite_a_concurrent_configuration_change(tmp_path) -> None:
    service: UserMCPService

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        else:
            current = service.list()[0]
            service.update(
                current.server_id,
                _request(display_name="Changed while testing", credential=None),
            )
            result = {
                "tools": [
                    {
                        "name": "stale",
                        "description": "Must not be frozen.",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    async def scenario() -> None:
        nonlocal service
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = _service(tmp_path, client=client)
        configured = service.create(_request())
        with pytest.raises(Exception, match="mcp_server_changed_during_test"):
            await service.test(configured.server_id, oauth_service=None)
        await client.aclose()

    asyncio.run(scenario())
    current = service.list()[0]
    assert current.display_name == "Changed while testing"
    assert current.tested_at is None
    assert current.tools == ()


def test_product_api_supports_create_test_enable_disable_delete(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
            if payload["method"] == "initialize"
            else {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Ping the service.",
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object"},
                    }
                ]
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reloads: list[str] = []
    service = _service(tmp_path, client=async_client, reloads=reloads)
    app = FastAPI()
    app.include_router(create_user_mcp_router(service, oauth_service=None))
    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/mcp/servers",
            json={
                "display_name": "Must not echo",
                "endpoint": "https://mcp.example.com/v1",
                "auth_kind": "none",
                "credential": "SECRET-MUST-NOT-ECHO",
            },
        )
        assert rejected.status_code == 422
        assert "SECRET-MUST-NOT-ECHO" not in rejected.text
        response = client.post(
            "/api/v1/mcp/servers",
            json={
                "display_name": "No auth MCP",
                "endpoint": "https://mcp.example.com/v1",
                "auth_kind": "none",
            },
        )
        assert response.status_code == 201
        server_id = response.json()["server"]["server_id"]
        assert response.json()["restart_scheduled"] is True
        assert client.post(f"/api/v1/mcp/servers/{server_id}/enable").status_code == 409
        assert client.post(f"/api/v1/mcp/servers/{server_id}/test").status_code == 200
        assert client.post(f"/api/v1/mcp/servers/{server_id}/enable").status_code == 200
        assert client.post(f"/api/v1/mcp/servers/{server_id}/disable").status_code == 200
        assert client.delete(f"/api/v1/mcp/servers/{server_id}").status_code == 204
        assert client.get("/api/v1/mcp/servers").json() == {"items": []}
    asyncio.run(async_client.aclose())
    assert len(reloads) == 5


def test_oauth_registration_and_namespace_include_organization(tmp_path) -> None:
    service = _service(tmp_path)
    configured = service.create(
        _request(
            auth_kind="oauth2",
            credential=None,
            oauth_client_id="desktop-client",
            oauth_scope="mcp.read",
            authorization_hosts=["auth.example.com"],
        )
    )
    registration = service.oauth_registrations()[0]
    assert registration.service_id == configured.server_id
    assert registration.authorization_hosts == frozenset(
        {"mcp.example.com", "auth.example.com"}
    )
    with pytest.raises(Exception, match="mcp_server_restart_required"):
        asyncio.run(service.test(configured.server_id, oauth_service=None))
    assert mcp_tenant_namespace("account-a", "org-a") != mcp_tenant_namespace(
        "account-a", "org-b"
    )
    raw = sqlite3.connect(tmp_path / "user-mcp.db").execute(
        "SELECT credential_ref FROM user_mcp_servers"
    ).fetchone()
    assert raw == (None,)


def test_runtime_product_mount_enforces_existing_auth_origin_and_csrf(tmp_path) -> None:
    bearer = "b" * 43
    csrf = "c" * 43
    reloads: list[str] = []
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=bearer,
            csrf_token=csrf,
            connector_vault=InMemoryCredentialVault(),
            session_reload_requester=lambda reason: reloads.append(reason) or True,
        )
    )
    payload = {
        "display_name": "Mounted MCP",
        "endpoint": "https://mcp.example.com/v1",
        "auth_kind": "none",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/mcp/servers", json=payload).status_code == 401
        headers = {
            "authorization": f"Bearer {bearer}",
            "origin": "http://127.0.0.1:8765",
            "x-ecorex-csrf": csrf,
        }
        created = client.post("/api/v1/mcp/servers", json=payload, headers=headers)
        assert created.status_code == 201
        server_id = created.json()["server"]["server_id"]
        listed = client.get(
            "/api/v1/mcp/servers",
            headers={"authorization": f"Bearer {bearer}"},
        )
        assert [item["server_id"] for item in listed.json()["items"]] == [server_id]
        assert client.delete(
            f"/api/v1/mcp/servers/{server_id}", headers=headers
        ).status_code == 204
    assert len(reloads) == 2

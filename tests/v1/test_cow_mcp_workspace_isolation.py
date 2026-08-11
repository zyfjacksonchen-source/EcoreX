from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.tools.mcp import mcp_oauth
from agent.tools.mcp.mcp_client import McpClientRegistry, notify_server_authorized
from agent.tools.tool_manager import ToolManager
from ecorex.extensions.cow_mcp import CowMCPSettingsService, create_cow_mcp_router
from ecorex.runtime import RuntimeSettings, create_app


def _mcp_file(workspace: Path, name: str) -> None:
    workspace.mkdir()
    (workspace / "mcp.json").write_text(
        json.dumps({"mcpServers": {name: {"command": name}}}),
        encoding="utf-8",
    )


def test_product_mcp_oauth_callback_uses_exact_runtime_origin(
    tmp_path: Path, monkeypatch,
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            workspace_root=tmp_path / "workspace",
            webui_origins=("http://127.0.0.1:9317",),
        )
    )

    redirect_uri = app.state.cow_mcp_service.manager.mcp_oauth_redirect_uri
    monkeypatch.setattr(
        mcp_oauth,
        "load_server_record",
        lambda _name: {
            "metadata": {"authorization_endpoint": "https://auth.example/authorize"},
            "client_id": "client",
        },
    )
    handler = mcp_oauth.OAuthHandler(
        "fixture", "https://mcp.example/session", redirect_uri
    )
    authorization_url = handler.build_authorization_url()
    query = parse_qs(urlsplit(authorization_url).query)
    mcp_oauth.pop_pending(query["state"][0])

    assert redirect_uri == "http://127.0.0.1:9317/mcp/oauth/callback"
    assert query["redirect_uri"] == [redirect_uri]


def test_tool_manager_and_mcp_registry_are_isolated_by_workspace(
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    manager_a = ToolManager(workspace_root=workspace_a)
    manager_b = ToolManager(workspace_root=workspace_b)
    manager_a._mcp_registry = McpClientRegistry()
    manager_b._mcp_registry = McpClientRegistry()
    stopped: list[str] = []
    client_a = SimpleNamespace(name="a", shutdown=lambda: stopped.append("a"))
    client_b = SimpleNamespace(name="b", shutdown=lambda: stopped.append("b"))
    manager_a._mcp_registry._clients["shared"] = client_a
    manager_b._mcp_registry._clients["shared"] = client_b
    manager_a._mcp_status["a"] = "ready"
    manager_b._mcp_status["b"] = "ready"

    assert manager_a is not manager_b
    assert ToolManager(workspace_root=workspace_a) is manager_a
    assert manager_a._mcp_registry is not manager_b._mcp_registry
    assert manager_a.list_mcp_status() == {"a": "ready"}
    assert manager_b.list_mcp_status() == {"b": "ready"}

    manager_a.shutdown_mcp()
    assert stopped == ["a"]
    assert manager_b._mcp_registry.get("shared") is client_b
    assert manager_b.list_mcp_status() == {"b": "ready"}

    reloaded: list[str] = []
    notify_server_authorized(
        "shared-name",
        SimpleNamespace(reload_callback=lambda name: reloaded.append(f"a:{name}")),
    )
    assert reloaded == ["a:shared-name"]


def test_settings_reads_do_not_rebind_another_workspace_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    _mcp_file(workspace_a, "server-a")
    _mcp_file(workspace_b, "server-b")
    manager_a = ToolManager(workspace_root=workspace_a)
    shutdowns: list[bool] = []
    monkeypatch.setattr(manager_a, "shutdown_mcp", lambda: shutdowns.append(True))
    service = CowMCPSettingsService(workspace_a, manager_a)
    barrier = threading.Barrier(2)
    original_list = CowMCPSettingsService.list

    def synchronized_list(selected: CowMCPSettingsService):
        barrier.wait(timeout=2)
        return original_list(selected)

    monkeypatch.setattr(CowMCPSettingsService, "list", synchronized_list)
    app = FastAPI()
    app.include_router(
        create_cow_mcp_router(
            service,
            workspace_resolver=lambda project_id: (
                workspace_a if project_id == "a" else workspace_b
            ),
        )
    )

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            project_id: pool.submit(
                client.get, "/api/v1/mcp/servers", params={"project_id": project_id}
            )
            for project_id in ("a", "b")
        }
        names = {
            project_id: future.result().json()["items"][0]["display_name"]
            for project_id, future in futures.items()
        }

    assert names == {"a": "server-a", "b": "server-b"}
    assert service.workspace_root == workspace_a.resolve()
    assert manager_a.workspace_root == workspace_a.resolve()
    assert shutdowns == []

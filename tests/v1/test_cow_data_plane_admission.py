from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

from pydantic import SecretStr

from agent.skills.manager import SkillManager
from agent.tools.tool_manager import ToolManager
from ecorex.extensions.cow_mcp import CowMCPSettingsService
from ecorex.extensions.user_mcp import UserMCPServerRequest
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import RuntimeSettings, create_app


def test_cow_turn_admission_does_not_consult_legacy_tool_or_permission_authority(
    tmp_path: Path, monkeypatch,
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            workspace_root=tmp_path / "workspace",
        )
    )
    composition = app.state.runtime_composition
    kernel = app.state.runtime
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = app.state.project_service.create_from_path(
        project_root,
        client_request_id="cow-project",
    )
    thread = kernel.create_thread(
        CreateThreadRequest(
            title="Cow admission",
            metadata={"project_id": project.project_id},
        )
    )

    def legacy_authority_used(*_args, **_kwargs):
        raise AssertionError("Cow Turn admission used legacy authority")

    monkeypatch.setattr(composition.capability_service, "create_plan", legacy_authority_used)
    monkeypatch.setattr(composition.extension_service, "snapshot", legacy_authority_used)
    monkeypatch.setattr(composition, "_permission_provider", legacy_authority_used)

    accepted = composition.admit_turn(
        CreateTurnRequest(
            input="delegate this task",
            explicit_tool_ids=["subagent"],
            client_message_id="cow-admission-subagent",
        ),
        lambda prepared: kernel.create_turn(
            thread.thread_id,
            prepared.request,
            snapshot_context=prepared.snapshot_context,
        ),
        thread_id=thread.thread_id,
    )

    assert accepted.turn.agent_model_id
    assert accepted.turn.metadata == {
        "_cow_workspace_root": str(project_root.resolve()),
    }
    with kernel.database.reader() as connection:
        context = connection.execute(
            "SELECT capability_snapshot_id, permission_snapshot_id, extension_snapshot_id "
            "FROM job_runtime_contexts WHERE job_id = ?",
            (accepted.job.job_id,),
        ).fetchone()
    assert dict(context) == {
        "capability_snapshot_id": "cow-tool-manager-2.1.5",
        "permission_snapshot_id": "cow-account-audit",
        "extension_snapshot_id": "cow-local-skills-mcp",
    }


def test_tool_manager_uses_bound_project_mcp_json_and_not_global_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mcp.json").write_text(
        json.dumps({"mcpServers": {"project-server": {"command": "project-mcp"}}}),
        encoding="utf-8",
    )
    global_workspace = tmp_path / "global"
    global_workspace.mkdir()
    (global_workspace / "mcp.json").write_text(
        json.dumps({"mcpServers": {"wrong-server": {"command": "wrong-mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ToolManager, "_instance", None)
    monkeypatch.setattr(
        "agent.tools.tool_manager.conf",
        lambda: {"agent_workspace": str(global_workspace)},
    )

    manager = ToolManager(workspace_root=project)

    assert Path(manager._mcp_json_path()) == project / "mcp.json"
    assert [item["name"] for item in manager._load_mcp_configs()] == ["project-server"]
    (project / "mcp.json").unlink()
    assert manager._load_mcp_configs() == []


def test_skill_manager_scans_only_builtin_and_current_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    home = tmp_path / "home"
    builtin = tmp_path / "builtin"
    workspace = tmp_path / "project" / "skills"
    for root, name in (
        (builtin, "builtin-skill"),
        (workspace, "workspace-skill"),
        (home / ".codex" / "skills", "codex-skill"),
        (home / ".agents" / "skills", "agents-skill"),
        (home / ".codex" / "plugins" / "cache" / "plugin" / "skills", "plugin-skill"),
    ):
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    manager = SkillManager(builtin_dir=str(builtin), custom_dir=str(workspace))

    assert set(manager.skills) == {"builtin-skill", "workspace-skill"}


def test_tool_manager_does_not_publish_channel_transport_cli_tools(monkeypatch) -> None:
    monkeypatch.setattr(ToolManager, "_instance", None)
    manager = ToolManager()
    manager.load_tools(start_mcp=False)

    assert {"feishu_cli", "tongxin_cli"}.isdisjoint(manager.list_tools())


def test_managed_feishu_auth_never_falls_back_to_browser_or_fake_qr() -> None:
    from agent.prompt.builder import _build_tooling_section

    managed_prompt = "\n".join(
        _build_tooling_section(
            [SimpleNamespace(name="browser"), SimpleNamespace(name="send")],
            "en",
        )
    )
    assert "real e-Mate Connector login challenge owned by this session" in managed_prompt
    assert "Never use browser automation, raw shell, `send`, screenshots, QR codes" in managed_prompt
    assert "call `feishu_cli` first" not in managed_prompt

    cow_prompt = "\n".join(
        _build_tooling_section([SimpleNamespace(name="feishu_cli")], "en")
    )
    assert "call `feishu_cli` first" in cow_prompt


def test_mcp_settings_write_the_same_project_file_tool_manager_reads(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(ToolManager, "_instance", None)
    manager = ToolManager(workspace_root=project)
    monkeypatch.setattr(manager, "refresh_mcp_if_changed", lambda: None)
    service = CowMCPSettingsService(project, manager)

    projected = service.create(
        UserMCPServerRequest(
            display_name="project-search",
            endpoint="https://mcp.example.test/mcp",
            auth_kind="none",
            oauth_scope="",
            authorization_hosts=[],
        )
    )

    assert projected["display_name"] == "project-search"
    assert Path(manager._mcp_json_path()) == project / "mcp.json"
    assert manager._load_mcp_configs() == [
        {
            "name": "project-search",
            "type": "streamable-http",
            "url": "https://mcp.example.test/mcp",
            "enabled": True,
            "_emate_display_name": "project-search",
            "_emate_auth_kind": "none",
            "_emate_oauth_client_id": None,
            "_emate_oauth_scope": "",
            "_emate_authorization_hosts": [],
            "_emate_revision": 1,
        }
    ]


def test_tencent_docs_bearer_is_written_only_to_the_cow_workspace_mcp_config(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(ToolManager, "_instance", None)
    manager = ToolManager(workspace_root=project)
    monkeypatch.setattr(manager, "refresh_mcp_if_changed", lambda: None)
    service = CowMCPSettingsService(project, manager)
    token = "tencent-docs-test-token"

    projected = service.create(
        UserMCPServerRequest(
            display_name="tencent-docs",
            endpoint="https://docs.qq.com/openapi/mcp",
            auth_kind="bearer",
            credential=SecretStr(token),
        )
    )

    stored = json.loads((project / "mcp.json").read_text(encoding="utf-8"))
    config = stored["mcpServers"]["tencent-docs"]
    assert config["type"] == "streamable-http"
    assert config["url"] == "https://docs.qq.com/openapi/mcp"
    assert config["headers"] == {"Authorization": f"Bearer {token}"}
    assert projected["credential_configured"] is True
    assert token not in json.dumps(projected)
    assert manager._load_mcp_configs() == [{"name": "tencent-docs", **config}]


def test_product_runtime_never_mounts_legacy_mcp_execution_supervisor(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            workspace_root=tmp_path / "workspace",
            mcp_runtime_bindings=(object(),),
        )
    )

    assert app.state.mcp_client_supervisor is None
    assert isinstance(app.state.cow_mcp_service, CowMCPSettingsService)


def test_project_mcp_json_registers_and_executes_on_the_native_tool_manager(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    server = project / "server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo",
            "description": "Echo text.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (project / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ToolManager, "_instance", None)
    manager = ToolManager(workspace_root=project)
    manager.load_tools(start_mcp=False)
    try:
        status = manager.ensure_mcp_configured_loaded(
            wait_seconds=5,
            poll_interval_seconds=0.02,
            server_name="fixture",
        )
        tool = manager.create_tool("echo")
        result = tool.execute({"text": "native-cow-mcp"})
        continued = tool.execute({"text": "same-runtime-context"})
    finally:
        manager.shutdown_mcp()

    assert status["status"] == {"fixture": "ready"}
    assert result.status == "success"
    assert result.result == "native-cow-mcp"
    assert continued.status == "success"
    assert continued.result == "same-runtime-context"

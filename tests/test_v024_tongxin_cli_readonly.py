import json
import os
import sys
import tempfile
import textwrap
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_tongxin_script(root: Path) -> Path:
    script = root / "xin_agent_cli.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            args = sys.argv[1:]
            if args == ["schema"]:
                print(json.dumps({"ok": True, "name": "xin_agent_cli", "mode": "read_only"}))
            elif args[:2] == ["realtime", "summary"]:
                print(json.dumps({"ok": True, "data": {"total_cost": 6}, "meta": {"xhs_channel": "all"}}))
            elif args[:2] == ["project", "list"]:
                print("authorization: Bearer raw-human-auth-token")
                print("credentialId=raw-human-credential-id")
                print("safeMetric: 1")
                print("authHeader: Bearer raw-human-auth-header-token", file=sys.stderr)
                print("Bearer raw-standalone-token", file=sys.stderr)
            elif args[:2] == ["account", "list"]:
                print(json.dumps({
                    "ok": True,
                    "access_token": "raw-access-token",
                    "accessToken": "raw-camel-access-token",
                    "refreshToken": "raw-camel-refresh-token",
                    "authHeader": "Bearer raw-auth-header-token",
                    "data": {
                        "items": [{
                            "authorization": "Bearer raw-authorization-token",
                            "nested": {"app_secret": "raw-app-secret", "appSecret": "raw-camel-app-secret"},
                            "api_key": "raw-api-key",
                            "apiKey": "raw-camel-api-key",
                            "credentialId": "raw-credential-id",
                            "url_like": "sk-1234567890abcdef",
                        }]
                    }
                }))
            else:
                print(json.dumps({"ok": False, "args": args}))
                sys.exit(2)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def test_tongxin_cli_wrapper_runs_only_read_only_commands():
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli

    with tempfile.TemporaryDirectory() as workspace:
        script = _make_tongxin_script(Path(workspace))
        tool = TongxinCli({"cwd": workspace, "script_path": str(script)})

        status = tool.execute({"action": "status", "include_paths": True})
        assert status.status == "success"
        assert status.result["available"] is True
        assert status.result["readOnly"] is True

        schema = tool.execute({"action": "schema"})
        assert schema.status == "success"
        assert schema.result["json"]["mode"] == "read_only"

        realtime = tool.execute({"action": "run", "args": ["realtime", "summary", "--xhs-channel", "all"]})
        assert realtime.status == "success"
        assert realtime.result["json"]["data"]["total_cost"] == 6

        accounts = tool.execute({"action": "run", "args": ["account", "list", "--source", "mpi"]})
        assert accounts.status == "success"
        serialized_accounts = json.dumps(accounts.result, ensure_ascii=False)
        assert "raw-access-token" not in serialized_accounts
        assert "raw-authorization-token" not in serialized_accounts
        assert "raw-app-secret" not in serialized_accounts
        assert "raw-api-key" not in serialized_accounts
        assert "raw-camel-access-token" not in serialized_accounts
        assert "raw-camel-refresh-token" not in serialized_accounts
        assert "raw-auth-header-token" not in serialized_accounts
        assert "raw-camel-app-secret" not in serialized_accounts
        assert "raw-camel-api-key" not in serialized_accounts
        assert "raw-credential-id" not in serialized_accounts
        assert "sk-1234567890abcdef" not in serialized_accounts
        assert accounts.result["json"]["access_token"] == "***"
        assert accounts.result["json"]["accessToken"] == "***"
        assert accounts.result["json"]["refreshToken"] == "***"
        assert accounts.result["json"]["authHeader"] == "***"
        assert accounts.result["json"]["data"]["items"][0]["authorization"] == "***"
        assert accounts.result["json"]["data"]["items"][0]["nested"]["app_secret"] == "***"
        assert accounts.result["json"]["data"]["items"][0]["nested"]["appSecret"] == "***"
        assert accounts.result["json"]["data"]["items"][0]["apiKey"] == "***"
        assert accounts.result["json"]["data"]["items"][0]["credentialId"] == "***"

        projects = tool.execute({"action": "run", "args": ["project", "list", "--search", "clinic"]})
        assert projects.status == "success"
        serialized_projects = json.dumps(projects.result, ensure_ascii=False)
        assert "raw-human-auth-token" not in serialized_projects
        assert "raw-human-auth-header-token" not in serialized_projects
        assert "raw-human-credential-id" not in serialized_projects
        assert "raw-standalone-token" not in serialized_projects
        assert "authorization: ***" in projects.result["output"]
        assert "authHeader: ***" in projects.result["output"]
        assert "credentialId=***" in projects.result["output"]
        assert "Bearer ***" in projects.result["output"]
        assert "safeMetric: 1" in projects.result["output"]

        with patch("agent.tools.tongxin_cli.tongxin_cli._run_process") as run_process:
            blocked = tool.execute({"action": "run", "args": ["realtime", "summary", "--write"]})
        assert blocked.status == "error"
        assert "blocked" in json.dumps(blocked.result, ensure_ascii=False).lower()
        run_process.assert_not_called()


def test_tongxin_cli_read_only_contract_is_command_specific():
    from agent.tools.tongxin_cli.tongxin_cli import validate_read_only_tongxin_args

    allowed = [
        ["account", "list", "--source", "mpi", "--platform", "xhs"],
        ["project", "list", "--search", "clinic"],
        ["report", "summary", "--source", "mpi", "--platform", "xhs", "--start-date", "2026-01-01"],
        ["note", "detail", "--source", "mpi", "--platform", "xhs", "--end-date", "2026-01-31"],
        ["realtime", "summary", "--xhs-channel", "all", "--limit", "20"],
    ]
    for args in allowed:
        ok, reason = validate_read_only_tongxin_args(args)
        assert ok, (args, reason)

    blocked = [
        ["account", "list", "--start-date", "2026-01-01"],
        ["account", "list", "--operator", "alice"],
        ["realtime", "summary", "--source", "mpi", "--xhs-channel", "all"],
        ["realtime", "summary", "--format", "json", "--xhs-channel", "all"],
        ["report", "summary", "--task-id", "abc"],
        ["note", "detail", "--json"],
        ["account", "list", "unexpected-positional"],
        ["project", "list", "clinic"],
        ["report", "summary", "daily"],
        ["note", "detail", "123"],
        ["realtime", "summary", "all"],
        ["realtime", "summary", "--xhs-channel", "all", "extra"],
    ]
    for args in blocked:
        ok, reason = validate_read_only_tongxin_args(args)
        assert not ok, (args, reason)


def test_tongxin_cli_broker_allows_readonly_and_blocks_mutations_in_readonly_mode():
    from common.ecorex_tool_permissions import ToolPermissionBroker

    old_user_data = os.environ.get("ECOREX_USER_DATA")
    old_desktop = os.environ.get("ECOREX_DESKTOP")
    with tempfile.TemporaryDirectory() as user_data:
        os.environ["ECOREX_USER_DATA"] = user_data
        os.environ.pop("ECOREX_DESKTOP", None)
        try:
            broker = ToolPermissionBroker()
            broker.set_mode("read-only")

            allowed = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "run", "args": ["account", "list", "--source", "mpi"]},
            )
            assert allowed["allowed"] is True
            assert allowed["reason"] == "default-read-only-tongxin-cli"

            denied = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "run", "args": ["account", "update", "--account-id", "123"]},
            )
            assert denied["allowed"] is False
            assert "read-only" in denied["reason"]
        finally:
            if old_user_data is None:
                os.environ.pop("ECOREX_USER_DATA", None)
            else:
                os.environ["ECOREX_USER_DATA"] = old_user_data
            if old_desktop is None:
                os.environ.pop("ECOREX_DESKTOP", None)
            else:
                os.environ["ECOREX_DESKTOP"] = old_desktop


def test_raw_tongxin_cli_bash_is_blocked_and_autorouted():
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.base_tool import ToolResult
    from agent.tools.bash.bash import Bash

    bash_result = Bash({"cwd": os.getcwd(), "safety_mode": False}).execute({
        "command": "python xin_agent_cli.py schema",
    })
    assert bash_result.status == "error"
    assert "tongxin_cli" in str(bash_result.result)

    class FakeTongxinTool:
        name = "tongxin_cli"

        def __init__(self):
            self.calls = []

        def execute_tool(self, params):
            self.calls.append(params)
            return ToolResult.success({"ok": True, "params": params})

    fake_tongxin = FakeTongxinTool()
    events = []
    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[fake_tongxin],
        on_event=lambda event: events.append(event),
    )

    with patch.object(executor, "_authorize_tool_execution", return_value={"allowed": True}):
        result = executor._execute_tool({
            "id": "tool-call-tongxin-cli",
            "name": "bash",
            "arguments": {
                "command": "python xin_agent_cli.py realtime summary --xhs-channel all",
                "timeout": 12,
            },
        })

    assert result["status"] == "success"
    assert fake_tongxin.calls == [{
        "action": "run",
        "args": ["realtime", "summary", "--xhs-channel", "all"],
        "timeout": 12,
    }]
    assert result["result"]["reroutedFrom"] == "bash:raw bash tongxin-cli"
    assert events[0]["data"]["tool_name"] == "tongxin_cli"

    routed_name, _routed_args, _reason = executor._external_capability_autoroute(
        "bash",
        {"command": "python xin_agent_cli.py schema && echo done"},
    )
    assert routed_name == ""
    guidance = executor._external_capability_reroute(
        "bash",
        {"command": "python xin_agent_cli.py schema && echo done"},
    )
    assert "Tongxin Assistant CLI" in guidance
    assert "tongxin_cli" in guidance


def test_tongxin_tool_is_registered_and_diagnostics_source_mentions_it():
    from agent.tools import TongxinCli
    from agent.tools.tool_manager import ToolManager

    manager = ToolManager()
    manager.load_tools(config_dict={"tongxin_cli": {"script_path": ""}})
    tool = manager.create_tool("tongxin_cli")

    assert TongxinCli is not None
    assert tool is not None
    assert tool.name == "tongxin_cli"
    assert "tongxin_cli" in Path("agent/tools/host_diagnostics/host_diagnostics.py").read_text(encoding="utf-8")
    assert "tongxin_cli" in Path("bridge/agent_initializer.py").read_text(encoding="utf-8")
    assert "tongxin_cli" in Path("agent/protocol/agent_stream.py").read_text(encoding="utf-8")


def test_tongxin_capability_pack_is_configure_only_not_installable():
    from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
    from agent.protocol.agent_stream import AgentStreamExecutor

    optional = OptionalAbilities()
    listed = optional.execute({"action": "list"}).result["abilities"]
    tongxin = next(item for item in listed if item.get("packId") == "tongxin-cli")
    assert tongxin["configureOnly"] is True
    assert tongxin["readOnly"] is True
    assert tongxin["defaultEnabled"] is True
    assert tongxin["agentCanInstall"] is False
    assert "installHint" in tongxin

    optional_install = optional.execute({"action": "install", "ability": "tongxin-cli"})
    assert optional_install.status == "error"
    assert optional_install.result["errorType"] == "capability_configure_only"
    assert optional_install.result["configureOnly"] is True

    agent_install = AgentCapabilityTool().execute({"action": "install_pack", "pack_id": "tongxin"})
    assert agent_install.status == "error"
    assert agent_install.result["packId"] == "tongxin-cli"
    assert agent_install.result["configureOnly"] is True
    assert "not an installable package" in agent_install.result["message"]

    proxy_name, proxy_args = AgentStreamExecutor._permission_proxy_for_tool(
        None,
        "agent_capability",
        {"action": "install_pack", "pack_id": "tx-assistant"},
    )
    assert proxy_name == "tongxin_cli"
    assert proxy_args["action"] == "status"


def test_tongxin_diagnostics_and_initializer_behaviour():
    from agent.tools.host_diagnostics.host_diagnostics import HostDiagnostics
    from bridge.agent_initializer import AgentInitializer

    with tempfile.TemporaryDirectory() as workspace:
        _make_tongxin_script(Path(workspace))
        diagnostics = HostDiagnostics({"cwd": workspace}).execute({"action": "status"})
        assert diagnostics.status == "success"
        assert "tongxin" in diagnostics.result
        assert diagnostics.result["tongxin"]["readOnly"] is True
        assert diagnostics.result["tongxin"]["available"] is True
        assert "scriptPath" not in diagnostics.result["tongxin"]

        initializer = AgentInitializer(bridge=types.SimpleNamespace(), agent_bridge=types.SimpleNamespace())
        tools = initializer._load_tools(workspace, memory_manager=None, memory_tools=[], session_id=None)
        tongxin_tools = [tool for tool in tools if getattr(tool, "name", "") == "tongxin_cli"]
        assert tongxin_tools
        assert tongxin_tools[0].cwd == workspace

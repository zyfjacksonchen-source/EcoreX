import json
import os
import sys
import tempfile
import textwrap
import types
import copy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        assert status.result["pathsRedacted"] is True
        assert status.result["scriptPathRef"]["name"] == "xin_agent_cli.py"
        assert str(script) not in json.dumps(status.result, ensure_ascii=False)

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


def test_tongxin_cli_auto_detects_and_persists_local_script_path():
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
    from config import conf

    with tempfile.TemporaryDirectory() as workspace:
        script = _make_tongxin_script(Path(workspace))
        config_path = Path(workspace) / "config.json"
        tool = TongxinCli({"cwd": workspace, "config_path": str(config_path)})
        old_tools = copy.deepcopy(conf().get("tools", {}))
        conf()["tools"] = {}

        try:
            with patch.object(TongxinCli, "_trusted_auto_config_roots", return_value=[]), \
                    patch.object(TongxinCli, "_env_script_path_values", return_value=[]):
                detected = tool.execute({"action": "status", "include_paths": True})
                assert detected.status == "success"
                assert detected.result["available"] is True
                assert detected.result["configured"] is False
                assert detected.result["autoConfigurable"] is False
                assert detected.result["configurationState"] == "detected_untrusted"
                assert detected.result["pathsRedacted"] is True
                assert detected.result["scriptPathRef"]["name"] == "xin_agent_cli.py"
                assert str(script) not in json.dumps(detected.result, ensure_ascii=False)

                automatic = tool.execute({"action": "configure", "include_paths": True})
                assert automatic.status == "error"
                assert automatic.result["configured"] is False
                assert automatic.result["configurationState"] == "detected_untrusted"
                assert str(script) not in json.dumps(automatic.result, ensure_ascii=False)

                configured = tool.execute({"action": "configure", "script_path": str(script), "include_paths": True})
                assert configured.status == "success"
                assert configured.result["configured"] is True
                assert configured.result["configurationState"] == "configured"
                assert configured.result["pathsRedacted"] is True
                assert configured.result["configPathRef"]["name"] == "config.json"
                assert str(config_path) not in json.dumps(configured.result, ensure_ascii=False)

            data = json.loads(config_path.read_text(encoding="utf-8"))
            assert data["tools"]["tongxin_cli"]["script_path"] == str(script.resolve())
            assert data["tools"]["tongxin_cli"]["read_only"] is True

            ready = tool.execute({"action": "status"})
            assert ready.result["configured"] is True
            assert ready.result["configurationState"] == "configured"
        finally:
            conf()["tools"] = old_tools


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

            configure = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "configure"},
            )
            assert configure["allowed"] is True
            assert configure["reason"] == "default-tongxin-cli-auto-config"

            explicit_path_configure = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "configure", "script_path": "C:/tmp/xin_agent_cli.py"},
            )
            assert explicit_path_configure["allowed"] is False
            assert "read-only" in explicit_path_configure["reason"]

            optional_configure = broker.authorize_noninteractive(
                "optional_abilities",
                {"action": "install", "ability": "tongxin-cli"},
            )
            assert optional_configure["allowed"] is True
            assert optional_configure["reason"] == "default-tongxin-cli-auto-config"

            optional_explicit_path = broker.authorize_noninteractive(
                "optional_abilities",
                {"action": "configure", "ability": "tongxin-cli", "script_path": "C:/tmp/xin_agent_cli.py"},
            )
            assert optional_explicit_path["allowed"] is False
            assert "read-only" in optional_explicit_path["reason"]

            agent_configure = broker.authorize_noninteractive(
                "agent_capability",
                {"action": "install_pack", "pack_id": "tx-assistant"},
            )
            assert agent_configure["allowed"] is True
            assert agent_configure["reason"] == "default-tongxin-cli-auto-config"

            denied = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "run", "args": ["account", "update", "--account-id", "123"]},
            )
            assert denied["allowed"] is False
            assert "read-only" in denied["reason"]

            bootstrap = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "bootstrap"},
            )
            assert bootstrap["allowed"] is True
            assert bootstrap["reason"] == "default-tongxin-cli-authenticated-bootstrap"

            explicit_bootstrap = broker.authorize_noninteractive(
                "tongxin_cli",
                {"action": "bootstrap", "url": "https://example.invalid/xin_agent_cli.py", "auth_token": "secret-token"},
            )
            assert explicit_bootstrap["allowed"] is False
            assert "read-only" in explicit_bootstrap["reason"]
        finally:
            if old_user_data is None:
                os.environ.pop("ECOREX_USER_DATA", None)
            else:
                os.environ["ECOREX_USER_DATA"] = old_user_data
            if old_desktop is None:
                os.environ.pop("ECOREX_DESKTOP", None)
            else:
                os.environ["ECOREX_DESKTOP"] = old_desktop


def test_tongxin_cli_authenticated_bootstrap_downloads_verifies_and_configures():
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
    from config import conf

    script_body = textwrap.dedent(
        """
        import json
        import sys
        if sys.argv[1:] == ["schema"]:
            print(json.dumps({"ok": True, "name": "xin_agent_cli"}))
        else:
            print(json.dumps({"ok": True, "args": sys.argv[1:]}))
        """
    ).strip() + "\n"
    script_bytes = script_body.encode("utf-8")
    script_sha = __import__("hashlib").sha256(script_bytes).hexdigest().upper()
    seen_headers = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_headers.append(self.headers.get("Authorization"))
            if self.path == "/manifest.json":
                payload = {
                    "downloadUrl": f"http://127.0.0.1:{self.server.server_port}/xin_agent_cli.py",
                    "sha256": script_sha,
                    "fileName": "xin_agent_cli.py",
                }
                body = json.dumps(payload).encode("utf-8")
            elif self.path == "/xin_agent_cli.py":
                body = script_bytes
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    with tempfile.TemporaryDirectory() as workspace:
        config_path = Path(workspace) / "config.json"
        target_dir = Path(workspace) / "runtime" / "tools" / "tongxin"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_tools = copy.deepcopy(conf().get("tools", {}))
        conf()["tools"] = {}
        try:
            tool = TongxinCli({
                "cwd": workspace,
                "config_path": str(config_path),
                "bootstrap_manifest_url": f"http://127.0.0.1:{server.server_port}/manifest.json",
                "bootstrap_token": "secret-bootstrap-token",
                "bootstrap_dir": str(target_dir),
            })
            with patch.object(TongxinCli, "_trusted_auto_config_roots", return_value=[Path(workspace)]):
                result = tool.execute({"action": "bootstrap", "allow_insecure_localhost": True, "include_paths": True})
                assert result.status == "success"
                serialized = json.dumps(result.result, ensure_ascii=False)
                assert "secret-bootstrap-token" not in serialized
                assert f"127.0.0.1:{server.server_port}" not in serialized
                assert result.result["downloaded"] is True
                assert result.result["sha256"] == script_sha
                assert result.result["scriptPathRef"]["name"] == "xin_agent_cli.py"
                assert seen_headers and all(header == "Bearer secret-bootstrap-token" for header in seen_headers)

                configured = json.loads(config_path.read_text(encoding="utf-8"))
                script_path = Path(configured["tools"]["tongxin_cli"]["script_path"])
                assert script_path.name == "xin_agent_cli.py"
                assert script_path.read_text(encoding="utf-8") == script_body

                schema = tool.execute({"action": "schema"})
                assert schema.status == "success"
                assert schema.result["json"]["name"] == "xin_agent_cli"
        finally:
            server.shutdown()
            server.server_close()
            conf()["tools"] = old_tools


def test_tongxin_cli_bootstrap_rejects_sha_mismatch_without_writing():
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli

    body = b"print('not trusted')\n"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    with tempfile.TemporaryDirectory() as workspace:
        config_path = Path(workspace) / "config.json"
        target_dir = Path(workspace) / "runtime" / "tools" / "tongxin"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tool = TongxinCli({
                "cwd": workspace,
                "config_path": str(config_path),
                "bootstrap_url": f"http://127.0.0.1:{server.server_port}/xin_agent_cli.py",
                "bootstrap_sha256": "0" * 64,
                "bootstrap_dir": str(target_dir),
            })
            with patch.object(TongxinCli, "_trusted_auto_config_roots", return_value=[Path(workspace)]):
                result = tool.execute({"action": "bootstrap", "allow_insecure_localhost": True})
                assert result.status == "error"
                assert result.result["configurationState"] == "bootstrap_sha256_mismatch"
                assert not (target_dir / "xin_agent_cli.py").exists()
                assert not config_path.exists()
        finally:
            server.shutdown()
            server.server_close()


def test_raw_tongxin_cli_bash_is_blocked_and_autorouted():
    from agent.protocol.agent_stream import AgentStreamExecutor
    from agent.tools.base_tool import ToolResult
    from agent.tools.bash.bash import Bash

    bash_result = Bash({"cwd": os.getcwd(), "safety_mode": False}).execute({
        "command": "python xin_agent_cli.py schema",
    })
    assert bash_result.status == "error"
    assert "tongxin_cli" in str(bash_result.result)
    assert Bash._looks_like_tongxin_cli_command('python "xin agent cli.py" schema') is True

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


def test_tongxin_capability_pack_configures_detected_local_cli():
    from agent.tools.agent_capability.agent_capability import AgentCapabilityTool
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
    from agent.tools.tongxin_cli.tongxin_cli import TongxinCli
    from agent.protocol.agent_stream import AgentStreamExecutor
    from config import conf

    old_cwd = os.getcwd()
    old_tools = copy.deepcopy(conf().get("tools", {}))
    old_state_dir = os.environ.get("ECOREX_CAPABILITY_STATE_DIR")
    with tempfile.TemporaryDirectory() as workspace:
        _make_tongxin_script(Path(workspace))
        config_path = Path(workspace) / "config.json"
        state_dir = Path(workspace) / "capability-state"
        os.environ["ECOREX_CAPABILITY_STATE_DIR"] = str(state_dir)
        os.chdir(workspace)
        conf()["tools"] = {}
        try:
            with patch.object(TongxinCli, "_runtime_config_path", return_value=config_path), \
                    patch.object(TongxinCli, "_trusted_auto_config_roots", return_value=[Path(workspace)]):
                optional = OptionalAbilities()
                listed = optional.execute({"action": "list"}).result["abilities"]
                tongxin = next(item for item in listed if item.get("packId") == "tongxin-cli")
                assert tongxin["configureOnly"] is True
                assert tongxin["readOnly"] is True
                assert tongxin["defaultEnabled"] is True
                assert tongxin["agentCanInstall"] is False
                assert "installHint" in tongxin
                assert tongxin["capabilityState"]["installed"] is False
                assert tongxin["capabilityState"]["available"] is True
                assert tongxin["capabilityState"]["configurationState"] == "detected_unconfigured"

                optional_install = optional.execute({"action": "install", "ability": "tongxin-cli"})
                assert optional_install.status == "success"
                assert optional_install.result["configured"] is True
                assert optional_install.result["capabilityState"]["installed"] is True
                assert json.loads(config_path.read_text(encoding="utf-8"))["tools"]["tongxin_cli"]["read_only"] is True

                agent_install = AgentCapabilityTool().execute({"action": "install_pack", "pack_id": "tongxin"})
                assert agent_install.status == "success"
                assert agent_install.result["packId"] == "tongxin-cli"
                assert agent_install.result["configureOnly"] is True
                assert agent_install.result["configured"] is True
        finally:
            os.chdir(old_cwd)
            conf()["tools"] = old_tools
            if old_state_dir is None:
                os.environ.pop("ECOREX_CAPABILITY_STATE_DIR", None)
            else:
                os.environ["ECOREX_CAPABILITY_STATE_DIR"] = old_state_dir

    proxy_name, proxy_args = AgentStreamExecutor._permission_proxy_for_tool(
        None,
        "agent_capability",
        {"action": "install_pack", "pack_id": "tx-assistant"},
    )
    assert proxy_name == "tongxin_cli"
    assert proxy_args["action"] == "configure"


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

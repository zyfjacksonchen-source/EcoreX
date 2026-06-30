import json
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _allow_system_python_for_fake_lark_cli(monkeypatch):
    from common.tool_execution_environment import ToolExecutionEnvironment

    original_popen = ToolExecutionEnvironment.popen

    def fake_popen(self, command, **kwargs):
        if command and str(command[0]) == sys.executable:
            kwargs["allow_external_executable"] = True
        return original_popen(self, command, **kwargs)

    monkeypatch.setattr(ToolExecutionEnvironment, "popen", fake_popen)


def _install_web_stub():
    if "web" in sys.modules:
        return
    web_stub = types.ModuleType("web")
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.cookies = lambda: {}
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.notfound = lambda: RuntimeError("not found")
    web_stub.HTTPError = RuntimeError
    web_stub.ctx = types.SimpleNamespace(status="")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=types.SimpleNamespace(log=lambda *args, **kwargs: None),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


def test_feishu_cli_auth_start_uses_codex_split_flow_and_qrcode(tmp_path):
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    tool = FeishuCli({"cwd": str(tmp_path)})
    start_payload = {
        "device_code": "device-code-123",
        "verification_url": "https://open.feishu.cn/open-apis/authen/v1/device?user_code=ABCD",
    }
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=["lark-cli"]), \
            patch.object(FeishuCli, "_safe_run") as safe_run:
        safe_run.side_effect = [
            {
                "status": "success",
                "exitCode": 0,
                "output": json.dumps(start_payload),
                "json": start_payload,
            },
            {
                "status": "success",
                "exitCode": 0,
                "output": "qr saved",
                "json": None,
            },
        ]

        result = tool.execute({"action": "auth_login", "scope": "calendar:calendar:read"})

    assert result.status == "success"
    assert result.result["authFlow"] == "start"
    assert result.result["authRequired"] is True
    assert result.result["deviceCode"] == "device-code-123"
    assert result.result["verificationUrl"] == start_payload["verification_url"]
    assert result.result["qrCode"]["relativePath"].startswith(".ecorex/lark-auth/feishu-auth-")
    assert result.result["nextAction"] == {
        "tool": "feishu_cli",
        "action": "auth_login",
        "device_code": "device-code-123",
    }
    assert safe_run.call_args_list[0].args[0] == [
        "lark-cli",
        "auth",
        "login",
        "--scope",
        "calendar:calendar:read",
        "--no-wait",
        "--json",
    ]
    qr_command = safe_run.call_args_list[1].args[0]
    assert qr_command[:3] == ["lark-cli", "auth", "qrcode"]
    assert qr_command[3] == start_payload["verification_url"]
    assert "--output" in qr_command
    assert safe_run.call_args_list[1].args[2] <= 5


def test_feishu_cli_auth_complete_uses_device_code_without_blocking_start_flags():
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    tool = FeishuCli()
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=["lark-cli"]), \
            patch.object(FeishuCli, "_safe_run", return_value={"status": "success", "exitCode": 0, "output": "ok", "json": None}) as safe_run, \
            patch.object(FeishuCli, "_status", return_value={"authState": "ready"}):
        result = tool.execute({"action": "auth_login", "device_code": "device-code-123"})

    assert result.status == "success"
    assert result.result["authFlow"] == "complete"
    assert result.result["authCompleted"] is True
    assert safe_run.call_args.args[0] == ["lark-cli", "auth", "login", "--device-code", "device-code-123"]


def test_feishu_cli_auth_login_without_target_does_not_default_to_base():
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    tool = FeishuCli()
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=["lark-cli"]), \
            patch.object(FeishuCli, "_safe_run") as safe_run:
        result = tool.execute({"action": "auth_login"})

    assert result.status == "error"
    assert result.result["status"] == "needs_target_scope"
    assert result.result["fixedFlow"] is False
    assert result.result["nextAction"]["action"] == "agent_auth"
    safe_run.assert_not_called()


def test_feishu_cli_agent_auth_uses_official_diagnostics_before_selecting_flow():
    from agent.tools.base_tool import ToolResult
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    tool = FeishuCli()
    captured = {}

    def fake_config_init(self, args, env, timeout):
        captured.update(args)
        return ToolResult.success({
            "status": "auth_pending",
            "authRequired": True,
            "verificationUrl": "https://open.feishu.cn/page/cli?user_code=ABCD",
        })

    diagnostics = {
        "authState": "needs_login",
        "capabilities": {
            "configInitNew": True,
            "authLoginNoWaitJson": True,
            "authLoginDeviceCode": True,
        },
        "selectionPolicy": "official lark-cli diagnostics",
    }
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=["lark-cli"]), \
            patch.object(FeishuCli, "_status", return_value={"authState": "needs_login", "authenticated": False}), \
            patch.object(FeishuCli, "_official_auth_diagnostics", return_value=diagnostics), \
            patch.object(FeishuCli, "_config_init", fake_config_init):
        result = tool.execute({"action": "agent_auth", "timeout": 240})

    assert result.status == "success"
    assert captured["use_saved_credentials"] is False
    assert captured["args"] == []
    assert result.result["authDecision"] == "config_init_new_from_official_diagnostics"
    assert result.result["officialAuthDiagnostics"] == diagnostics
    assert result.result["fixedFlow"] is False


def test_feishu_cli_config_init_uses_external_credentials_over_stdin():
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    tool = FeishuCli()
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=["lark-cli"]), \
            patch.object(FeishuCli, "_safe_run", return_value={"status": "success", "exitCode": 0, "output": "ok", "json": None}) as safe_run:
        result = tool.execute({
            "action": "config_init",
            "app_id": "cli_test_app",
            "app_secret": "test-secret",
            "brand": "feishu",
        })

    assert result.status == "success"
    command = safe_run.call_args.args[0]
    assert command == [
        "lark-cli",
        "config",
        "init",
        "--app-id",
        "cli_test_app",
        "--app-secret-stdin",
        "--brand",
        "feishu",
    ]
    assert safe_run.call_args.kwargs["input_text"] == "test-secret\n"
    assert result.result["credentialSource"] == "tool_args"


def test_feishu_cli_config_init_new_returns_url_before_cli_writeback(tmp_path):
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    fake_cli = tmp_path / "fake_lark_cli.py"
    fake_cli.write_text(
        "\n".join([
            "import sys, time",
            "args = sys.argv[1:]",
            "if args[:3] == ['config', 'init', '--new']:",
            "    sys.stderr.write('打开以下链接配置应用:\\n')",
            "    sys.stderr.write('  https://open.feishu.cn/page/cli?user_code=ABCD-1234&from=cli\\n')",
            "    sys.stderr.write('等待配置应用...\\n')",
            "    sys.stderr.flush()",
            "    time.sleep(0.8)",
            "    sys.stdout.write('{\"appId\":\"cli_test\",\"appSecret\":\"real-secret-after-writeback\",\"brand\":\"feishu\"}\\n')",
            "    sys.stdout.flush()",
            "    sys.exit(0)",
            "if args[:3] == ['auth', 'status', '--json']:",
            "    sys.stdout.write('{\"authenticated\": true}\\n')",
            "    sys.stdout.flush()",
            "    sys.exit(0)",
            "sys.stderr.write('unexpected command: ' + ' '.join(args))",
            "sys.exit(2)",
        ]),
        encoding="utf-8",
    )

    tool = FeishuCli({"cwd": str(tmp_path)})
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=[sys.executable, str(fake_cli)]), \
            patch.object(FeishuCli, "_generate_auth_qrcode", return_value={"status": "success", "path": "qr.png"}):
        started = time.monotonic()
        result = tool.execute({"action": "config_init", "timeout": 240})
        elapsed = time.monotonic() - started
        time.sleep(1.0)
        poll = tool.execute({
            "action": "config_init_status",
            "session_id": result.result["sessionId"],
            "timeout": 15,
        })

    assert elapsed < 5
    assert result.status == "success"
    assert result.result["status"] == "auth_pending"
    assert result.result["authFlow"] == "config_init_start"
    assert result.result["authRequired"] is True
    assert result.result["backgroundProcess"] is True
    assert result.result["writebackPending"] is True
    assert result.result["cliWritebackTimeoutSeconds"] == 240
    assert result.result["verificationUrl"] == "https://open.feishu.cn/page/cli?user_code=ABCD-1234&from=cli"
    assert result.result["sessionId"].startswith("lark-auth-")
    assert result.result["nextAction"] == {
        "tool": "feishu_cli",
        "action": "config_init_status",
        "session_id": result.result["sessionId"],
    }
    assert poll.status == "success"
    assert poll.result["status"] == "success"
    assert poll.result["writebackPending"] is False
    assert poll.result["authCompleted"] is True
    assert poll.result["authState"] == "ready"
    assert poll.result["authenticated"] is True
    assert result.result["stdoutLogPath"].endswith(".out.log")
    assert result.result["stderrLogPath"].endswith(".err.log")
    stdout_log = open(result.result["stdoutLogPath"], encoding="utf-8").read()
    stderr_log = open(result.result["stderrLogPath"], encoding="utf-8").read()
    assert "real-secret-after-writeback" not in stdout_log + stderr_log
    assert "appSecret" in stdout_log


def test_feishu_cli_config_init_status_requires_ready_auth_state(tmp_path):
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    fake_cli = tmp_path / "fake_lark_cli_incomplete.py"
    fake_cli.write_text(
        "\n".join([
            "import sys, time",
            "args = sys.argv[1:]",
            "if args[:3] == ['config', 'init', '--new']:",
            "    sys.stderr.write('https://open.feishu.cn/page/cli?user_code=INCOMPLETE\\n')",
            "    sys.stderr.flush()",
            "    time.sleep(0.2)",
            "    sys.stdout.write('{\"appId\":\"cli_test\",\"brand\":\"feishu\"}\\n')",
            "    sys.stdout.flush()",
            "    sys.exit(0)",
            "if args[:3] == ['auth', 'status', '--json']:",
            "    sys.stdout.write('{\"authenticated\": false}\\n')",
            "    sys.stdout.flush()",
            "    sys.exit(0)",
            "sys.exit(2)",
        ]),
        encoding="utf-8",
    )

    tool = FeishuCli({"cwd": str(tmp_path)})
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=[sys.executable, str(fake_cli)]), \
            patch.object(FeishuCli, "_generate_auth_qrcode", return_value={"status": "success", "path": "qr.png"}):
        result = tool.execute({"action": "config_init", "timeout": 240})
        time.sleep(0.5)
        poll = tool.execute({
            "action": "config_init_status",
            "session_id": result.result["sessionId"],
            "timeout": 15,
        })

    assert result.status == "success"
    assert poll.status == "error"
    assert poll.result["status"] == "auth_incomplete"
    assert poll.result["authCompleted"] is False
    assert poll.result["authenticated"] is False
    assert poll.result["authRequired"] is True


def test_feishu_cli_config_init_watchdog_kills_expired_writeback_process(tmp_path):
    import importlib

    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    module = importlib.import_module("agent.tools.feishu_cli.feishu_cli")
    fake_cli = tmp_path / "fake_lark_cli_hangs.py"
    fake_cli.write_text(
        "\n".join([
            "import sys, time",
            "args = sys.argv[1:]",
            "if args[:3] == ['config', 'init', '--new']:",
            "    sys.stderr.write('https://open.feishu.cn/page/cli?user_code=HANG\\n')",
            "    sys.stderr.flush()",
            "    time.sleep(60)",
            "    sys.exit(0)",
            "sys.exit(2)",
        ]),
        encoding="utf-8",
    )

    tool = FeishuCli({"cwd": str(tmp_path)})
    with patch("agent.tools.feishu_cli.feishu_cli._resolve_lark_command", return_value=[sys.executable, str(fake_cli)]), \
            patch.object(FeishuCli, "_generate_auth_qrcode", return_value={"status": "success", "path": "qr.png"}):
        result = tool.execute({"action": "config_init", "timeout": 1})

    session_id = result.result["sessionId"]
    process = module._AUTH_SESSIONS[session_id]["process"]
    deadline = time.time() + 6
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.1)

    assert process.poll() is not None
    poll = tool.execute({"action": "config_init_status", "session_id": session_id, "timeout": 2})
    assert poll.status == "error"
    assert poll.result["status"] == "timeout"
    assert poll.result["writebackPending"] is False


def test_feishu_cli_safe_run_redacts_json_payload():
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    raw = {
        "appId": "cli_visible_app",
        "appSecret": "raw-app-secret",
        "access_token": "raw-access-token",
        "refreshToken": "raw-refresh-token",
        "nested": {
            "authorization": "Bearer raw-auth-token",
            "device_code": "device-code-kept-for-split-flow",
            "verification_url": "https://open.feishu.cn/page/cli?user_code=ABCD",
        },
    }
    completed = subprocess.CompletedProcess(["lark-cli"], 0, json.dumps(raw), "")
    with patch("agent.tools.feishu_cli.feishu_cli._run_process", return_value=completed):
        result = FeishuCli()._safe_run(["lark-cli", "auth", "status", "--json"], {}, 5)

    serialized = json.dumps(result, ensure_ascii=False)
    assert "raw-app-secret" not in serialized
    assert "raw-access-token" not in serialized
    assert "raw-refresh-token" not in serialized
    assert "raw-auth-token" not in serialized
    assert result["json"]["appSecret"] == "***"
    assert result["json"]["access_token"] == "***"
    assert result["json"]["refreshToken"] == "***"
    assert result["json"]["nested"]["authorization"] == "***"
    assert result["json"]["nested"]["device_code"] == "device-code-kept-for-split-flow"
    assert result["json"]["nested"]["verification_url"] == "https://open.feishu.cn/page/cli?user_code=ABCD"


def test_public_redaction_hides_feishu_device_code():
    from common.ecorex_public_payload import redact_public_tool_value

    public = redact_public_tool_value({
        "deviceCode": "device-code-secret",
        "nextAction": {
            "tool": "feishu_cli",
            "action": "auth_login",
            "device_code": "device-code-secret",
        },
    })
    serialized = json.dumps(public, ensure_ascii=False)

    assert "device-code-secret" not in serialized
    assert public["deviceCode"] == "[redacted]"
    assert public["nextAction"]["device_code"] == "[redacted]"


def test_feishu_cli_display_command_redacts_app_and_device_values():
    from agent.tools.feishu_cli.feishu_cli import FeishuCli

    display = FeishuCli._display_command([
        "lark-cli",
        "config",
        "init",
        "--app-id",
        "cli_sensitive",
        "--app-secret-stdin",
        "--brand",
        "feishu",
        "--device-code",
        "device-sensitive",
    ])

    assert "cli_sensitive" not in display
    assert "device-sensitive" not in display
    assert display[display.index("--app-id") + 1] == "***"
    assert display[display.index("--device-code") + 1] == "***"


def test_feishu_cli_external_connection_status_probe_is_secret_free():
    _install_web_stub()
    from channel.web.web_channel import _safe_feishu_cli_status_probe

    safe = _safe_feishu_cli_status_probe({
        "status": "success",
        "available": True,
        "authState": "ready",
        "authenticated": True,
        "command": ["lark-cli"],
        "authStatus": {"output": "open_id=ou_secret scope=calendar:calendar:read"},
        "nextAction": {"tool": "feishu_cli", "action": "agent_auth"},
    })

    serialized = json.dumps(safe, ensure_ascii=False)
    assert safe["available"] is True
    assert safe["authState"] == "ready"
    assert "authStatus" not in safe
    assert "ou_secret" not in serialized
    assert "calendar:calendar:read" not in serialized


def test_feishu_cli_external_connection_probe_uses_structured_tool(monkeypatch, tmp_path):
    _install_web_stub()
    from agent.tools.base_tool import ToolResult
    from agent.tools.feishu_cli.feishu_cli import FeishuCli
    from channel.web import web_channel

    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: str(tmp_path))

    def fake_execute(self, args):
        assert args["action"] == "status"
        return ToolResult.success({
            "status": "success",
            "available": True,
            "authState": "ready",
            "authenticated": True,
            "authStatus": {"output": "open_id=ou_secret"},
        })

    monkeypatch.setattr(FeishuCli, "execute", fake_execute)

    safe = web_channel.ExternalConnectionActionHandler._probe_feishu_cli_status()

    assert safe["toolStatus"] == "success"
    assert safe["available"] is True
    assert safe["authState"] == "ready"
    assert "authStatus" not in safe


def test_feishu_cli_auth_url_survives_webui_tool_result_redaction():
    _install_web_stub()
    from channel.web import web_channel

    channel = web_channel.WebChannel()
    url = "https://open.feishu.cn/page/cli?user_code=ABCD-1234&from=cli"

    feishu_result, _meta = channel._bounded_tool_result_for_sse({
        "authRequired": True,
        "writebackPending": True,
        "verificationUrl": url,
        "qrCode": {"status": "success", "path": "C:/workspace/.ecorex/lark-auth/qr.png"},
        "output": f"open {url}",
    }, "feishu_cli")
    other_result, _ = channel._bounded_tool_result_for_sse({
        "verificationUrl": url,
        "output": f"open {url}",
    }, "remote_tool")

    assert url in feishu_result
    assert "writebackPending" in feishu_result
    assert "C:/workspace/.ecorex/lark-auth/qr.png" not in feishu_result
    assert url not in other_result


def test_feishu_auth_restore_does_not_readd_raw_device_code():
    _install_web_stub()
    from channel.web import web_channel

    channel = web_channel.WebChannel()
    url = "https://open.feishu.cn/page/cli?user_code=ABCD-1234&from=cli"
    result, _meta = channel._bounded_tool_result_for_sse({
        "authRequired": True,
        "verificationUrl": url,
        "deviceCode": "device-code-secret",
        "nextAction": {
            "tool": "feishu_cli",
            "action": "auth_login",
            "device_code": "device-code-secret",
        },
    }, "feishu_cli")

    assert url in result
    assert "device-code-secret" not in result
    assert "[redacted]" in result


def test_feishu_external_connection_agent_auth_returns_visible_cli_link(monkeypatch):
    _install_web_stub()
    from agent.tools.base_tool import ToolResult
    from agent.tools.feishu_cli.feishu_cli import FeishuCli
    from channel.web import web_channel

    url = "https://open.feishu.cn/page/cli?user_code=ABCD-1234&from=cli"
    monkeypatch.setattr(web_channel, "conf", lambda: {})
    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: "C:/workspace")
    monkeypatch.setattr(web_channel, "record_external_connection_runtime_event", lambda *args, **kwargs: None)

    def fake_execute(self, args):
        assert args["action"] == "agent_auth"
        assert args["surface"] == "web_external_connection"
        return ToolResult.success({
            "status": "auth_pending",
            "authRequired": True,
            "writebackPending": True,
            "verificationUrl": url,
            "qrCode": {"status": "success", "path": "C:/workspace/.ecorex/lark-auth/qr.png", "relativePath": ".ecorex/lark-auth/qr.png"},
            "json": {"appSecret": "raw-secret"},
        })

    monkeypatch.setattr(FeishuCli, "execute", fake_execute)

    payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_agent_auth("feishu", {}))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "success"
    assert payload["verificationUrl"] == url
    assert payload["agentAuth"]["verificationUrl"] == url
    assert payload["agentAuth"]["writebackPending"] is True
    assert payload["agentAuth"]["qrCode"]["relativePath"] == ".ecorex/lark-auth/qr.png"
    assert "stdoutLogPath" not in payload["agentAuth"]
    assert "stderrLogPath" not in payload["agentAuth"]
    assert "C:/workspace/.ecorex/lark-auth/qr.png" not in serialized
    assert "raw-secret" not in serialized


def test_feishu_external_connection_agent_auth_status_polls_session(monkeypatch):
    _install_web_stub()
    from agent.tools.base_tool import ToolResult
    from agent.tools.feishu_cli.feishu_cli import FeishuCli
    from channel.web import web_channel

    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: "C:/workspace")
    monkeypatch.setattr(web_channel, "record_external_connection_runtime_event", lambda *args, **kwargs: None)

    def fake_execute(self, args):
        assert args["action"] == "config_init_status"
        assert args["session_id"] == "lark-auth-test"
        return ToolResult.success({
            "status": "success",
            "sessionId": "lark-auth-test",
            "authCompleted": True,
            "authenticated": True,
            "authState": "ready",
            "writebackPending": False,
            "verificationUrl": "https://open.feishu.cn/page/cli?user_code=ABCD",
            "stdoutLogPath": "C:/workspace/.ecorex/lark-auth/raw.out.log",
            "json": {"appSecret": "raw-secret"},
        })

    monkeypatch.setattr(FeishuCli, "execute", fake_execute)

    payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_agent_auth_status("feishu", {
        "sessionId": "lark-auth-test",
    }))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "success"
    assert payload["sessionId"] == "lark-auth-test"
    assert payload["authCompleted"] is True
    assert payload["authenticated"] is True
    assert payload["agentAuth"]["authState"] == "ready"
    assert "raw-secret" not in serialized
    assert "stdoutLogPath" not in payload["agentAuth"]


def test_feishu_web_console_auth_poll_has_failed_state_contract():
    console_source = Path("channel/web/static/js/console.js").read_text(encoding="utf-8")
    web_source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")

    assert "const failed = !pending && !completed" in console_source
    assert "auth_incomplete" in console_source
    assert "fa-xmark text-red-500" in console_source
    assert "showFeishuCliAuthFailedNotice" in web_source
    assert "agent_auth_status" in web_source


def test_feishu_external_connection_projection_exposes_cli_auth_action():
    _install_web_stub()
    from channel.web import web_channel

    connection = web_channel._external_connection_from_channel({
        "name": "feishu",
        "label": {"zh": "飞书", "en": "Feishu / Lark"},
        "auth": {"agentAuthSupported": True},
        "agentSurface": {"tool": "feishu_cli", "callable": False},
    })

    actions = {item["id"]: item for item in connection["actions"]}
    assert actions["agent_auth"]["enabled"] is True
    assert actions["agent_auth"]["label"] == "Agent 授权"
    assert actions["agent_auth"]["discoveryDriven"] is True


def test_external_connection_agent_auth_action_is_catalog_driven_not_feishu_special_case():
    _install_web_stub()
    from channel.web import web_channel

    connection = web_channel._external_connection_from_channel({
        "name": "slack",
        "label": {"zh": "Slack", "en": "Slack"},
        "auth": {
            "agentAuthSupported": True,
            "agentAuthorizationAction": {"tool": "slack_cli", "action": "agent_auth"},
        },
        "agentSurface": {"tool": "slack_cli", "callable": False},
    })

    actions = {item["id"]: item for item in connection["actions"]}
    assert actions["agent_auth"]["enabled"] is True
    assert actions["agent_auth"]["tool"] == "slack_cli"
    assert actions["agent_auth"]["discoveryDriven"] is True


def test_channel_catalog_exposes_agent_discovery_contract_for_all_external_connections():
    from channel.channel_catalog import CHANNEL_CATALOG, channel_auth_surface

    for name in CHANNEL_CATALOG:
        auth = channel_auth_surface({}, name)
        contract = auth["agentDiscoveryContract"]
        assert contract["version"] == "external-connection-agent-discovery-v1"
        assert contract["discoveryDriven"] is True
        assert contract["webOwnsInstallOrAuthFlow"] is False
    feishu = channel_auth_surface({}, "feishu")
    assert feishu["agentAuthorizationAction"]["action"] == "agent_auth"
    assert feishu["agentDiscoveryContract"]["officialDiagnosticsRequired"] is True
    assert feishu["agentDiscoveryContract"]["declaredTool"] == "feishu_cli"

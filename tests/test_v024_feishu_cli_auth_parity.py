import json
import sys
import types
from unittest.mock import patch


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
        "nextAction": {"tool": "feishu_cli", "action": "auth_login", "domain": "base"},
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

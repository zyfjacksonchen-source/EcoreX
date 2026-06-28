import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_lark_oapi_probe_classifies_missing_sdk(monkeypatch):
    from common import feishu_runtime_readiness as readiness

    monkeypatch.setattr(readiness.importlib.util, "find_spec", lambda name: None if name == "lark_oapi" else None)

    status = readiness.feishu_dependency_status({
        "feishu_app_id": "cli_masked_test",
        "feishu_app_secret": "secret-value",
        "feishu_event_mode": "websocket",
    })

    assert status["status"] == "missing"
    assert status["sdkPresent"] is False
    assert status["credentialPresent"] is True
    assert status["credentialValid"] == "unknown"
    assert status["remoteConnectivityProbed"] is False
    assert "pythonExecutable" not in status


def test_feishu_connect_missing_sdk_saves_credentials_without_start(monkeypatch):
    _install_web_stub()
    from channel.web import web_channel

    fake_conf = {"channel_type": "web"}
    saved = {}
    events = []
    web_channel.ChannelsHandler.CHANNEL_RUNTIME_STATE.clear()

    monkeypatch.setattr(web_channel, "conf", lambda: fake_conf)
    monkeypatch.setattr(web_channel.ChannelsHandler, "_read_file_config", classmethod(lambda cls: dict(fake_conf)))

    def fake_write(cls, data):
        saved.clear()
        saved.update(data)

    monkeypatch.setattr(web_channel.ChannelsHandler, "_write_file_config_atomic", classmethod(fake_write))
    monkeypatch.setattr(web_channel.ChannelsHandler, "_refresh_runtime_capabilities", staticmethod(lambda reason: None))
    monkeypatch.setattr(
        web_channel.ChannelsHandler,
        "_feishu_dependency_status",
        staticmethod(lambda config: {
            "status": "missing",
            "dependency": "lark_oapi",
            "sdkPresent": False,
            "credentialPresent": True,
            "credentialValid": "unknown",
            "remoteConnectivityProbed": False,
        }),
    )
    monkeypatch.setattr(
        web_channel,
        "record_external_connection_runtime_event",
        lambda platform, event_type, payload, operation_id=None: events.append((platform, event_type, payload)),
    )

    payload = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {
        "feishu_app_id": "cli_test_value",
        "feishu_app_secret": "secret-value",
    }))

    assert payload["status"] == "blocked"
    assert payload["reason"] == "dependency_missing"
    assert payload["configured"] is True
    assert payload["dependencyStatus"]["dependency"] == "lark_oapi"
    assert saved["feishu_app_id"] == "cli_test_value"
    assert saved["feishu_app_secret"] == "secret-value"
    assert "feishu" not in saved.get("channel_type", "")
    runtime = web_channel.ChannelsHandler.CHANNEL_RUNTIME_STATE["feishu"]
    assert runtime["status"] == "dependency_missing"
    assert runtime["last_error"] == ""
    assert events and events[0][1] == "external_connection.lifecycle.dependency_missing"


def test_feishu_masked_app_id_is_not_rewritten():
    _install_web_stub()
    from channel.web.web_channel import ChannelsHandler

    ch_def = {
        "fields": [
            {"key": "feishu_app_id", "label": "App ID", "type": "text"},
            {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
            {"key": "allow_all_users", "label": "Allow all users", "type": "bool"},
        ]
    }

    applied, skipped = ChannelsHandler._collect_channel_config_updates(ch_def, {
        "feishu_app_id": "cli_****xxxx",
        "feishu_app_secret": "****",
        "allow_all_users": True,
    })

    assert applied == {"allow_all_users": True}
    assert skipped == 2


def test_feishu_values_are_redacted_from_public_payload_and_ledger():
    from common.ecorex_public_payload import mask_sensitive_text, redact_public_tool_value
    from agent.protocol.run_event_ledger import _redact_sensitive_text

    raw = (
        "app_id=cli_abcd123456 open_id=ou_secret12345 chat_id=oc_secret12345 "
        "tenant_access_token=tenant-secret https://open.feishu.cn/qr/secret "
        r"file_key=file_v3_secret12345678 image_key=img_v3_secret12345678 "
        r"path=C:\Users\alice\tmp\file_v3_secret12345678.pdf"
    )
    public = mask_sensitive_text(raw, max_chars=2000)
    ledger = _redact_sensitive_text(raw)
    payload = redact_public_tool_value({
        "app_id": "cli_abcd123456",
        "homeChannel": {"id": "oc_secret12345"},
        "message": raw,
    })

    combined = json.dumps([public, ledger, payload], ensure_ascii=False)
    assert "cli_abcd123456" not in combined
    assert "ou_secret12345" not in combined
    assert "oc_secret12345" not in combined
    assert "tenant-secret" not in combined
    assert "https://open.feishu.cn/qr/secret" not in combined
    assert "file_v3_secret12345678" not in combined
    assert "img_v3_secret12345678" not in combined
    assert r"C:\Users\alice" not in combined


def test_feishu_channel_log_helpers_redact_identifiers():
    _install_web_stub()
    from channel.feishu import feishu_channel

    raw_event = {
        "message": {
            "message_id": "om_secret12345678",
            "chat_id": "oc_secret12345678",
            "chat_type": "group",
            "message_type": "text",
            "content": "{\"text\":\"private message body\"}",
            "mentions": [{"id": {"open_id": "ou_secret12345678"}}],
        },
        "sender": {"sender_id": {"open_id": "ou_sender12345678"}},
        "app_id": "cli_secret12345678",
    }

    summary = feishu_channel._feishu_event_log_summary(raw_event)
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "om_secret12345678" not in serialized
    assert "oc_secret12345678" not in serialized
    assert "ou_sender12345678" not in serialized
    assert "private message body" not in serialized
    assert summary["messageRef"].startswith("hmac:")
    assert summary["chatRef"].startswith("hmac:")
    assert feishu_channel._feishu_log_text("app_id=cli_secret12345678") != "app_id=cli_secret12345678"
    assert feishu_channel._feishu_log_presence("secret-value") == "present"
    register_summary = feishu_channel._feishu_register_status_summary({
        "status": "created",
        "url": "https://open.feishu.cn/qr/secret",
        "app_id": "cli_secret12345678",
        "client_secret": "client-secret",
        "message": "open_id=ou_secret12345678",
    })
    register_serialized = json.dumps(register_summary, ensure_ascii=False)
    assert "https://open.feishu.cn/qr/secret" not in register_serialized
    assert "cli_secret12345678" not in register_serialized
    assert "client-secret" not in register_serialized
    assert "ou_secret12345678" not in register_serialized
    api_summary = feishu_channel._feishu_api_response_log_summary({
        "code": 0,
        "request_id": "req_secret12345678",
        "data": {
            "file_key": "file_v3_secret12345678",
            "image_key": "img_v3_secret12345678",
            "message_id": "om_secret12345678",
        },
    })
    api_serialized = json.dumps(api_summary, ensure_ascii=False)
    assert "file_v3_secret12345678" not in api_serialized
    assert "img_v3_secret12345678" not in api_serialized
    assert "om_secret12345678" not in api_serialized
    assert "req_secret12345678" not in api_serialized


def test_feishu_channel_source_has_no_legacy_raw_log_templates():
    source = (ROOT / "channel" / "feishu" / "feishu_channel.py").read_text(encoding="utf-8")
    message_source = (ROOT / "channel" / "feishu" / "feishu_message.py").read_text(encoding="utf-8")

    assert "或点击链接创建: {qr_url}" not in source
    assert "logger.debug(f\"[FeiShu] receive request: {request}\")" not in source
    assert "Image cached for session {session_id}" not in source
    assert "File cached for session {session_id}" not in source
    assert "register_app status: {info}" not in source
    assert "Downloaded single image, key={image_key}, path={image_path}" not in message_source
    assert "Image downloaded from post message, key={image_key}, path={image_path}" not in message_source
    assert "Received post message with {len(image_keys)} image(s) and text: {self.content}" not in message_source
    assert "audio message: file_key={file_key}, save_path={self.content}" not in message_source
    assert "downloading audio: file_key={file_key}, msg_id={self.msg_id}" not in message_source
    assert "Failed to download file, file_ref=%s, status=%s, res=%s" not in message_source
    assert "Failed to download audio, file_ref=%s, status=%s, res=%s" not in message_source
    assert "Failed to get video duration via ffprobe: {result.stderr}" not in source
    assert "Failed to get video duration: {e}" not in source
    assert "_feishu_log_text(e)" not in source
    assert "exc_info=True" not in source
    assert "websocket handle message error: %s" not in source
    assert "Websocket client error: %s" not in source
    assert "Stream: send card failed: %s" not in source
    assert "Stream: create/send card exception: {e}" not in source
    assert "Stream: update text failed: {res_json}" not in source
    assert "Stream: finalize card (close+summary) failed: {res_json}" not in source
    assert "upload failed: %s" not in source
    assert "upload video exception: %s" not in source
    assert "upload audio exception: %s" not in source
    assert "upload file exception: %s" not in source
    assert "upload_response.content" not in source
    assert "response.text" not in source
    assert "response.text" not in message_source
    assert "_feishu_msg_log_text(e)" not in message_source


def test_host_diagnostics_mask_redacts_feishu_runtime_log_values():
    from agent.tools.host_diagnostics.host_diagnostics import _mask

    raw = (
        "app_id=cli_secret12345678 open_id=ou_secret12345678 "
        "chat_id=oc_secret12345678 message_id=om_secret12345678 "
        "tenant_access_token=tenant-secret https://open.feishu.cn/qr/secret "
        "data:image/png;base64,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
        r"file_key=file_v3_secret12345678 image_key=img_v3_secret12345678 "
        r"path=C:\Users\alice\tmp\file_v3_secret12345678.pdf"
    )

    masked = _mask(raw)
    assert "cli_secret12345678" not in masked
    assert "ou_secret12345678" not in masked
    assert "oc_secret12345678" not in masked
    assert "om_secret12345678" not in masked
    assert "tenant-secret" not in masked
    assert "https://open.feishu.cn/qr/secret" not in masked
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in masked
    assert "file_v3_secret12345678" not in masked
    assert "img_v3_secret12345678" not in masked
    assert r"C:\Users\alice" not in masked


def test_webui_packaging_requires_lark_oapi_runtime_install():
    packaging = (ROOT / "scripts" / "prepare-ecorex-webui-local-release.ps1").read_text(encoding="utf-8")
    v023_contract = (ROOT / "scripts" / "smoke-v023-install-packaging-contracts.py").read_text(encoding="utf-8")
    hotfix_contract = (ROOT / "scripts" / "smoke-web-hotfix-contracts.py").read_text(encoding="utf-8")

    assert 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"' in packaging
    assert 'Ensure-PythonDependency -Python $python -StateDir $stateDir -ModuleName "lark_oapi"' not in packaging
    assert 'Write-OptionalPythonDependencyNotice -StateDir $stateDir -ModuleName "lark_oapi"' in packaging
    assert 'StartsWith("lark-oapi")' not in packaging
    assert "windows package preinstalls lark_oapi before first-run" in v023_contract
    assert "windows webui package preinstalls active runtime lark_oapi" in hotfix_contract

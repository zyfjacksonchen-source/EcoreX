from __future__ import annotations

import json
import os
import sys
import types

from agent.tools.base_tool import ToolResult


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


def test_tencent_docs_mcp_config_is_local_and_status_is_redacted(tmp_path, monkeypatch):
    from channel.web import web_channel

    monkeypatch.setattr(web_channel, "conf", lambda: {"agent_workspace": str(tmp_path)})
    monkeypatch.setattr(
        web_channel,
        "_tencent_docs_tool_snapshot",
        lambda start=False: {"runtimeStatus": "ready", "toolCount": 2, "contentToolCount": 1, "tools": []},
    )

    web_channel._write_tencent_docs_mcp_config("secret-token-abc")

    payload = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    entry = payload["mcpServers"]["tencent-docs"]
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "https://docs.qq.com/openapi/mcp"
    assert entry["headers"]["Authorization"] == "Bearer secret-token-abc"

    status = web_channel._tencent_docs_status_payload(start=False)
    public_json = json.dumps(status, ensure_ascii=False)
    assert status["capability"]["redacted"] is True
    assert status["capability"]["endpoint"] == "https://docs.qq.com/openapi/mcp"
    assert status["capability"]["authUrl"] == "https://docs.qq.com/open/auth/mcp.html"
    assert status["capability"]["authMode"] == "official_qr_scan_with_token_fallback"
    assert status["capability"]["qrLoginAvailable"] is True
    assert status["capability"]["tokenFallbackAvailable"] is True
    assert "secret-token-abc" not in public_json
    assert "Authorization" not in public_json

    assert web_channel._remove_tencent_docs_mcp_config() is True
    after_remove = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert "tencent-docs" not in after_remove.get("mcpServers", {})


def test_tencent_docs_mcp_waits_for_ready_after_explicit_start(monkeypatch):
    from channel.web import web_channel

    calls = []
    snapshots = [
        {"runtimeStatus": "pending", "toolCount": 0, "contentToolCount": 0, "tools": []},
        {"runtimeStatus": "ready", "toolCount": 2, "contentToolCount": 1, "tools": []},
    ]

    def fake_snapshot(start=False):
        calls.append(start)
        return snapshots.pop(0) if snapshots else {"runtimeStatus": "ready", "toolCount": 2, "contentToolCount": 1, "tools": []}

    monkeypatch.setattr(web_channel, "_tencent_docs_tool_snapshot", fake_snapshot)

    payload = web_channel._tencent_docs_wait_for_ready(timeout_seconds=0.2, interval_seconds=0.01)

    assert payload["runtimeStatus"] == "ready"
    assert payload["toolCount"] == 2
    assert calls[0] is True
    assert False in calls


def test_tencent_docs_remote_attachment_context_is_not_local_file():
    from channel.web import web_channel

    refs, hidden_context = web_channel._web_attachment_prompt_refs_and_context([
        {
            "provider": "tencent-docs",
            "file_path": "tencent-docs://doc123",
            "file_name": "Q3 Plan",
            "file_id": "doc123",
            "node_id": "node9",
            "doc_type": "doc",
            "url": "https://docs.qq.com/doc/abc",
        }
    ])

    assert refs == ["[腾讯文档: Q3 Plan (doc123)]"]
    assert "remote documents, not local file paths" in hidden_context
    assert "Do not read them from the local filesystem" in hidden_context
    assert "`tencent-docs`" in hidden_context
    assert "read-only" in hidden_context
    assert "Authorization" not in hidden_context


def test_tencent_docs_file_listing_normalizes_discovered_mcp_tool_results(monkeypatch):
    from channel.web import web_channel

    class FakeTencentDocsTool:
        name = "tencent_docs_search"
        remote_name = "search_docs"
        server_name = "tencent-docs"
        description = "Search Tencent Docs documents"
        params = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            }
        }

        def __init__(self):
            self.last_args = {}

        def execute(self, args):
            self.last_args = args
            return ToolResult.success({
                "documents": [
                    {
                        "doc_id": "doc-budget",
                        "node_id": "node-budget",
                        "title": "预算复盘",
                        "type": "sheet",
                        "url": "https://docs.qq.com/sheet/budget",
                        "owner_name": "同学",
                        "updated_at": "2026-07-04T10:00:00+08:00",
                    }
                ]
            })

    fake_tool = FakeTencentDocsTool()
    monkeypatch.setattr(web_channel, "_tencent_docs_is_configured", lambda: True)
    monkeypatch.setattr(web_channel, "_tencent_docs_tool_snapshot", lambda start=False: {"runtimeStatus": "ready", "toolCount": 1, "contentToolCount": 1, "tools": []})
    monkeypatch.setattr(web_channel, "_tencent_docs_tool_candidates", lambda mode, query="": [fake_tool])

    payload = web_channel._tencent_docs_files_payload("search", "预算", 10)

    assert payload["status"] == "success"
    assert payload["redacted"] is True
    assert fake_tool.last_args == {"query": "预算", "limit": 10}
    assert payload["files"] == [
        {
            "key": "tencent-docs://doc-budget",
            "provider": "tencent-docs",
            "source": "tencent-docs",
            "file_id": "doc-budget",
            "node_id": "node-budget",
            "title": "预算复盘",
            "file_name": "预算复盘",
            "file_type": "file",
            "doc_type": "sheet",
            "url": "https://docs.qq.com/sheet/budget",
            "owner": "同学",
            "updated_at": "2026-07-04T10:00:00+08:00",
        }
    ]


def test_tencent_docs_mcp_startup_permission_only_allows_official_endpoint(tmp_path, monkeypatch):
    from common.ecorex_tool_permissions import ToolPermissionBroker

    monkeypatch.setenv("ECOREX_DESKTOP", "1")
    monkeypatch.setenv("ECOREX_DESKTOP_USER_DATA", str(tmp_path / "user-data"))
    broker = ToolPermissionBroker()

    allowed = broker.authorize_noninteractive(
        "mcp_server",
        {"server": "tencent-docs", "url": "https://docs.qq.com/openapi/mcp"},
    )
    spoof = broker.authorize_noninteractive(
        "mcp_server",
        {"server": "tencent-docs", "url": "https://docs.qq.evil.example/openapi/mcp"},
    )
    broker.set_mode("read-only")
    read_only = broker.authorize_noninteractive(
        "mcp_server",
        {"server": "tencent-docs", "url": "https://docs.qq.com/openapi/mcp"},
    )

    assert allowed == {"allowed": True, "reason": "default-tencent-docs-mcp-startup"}
    assert spoof["allowed"] is False
    assert read_only["allowed"] is False
    assert "read-only" in read_only["reason"]


def test_tencent_docs_webui_has_official_logo_and_auth_picker():
    root = os.path.join(os.path.dirname(__file__), "..")
    chat_html = open(os.path.join(root, "channel", "web", "chat.html"), encoding="utf-8").read()
    console_js = open(os.path.join(root, "channel", "web", "static", "js", "console.js"), encoding="utf-8").read()
    console_css = open(os.path.join(root, "channel", "web", "static", "css", "console.css"), encoding="utf-8").read()
    web_channel_source = open(os.path.join(root, "channel", "web", "web_channel.py"), encoding="utf-8").read()
    app_index = open(os.path.join(root, "channel", "web", "static", "app", "index.html"), encoding="utf-8").read()
    app_overlay_js = open(os.path.join(root, "channel", "web", "static", "app", "assets", "ecorex-v029-overlay.js"), encoding="utf-8").read()
    app_overlay_css = open(os.path.join(root, "channel", "web", "static", "app", "assets", "ecorex-v029-overlay.css"), encoding="utf-8").read()
    dist_index = open(os.path.join(root, "desktop", "dist", "index.html"), encoding="utf-8").read()
    logo_path = os.path.join(root, "channel", "web", "static", "logos", "tencent-docs.png")
    app_logo_path = os.path.join(root, "channel", "web", "static", "app", "assets", "logos", "tencent-docs.png")
    dist_logo_path = os.path.join(root, "desktop", "dist", "assets", "logos", "tencent-docs.png")

    assert os.path.getsize(logo_path) > 1000
    assert os.path.getsize(app_logo_path) == os.path.getsize(logo_path)
    assert os.path.getsize(dist_logo_path) == os.path.getsize(logo_path)
    assert "assets/logos/tencent-docs.png" in chat_html
    assert "openTencentDocsFlow" in chat_html
    assert "tencent_docs_auth_title" in console_js
    assert "tencent_docs_qr_title" in console_js
    assert "tencentDocsQrImageUrl" in console_js
    assert "startTencentDocsAuthPolling" in console_js
    assert "https://docs.qq.com/open/auth/mcp.html" in console_js
    assert "beginTencentDocsAuthorization" in console_js
    assert "connectTencentDocsWithToken" in console_js
    assert "/api/tencent-docs/files" in console_js
    assert "provider: 'tencent-docs'" in console_js
    assert ".tencent-docs-modal-overlay" in console_css
    assert ".att-tencent-docs" in console_css
    assert ".memory-starry-page" in console_css
    assert "ecorex-v029-overlay.css" in app_index
    assert "ecorex-v029-overlay.js" in app_index
    assert "logos/tencent-docs.png" in app_overlay_js
    assert "openTencentDocs" in app_overlay_js
    assert "扫码授权" in app_overlay_js
    assert "qrImageUrl" in app_overlay_js
    assert "startTencentDocsAuthPolling" in app_overlay_js
    assert "ecorex-v029-token-fallback" in app_overlay_js
    assert "findNativeTencentDocsButton" in app_overlay_js
    assert "official-logo" in app_overlay_js
    assert "#ecorex-tencent-docs" in app_overlay_js
    assert "handleTencentDocsHash" in app_overlay_js
    assert "/api/tencent-docs/status?start=1" in app_overlay_js
    assert "/api/knowledge/graph" in app_overlay_js
    assert "memory-starry-legend" in app_overlay_js
    assert "categoryColor" in app_overlay_js
    assert "memory-starry-stars" in app_overlay_js
    assert ".connection-logo.is-tencent-docs" in app_overlay_css
    assert ".ecorex-v029-qr-auth" in app_overlay_css
    assert ".memory-starry-body" in app_overlay_css
    assert ".memory-starry-legend" in app_overlay_css
    assert ".memory-starry-page" in app_overlay_css
    assert "assets/index-" in dist_index
    assert "if (enterpriseClientConfigured()) {\n        try {\n          adminPayload = await clientJson(\"/auth/login\"" in web_channel_source
    assert "function runtimeAuthHeaders()" in web_channel_source
    assert 'fetch(runtimePath("/upload"), { method: "POST", credentials: "same-origin", headers: runtimeAuthHeaders(), body: form })' in web_channel_source


def test_web_auth_login_returns_session_without_password(monkeypatch):
    from channel.web import auth, web_channel

    monkeypatch.setattr(web_channel, "_is_password_enabled", lambda: False)
    monkeypatch.setattr(web_channel, "_web_device_id", lambda: "test-device")
    monkeypatch.setattr(web_channel, "_session_expire_seconds", lambda: 86400)
    monkeypatch.setattr(auth.web, "data", lambda: json.dumps({"email": "user@example.com"}).encode("utf-8"))
    monkeypatch.setattr(auth.web, "header", lambda *args, **kwargs: None)

    payload = json.loads(auth.AuthLoginHandler().POST())

    assert payload["status"] == "success"
    assert payload["auth_required"] is False
    assert payload["session"]["user"]["email"] == "user@example.com"
    assert payload["session"]["identitySource"] == "login-email"


def test_web_session_share_payload_preserves_safe_artifact_media():
    from channel.web import web_channel

    artifact = web_channel._session_share_artifact_payload({
        "title": r"C:\Users\Alice\output\cover.png",
        "kind": "image",
        "fileName": "cover.png",
        "mimeType": "image/png",
        "sizeBytes": 4096,
        "mediaUrl": "https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/cover.png",
        "url": "file:///C:/Users/Alice/output/cover.png",
    })

    assert artifact["title"] == "[local-path]"
    assert artifact["fileName"] == "cover.png"
    assert artifact["mimeType"] == "image/png"
    assert artifact["sizeBytes"] == 4096
    assert artifact["mediaUrl"] == "https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/cover.png"
    assert "url" not in artifact


def test_web_session_share_payload_keeps_structured_messages_and_real_artifacts():
    from channel.web import web_channel

    message = web_channel._session_share_message_payload({
        "role": "assistant",
        "content": [{"type": "text", "text": r"已生成 C:\Users\Alice\output\cover.png token=abc123"}],
        "artifacts": [
            {
                "title": "cover.png",
                "kind": "image",
                "mimeType": "image/png",
                "mediaUrl": "data:image/png;base64," + ("a" * 1200),
            }
        ],
    })

    assert message is not None
    assert message["role"] == "assistant"
    assert message["content"] == "已生成 [local-path] token=[redacted]"
    assert message["artifacts"][0]["mediaUrl"].startswith("data:image/png;base64,")
    assert len(message["artifacts"][0]["mediaUrl"]) > 1000

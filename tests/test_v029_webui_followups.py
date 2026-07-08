from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_tencent_docs_entry_moves_to_external_connections_agent_flow():
    app = read_text("desktop/src/App.tsx")
    overlay = read_text("channel/web/static/app/assets/ecorex-v029-overlay.js")

    assert "tencent-docs-trigger" not in app
    assert "startTencentDocsAgentFlow" in app
    assert "在聊天中连接" in app
    assert "connection-logo is-tencent-docs" in app
    assert "https://docs.qq.com/open/auth/mcp.html" in app
    assert "/api/tencent-docs/connect" in app
    assert "/api/tencent-docs/status?start=1" in app
    assert "设置 > 外部连接" in read_text("common/ecorex_release_notes.py")

    enhance_body = overlay.split("function enhance() {", 1)[1].split("function start()", 1)[0]
    assert "enhanceTencentLogos();" in enhance_body
    assert "enhanceComposer();" not in enhance_body
    assert "enhanceMemoryPanel();" not in enhance_body
    assert "window.EcoreXV029OpenTencentDocs = null" in overlay


def test_session_titles_filter_icon_only_tencent_docs_rows():
    app = read_text("desktop/src/App.tsx")

    assert "cleanSessionTitleForDisplay" in app
    assert "sessionTitleReadableText" in app
    assert "outgoingTitleSeed" in app
    assert "腾讯文档：" in app
    assert "if (!messagesRef.current.length)" in app
    assert "void refreshSessionFromHistory(row.id);" in app


def test_memory_page_no_longer_hosts_knowledge_graph_or_starry_tab():
    app = read_text("desktop/src/App.tsx")

    assert '"knowledge-graph"' in app
    assert 'label: "知识图谱"' in app

    memory_block = app.split('{settingsSection === "memory" && (', 1)[1].split('{settingsSection === "knowledge-graph" && (', 1)[0]
    graph_block = app.split('{settingsSection === "knowledge-graph" && (', 1)[1].split('{settingsSection === "diagnostics" && (', 1)[0]

    assert "knowledge-graph-grid" not in memory_block
    assert "星空图谱" not in app
    assert "knowledge-graph-grid is-dedicated" in graph_block
    assert "has-selection" in graph_block
    assert "clearSelectedKnowledgeNode" in graph_block
    assert "event.stopPropagation();" in graph_block
    assert "正文摘要" in graph_block
    assert "knowledgeCategoryLabel" in app
    assert "knowledge-related-list" not in graph_block
    assert "相邻知识页" not in graph_block


def test_retouch_canvas_matches_reference_editor_controls():
    component = read_text("desktop/src/components/ImageRetouchCanvas.tsx")
    css = read_text("desktop/src/styles/app.css")
    api = read_text("desktop/src/services/ecorexApi.ts")

    assert "annotationPath" in component
    assert "arrowHeadPath" in component
    assert "stageMetrics" in component
    assert "labelLayout" in component
    assert '" has-stage"' in component
    assert "exportMetrics.imageX" in component
    assert "<line" not in component
    assert "<marker" not in component
    assert "image-retouch-bottom-toolbar" in component
    assert "image-retouch-style-panel" in component
    assert "image-retouch-stage-inner" in component
    assert "handleStageWheel" in component
    assert "setClampedZoom" in component
    assert "ZoomIn" in component
    assert "ZoomOut" in component
    assert 'useState<AnnotationTextSize>("L")' in component
    assert "COLOR_SWATCHES" in component
    assert "TEXT_SIZE_MAP" in component
    assert "加入聊天框" in component

    app = read_text("desktop/src/App.tsx")
    assert "imageRetouchDraftBlock" in app
    assert "appendImageRetouchDraft" in app
    assert "不要对同一标注图重复调用" in app
    assert "不要重复调用工具或重复发送同一产物" in app
    assert 'return `${kind}:file:${imageNameKey}`;' in app
    assert 'source: "image-retouch"' in app
    assert "const previewUrl = attachmentPreviewUrl(file);" in app
    assert "{previewUrl ? <img src={previewUrl} alt=\"\" /> : fileIcon(file)}" in app
    assert "startImageJob" not in app
    assert "runtimeBaseUrl" in api
    assert "currentHttpPort" in api
    assert "function runtimePathPrefix()" in api
    assert 'const markers = ["/app", "/chat", "/auth", "/message", "/upload", "/uploads", "/api", "/poll", "/stream", "/cancel", "/config", "/assets"];' in api
    assert "return `${runtimeBaseUrl(webPort)}/upload`" in api
    assert "function webRuntimeAuthHeaders()" in api
    assert "headers: webRuntimeAuthHeaders()" in api

    assert ".image-retouch-sheet.is-editor" in css
    assert ".image-retouch-bottom-toolbar" in css
    assert ".image-retouch-style-panel" in css
    assert "z-index: 140" in css
    assert "--retouch-stage-zoom" in css
    assert ".image-retouch-stage-inner" in css
    assert "overflow: auto" in css
    assert ".image-retouch-image-wrap.has-stage" in css
    assert "grid-template-rows: 34px minmax(0, 1fr)" in css
    assert ".image-retouch-sheet.is-editor .image-retouch-overlay path" in css


def test_webui_project_sessions_are_retained_across_restart_cache_pruning():
    app = read_text("desktop/src/App.tsx")

    assert "SESSION_UI_PROJECT_RETAINED_SESSIONS" in app
    assert "isProjectSessionUiState" in app
    assert 'sessionId.startsWith("ecorex-project-")' in app
    assert "projectEntries" in app
    assert "filter(([sessionId, value]) => !isProjectSessionUiState(sessionId, value))" in app
    assert "persistActiveSessionUiStateNow" in app
    assert 'window.addEventListener("pagehide", persist);' in app
    assert 'window.addEventListener("beforeunload", persist);' in app
    assert 'document.addEventListener("visibilitychange", persistWhenHidden);' in app


def test_webui_online_update_uses_ready_dialog_instead_of_confusing_banner():
    app = read_text("desktop/src/App.tsx")
    css = read_text("desktop/src/styles/app.css")

    assert "showBackgroundUpdateDialog" in app
    assert "新版本已安装完成" in app
    assert "立即更新" in app
    assert 'state.status === "installed" || state.status === "activated"' in app
    assert "state.healthCheck?.passed === true || state.healthCheck?.status === \"pass\"" in app
    assert "showRuntimeUpdateStateBanner" in app
    assert "runtimeUpdateStateDetails" in app
    assert "正在下载更新" in app
    assert "更新已延迟" in app
    assert "已回滚到稳定版本" in app
    assert "发现 EcoreX 新版本" in app
    assert "查看更新" in app

    assert ".update-ready-dialog" in css
    assert ".update-state-banner" in css
    assert ".update-state-banner.is-failed" in css
    update_banner_block = css.split(".update-banner {", 1)[1].split(".update-banner.is-hidden", 1)[0]
    assert "border: 1px solid" in update_banner_block
    assert "border-radius: 16px" in update_banner_block
    update_ready_block = css.split(".update-ready-dialog {", 1)[1].split(".update-ready-dialog > div:first-child", 1)[0]
    assert "border: 1px solid" in update_ready_block
    assert "border-radius: 18px" in update_ready_block


def test_webui_message_context_guards_gemini_provider_identity_drift():
    web_channel = read_text("channel/web/web_channel.py")

    assert "WEBUI_IDENTITY_GUARD_CONTEXT" in web_channel
    assert "You are 小芯, the AI Agent for EcoreX WebUI" in web_channel
    assert "Do not claim to be Gemini, Google DeepMind, Antigravity" in web_channel
    assert "_append_hidden_context(WEBUI_IDENTITY_GUARD_CONTEXT, hidden_context)" in web_channel


def test_webui_enterprise_token_auth_syncs_model_policy_server_side():
    web_channel = read_text("channel/web/web_channel.py")

    assert "_sync_enterprise_model_policy_payload(payload, cache_key)" in web_channel
    assert "def _sync_enterprise_model_policy_payload" in web_channel
    assert "Enterprise model policy synced from authenticated user token" in web_channel
    assert 'handler = globals().get("ModelsHandler")' in web_channel
    assert "handler._write_file_config(file_cfg)" in web_channel
    assert "handler._reset_bridge()" in web_channel

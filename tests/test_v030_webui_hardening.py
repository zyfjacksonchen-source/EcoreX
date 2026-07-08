from __future__ import annotations

import json
import hashlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def read_bytes(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def test_v030_active_turn_control_defaults_to_replace_and_queue_is_explicit():
    app = read_text("desktop/src/App.tsx")
    api = read_text("desktop/src/services/ecorexApi.ts")
    web_channel = read_text("channel/web/web_channel.py")

    assert 'type ActiveTurnMode = ChatInterruptMode' in app
    assert 'async function sendNow(skipCapabilityCheck = false, interruptMode: ActiveTurnMode = "replace")' in app
    assert "void sendNow(false, mode);" in app
    assert "更新任务" in app
    assert "排队稍后执行" in app
    assert "新开分支" in app
    assert "更新当前任务" in app
    assert "提到队首" in app
    assert "取消排队" in app
    assert 'interrupt_mode: input.interruptMode || "replace"' in api
    assert 'if interrupt_mode not in {"replace", "amend", "queue", "branch"}:' in web_channel
    assert 'interrupt_mode = "replace"' in web_channel
    assert 'if interrupt_mode == "branch":' in web_channel
    assert 'if interrupt_mode != "queue":' in web_channel


def test_v030_image_artifacts_are_stably_indexed_and_sorted():
    image_job = read_text("agent/protocol/image_job_service.py")
    runtime_projection = read_text("agent/protocol/runtime_projection.py")
    imagegen = read_text("agent/tools/imagegen/imagegen.py")
    app = read_text("desktop/src/App.tsx")
    api = read_text("desktop/src/services/ecorexApi.ts")

    assert '"task_index": max(0, int(index or 0))' in image_job
    assert '"artifact_index": max(0, int(artifact_index or 0))' in image_job
    assert "def _artifact_sort_key" in image_job
    assert "state.artifacts = _sort_artifacts_for_display(state.artifacts)" in image_job
    assert "def _projection_artifact_sort_key" in runtime_projection
    assert '"task_index"' in runtime_projection
    assert '"artifact_index"' in runtime_projection
    assert 'suffix_parts.append(f"t{task_ordinal + 1:02d}")' in imagegen
    assert 'suffix_parts.append(f"i{artifact_ordinal + 1:02d}")' in imagegen
    assert "sortNormalizedArtifacts" in app
    assert "task_index?: number;" in api
    assert "artifact_index?: number;" in api


def test_v030_retouch_canvas_is_not_a_shell():
    canvas = read_text("desktop/src/components/ImageRetouchCanvas.tsx")
    app = read_text("desktop/src/App.tsx")
    css = read_text("desktop/src/styles/app.css")

    assert 'type RetouchTool = "annotate" | "rect" | "lasso" | "text" | "hand"' in canvas
    assert 'type AnnotationKind = "arrow" | "rect" | "lasso" | "text" | "image"' in canvas
    assert "selectedSources: string[]" in canvas
    assert "textEditCount: number" in canvas
    assert "handleUploadReferenceImage" in canvas
    assert "image-retouch-side-panel" in canvas
    assert "image-retouch-image-picker" in canvas
    assert "lassoPath" in canvas
    assert "drawUploadedImage" in canvas
    assert "文字修改约束" in app
    assert "relatedImages={imageRetouchTarget.relatedImages}" in app
    assert ".image-retouch-side-panel" in css
    assert ".image-retouch-sticker-layer" in css


def test_v030_markdown_image_paths_on_separate_lines_enter_artifact_shelf():
    message_content = read_text("desktop/src/components/MessageContent.tsx")

    assert r'\\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[\\s)\\]' in message_content
    assert r'\\.(${ARTIFACT_FILE_EXTENSIONS}))(?:[\\s)\\]' in message_content
    assert "looseLocalPattern" in message_content
    assert "imageArtifactsToLegacyArtifacts(extractImageArtifacts(content))" in message_content
    assert "fileType: \"image\"" in message_content
    assert "imagePreviewFallback" in message_content
    assert "const openBlocked = !artifactActionAllowed(availabilityStatus)" in message_content
    assert "onImageRetouchRequest?.(artifact" in message_content


def test_v030_update_state_machine_and_installer_statuses_are_visible():
    manifest = json.loads(read_text("deploy/ecorex-site/manifest.json"))
    app = read_text("desktop/src/App.tsx")
    api = read_text("desktop/src/services/ecorexApi.ts")
    web_channel = read_text("channel/web/web_channel.py")
    packager = read_text("scripts/prepare-ecorex-webui-local-release.ps1")

    assert manifest["version"] == "0.3.0"
    assert manifest["releaseIndex"] == "release-index.json"
    assert manifest["download"]["mode"] == "github-cn-primary"
    assert [mirror["id"] for mirror in manifest["download"]["mirrors"][:2]] == [
        "ecorex-ghproxy-net-v0.3.0",
        "ecorex-origin-v0.3.0",
    ]
    assert manifest["download"]["mirrors"][0]["pathMode"] == "fileName"
    assert manifest["download"]["mirrors"][1]["pathMode"] == "href"
    assert manifest["update"]["webui"]["stateMachine"] == [
        "available",
        "downloading",
        "verified",
        "staged",
        "deferred",
        "failed",
        "installed",
        "activated",
        "rollback",
    ]
    assert 'status?: "available" | "downloading" | "verified" | "staged" | "deferred" | "installed" | "activated" | "failed" | "rollback" | string;' in api
    assert "runtimeUpdateStateDetails" in app
    assert "showRuntimeUpdateStateBanner" in app
    assert "查看日志" in app
    assert "立即切换" in app
    assert '"available",' in web_channel
    assert '"activated",' in web_channel
    assert "ECOREX_WEB_NO_BROWSER" in web_channel
    assert "ECOREX_UPDATE_MODE" in web_channel
    assert "web_auto_open" in web_channel
    assert "web_auto_open = (-not $NoBrowser -and -not $backgroundUpdate)" in packager
    assert "ECOREX_WEB_NO_BROWSER" in packager
    assert 'RUNTIME_DIR="$INSTALL_ROOT/runtime"' not in packager
    assert 'CURRENT_RUNTIME_PATH="$STATE_DIR/current-runtime.txt"' in packager
    assert 'PREVIOUS_RUNTIME_DIR=""' in packager
    assert "restart_previous_runtime()" in packager
    assert 'echo "$RUNTIME_DIR" > "$CURRENT_RUNTIME_PATH"' in packager
    assert 'external_status in ("pass", "not_applicable")' in packager
    assert "ECOREX_DOWNLOAD_DISABLE_PARALLEL" in read_text("deploy/ecorex-site/install-webui.ps1")
    assert "local-file" in read_text("scripts/smoke-v030-webui-online-update-local.ps1")
    for marker in (
        'Write-UpdateState -StateDir $stateDir -Status "available"',
        'Write-UpdateState -StateDir $stateDir -Status "downloading"',
        'Write-UpdateState -StateDir $stateDir -Status "verified"',
        'Write-UpdateState -StateDir $stateDir -Status "staged"',
        'write_update_state "available"',
        'write_update_state "downloading"',
        'write_update_state "verified"',
        'write_update_state "staged"',
    ):
        assert marker in packager


def test_v030_release_index_is_immutable_until_orchestrator_promotes_ready_metadata():
    manifest_text = read_text("deploy/ecorex-site/manifest.json")
    manifest = json.loads(manifest_text)
    release_index = json.loads(read_text("deploy/ecorex-site/release-index.json"))
    orchestrator = read_text("scripts/release-ecorex-webui-orchestrator.ps1")

    assert manifest["trust"]["releaseIndex"]["required"] is True
    assert manifest["trust"]["artifactSignaturesRequired"] is False
    assert manifest["trust"]["integrityRequired"] is True
    assert release_index["schema"] == "ecorex.release-index.v1"
    assert release_index["version"] == "0.3.0"
    assert release_index["status"] == "ready"
    assert release_index["manifest"]["sha256"] == hashlib.sha256(
        read_bytes("deploy/ecorex-site/manifest.json")
    ).hexdigest().upper()
    assert release_index["smoke"]["status"] == "pass"
    assert {artifact["id"] for artifact in release_index["artifacts"]} == {
        "webui-windows-x64",
        "webui-macos-universal",
    }
    assert all(artifact["sha256"] != "pending" and artifact["size"] > 0 for artifact in release_index["artifacts"])
    assert all(artifact["signature"]["required"] is False for artifact in release_index["artifacts"])
    assert "[switch]$RequireSignatures" in orchestrator
    assert "[switch]$IncludeWebService" in orchestrator
    assert 'signature.status = "not-required"' in orchestrator
    assert "Assert-ManifestArtifactTrust" in orchestrator
    assert 'status = "ready"' in orchestrator
    assert "Move-Item -LiteralPath $tmpPath -Destination $releaseIndexPath -Force" in orchestrator


def test_v030_admin_release_management_surfaces_trust_and_blocks_untrusted_promotions():
    admin_api = read_text("deploy/ecorex-admin-api/ecorex_admin_api.py")
    admin_js = read_text("deploy/ecorex-site/admin/admin.js")
    admin_css = read_text("deploy/ecorex-site/admin/admin.css")

    assert "def _release_index_validation" in admin_api
    assert "release-index smoke status must be pass" in admin_api
    assert "artifactSignaturesRequired" in admin_api
    assert "本轮 WebUI 本地包不要求桌面端签名" in admin_api
    assert "immutable_release_index" in admin_api
    assert "通知用户只写 admin release notice" in admin_api
    assert '"connectorHealthCheck"' in admin_api
    assert "releaseIndexLabel" in admin_js
    assert "releaseFailureDetails" in admin_js
    assert "Kill-switch" in admin_js
    assert "在线更新状态机" in admin_js
    assert "外部连接保护" in admin_js
    assert 'button.dataset.releaseAction === "notify" && button.dataset.releaseCanNotify !== "1"' in admin_js
    assert ".release-control-grid" in admin_css
    assert ".release-failures" in admin_css


def test_v030_share_payload_is_bounded_before_retrying_without_media():
    app = read_text("desktop/src/App.tsx")
    web_channel = read_text("channel/web/web_channel.py")

    assert "SHARE_PAYLOAD_SOFT_LIMIT" in app
    assert "SHARE_IMAGE_DATA_URL_LIMIT" in app
    assert "boundShareMessagesPayload" in app
    assert "stripShareMessageMedia" in app
    assert "payload too large" in app
    assert "shareMessagesPayloadWithMedia" in app
    assert "SESSION_SHARE_UPSTREAM_SOFT_LIMIT = 1_200_000" in web_channel
    assert "def _session_share_compact_body" in web_channel
    assert "_session_share_strip_message_media" in web_channel


def test_v030_external_connectors_expose_only_real_implemented_connectors():
    app = read_text("desktop/src/App.tsx")
    css = read_text("desktop/src/styles/app.css")
    api = read_text("desktop/src/services/ecorexApi.ts")
    web_channel = read_text("channel/web/web_channel.py")

    assert "FEATURED_EXTERNAL_CONNECTOR_IDS" in app
    assert "WORKBUDDY_STYLE_CONNECTOR_CANDIDATES" not in app
    assert "腾讯会议" not in app
    assert "腾讯问卷" not in app
    assert "QQ 邮箱" not in app
    assert "乐享知识库" not in app
    assert "ima 知识库" not in app
    assert "通达信" not in app
    assert "连接应用" in app
    assert "直接连接 Tencent Docs MCP" in app
    assert "startTencentDocsAgentFlow" not in app
    assert "onClick={() => void openTencentDocsFlow()}" in app
    assert "external-connector-panel" in app
    assert "external-connector-row" in app
    assert "更多真实连接器与高级配置" in app
    assert "externalConnectionsAdvancedOpen" in app
    assert ".external-connector-panel" in css
    assert ".external-connector-row" in css
    assert ".external-connector-more-row" in css
    assert ".external-connector-row.is-planned" not in css
    assert "workbuddyStyle?: boolean" in api
    assert "ecorex.external-connectors.implemented.v1" in web_channel
    assert "_IMPLEMENTED_EXTERNAL_CONNECTOR_CATALOG" in web_channel
    assert "planned" not in web_channel[web_channel.index("class ExternalConnectionsHandler"):web_channel.index("class ExternalConnectionActionHandler")]
    assert "connectStyle" in web_channel


def test_v030_configured_mcp_is_discovered_across_sessions_and_attachments():
    manager = read_text("agent/tools/tool_manager.py")
    runtime_capabilities = read_text("agent/runtime_capabilities.py")
    initializer = read_text("bridge/agent_initializer.py")
    bridge = read_text("bridge/agent_bridge.py")
    stream = read_text("agent/protocol/agent_stream.py")
    skills = read_text("agent/skills/service.py")
    extensions = read_text("agent/extensions/registry.py")
    web_channel = read_text("channel/web/web_channel.py")

    assert "def ensure_mcp_configured_loaded" in manager
    assert "def has_mcp_configured" in manager
    assert "include_config_fallback = bool(conf().get(\"mcp_auto_start\", False))" in manager
    assert "ensure_mcp(wait_seconds=0.0)" in runtime_capabilities
    assert "ensure_mcp(wait_seconds=2.0" in initializer
    assert "ensure_mcp(wait_seconds=0.0)" in bridge
    assert "ensure_mcp(wait_seconds=0.2)" in stream
    assert "ensure_mcp(wait_seconds=0.0)" in skills
    assert "ensure_mcp(wait_seconds=0.0)" in extensions
    assert "_ensure_tencent_docs_tools_for_attachments" in web_channel
    assert "_tencent_docs_wait_for_ready(timeout_seconds=4.0)" in web_channel


def test_v030_online_update_preserves_external_connectors_before_activation():
    manifest = json.loads(read_text("deploy/ecorex-site/manifest.json"))
    packager = read_text("scripts/prepare-ecorex-webui-local-release.ps1")
    app = read_text("desktop/src/App.tsx")
    api = read_text("desktop/src/services/ecorexApi.ts")
    web_channel = read_text("channel/web/web_channel.py")
    orchestrator = read_text("scripts/release-ecorex-webui-orchestrator.ps1")

    connector_health = manifest["update"]["webui"]["connectorHealthCheck"]
    assert connector_health["required"] is True
    assert "/api/external-connections" in connector_health["endpoints"]
    assert "/api/tencent-docs/status?start=1" in connector_health["endpoints"]
    assert connector_health["failureAction"] == "defer-or-rollback"
    assert "Get-ExternalConnectionSnapshot" in packager
    assert "Compare-ExternalConnectionSnapshots" in packager
    assert "$Before.configuredIds + $Before.connectedIds + $Before.callableIds" in packager
    assert "set(before.get(\"configuredIds\") or []) | set(before.get(\"connectedIds\") or []) | set(before.get(\"callableIds\") or [])" in packager
    assert "external_connections_missing_after_update" in packager
    assert "external_connection_snapshot" in packager
    assert "compare_external_connection_snapshots" in packager
    assert '"externalConnections"' in packager
    assert '"not_applicable"' in packager
    assert "externalConnections?:" in api
    assert "externalConnectionsPassed" in app
    assert "外部连接健康检查失败" in app
    assert "externalConnections" in web_channel
    assert "Manifest must require connectorHealthCheck" in orchestrator


def test_v030_precise_image_retouch_is_hard_routed_to_imagegen_not_bash():
    app = read_text("desktop/src/App.tsx")
    canvas = read_text("desktop/src/components/ImageRetouchCanvas.tsx")
    stream = read_text("agent/protocol/agent_stream.py")
    web_channel = read_text("channel/web/web_channel.py")

    assert "必须使用 imagegen/图像编辑能力" in app
    assert "禁止用 bash、Python、PIL、OpenCV、ImageMagick、SVG/canvas" in app
    assert "工具要求：使用 imagegen/图像编辑能力完成语义修图" in canvas
    assert "标注附件是透明标注层" in canvas
    assert "复制同一修改意图" in canvas
    assert "不要把主图坐标强行映射到不同构图的图片" in app
    assert "ctx.strokeRect(exportMetrics.imageX" in canvas
    assert "drawUploadedImage" in canvas
    assert "ctx.drawImage(image, rect.x" in canvas
    assert "ctx.drawImage(imageRef.current" not in canvas
    assert "def _current_turn_is_image_retouch" in stream
    assert "def _retouch_shell_postprocess_allowed" in stream
    assert "生成|画|绘制|出|做|设计|创作" in stream
    assert "This is an EcoreX 精准修图 / semantic image-editing task" in stream
    assert '"精准修图", "局部修图", "精修标注", "标注图", "箭头尖端", "语义图片编辑"' in stream
    assert "单字|一个字|文字|错字" in stream
    assert "imagegen_intent_primary_route" in stream
    assert "imagegen_visibility_diagnostics" in stream
    assert "imagegen_intent_no_safe_schema_tool" in stream
    assert '"ecorex_cli"' in stream[stream.index("IMAGEGEN_PRIORITY_TOOL_NAMES"):stream.index("IMAGEGEN_SHELL_SEMANTIC_SIGNAL_REGEXES")]
    assert "single-character image text fixes" in web_channel
    assert "Shell may only be used after imagegen output for deterministic post-processing" in web_channel
    assert '"imagegen",' in app
    assert '"image-generation", "imagegen"' in app
    assert "runtimeToolReady(runtimeSnapshot, \"imagegen\")" in app
    assert "图像生成/精准修图工具已加载" in app


def test_v030_first_turn_pending_and_output_merge_are_stable():
    app = read_text("desktop/src/App.tsx")

    assert "type PendingPreflightTurn" in app
    assert "pendingPreflightTurnsRef" in app
    assert "currentPendingPreflightTurn(sourceSessionId)" in app
    assert "supersedePendingPreflightTurn(previousSessionId, sourcePendingPreflight)" in app
    assert "hasActiveTurnControl" in app
    assert "const finalText = streamItemExplicitText(item, [\"final_text\"])" in app
    assert "const explicitText = finalText !== null ? finalText : streamItemExplicitText(item, [\"content\", \"text\", \"message\"])" in app
    assert "currentTrimmed.length > doneTrimmed.length + 64" in app
    assert "localHasRicherVisibleAnswer" in app


def test_v030_office_artifacts_hide_implementation_files_and_keep_markdown_openable():
    message_content = read_text("desktop/src/components/MessageContent.tsx")
    css = read_text("desktop/src/styles/app.css")

    assert "OFFICE_HIDDEN_IMPLEMENTATION_EXTENSIONS" in message_content
    assert '"py", "pyw", "js", "jsx", "ts", "tsx"' in message_content
    assert "isOfficeVisibleArtifact" in message_content
    assert "isOfficeImplementationArtifact" in message_content
    assert "showImplementationFiles ? mergedItems : mergedItems.filter(isOfficeVisibleArtifact)" in message_content
    assert "显示实现文件" in message_content
    assert "isMarkdownArtifact" in message_content
    assert "本地打开 Markdown" in message_content
    assert ".artifact-row-actions.is-pinned" in css


def test_v030_status_and_tool_icons_are_plainer():
    app = read_text("desktop/src/App.tsx")
    css = read_text("desktop/src/styles/app.css")

    assert "chat-status-icon is-runtime" in app
    assert "当前企业账号：" in app
    assert "sidebar-plain-icon" in app
    assert ".chat-status .chat-status-icon" in css
    assert ".sidebar-section-title .sidebar-plain-icon" in css
    assert ".agent-step-icon {" in css
    agent_icon_block = css[css.index(".agent-step-icon {"):css.index(".agent-step-icon svg")]
    assert "border:" not in agent_icon_block
    assert "background: transparent" in agent_icon_block
    assert ".artifact-row.is-image .artifact-row-actions" in css
    assert "session-list-more is-collapse" in app


def test_v030_imagegen_schema_selects_native_route_for_precise_retouch():
    from agent.protocol.agent_stream import AgentStreamExecutor

    def tool(name: str):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {}},
        )

    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[tool("read"), tool("bash"), tool("host_diagnostics"), tool("optional_abilities"), tool("imagegen")],
        messages=[{"role": "user", "content": [{"type": "text", "text": "请做精准修图，把标注图箭头尖端指向的错字改掉"}]}],
    )

    selected, budget = executor._select_tools_for_schema()

    assert set(selected) == {"imagegen"}
    assert "bash" not in selected
    assert budget["imagegen_intent"] is True
    assert budget["selection_reasons"]["imagegen"] == "imagegen_primary_route"


def test_v030_imagegen_schema_selects_native_route_for_natural_single_image_phrasing():
    from agent.protocol.agent_stream import AgentStreamExecutor

    def tool(name: str):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {}},
        )

    for prompt in (
        "生成一张产品海报图片",
        "画一张图",
        "做一张封面图",
        "请把图片里的背景去掉",
        "请把照片里的路人移除",
    ):
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(),
            system_prompt="",
            tools=[tool("read"), tool("bash"), tool("host_diagnostics"), tool("optional_abilities"), tool("imagegen")],
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        )

        selected, budget = executor._select_tools_for_schema()

        assert set(selected) == {"imagegen"}
        assert "bash" not in selected
        assert budget["imagegen_intent"] is True


def test_v030_image_retouch_blocks_semantic_bash_execution_before_tool_run():
    from agent.protocol.agent_stream import AgentStreamExecutor

    def tool(name: str):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {}},
        )

    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[tool("bash"), tool("imagegen")],
        messages=[{"role": "user", "content": [{"type": "text", "text": "### 精准修图 1：把箭头尖端那个字改成地"}]}],
    )

    reason = executor._external_capability_reroute("bash", {
        "command": "python - <<'PY'\nfrom PIL import Image, ImageDraw\nprint('patch one glyph')\nPY"
    })

    assert "semantic image-editing task" in reason
    assert "Use the native `imagegen` tool" in reason


def test_v030_single_character_image_text_fix_blocks_bash_even_without_precise_marker():
    from agent.protocol.agent_stream import AgentStreamExecutor

    def tool(name: str):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {}},
        )

    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[tool("bash"), tool("imagegen")],
        messages=[{"role": "user", "content": [{"type": "text", "text": "请把图里的一个字改成地，其他画面别动"}]}],
    )

    reason = executor._external_capability_reroute("bash", {
        "command": "python - <<'PY'\nfrom PIL import Image, ImageDraw, ImageFont\nprint('patch one glyph')\nPY"
    })

    assert "semantic image-editing task" in reason
    assert "Use the native `imagegen` tool" in reason


def test_v030_semantic_image_edit_blocks_bash_even_without_precise_marker():
    from agent.protocol.agent_stream import AgentStreamExecutor

    def tool(name: str):
        return types.SimpleNamespace(
            name=name,
            description=f"{name} tool",
            params={"type": "object", "properties": {}},
        )

    executor = AgentStreamExecutor(
        agent=types.SimpleNamespace(last_usage={}),
        model=types.SimpleNamespace(),
        system_prompt="",
        tools=[tool("bash"), tool("imagegen")],
        messages=[{"role": "user", "content": [{"type": "text", "text": "请把照片里的路人移除，其他画面别动"}]}],
    )

    reason = executor._external_capability_reroute("bash", {
        "command": "python - <<'PY'\nfrom PIL import Image, ImageDraw\nprint('remove person')\nPY"
    })

    assert "semantic image-editing task" in reason
    assert "Use the native `imagegen` tool" in reason


def test_v030_browser_cdp_cancel_event_is_per_call_not_shared():
    tool_source = read_text("agent/tools/browser/browser_tool.py")
    service_source = read_text("agent/tools/browser/browser_service.py")

    assert ".cancel_event = getattr(self" not in tool_source
    assert "def _current_cancel_event(self)" in tool_source
    assert "cancel_event=self._current_cancel_event()" in tool_source
    assert "def _submit(self, fn: Callable, *args, cancel_event=None, **kwargs)" in service_source
    assert "active_cancel_event = cancel_event if cancel_event is not None else getattr(self, \"cancel_event\", None)" in service_source


def test_v030_session_share_backend_compacts_large_payloads_before_upstream():
    class _WebStub(types.SimpleNamespace):
        def __getattr__(self, name):
            def _noop(*_args, **_kwargs):
                return None
            return _noop

    previous_web = sys.modules.get("web")
    sys.modules.setdefault("web", _WebStub(ctx=types.SimpleNamespace(env={}, method="GET", status="200 OK")))
    try:
        from channel.web import web_channel
    finally:
        if previous_web is None:
            sys.modules.pop("web", None)
        else:
            sys.modules["web"] = previous_web

    media = "data:image/png;base64," + ("A" * 900_000)
    messages = [
        {
            "role": "assistant",
            "content": "产物 " + str(index),
            "createdAt": "2026-07-07T00:00:00Z",
            "artifacts": [{"title": "image", "mediaUrl": media, "url": media}],
        }
        for index in range(8)
    ]

    body, compacted, includes_media = web_channel._session_share_compact_body("share", "session-1", messages)

    assert len(body) <= web_channel.SESSION_SHARE_UPSTREAM_SOFT_LIMIT
    assert compacted
    assert includes_media is False
    assert b"data:image/png;base64" not in body

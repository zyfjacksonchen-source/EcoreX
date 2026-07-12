import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = resolve(fileURLToPath(new URL(".", import.meta.url)));
const DEFAULT_DIST = resolve(HERE, "..", "dist");
const AXE_CORE = resolve(HERE, "..", "node_modules", "axe-core", "axe.min.js");
const NOW = "2026-07-10T07:34:00.000Z";
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mP8z8Dwn4GBgYGJAQoAHgQCAf2Q3xQAAAAASUVORK5CYII=",
  "base64",
);

const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml; charset=utf-8",
};

const SCENARIOS = new Set(["empty", "unauthenticated", "thinking", "retry", "hitl", "connector-login", "connector-device", "connector-reauth", "connector-restart", "artifact", "replay", "thread-switch"]);
const TERMINAL_TURN_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted", "superseded"]);
const GA_THEMES = new Set(["light", "dark"]);
const GA_VIEWPORTS = Object.freeze({
  "1440x900": Object.freeze({ width: 1440, height: 900, label: "Desktop" }),
  "1024x768": Object.freeze({ width: 1024, height: 768, label: "Compact desktop" }),
  "768x900": Object.freeze({ width: 768, height: 900, label: "Tablet" }),
  "390x844": Object.freeze({ width: 390, height: 844, label: "Mobile" }),
  "320x568": Object.freeze({ width: 320, height: 568, label: "Narrow mobile" }),
});

const APP_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";
const FRAME_APP_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'self'";
const VIEWPORT_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

const VIEWPORT_CSS = `
:root {
  color-scheme: light;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

:root[data-theme="dark"] {
  color-scheme: dark;
}

* {
  box-sizing: border-box;
}

html,
body {
  min-width: 320px;
  min-height: 100%;
  margin: 0;
}

body {
  padding: 16px;
  color: #172033;
  background: #eef1f6;
}

:root[data-theme="dark"] body {
  color: #edf2ff;
  background: #10131a;
}

.ga-header {
  width: min(100%, 900px);
  margin-bottom: 12px;
}

.ga-header h1 {
  margin: 0;
  font-size: 18px;
  line-height: 24px;
}

.ga-header p {
  margin: 4px 0 0;
  color: #5d687c;
  font-size: 13px;
  line-height: 20px;
}

:root[data-theme="dark"] .ga-header p {
  color: #aab4c9;
}

.ga-stage {
  max-width: 100%;
  overflow: auto;
  border: 1px solid #bac3d2;
  border-radius: 8px;
  background: #dfe4ec;
}

:root[data-theme="dark"] .ga-stage {
  border-color: #3b4558;
  background: #090b10;
}

.ga-viewport-frame {
  display: block;
  max-width: none;
  border: 0;
  background: #fff;
}

.ga-report {
  width: min(100%, 900px);
  margin: 12px 0 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid #bac3d2;
  border-radius: 8px;
  color: inherit;
  background: #fff;
  font: 12px/18px ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  white-space: pre-wrap;
}

:root[data-theme="dark"] .ga-report {
  border-color: #3b4558;
  background: #171b24;
}

body[data-ga-status="passed"] .ga-header h1::after {
  content: " · PASS";
  color: #147a49;
}

body[data-ga-status="failed"] .ga-header h1::after {
  content: " · CHECK";
  color: #b42318;
}
`;

const VIEWPORT_JS = `
(() => {
  "use strict";
  const frame = document.getElementById("ga-viewport-frame");
  const output = document.getElementById("ga-viewport-report");
  if (!(frame instanceof HTMLIFrameElement) || !(output instanceof HTMLElement)) return;

  const visible = (element, view) => {
    if (!(element instanceof view.HTMLElement)) return false;
    const style = view.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none"
      && style.visibility !== "hidden"
      && Number(style.opacity || "1") > 0
      && rect.width > 0
      && rect.height > 0
      && rect.right > 0
      && rect.bottom > 0
      && rect.left < view.innerWidth
      && rect.top < view.innerHeight;
  };

  const wrappedClickableLabels = (documentNode, view) => {
    const candidates = documentNode.querySelectorAll(
      'button:not(.ex-artifact-primary), a[href], summary, [role="button"]',
    );
    return [...candidates].flatMap((element) => {
      if (!visible(element, view)) return [];
      const label = (element.innerText || "").trim().replace(/\\s+/g, " ");
      if (!label) return [];
      const walker = documentNode.createTreeWalker(element, view.NodeFilter.SHOW_TEXT);
      const lineTops = new Set();
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (!node.textContent?.trim()) continue;
        const range = documentNode.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (rect.width <= 0 || rect.height <= 0) continue;
          lineTops.add(Math.round(rect.top * 2) / 2);
        }
      }
      return lineTops.size > 1
        ? [{ label, lines: lineTops.size }]
        : [];
    });
  };

  const inspect = () => {
    const view = frame.contentWindow;
    const documentNode = frame.contentDocument;
    if (!view || !documentNode?.documentElement || !documentNode.body) return null;
    const root = documentNode.documentElement;
    const body = documentNode.body;
    const find = (selector) => documentNode.querySelector(selector);
    const navigationCandidates = [
      find('[aria-label="任务导航"]'),
      find('[data-ecorex-feature-trigger="navigation"]'),
    ].filter(Boolean);
    const controls = {
      navigation: {
        present: navigationCandidates.length > 0,
        visible: navigationCandidates.some((element) => visible(element, view)),
      },
      model_selector: {
        present: Boolean(find('[aria-label="模型选择"]')),
        visible: visible(find('[aria-label="模型选择"]'), view),
      },
      composer: {
        present: Boolean(find("#ecorex-composer")),
        visible: visible(find("#ecorex-composer"), view),
      },
      task_type: {
        present: Boolean(find('[aria-label="任务类型"]')),
        visible: visible(find('[aria-label="任务类型"]'), view),
      },
      artifact_shelf: {
        present: Boolean(find('[aria-label="任务产物"]')),
        visible: visible(find('[aria-label="任务产物"]'), view),
      },
    };
    const expectedWidth = Number(frame.dataset.expectedWidth);
    const expectedHeight = Number(frame.dataset.expectedHeight);
    const contentWidth = Math.max(root.scrollWidth, body.scrollWidth);
    const overflowPixels = Math.max(0, contentWidth - view.innerWidth);
    const wrappedLabels = wrappedClickableLabels(documentNode, view);
    const requiredControls = [
      controls.navigation.visible,
      controls.model_selector.visible,
      controls.composer.visible,
      controls.task_type.visible,
      frame.dataset.scenario !== "artifact" || controls.artifact_shelf.present,
    ];
    const report = {
      contract_version: "1.0",
      viewport: {
        expected_width: expectedWidth,
        expected_height: expectedHeight,
        actual_width: view.innerWidth,
        actual_height: view.innerHeight,
        root_client_width: root.clientWidth,
        root_client_height: root.clientHeight,
        device_pixel_ratio: view.devicePixelRatio,
      },
      theme: {
        expected: frame.dataset.theme,
        actual: root.dataset.theme || null,
      },
      horizontal_overflow: {
        present: overflowPixels > 1,
        overflow_pixels: overflowPixels,
        content_width: contentWidth,
      },
      wrapped_clickable_labels: wrappedLabels,
      key_controls: controls,
      passed: view.innerWidth === expectedWidth
        && view.innerHeight === expectedHeight
        && root.dataset.theme === frame.dataset.theme
        && overflowPixels <= 1
        && wrappedLabels.length === 0
        && requiredControls.every(Boolean),
    };
    return report;
  };

  const inspectAccessibility = async (view, documentNode) => {
    if (!view.axe || typeof view.axe.run !== "function") {
      return {
        engine: null,
        violations: [{ id: "axe_unavailable", impact: "critical", nodes: 0 }],
        incomplete: [],
        passes: 0,
      };
    }
    try {
      const result = await view.axe.run(documentNode, {
        resultTypes: ["violations", "incomplete", "passes"],
      });
      return {
        engine: result.testEngine?.version || null,
        violations: result.violations.map((violation) => ({
          id: violation.id,
          impact: violation.impact,
          nodes: violation.nodes.length,
          help: violation.help,
          targets: violation.nodes.slice(0, 8).map((node) => node.target),
        })),
        incomplete: result.incomplete.map((item) => ({
          id: item.id,
          impact: item.impact,
          nodes: item.nodes.length,
          targets: item.nodes.slice(0, 8).map((node) => node.target),
        })),
        passes: result.passes.length,
      };
    } catch (error) {
      return {
        engine: view.axe.version || null,
        violations: [{
          id: "axe_execution_failed",
          impact: "critical",
          nodes: 0,
          help: error instanceof Error ? error.message : "axe failed",
        }],
        incomplete: [],
        passes: 0,
      };
    }
  };

  const publish = (report) => {
    window.__ECOREX_GA_VIEWPORT_REPORT__ = report;
    output.textContent = JSON.stringify(report, null, 2);
    document.body.dataset.gaStatus = report.passed ? "passed" : "failed";
  };

  let inspectionStarted = false;
  let scenarioPrepared = false;
  const startInspection = () => {
    if (inspectionStarted) return;
    inspectionStarted = true;
    let attempts = 0;
    const poll = async () => {
      attempts += 1;
      const report = inspect();
      const ready = report
        && report.key_controls.navigation.present
        && report.key_controls.model_selector.present
        && report.key_controls.composer.present
        && report.key_controls.task_type.present
        && (frame.dataset.scenario !== "artifact" || report.key_controls.artifact_shelf.present);
      if (
        report
        && frame.dataset.scenario === "artifact"
        && !report.key_controls.artifact_shelf.present
        && !scenarioPrepared
      ) {
        const openThread = frame.contentDocument?.querySelector('[aria-label^="打开任务："]');
        if (openThread instanceof frame.contentWindow.HTMLElement) {
          scenarioPrepared = true;
          openThread.click();
        }
      }
      if (report && (ready || attempts >= 200)) {
        const documentNode = frame.contentDocument;
        report.accessibility = documentNode
          ? await inspectAccessibility(frame.contentWindow, documentNode)
          : {
              engine: null,
              violations: [{ id: "frame_document_unavailable", impact: "critical", nodes: 0 }],
              incomplete: [],
              passes: 0,
            };
        report.passed = report.passed && report.accessibility.violations.length === 0;
        publish(report);
        return;
      }
      window.setTimeout(poll, 50);
    };
    poll();
  };
  frame.addEventListener("load", startInspection);
  if (frame.contentDocument?.readyState === "complete") startInspection();
})();
`;

const GA_CSRF_TOKEN = "ga-csrf-token-0123456789abcdef0123456789abcdef";

class GaHarnessRequestError extends Error {
  constructor(code, message, status = 422) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function thread(threadId = "thread-ga", title = "季度资料整理") {
  return {
    thread_id: threadId,
    status: "active",
    title,
    metadata: {},
    forked_from_thread_id: null,
    forked_from_turn_id: null,
    forked_from_seq: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function turn(
  turnId,
  status,
  input,
  agentModelId = "ecorex-chat",
  imageModelId = "gpt-image-2",
  threadId = "thread-ga",
) {
  return {
    turn_id: turnId,
    thread_id: threadId,
    status,
    input,
    agent_model_id: agentModelId,
    image_model_id: imageModelId,
    client_message_id: `message-${turnId}`,
    metadata: {},
    terminal_reason: ["completed", "failed", "cancelled", "interrupted", "superseded"].includes(status)
      ? status
      : null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function item(itemId, turnId, role, text, threadId = "thread-ga") {
  return {
    item_id: itemId,
    thread_id: threadId,
    turn_id: turnId,
    kind: "message",
    status: "completed",
    content: { role, text },
    created_at: NOW,
    updated_at: NOW,
  };
}

function reasoningItem(itemId, turnId, atomId, text) {
  return {
    item_id: itemId,
    thread_id: "thread-ga",
    turn_id: turnId,
    kind: "reasoning",
    status: "in_progress",
    content: {
      channel: "reasoning_summary",
      atom_id: atomId,
      text,
      revision: 1,
      presentation: "visible",
      archived_reason: null,
    },
    created_at: NOW,
    updated_at: NOW,
  };
}

function artifact(artifactId = "artifact-ga-source", revisionId = "revision-ga-source", displayName = "活动主视觉_20260710-1534_01.png") {
  return {
    artifact_id: artifactId,
    revision_id: revisionId,
    family: "image",
    role: "deliverable",
    visibility: "primary",
    status: "ready",
    display_name: displayName,
    mime_type: "image/png",
    size_bytes: PNG.length,
    sha256: "a".repeat(64),
    created_at: NOW,
    renditions: [{ kind: "preview", mime_type: "image/png", size_bytes: PNG.length, sha256: "b".repeat(64) }],
    actions: ["preview", "open", "download", "reveal", "feedback", "precise_retouch"],
    feedback: null,
    lineage: { source_artifact_ids: [], supersedes_revision_id: null },
    quality_evidence: {
      status: "passed",
      checks: [{ name: "visual_review", status: "passed", detail: "主体和标题区域已检查" }],
      score: 0.94,
      summary: "主体边缘、标题安全区和整体对比度已检查。",
    },
  };
}

function editSurface(source) {
  return {
    base_revision_id: source.revision_id,
    raster_digest: source.sha256,
    width_px: 2,
    height_px: 2,
    orientation: 1,
    color_space: "srgb-alpha",
    mime_type: "image/png",
    coordinate_space_version: "oriented-normalized-v1",
  };
}

function workspaceWire(workspace) {
  return {
    ...workspace,
    surface_url: `/api/v1/retouch-workspaces/${workspace.workspace_id}/surface`,
    result_url: workspace.result
      ? `/api/v1/retouch-workspaces/${workspace.workspace_id}/result`
      : null,
    references: workspace.references.map((reference) => ({
      ...reference,
      preview_url: `/api/v1/retouch-workspaces/${workspace.workspace_id}/references/${reference.artifact_id}/preview`,
    })),
  };
}

function interaction() {
  return {
    interaction_id: "interaction-ga",
    kind: "permission_approval",
    status: "pending",
    prompt: "需要读取工作区中的季度资料，再生成汇总文档。",
    options: [{ id: "approve_once", label: "本次允许" }, { id: "deny", label: "拒绝" }],
    contract: {
      schema_version: 1,
      title: "需要你的允许",
      fields: [],
      actions: [
        {
          action_id: "approve_once",
          label: "本次允许",
          action_type: "allow",
          style: "primary",
          submits_form: false,
        },
        {
          action_id: "deny",
          label: "拒绝",
          action_type: "deny",
          style: "danger",
          submits_form: false,
        },
      ],
      connector: null,
    },
    response: null,
    response_client_request_id: null,
    thread_id: "thread-ga",
    turn_id: "turn-ga",
    job_id: "job-ga",
    expires_at: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function connectorLoginInteraction() {
  return {
    interaction_id: "interaction-connector-ga",
    kind: "connector_login",
    status: "pending",
    prompt: "继续整理飞书文档前，需要先完成安全登录。",
    options: [],
    contract: {
      schema_version: 1,
      title: "连接飞书文档",
      fields: [],
      actions: [
        {
          action_id: "begin_login",
          label: "开始登录",
          action_type: "connector_begin_login",
          style: "primary",
          submits_form: false,
        },
        {
          action_id: "check_status",
          label: "检查状态",
          action_type: "connector_check_status",
          style: "secondary",
          submits_form: false,
        },
        {
          action_id: "cancel",
          label: "取消",
          action_type: "cancel",
          style: "danger",
          submits_form: false,
        },
      ],
      connector: {
        connector_id: "feishu/docs",
        display_name: "飞书文档",
        state: "authorization_required",
        required_action_ids: ["feishu.document.read"],
      },
    },
    response: null,
    response_client_request_id: null,
    thread_id: "thread-ga",
    turn_id: "turn-ga",
    job_id: "job-ga",
    expires_at: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function mockExtensions() {
  return [
    {
      extension_id: "ecorex.office-tools",
      display_name: "EcoreX 办公工具",
      description: "由核心包提供的文档、表格与演示文稿办公能力。",
      kind: "tool_provider",
      active_revision_id: "extrev-office-tools-1",
      active_version: "1.0.0",
      active_digest: "1".repeat(64),
      source: "core_bundle",
      trust: "builtin",
      status: "enabled",
      health: "healthy",
      dependencies: [],
      exports: [
        { export_id: "office.document", kind: "tool", exposure: "direct", permission_effects: ["read", "write"] },
      ],
      last_error_code: null,
      revision: 1,
      updated_at: NOW,
      rollback_version: null,
    },
    {
      extension_id: "ecorex.feishu-mcp",
      display_name: "飞书 MCP",
      description: "通过受管适配器发现并调用飞书文档能力。",
      kind: "mcp_server",
      active_revision_id: "extrev-feishu-2",
      active_version: "1.2.0",
      active_digest: "2".repeat(64),
      source: "signed_release",
      trust: "verified_publisher",
      status: "enabled",
      health: "degraded",
      dependencies: [{ extension_id: "ecorex.office-tools", version_range: ">=1.0.0" }],
      exports: [
        { export_id: "feishu.documents", kind: "mcp_server", exposure: "deferred", permission_effects: ["network", "read", "write"] },
      ],
      last_error_code: "provider_health_timeout",
      revision: 2,
      updated_at: NOW,
      rollback_version: "1.1.0",
    },
    {
      extension_id: "legacy.document-skill",
      display_name: "旧版文档整理技能",
      description: "从 v0.3.0 迁移的本地声明式技能；不会获得进程执行边界。",
      kind: "skill",
      active_revision_id: "extrev-legacy-skill-1",
      active_version: "0.3.0",
      active_digest: "3".repeat(64),
      source: "legacy_import",
      trust: "local_untrusted",
      status: "disabled",
      health: "unknown",
      dependencies: [],
      exports: [
        { export_id: "legacy.document.organize", kind: "skill", exposure: "deferred", permission_effects: ["read"] },
      ],
      last_error_code: null,
      revision: 1,
      updated_at: NOW,
      rollback_version: null,
    },
  ];
}

function extensionActions(extension) {
  const unavailable = (action_id, disabled_reason, requires_confirmation = false) => ({
    action_id,
    enabled: false,
    disabled_reason,
    requires_confirmation,
  });
  if (extension.status === "quarantined") {
    return [
      unavailable("enable", "扩展已被 Runtime 隔离；必须先由管理员解决隔离原因。", true),
      unavailable("health_check", "隔离期间不执行扩展健康检查。"),
    ];
  }
  return [
    extension.status === "disabled" || extension.status === "staged"
      ? { action_id: "enable", enabled: true, disabled_reason: null, requires_confirmation: true }
      : unavailable("enable", "扩展已经启用。", true),
    extension.status === "enabled"
      ? { action_id: "disable", enabled: true, disabled_reason: null, requires_confirmation: true }
      : unavailable("disable", "只有已启用扩展可以停用。", true),
    extension.status === "enabled"
      ? { action_id: "health_check", enabled: true, disabled_reason: null, requires_confirmation: false }
      : unavailable("health_check", "只有已启用扩展可以执行健康检查。"),
    extension.rollback_version && extension.active_version !== extension.rollback_version
      ? { action_id: "rollback", enabled: true, disabled_reason: null, requires_confirmation: true }
      : unavailable("rollback", "没有可回滚的已知良好版本。", true),
  ];
}

function extensionProjection(extension) {
  return {
    extension_id: extension.extension_id,
    display_name: extension.display_name,
    description: extension.description,
    kind: extension.kind,
    active_revision_id: extension.active_revision_id,
    active_version: extension.active_version,
    active_digest: extension.active_digest,
    source: extension.source,
    trust: extension.trust,
    status: extension.status,
    health: extension.health,
    dependencies: extension.dependencies,
    exports: extension.exports,
    actions: extensionActions(extension),
    last_error_code: extension.last_error_code,
    revision: extension.revision,
    updated_at: extension.updated_at,
  };
}

function extensionCatalog(state) {
  const items = state.extensions.map(extensionProjection);
  return {
    snapshot_id: `extensions-ga-${items.map((item) => `${item.extension_id}-${item.revision}`).join("_")}`,
    contract_version: "1.0",
    items,
  };
}

function scenarioState(name) {
  const base = {
    scenario: name,
    authenticated: name !== "unauthenticated",
    permissionProfile: "default",
    permissionRevision: 1,
    threads: [],
    projection: null,
    projections: new Map(),
    projectionDelays: new Map(),
    artifacts: [],
    artifactActions: new Map(),
    retouchWorkspaces: new Map(),
    retouchCounter: 0,
    shares: [],
    shareCounter: 0,
    liveReplayRequests: new Map(),
    seq: 0,
    clients: new Set(),
    timers: new Set(),
    terminalScheduled: false,
    deviceFlow: null,
    deviceBeginRequestId: null,
    extensions: mockExtensions(),
    extensionRequests: new Map(),
    memoryRevision: 1,
    memoryResettableCount: 2,
    memoryReset: null,
    outputLocation: "documents",
    outputRevision: 1,
    outputRequests: new Map(),
    connectorLoginBeginCount: 0,
    connectorLoginCheckCount: 0,
    connectorLoginCancelCount: 0,
    interactionRespondCount: 0,
  };
  if (name === "empty" || name === "unauthenticated") return base;

  if (name === "thread-switch") {
    const threadSpecs = [
      ["thr_current_ga", "当前季度任务", "当前任务原始内容", 0],
      ["thr_target_ga", "已恢复的年度任务", "年度任务已从恢复点载入", 0],
      ["thr_slow_ga", "较慢的历史任务", "较慢任务内容", 450],
      ["thr_fast_ga", "较新的快速任务", "快速任务最终内容", 0],
    ];
    for (const [threadId, title, input, delay] of threadSpecs) {
      const selectedThread = thread(threadId, title);
      const selectedTurn = turn(
        `turn-${threadId}`,
        "completed",
        input,
        "ecorex-chat",
        "gpt-image-2",
        threadId,
      );
      const selectedProjection = {
        thread: selectedThread,
        turns: [selectedTurn],
        items: [
          item(`item-user-${threadId}`, selectedTurn.turn_id, "user", input, threadId),
          item(
            `item-assistant-${threadId}`,
            selectedTurn.turn_id,
            "assistant",
            `“${title}”已恢复，可以从上次的位置继续。`,
            threadId,
          ),
        ],
        jobs: [],
        interactions: [],
        watermark: 2,
      };
      base.threads.push(selectedThread);
      base.projections.set(threadId, selectedProjection);
      if (delay) base.projectionDelays.set(threadId, delay);
    }
    base.projection = base.projections.get("thr_current_ga");
    base.seq = 2;
    return base;
  }

  const activeThread = thread();
  let activeTurn = turn("turn-ga", "completed", "整理季度资料");
  const items = [item("item-user-ga", "turn-ga", "user", "整理季度资料")];
  const interactions = [];
  if (name === "thinking") {
    activeTurn = turn("turn-ga", "model_requested", "整理季度资料");
    items.push(reasoningItem(
      "reasoning-ga-1",
      activeTurn.turn_id,
      "reasoning-atom-ga-1",
      "正在核对季度资料。",
    ));
  }
  if (name === "retry") activeTurn = turn("turn-ga", "retry_wait", "整理季度资料");
  if (name === "hitl") {
    activeTurn = turn("turn-ga", "waiting_human", "整理季度资料");
    interactions.push(interaction());
  }
  if (name === "connector-login" || name === "connector-device" || name === "connector-reauth" || name === "connector-restart") {
    activeTurn = turn("turn-ga", "waiting_human", "整理飞书文档");
    interactions.push(connectorLoginInteraction());
  }
  if (name === "artifact") {
    items.push(item("item-assistant-ga", "turn-ga", "assistant", "已完成主视觉，并检查了主体边缘与标题安全区。请先看一眼下方图片。"));
    base.artifacts.push(artifact());
  }
  if (name === "replay") {
    items.push(item("item-assistant-ga", "turn-ga", "assistant", "已完成季度资料整理，可以使用任务诊断验证事件或显式创建新执行。"));
  }
  base.threads.push(activeThread);
  base.projection = {
    thread: activeThread,
    turns: [activeTurn],
    items,
    jobs: [],
    interactions,
    watermark: name === "hitl" || name === "connector-login" || name === "connector-device" || name === "connector-reauth" || name === "connector-restart" ? 3 : 2,
  };
  base.projections.set(activeThread.thread_id, base.projection);
  base.seq = base.projection.watermark;
  return base;
}

function bootstrap(state) {
  const authenticated = state.authenticated;
  const defaultPermission = state.permissionProfile === "default";
  return {
    api_version: "v1",
    event_schema_version: 1,
    storage_schema_version: 1,
    login: {
      authenticated,
      account_id: authenticated ? "account-ga" : null,
      display_name: authenticated ? "验收账号" : null,
      organization_id: authenticated ? "org-ga" : null,
      roles: authenticated ? ["member"] : [],
      session_revision: authenticated ? 1 : null,
    },
    policy_lease: authenticated ? {
      lease_id: "lease-ga",
      issued_at: NOW,
      expires_at: "2026-07-13T07:34:00.000Z",
      duration_hours: 72,
    } : null,
    models: {
      snapshot_id: authenticated ? "models-ga" : null,
      chat: authenticated ? [{
        model_id: "ecorex-chat",
        display_name: "GPT-5.6 SOL · 中等推理",
        capabilities: ["chat", "tools", "reasoning"],
        aliases: ["chat", "default", "gpt-5.6-sol", "gpt5.6-sol"],
        is_default: true,
        model_policy: {
          schema_version: 1,
          policy_id: "ecorex-chat-gpt-5.6-sol",
          policy_version: "1.0.0",
          local_model_id: "ecorex-chat",
          upstream_model_id: "gpt-5.6-sol",
          reasoning_effort: "medium",
          context_management: {
            type: "compaction",
            compact_threshold_tokens: 272_000,
          },
        },
      }] : [],
      image: authenticated ? [{
        model_id: "gpt-image-2",
        display_name: "Image 2",
        capabilities: ["image"],
        aliases: ["image2", "image-2"],
        is_default: true,
        model_policy: null,
      }] : [],
      vision: authenticated ? [{
        model_id: "ecorex-vision-1",
        display_name: "EcoreX Vision",
        capabilities: ["vision"],
        aliases: [],
        is_default: true,
        model_policy: null,
      }] : [],
      audio: [],
      embedding: [],
    },
    model_service: authenticated
      ? { state: "ready", reason: null }
      : { state: "unavailable", reason: "managed_session_unavailable" },
    login_service: { state: "ready", reason: null },
    share_service: authenticated
      ? { state: "ready", reason: null }
      : { state: "unavailable", reason: "managed_session_unavailable" },
    retouch_service: authenticated
      ? { state: "ready", reason: null }
      : { state: "unavailable", reason: "managed_session_unavailable" },
    quota: { remaining: authenticated ? 128 : null, unit: "managed_requests", resets_at: null, limits: authenticated ? { managed_requests: 128 } : {} },
    permissions: {
      snapshot_id: `perm_${"c".repeat(64)}`,
      profile: state.permissionProfile,
      revision: state.permissionRevision,
      updated_at: NOW,
      sandbox: defaultPermission ? "workspace-write" : "danger-full-access",
      approval: defaultPermission ? "on-request" : "never",
      full_access: !defaultPermission,
      admin_hard_denies: [],
    },
    connectors: [],
    extensions: extensionCatalog(state),
    update: {
      current_version: "1.0.0",
      state: "idle",
      target_version: null,
      release_id: null,
      build_digest: null,
      transaction_id: null,
      can_activate: false,
      requires_refresh: false,
      error_code: null,
    },
    csrf_token: GA_CSRF_TOKEN,
    server_time: NOW,
  };
}

function connectorCatalog() {
  const definition = (connectorId, displayName) => ({
    connector_id: connectorId,
    contract_version: "1.0",
    display_name: displayName,
    description: `连接并使用${displayName}中的办公文档。`,
    tier: "stable",
    auth_kinds: ["oauth2"],
    config_schema: {},
    actions: [],
    events: [],
    icon_key: "link",
  });
  return {
    contract_version: "1.0",
    items: [
      { definition: definition("feishu", "飞书"), adapter_available: true, instances: [], unavailable_reason: null },
      { definition: definition("tencent-docs", "腾讯文档"), adapter_available: true, instances: [], unavailable_reason: null },
    ],
  };
}

function memorySnapshot(state) {
  return {
    revision: state.memoryRevision,
    active_learned_records: state.memoryResettableCount,
    active_user_files: 0,
    factory_records: 3,
    tombstoned_records: state.memoryResettableCount ? 0 : 2,
    tombstoned_files: 0,
    resettable_count: state.memoryResettableCount,
    latest_reset: state.memoryReset,
  };
}

function systemHealth(state, technical = false) {
  const sampledAt = new Date().toISOString();
  const sample = {
    sample_id: `syssample-ga-${state.seq}`,
    overall: "healthy",
    summary: "EcoreX 运行正常",
    components: [
      { component_id: "responsiveness", label: "运行响应", status: "healthy", message: "界面和后台响应正常。" },
      { component_id: "jobs", label: "任务队列", status: "healthy", message: "任务队列运行正常。" },
      { component_id: "storage", label: "本地数据", status: "healthy", message: "本地记录和产物索引正常。" },
      { component_id: "services", label: "扩展与连接", status: "healthy", message: "扩展和连接服务可用。" },
    ],
    sampled_at: sampledAt,
  };
  if (!technical) return sample;
  return {
    ...sample,
    metrics: {
      runtime: { sse_connections: state.clients.size, sse_events_sent: state.seq, event_loop_lag_ms: 0 },
      process: { uptime_seconds: 1, rss_bytes: 0 },
      storage: { events_total: state.seq, artifacts_total: state.artifacts.length },
      services: { extensions: { state: "ready", total: state.extensions.length } },
    },
  };
}

function deviceFlowProjection(state, { restartScheduled = false } = {}) {
  if (!state.deviceFlow) return null;
  return { ...state.deviceFlow, restart_scheduled: restartScheduled };
}

function json(res, status, payload, extraHeaders = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    ...extraHeaders,
  });
  res.end(body);
}

function apiError(res, status, code, message, extra = {}) {
  json(res, status, { detail: { code, message, ...extra } });
}

async function body(req) {
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > 64 * 1024) throw new Error("request_too_large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function envelope(state, eventType, { turnId = null, itemId = null, jobId = null, payload = {} } = {}) {
  state.seq += 1;
  if (state.projection) state.projection.watermark = state.seq;
  return {
    schema_version: 1,
    event_id: `event-${state.seq}`,
    seq: state.seq,
    thread_id: "thread-ga",
    turn_id: turnId,
    item_id: itemId,
    job_id: jobId,
    tool_call_id: null,
    client_message_id: null,
    causation_id: null,
    correlation_id: "correlation-ga",
    trace_id: "trace-ga",
    config_snapshot_id: "config-ga",
    capability_snapshot_id: "capability-ga",
    permission_snapshot_id: `perm_${"c".repeat(64)}`,
    extension_snapshot_id: extensionCatalog(state).snapshot_id,
    event_type: eventType,
    created_at: new Date().toISOString(),
    payload,
  };
}

function emit(state, event) {
  const block = `id: ${event.seq}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
  for (const client of state.clients) client.write(block);
}

function schedule(state, callback, delay = 300) {
  const timer = setTimeout(() => {
    state.timers.delete(timer);
    callback();
  }, delay);
  state.timers.add(timer);
}

function closeState(state) {
  for (const timer of state.timers) clearTimeout(timer);
  state.timers.clear();
  for (const client of state.clients) client.end();
  state.clients.clear();
}

function scheduleThinkingTerminal(state) {
  if (state.scenario !== "thinking" || state.terminalScheduled || !state.projection) return;
  state.terminalScheduled = true;
  schedule(state, () => {
    const previous = state.projection.items.find((candidate) => candidate.item_id === "reasoning-ga-1");
    if (!previous || previous.kind !== "reasoning") return;
    previous.status = "completed";
    previous.content.presentation = "archived";
    previous.content.archived_reason = "replaced_by_next_atom";
    const next = reasoningItem(
      "reasoning-ga-2",
      "turn-ga",
      "reasoning-atom-ga-2",
      "资料已核对，正在整理结果。",
    );
    state.projection.items.push(next);
    emit(state, envelope(state, "reasoning.replaced", {
      turnId: "turn-ga",
      itemId: next.item_id,
      payload: {
        atom_id: next.content.atom_id,
        delta: next.content.text,
        revision: next.content.revision,
        presentation: "visible",
        previous_item_id: previous.item_id,
        previous_revision: previous.content.revision,
        previous_presentation: "archived",
      },
    }));
  }, 600);
  schedule(state, () => {
    const active = state.projection.turns.find((candidate) => candidate.turn_id === "turn-ga");
    if (!active) return;
    active.status = "completed";
    active.terminal_reason = "completed";
    const terminal = envelope(state, "turn.status_changed", {
      turnId: active.turn_id,
      payload: { from: "model_requested", to: "completed", reason: "completed" },
    });
    emit(state, terminal);
    const reasoning = state.projection.items.find((candidate) => candidate.item_id === "reasoning-ga-2");
    if (!reasoning || reasoning.kind !== "reasoning") return;
    reasoning.status = "completed";
    reasoning.content.revision += 1;
    reasoning.content.presentation = "collapsed";
    reasoning.content.archived_reason = "completed";
    emit(state, envelope(state, "reasoning.archived", {
      turnId: active.turn_id,
      itemId: reasoning.item_id,
      payload: {
        revision: reasoning.content.revision,
        presentation: "collapsed",
        reason: "completed",
        terminal_status: "completed",
        terminal_event_id: terminal.event_id,
      },
    }));
  }, 1_200);
}

function scheduleTurnCompletion(state, activeTurn, selectedModel) {
  schedule(state, () => {
    if (!state.projection) return;
    const assistant = item(`item-assistant-${state.seq + 1}`, activeTurn.turn_id, "assistant", selectedModel === "gpt-image-2"
      ? "图片已生成，并检查了主体边缘和标题安全区。请先看一眼下方图片。"
      : "已完成资料整理；关键结论与待办已写入结果。"
    );
    state.projection.items.push(assistant);
    emit(state, envelope(state, "item.created", {
      turnId: activeTurn.turn_id,
      itemId: assistant.item_id,
      payload: { kind: "message", status: "completed", content: assistant.content },
    }));
    if (selectedModel === "gpt-image-2") {
      const result = artifact(`artifact-ga-${state.seq + 1}`, `revision-ga-${state.seq + 1}`, "生成图片_20260710-1534_01.png");
      state.artifacts.push(result);
      emit(state, envelope(state, "artifact.created", {
        turnId: activeTurn.turn_id,
        itemId: `artifact-item-${state.seq + 1}`,
        payload: { artifact_id: result.artifact_id, revision_id: result.revision_id },
      }));
    }
    activeTurn.status = "completed";
    activeTurn.terminal_reason = "completed";
    emit(state, envelope(state, "turn.status_changed", {
      turnId: activeTurn.turn_id,
      payload: { from: "model_requested", to: "completed", reason: "completed" },
    }));
  }, 350);
}

function projectionResponse(state, threadId) {
  return state.projections.get(threadId) ?? (
    state.projection?.thread.thread_id === threadId ? state.projection : null
  );
}

function threadMutationResponse(state, activeTurn) {
  return { turn: activeTurn, job: null, watermark: state.projection?.watermark ?? 0 };
}

function resetScenario(holder, name) {
  if (!SCENARIOS.has(name)) throw new Error("unknown_scenario");
  closeState(holder.state);
  holder.state = scenarioState(name);
  return holder.state;
}

async function handleApi(holder, req, res, url) {
  const state = holder.state;
  const path = url.pathname;
  if (path === "/__ga/state" && req.method === "GET") {
    return json(res, 200, {
      scenario: state.scenario,
      seq: state.seq,
      permission_profile: state.permissionProfile,
      output_location: state.outputLocation,
      memory_resettable_count: state.memoryResettableCount,
      connector_login: {
        begin: state.connectorLoginBeginCount,
        check: state.connectorLoginCheckCount,
        cancel: state.connectorLoginCancelCount,
        ordinary_respond: state.interactionRespondCount,
      },
    });
  }
  if (path === "/__ga/reset" && req.method === "POST") {
    const name = url.searchParams.get("scenario") || "empty";
    try {
      resetScenario(holder, name);
      return json(res, 200, { scenario: name });
    } catch {
      return apiError(res, 422, "unknown_scenario", `Unknown GA scenario: ${name}`);
    }
  }

  if (!path.startsWith("/api/v1/")) return false;
  if (req.method !== "GET" && req.headers["x-ecorex-csrf"] !== GA_CSRF_TOKEN) {
    return apiError(res, 403, "csrf_required", "GA mutations require the bootstrap CSRF token");
  }
  if (path === "/api/v1/bootstrap" && req.method === "GET") return json(res, 200, bootstrap(state));
  if (path === "/api/v1/system/health" && req.method === "GET") {
    return json(res, 200, systemHealth(state, url.searchParams.get("technical") === "true"));
  }
  if (path === "/api/v1/system/metrics" && req.method === "GET") {
    return json(res, 200, { items: [systemHealth(state, true)] });
  }
  if (path === "/api/v1/migration/quarantine" && req.method === "GET") {
    return json(res, 200, {
      status: "absent",
      entry_count: 0,
      can_delete: false,
      deleted_at: null,
      items: [],
    });
  }
  if (path === "/api/v1/memory" && req.method === "GET") {
    return json(res, 200, memorySnapshot(state));
  }
  if (path === "/api/v1/output/locations" && req.method === "GET") {
    return json(res, 200, {
      items: ["documents", "downloads", "workspace"].map((alias) => ({ alias, available: true })),
    });
  }
  if (path === "/api/v1/output/preference" && req.method === "GET") {
    return json(res, 200, {
      account_id: "account-ga",
      location_alias: state.outputLocation,
      revision: state.outputRevision,
      output_policy_snapshot_id: `outpolicy_ga_${state.outputRevision}`,
      updated_at: new Date().toISOString(),
    });
  }
  if (path === "/api/v1/output/preference" && req.method === "PUT") {
    const request = await body(req);
    if (request.expected_revision !== state.outputRevision) {
      return apiError(res, 409, "output_revision_conflict", "Output preference changed");
    }
    if (!["documents", "downloads", "workspace"].includes(request.location_alias)) {
      return apiError(res, 422, "output_location_unavailable", "Output location is unavailable");
    }
    state.outputLocation = request.location_alias;
    state.outputRevision += 1;
    return json(res, 200, {
      account_id: "account-ga",
      location_alias: state.outputLocation,
      revision: state.outputRevision,
      output_policy_snapshot_id: `outpolicy_ga_${state.outputRevision}`,
      updated_at: new Date().toISOString(),
    });
  }
  const materializeMatch = path.match(/^\/api\/v1\/output\/artifacts\/([^/]+)\/materialize$/);
  if (materializeMatch && req.method === "POST") {
    const request = await body(req);
    const artifactId = decodeURIComponent(materializeMatch[1]);
    const selected = state.artifacts.find((candidate) => candidate.artifact_id === artifactId);
    if (!selected || selected.revision_id !== request.revision_id) {
      return apiError(res, 404, "output_artifact_not_eligible", "Artifact is unavailable");
    }
    const fingerprint = `${artifactId}:${request.revision_id}:${state.outputLocation}`;
    const previous = state.outputRequests.get(request.client_request_id);
    if (previous && previous.fingerprint !== fingerprint) {
      return apiError(res, 409, "output_idempotency_conflict", "Output request identity changed");
    }
    const receipt = previous?.receipt ?? {
      materialization_id: `materialization_${"e".repeat(24)}`,
      artifact_id: artifactId,
      revision_id: request.revision_id,
      output_policy_snapshot_id: `outpolicy_ga_${state.outputRevision}`,
      location_alias: state.outputLocation,
      display_name: selected.display_name,
      sha256: selected.sha256,
      size_bytes: selected.size_bytes,
      status: "completed",
      reused_existing: false,
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };
    state.outputRequests.set(request.client_request_id, { fingerprint, receipt });
    return json(res, 200, receipt);
  }
  if (path === "/api/v1/memory/reset" && req.method === "POST") {
    await body(req);
    state.memoryRevision += 1;
    state.memoryResettableCount = 0;
    state.memoryReset = {
      reset_id: `memreset_${"a".repeat(32)}`,
      status: "active",
      affected_records: 2,
      affected_files: 0,
      created_at: new Date().toISOString(),
      undo_until: new Date(Date.now() + 24 * 60 * 60_000).toISOString(),
      updated_at: new Date().toISOString(),
      can_undo: true,
    };
    return json(res, 200, { memory: memorySnapshot(state), reset: state.memoryReset });
  }
  const memoryUndoMatch = path.match(/^\/api\/v1\/memory\/resets\/([^/]+)\/undo$/);
  if (memoryUndoMatch && req.method === "POST") {
    await body(req);
    if (!state.memoryReset || state.memoryReset.reset_id !== decodeURIComponent(memoryUndoMatch[1])) {
      return apiError(res, 404, "memory_reset_not_found", "Memory reset was not found");
    }
    state.memoryRevision += 1;
    state.memoryResettableCount = 2;
    state.memoryReset = { ...state.memoryReset, status: "undone", updated_at: new Date().toISOString(), can_undo: false };
    return json(res, 200, { memory: memorySnapshot(state), reset: state.memoryReset });
  }
  if (path === "/api/v1/session/device" && req.method === "POST") {
    if (state.authenticated) return apiError(res, 409, "session_already_authenticated", "Log out before switching managed accounts");
    const request = await body(req);
    if (state.deviceFlow && state.deviceBeginRequestId === request.client_request_id) {
      return json(res, 202, deviceFlowProjection(state));
    }
    state.deviceBeginRequestId = request.client_request_id;
    state.deviceFlow = {
      flow_id: `devflow_${"d".repeat(32)}`,
      status: "pending",
      user_code: "GA7K-2Q9P",
      verification_url: "https://login.example.test/device",
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
      poll_interval_seconds: 2,
      next_poll_at: new Date(Date.now() + 2_000).toISOString(),
      restart_required: false,
      restart_scheduled: false,
      session_generation: null,
      error_code: null,
    };
    return json(res, 202, deviceFlowProjection(state));
  }
  const deviceMatch = path.match(/^\/api\/v1\/session\/device\/(devflow_[0-9a-f]{32})$/);
  if (deviceMatch && req.method === "GET") {
    const current = deviceFlowProjection(state);
    return current && current.flow_id === deviceMatch[1]
      ? json(res, 200, current)
      : apiError(res, 404, "device_flow_not_found", "Device login flow not found");
  }
  const devicePollMatch = path.match(/^\/api\/v1\/session\/device\/(devflow_[0-9a-f]{32})\/poll$/);
  if (devicePollMatch && req.method === "POST") {
    const current = deviceFlowProjection(state);
    if (!current || current.flow_id !== devicePollMatch[1]) {
      return apiError(res, 404, "device_flow_not_found", "Device login flow not found");
    }
    await body(req);
    state.deviceFlow.status = "authorized";
    state.deviceFlow.restart_required = true;
    state.deviceFlow.session_generation = 2;
    state.deviceFlow.next_poll_at = new Date().toISOString();
    return json(res, 200, deviceFlowProjection(state, { restartScheduled: true }));
  }
  if (path === "/api/v1/connectors" && req.method === "GET") return json(res, 200, connectorCatalog());
  if (path === "/api/v1/extensions" && req.method === "GET") {
    return json(res, 200, extensionCatalog(state));
  }
  const extensionActionMatch = path.match(
    /^\/api\/v1\/extensions\/([^/]+)\/(enable|disable|health|rollback)$/,
  );
  if (extensionActionMatch && req.method === "POST") {
    const request = await body(req);
    if (
      !Number.isInteger(request.expected_revision)
      || typeof request.client_request_id !== "string"
      || !request.client_request_id
      || Object.keys(request).sort().join(",") !== "client_request_id,expected_revision"
    ) {
      return apiError(res, 422, "invalid_extension_mutation", "Extension mutation payload is invalid");
    }
    const extensionId = decodeURIComponent(extensionActionMatch[1]);
    const extension = state.extensions.find((candidate) => candidate.extension_id === extensionId);
    if (!extension) return apiError(res, 404, "extension_not_found", "Extension was not found");
    const actionId = extensionActionMatch[2] === "health"
      ? "health_check"
      : extensionActionMatch[2];
    const fingerprint = `${extensionId}:${actionId}:${request.expected_revision}`;
    const previous = state.extensionRequests.get(request.client_request_id);
    if (previous) {
      if (previous.fingerprint !== fingerprint) {
        return apiError(
          res,
          409,
          "extension_idempotency_conflict",
          "Extension request identity was already used for different content",
          { current_revision: extension.revision },
        );
      }
      return json(res, 200, previous.response);
    }
    if (request.expected_revision !== extension.revision) {
      return apiError(
        res,
        409,
        "extension_revision_conflict",
        "Extension revision changed; refresh and retry",
        { current_revision: extension.revision },
      );
    }
    const action = extensionActions(extension).find((candidate) => candidate.action_id === actionId);
    if (!action?.enabled) {
      return apiError(
        res,
        409,
        "extension_action_unavailable",
        action?.disabled_reason || "Extension action is unavailable",
        { current_revision: extension.revision },
      );
    }
    if (actionId === "enable") {
      extension.status = "enabled";
      extension.health = "unknown";
    } else if (actionId === "disable") {
      extension.status = "disabled";
      extension.health = "unknown";
    } else if (actionId === "health_check") {
      extension.health = "healthy";
      extension.last_error_code = null;
    } else if (actionId === "rollback") {
      extension.active_version = extension.rollback_version;
      extension.active_revision_id = `${extension.active_revision_id}-rollback`;
      extension.health = "unknown";
    }
    extension.revision += 1;
    extension.updated_at = new Date().toISOString();
    const extensions = extensionCatalog(state);
    const response = {
      extension: extensions.items.find((candidate) => candidate.extension_id === extensionId),
      extensions,
    };
    state.extensionRequests.set(request.client_request_id, { fingerprint, response });
    return json(res, 200, response);
  }
  if (path === "/api/v1/update" && req.method === "GET") return json(res, 200, { update: bootstrap(state).update });
  if (path === "/api/v1/update/check" && req.method === "POST") return json(res, 200, { update: bootstrap(state).update });

  if (path === "/api/v1/settings/permissions" && req.method === "PUT") {
    if (!state.authenticated) return apiError(res, 401, "managed_session_unavailable", "Managed login is required");
    const request = await body(req);
    if (request.expected_revision !== state.permissionRevision) {
      return apiError(res, 409, "stale_permission_revision", "Permission revision changed; refresh and retry");
    }
    if (request.profile !== "default" && request.profile !== "full_access") {
      return apiError(res, 422, "invalid_permission_profile", "Unknown permission profile");
    }
    state.permissionProfile = request.profile;
    state.permissionRevision += 1;
    return json(res, 200, { permissions: bootstrap(state).permissions });
  }

  if (path === "/api/v1/threads" && req.method === "GET") {
    return json(res, 200, { items: state.threads, next_cursor: null });
  }
  if (path === "/api/v1/threads" && req.method === "POST") {
    if (!state.authenticated) return apiError(res, 401, "managed_session_unavailable", "Managed login is required");
    const created = thread("thread-ga", null);
    state.threads = [created];
    state.projection = { thread: created, turns: [], items: [], jobs: [], interactions: [], watermark: 0 };
    state.projections = new Map([[created.thread_id, state.projection]]);
    state.seq = 0;
    return json(res, 201, created);
  }

  const projectionMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/projection$/);
  if (projectionMatch && req.method === "GET") {
    const threadId = decodeURIComponent(projectionMatch[1]);
    const delay = state.projectionDelays.get(threadId) ?? 0;
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    const projection = projectionResponse(state, threadId);
    return projection ? json(res, 200, projection) : apiError(res, 404, "thread_not_found", "Thread not found");
  }

  const replayMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/replay$/);
  if (replayMatch && req.method === "GET") {
    const projection = projectionResponse(state, decodeURIComponent(replayMatch[1]));
    if (!projection) return apiError(res, 404, "thread_not_found", "Thread not found");
    return json(res, 200, {
      projection,
      interactions: projection.interactions ?? [],
      live_replay_turn_ids: projection.turns
        .filter((candidate) => TERMINAL_TURN_STATUSES.has(candidate.status))
        .map((candidate) => candidate.turn_id),
      source_watermark: projection.watermark,
      through_seq: projection.watermark,
      event_count: Math.max(1, state.seq),
      event_digest: "d".repeat(64),
    });
  }

  const liveReplayMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/replay\/live$/);
  if (liveReplayMatch && req.method === "POST") {
    const threadId = decodeURIComponent(liveReplayMatch[1]);
    const projection = projectionResponse(state, threadId);
    if (!projection) return apiError(res, 404, "thread_not_found", "Thread not found");
    const request = await body(req);
    if (request.confirmed !== true) {
      return apiError(res, 422, "live_replay_confirmation_required", "Live Replay requires explicit confirmation");
    }
    const source = projection.turns.find((candidate) => candidate.turn_id === request.source_turn_id);
    if (!source) return apiError(res, 404, "turn_not_found", "Source Turn not found");
    if (!TERMINAL_TURN_STATUSES.has(source.status)) {
      return apiError(res, 409, "turn_not_terminal", "Only a terminal Turn can be replayed");
    }
    const requestId = String(request.client_request_id ?? "");
    const existing = state.liveReplayRequests.get(requestId);
    if (existing) {
      return existing.source_turn_id === source.turn_id
        ? json(res, 202, existing)
        : apiError(res, 409, "client_request_id_conflict", "Replay request id was reused with a different source Turn");
    }
    const replayTurn = turn(
      `turn-live-replay-${state.liveReplayRequests.size + 1}`,
      "queued",
      source.input,
      source.agent_model_id,
      source.image_model_id,
    );
    replayTurn.metadata = {
      ...source.metadata,
      _replay: {
        mode: "live",
        source_thread_id: threadId,
        source_turn_id: source.turn_id,
        reuse_external_side_effects: false,
      },
    };
    projection.turns.push(replayTurn);
    const accepted = envelope(state, "turn.accepted", {
      turnId: replayTurn.turn_id,
      payload: {
        input: replayTurn.input,
        agent_model_id: replayTurn.agent_model_id,
        image_model_id: replayTurn.image_model_id,
        metadata: replayTurn.metadata,
      },
    });
    emit(state, accepted);
    const response = {
      source_thread_id: threadId,
      source_turn_id: source.turn_id,
      causation_event_id: "event-source-ga",
      replay: { turn: replayTurn, job: null, watermark: state.seq },
      permission_snapshot_id: bootstrap(state).permissions.snapshot_id,
      extension_snapshot_id: extensionCatalog(state).snapshot_id,
    };
    state.liveReplayRequests.set(requestId, response);
    return json(res, 202, response);
  }

  const streamMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/events\/stream$/);
  if (streamMatch && req.method === "GET") {
    const threadId = decodeURIComponent(streamMatch[1]);
    if (!projectionResponse(state, threadId)) return apiError(res, 404, "thread_not_found", "Thread not found");
    res.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type": "text/event-stream; charset=utf-8",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    res.write(`: ga-stream\n\nevent: watermark\ndata: {"watermark":${state.seq}}\n\n`);
    state.clients.add(res);
    req.on("close", () => state.clients.delete(res));
    scheduleThinkingTerminal(state);
    return true;
  }

  const createTurnMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/turns$/);
  if (createTurnMatch && req.method === "POST") {
    if (!state.authenticated) return apiError(res, 401, "managed_session_unavailable", "Managed login is required");
    const request = await body(req);
    if (!state.projection) return apiError(res, 404, "thread_not_found", "Thread not found");
    const activeTurn = turn(
      `turn-ga-${state.projection.turns.length + 1}`,
      "model_requested",
      String(request.input ?? ""),
      String(request.agent_model_id ?? ""),
      request.image_model_id == null ? null : String(request.image_model_id),
    );
    activeTurn.client_message_id = String(request.client_message_id ?? activeTurn.client_message_id);
    const userItem = item(`item-user-${state.projection.items.length + 1}`, activeTurn.turn_id, "user", activeTurn.input);
    state.projection.turns.push(activeTurn);
    state.projection.items.push(userItem);
    scheduleTurnCompletion(
      state,
      activeTurn,
      /(?:image|图|海报)/iu.test(activeTurn.input)
        ? activeTurn.image_model_id
        : null,
    );
    return json(res, 201, threadMutationResponse(state, activeTurn));
  }

  const queueMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/queue$/);
  if (queueMatch && req.method === "POST") {
    const request = await body(req);
    if (!state.projection) return apiError(res, 404, "thread_not_found", "Thread not found");
    const queued = turn(
      `turn-queued-${state.projection.turns.length + 1}`,
      "queued",
      String(request.input ?? ""),
      String(request.agent_model_id ?? ""),
      request.image_model_id == null ? null : String(request.image_model_id),
    );
    queued.client_message_id = String(request.client_message_id ?? queued.client_message_id);
    state.projection.turns.push(queued);
    state.projection.items.push(item(`item-queued-${state.projection.items.length + 1}`, queued.turn_id, "user", queued.input));
    return json(res, 201, threadMutationResponse(state, queued));
  }

  const interruptMatch = path.match(/^\/api\/v1\/turns\/([^/]+)\/interrupt$/);
  if (interruptMatch && req.method === "POST") {
    const activeTurn = state.projection?.turns.find((candidate) => candidate.turn_id === decodeURIComponent(interruptMatch[1]));
    if (!activeTurn) return apiError(res, 404, "turn_not_found", "Turn not found");
    activeTurn.status = "interrupted";
    activeTurn.terminal_reason = "interrupted_by_user";
    emit(state, envelope(state, "turn.status_changed", { turnId: activeTurn.turn_id, payload: { from: "retry_wait", to: "interrupted", reason: "interrupted_by_user" } }));
    return json(res, 200, threadMutationResponse(state, activeTurn));
  }

  const connectorLoginMatch = path.match(
    /^\/api\/v1\/interactions\/([^/]+)\/connector-login\/(begin|check|cancel)$/,
  );
  if (connectorLoginMatch && req.method === "POST") {
    const interactionId = decodeURIComponent(connectorLoginMatch[1]);
    const operation = connectorLoginMatch[2];
    const pending = state.projection?.interactions.find(
      (candidate) => candidate.interaction_id === interactionId,
    );
    if (!pending || pending.kind !== "connector_login") {
      return apiError(res, 404, "interaction_not_found", "Connector login interaction not found");
    }
    const request = await body(req);
    if (!request || typeof request !== "object" || Object.keys(request).length) {
      return apiError(res, 422, "connector_login_invalid_request", "Connector login lifecycle body must be empty");
    }
    const connector = pending.contract.connector;
    const activeTurn = state.projection.turns.find(
      (candidate) => candidate.turn_id === pending.turn_id,
    );
    if (operation === "begin") {
      state.connectorLoginBeginCount += 1;
      connector.state = "awaiting_callback";
      return json(res, 200, {
        interaction_id: interactionId,
        connector_id: connector.connector_id,
        state: "awaiting_callback",
        authorization_url: state.scenario === "connector-device"
          ? null
          : "https://open.feishu.cn/authorize?state=ga",
        verification_url: state.scenario === "connector-device"
          ? "https://open.feishu.cn/device"
          : null,
        user_code: state.scenario === "connector-device" ? "ECX-2048" : null,
        expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
      });
    }
    if (operation === "check") {
      state.connectorLoginCheckCount += 1;
      if (state.scenario === "connector-reauth") {
        connector.state = "reauthorization_required";
        return json(res, 202, {
          interaction_id: interactionId,
          connector_id: connector.connector_id,
          connected: false,
          state: "reauthorization_required",
          reason: "missing_required_scope",
        });
      }
      if (state.scenario === "connector-restart") {
        connector.state = "authorization_required";
        return json(res, 202, {
          interaction_id: interactionId,
          connector_id: connector.connector_id,
          connected: false,
          state: "authorization_required",
          reason: "auth_completion_interrupted",
        });
      }
      if (state.connectorLoginCheckCount === 1) {
        connector.state = "awaiting_callback";
        return json(res, 202, {
          interaction_id: interactionId,
          connector_id: connector.connector_id,
          connected: false,
          state: "awaiting_callback",
        });
      }
      pending.status = "resolved";
      pending.response = { action_id: "check_status", values: {} };
      pending.response_client_request_id = "connector-check-ga";
      if (activeTurn) activeTurn.status = "preparing";
      emit(state, envelope(state, "interaction.resolved", {
        turnId: pending.turn_id,
        itemId: pending.interaction_id,
        jobId: pending.job_id,
        payload: {
          response: pending.response,
          client_request_id: pending.response_client_request_id,
        },
      }));
      const mutation = {
        interaction: pending,
        turn: activeTurn ?? null,
        job: null,
        watermark: state.seq,
      };
      return json(res, 200, {
        interaction_id: interactionId,
        connector_id: connector.connector_id,
        connected: true,
        state: "connected",
        authority_refresh_revision_id: "revision-authority-ga",
        mutation,
      });
    }
    state.connectorLoginCancelCount += 1;
    pending.status = "resolved";
    pending.response = { action_id: "cancel", values: {} };
    pending.response_client_request_id = "connector-cancel-ga";
    if (activeTurn) activeTurn.status = "cancelled";
    emit(state, envelope(state, "interaction.resolved", {
      turnId: pending.turn_id,
      itemId: pending.interaction_id,
      jobId: pending.job_id,
      payload: {
        response: pending.response,
        client_request_id: pending.response_client_request_id,
      },
    }));
    return json(res, 200, {
      interaction_id: interactionId,
      connector_id: connector.connector_id,
      cancelled: true,
      mutation: {
        interaction: pending,
        turn: activeTurn ?? null,
        job: null,
        watermark: state.seq,
      },
    });
  }

  const respondMatch = path.match(/^\/api\/v1\/interactions\/([^/]+)\/respond$/);
  if (respondMatch && req.method === "POST") {
    const pending = state.projection?.interactions.find((candidate) => candidate.interaction_id === decodeURIComponent(respondMatch[1]));
    if (!pending) return apiError(res, 404, "interaction_not_found", "Interaction not found");
    state.interactionRespondCount += 1;
    if (pending.kind === "connector_login") {
      return apiError(res, 409, "connector_login_dedicated_route_required", "Connector login uses dedicated lifecycle routes");
    }
    const request = await body(req);
    pending.status = "resolved";
    pending.response = request.response ?? {};
    pending.response_client_request_id = String(request.client_request_id ?? "");
    const activeTurn = state.projection.turns.find((candidate) => candidate.turn_id === pending.turn_id);
    if (activeTurn) activeTurn.status = "tool_running";
    emit(state, envelope(state, "interaction.resolved", {
      turnId: pending.turn_id,
      itemId: pending.interaction_id,
      jobId: pending.job_id,
      payload: {
        response: pending.response,
        client_request_id: pending.response_client_request_id,
      },
    }));
    return json(res, 200, { interaction: pending, turn: activeTurn ?? null, job: null, watermark: state.seq });
  }

  if (path === "/api/v1/artifacts" && req.method === "GET") {
    return json(res, 200, { items: state.artifacts, count: state.artifacts.length });
  }
  const blobMatch = path.match(/^\/api\/v1\/artifacts\/([^/]+)\/(preview|content)$/);
  if (blobMatch && req.method === "GET") {
    const found = state.artifacts.find((candidate) => candidate.artifact_id === decodeURIComponent(blobMatch[1]));
    if (!found) return apiError(res, 404, "artifact_not_found", "Artifact not found");
    res.writeHead(200, { "Cache-Control": "no-store", "Content-Type": "image/png", "Content-Length": PNG.length });
    res.end(PNG);
    return true;
  }
  const feedbackMatch = path.match(/^\/api\/v1\/artifacts\/([^/]+)\/feedback$/);
  if (feedbackMatch && req.method === "POST") {
    const found = state.artifacts.find((candidate) => candidate.artifact_id === decodeURIComponent(feedbackMatch[1]));
    if (!found) return apiError(res, 404, "artifact_not_found", "Artifact not found");
    const request = await body(req);
    found.feedback = { feedback_id: `feedback-${state.seq + 1}`, revision_id: found.revision_id, signal: request.signal, recorded_at: new Date().toISOString() };
    emit(state, envelope(state, "artifact.feedback_recorded", { payload: { artifact_id: found.artifact_id } }));
    return json(res, 200, found.feedback);
  }
  const externalActionMatch = path.match(
    /^\/api\/v1\/artifacts\/([^/]+)\/actions\/(open|reveal)$/,
  );
  if (externalActionMatch && req.method === "POST") {
    const artifactId = decodeURIComponent(externalActionMatch[1]);
    const action = externalActionMatch[2];
    const found = state.artifacts.find((candidate) => candidate.artifact_id === artifactId);
    if (!found) return apiError(res, 404, "ARTIFACT_NOT_FOUND", "Artifact not found");
    if (!found.actions.includes(action)) {
      return apiError(res, 409, "ARTIFACT_ACTION_UNAVAILABLE", "Artifact action unavailable");
    }
    const request = await body(req);
    if (
      typeof request.client_request_id !== "string"
      || !request.client_request_id
      || Object.keys(request).some((key) => key !== "client_request_id")
    ) {
      return apiError(res, 422, "ARTIFACT_INVALID_REQUEST", "Only client_request_id is accepted");
    }
    const key = `${artifactId}:${request.client_request_id}`;
    const existing = state.artifactActions.get(key);
    if (existing) {
      return existing.action === action
        ? json(res, 200, existing)
        : apiError(res, 409, "ARTIFACT_IDEMPOTENCY_CONFLICT", "Request id was reused");
    }
    const timestamp = new Date().toISOString();
    const receipt = {
      artifact_id: artifactId,
      revision_id: found.revision_id,
      action,
      client_request_id: request.client_request_id,
      status: "completed",
      requested_at: timestamp,
      updated_at: timestamp,
      failure_code: null,
    };
    state.artifactActions.set(key, receipt);
    emit(state, envelope(state, "artifact.action.requested", {
      payload: { artifact_id: artifactId, revision_id: found.revision_id, action },
    }));
    return json(res, 200, receipt);
  }

  const openWorkspaceMatch = path.match(/^\/api\/v1\/artifacts\/([^/]+)\/retouch-workspaces$/);
  if (openWorkspaceMatch && req.method === "POST") {
    const source = state.artifacts.find((candidate) => candidate.artifact_id === decodeURIComponent(openWorkspaceMatch[1]));
    if (!source) return apiError(res, 404, "ARTIFACT_NOT_FOUND", "Artifact not found");
    const request = await body(req);
    const existing = [...state.retouchWorkspaces.values()].find((candidate) => (
      candidate.artifact_id === source.artifact_id
      && candidate.edit_surface.base_revision_id === request.base_revision_id
    ));
    if (existing) return json(res, 201, workspaceWire(existing));
    state.retouchCounter += 1;
    const workspace = {
      workspace_id: `rtw-ga-${state.retouchCounter}`,
      artifact_id: source.artifact_id,
      version: 1,
      status: "editing",
      edit_surface: editSurface(source),
      annotations: [],
      references: [],
      global_instruction: "",
      view_state: {},
      mask: null,
      submitted_job_id: null,
      job: null,
      result: null,
      result_surface: null,
      created_at: NOW,
      updated_at: NOW,
    };
    state.retouchWorkspaces.set(workspace.workspace_id, workspace);
    return json(res, 201, workspaceWire(workspace));
  }

  const workspaceMatch = path.match(/^\/api\/v1\/retouch-workspaces\/([^/]+)$/);
  if (workspaceMatch && req.method === "GET") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceMatch[1]));
    return workspace
      ? json(res, 200, workspaceWire(workspace))
      : apiError(res, 404, "ARTIFACT_NOT_FOUND", "Retouch workspace not found");
  }
  if (workspaceMatch && req.method === "PATCH") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceMatch[1]));
    if (!workspace) return apiError(res, 404, "ARTIFACT_NOT_FOUND", "Retouch workspace not found");
    const request = await body(req);
    if (workspace.status !== "editing" || request.expected_version !== workspace.version) {
      return apiError(res, 409, "ARTIFACT_RETOUCH_CONFLICT", "Retouch workspace version is stale");
    }
    if (!Array.isArray(request.reference_artifact_ids) || request.reference_artifact_ids.length > 10) {
      return apiError(res, 422, "ARTIFACT_INVALID_REQUEST", "At most ten reference images are allowed");
    }
    workspace.annotations = Array.isArray(request.annotations) ? request.annotations : [];
    workspace.references = request.reference_artifact_ids.map((artifactId) => {
      const reference = state.artifacts.find((candidate) => candidate.artifact_id === artifactId);
      return {
        artifact_id: reference.artifact_id,
        revision_id: reference.revision_id,
        display_name: reference.display_name,
        mime_type: reference.mime_type,
        sha256: reference.sha256,
      };
    });
    workspace.global_instruction = String(request.global_instruction ?? "");
    workspace.view_state = request.view_state ?? {};
    workspace.version += 1;
    workspace.updated_at = new Date().toISOString();
    workspace.mask = workspace.annotations.length ? {
      schema_version: 1,
      coordinate_space_version: "oriented-normalized-v1",
      width_px: 2,
      height_px: 2,
      sha256: "c".repeat(64),
      size_bytes: PNG.length,
      covered_fraction: 0.25,
      pixel_regions: workspace.annotations.map(() => ({ x: 0, y: 0, width: 1, height: 1 })),
    } : null;
    return json(res, 200, workspaceWire(workspace));
  }

  const workspaceBlobMatch = path.match(/^\/api\/v1\/retouch-workspaces\/([^/]+)\/(surface|result)$/);
  if (workspaceBlobMatch && req.method === "GET") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceBlobMatch[1]));
    if (!workspace || (workspaceBlobMatch[2] === "result" && !workspace.result)) {
      return apiError(res, 409, "ARTIFACT_ACTION_UNAVAILABLE", "Retouch image is not ready");
    }
    res.writeHead(200, { "Cache-Control": "no-store", "Content-Type": "image/png", "Content-Length": PNG.length });
    res.end(PNG);
    return true;
  }
  const workspaceReferenceMatch = path.match(/^\/api\/v1\/retouch-workspaces\/([^/]+)\/references\/([^/]+)\/preview$/);
  if (workspaceReferenceMatch && req.method === "GET") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceReferenceMatch[1]));
    const referenceId = decodeURIComponent(workspaceReferenceMatch[2]);
    if (!workspace?.references.some((reference) => reference.artifact_id === referenceId)) {
      return apiError(res, 404, "ARTIFACT_NOT_FOUND", "Retouch reference not found");
    }
    res.writeHead(200, { "Cache-Control": "no-store", "Content-Type": "image/png", "Content-Length": PNG.length });
    res.end(PNG);
    return true;
  }

  const workspaceSubmitMatch = path.match(/^\/api\/v1\/retouch-workspaces\/([^/]+)\/submit$/);
  if (workspaceSubmitMatch && req.method === "POST") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceSubmitMatch[1]));
    if (!workspace) return apiError(res, 404, "ARTIFACT_NOT_FOUND", "Retouch workspace not found");
    const request = await body(req);
    if (workspace.status === "editing" && request.expected_version !== workspace.version) {
      return apiError(res, 409, "ARTIFACT_RETOUCH_CONFLICT", "Retouch workspace version is stale");
    }
    if (workspace.status === "editing") {
      const source = state.artifacts.find((candidate) => candidate.artifact_id === workspace.artifact_id);
      const baseRevision = workspace.edit_surface.base_revision_id;
      const jobId = `retouch-workspace-job-${state.seq + 1}`;
      workspace.status = "submitted";
      workspace.version += 1;
      workspace.submitted_job_id = jobId;
      workspace.job = {
        job_id: jobId,
        artifact_id: source.artifact_id,
        base_revision_id: baseRevision,
        request: {
          base_revision_id: baseRevision,
          selected_artifact_ids: [source.artifact_id],
          annotations: workspace.annotations,
          reference_artifact_ids: workspace.references.map((reference) => reference.artifact_id),
          global_instruction: workspace.global_instruction,
          client_request_id: request.client_request_id,
          edit_surface: workspace.edit_surface,
          mask: workspace.mask,
        },
        status: "queued",
        created_at: NOW,
        result_revision_id: null,
        change_summary: null,
        inspection_regions: [],
        failure_reason: null,
      };
      schedule(state, () => {
        if (!state.projection) return;
        const result = {
          ...source,
          revision_id: `revision-retouch-${state.seq + 1}`,
          display_name: "精准修图_20260710-1534_01.png",
          lineage: { source_artifact_ids: [source.artifact_id], supersedes_revision_id: baseRevision },
        };
        Object.assign(source, result);
        workspace.result = { ...result };
        workspace.result_surface = editSurface(result);
        workspace.job.status = "completed";
        workspace.job.result_revision_id = result.revision_id;
        workspace.job.change_summary = "已移除标注区域中的干扰物，保留主体轮廓与整体光线。";
        workspace.job.inspection_regions = workspace.annotations.map((annotation) => ({
          normalized_geometry: annotation.normalized_geometry,
          summary: annotation.instruction,
        }));
        const artifactItem = {
          item_id: `item-${result.revision_id}`,
          thread_id: "thread-ga",
          turn_id: "turn-ga",
          kind: "artifact",
          status: "completed",
          content: {
            retouch_job_id: jobId,
            artifact: result,
            change_summary: workspace.job.change_summary,
            inspection_regions: workspace.job.inspection_regions,
          },
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        state.projection.items.push(artifactItem);
        emit(state, envelope(state, "item.created", { itemId: artifactItem.item_id, payload: { kind: "artifact", status: "completed", content: artifactItem.content } }));
      }, 350);
    }
    return json(res, 202, workspaceWire(workspace));
  }

  const workspaceReopenMatch = path.match(/^\/api\/v1\/retouch-workspaces\/([^/]+)\/reopen$/);
  if (workspaceReopenMatch && req.method === "POST") {
    const workspace = state.retouchWorkspaces.get(decodeURIComponent(workspaceReopenMatch[1]));
    if (!workspace || !["failed", "cancelled"].includes(workspace.job?.status)) {
      return apiError(res, 409, "ARTIFACT_RETOUCH_CONFLICT", "Retouch job cannot reopen");
    }
    workspace.status = "editing";
    workspace.version += 1;
    workspace.job = null;
    workspace.submitted_job_id = null;
    return json(res, 200, workspaceWire(workspace));
  }

  const retouchMatch = path.match(/^\/api\/v1\/artifacts\/([^/]+)\/retouch$/);
  if (retouchMatch && req.method === "POST") {
    const source = state.artifacts.find((candidate) => candidate.artifact_id === decodeURIComponent(retouchMatch[1]));
    if (!source) return apiError(res, 404, "artifact_not_found", "Artifact not found");
    const request = await body(req);
    const retouchTurn = turn(
      `turn-retouch-${state.seq + 1}`,
      "queued",
      String(request.global_instruction ?? "精准修图"),
      String(request.agent_model_id ?? ""),
      String(request.image_model_id ?? ""),
    );
    state.projection?.turns.push(retouchTurn);
    emit(state, envelope(state, "turn.accepted", {
      turnId: retouchTurn.turn_id,
      payload: {
        input: retouchTurn.input,
        agent_model_id: retouchTurn.agent_model_id,
        image_model_id: retouchTurn.image_model_id,
        metadata: { retouch: true },
      },
    }));
    schedule(state, () => {
      if (!state.projection) return;
      const result = artifact(`artifact-retouch-${state.seq + 1}`, `revision-retouch-${state.seq + 1}`, "精准修图_20260710-1534_01.png");
      result.lineage = { source_artifact_ids: [source.artifact_id], supersedes_revision_id: source.revision_id };
      state.artifacts.push(result);
      const artifactItem = {
        item_id: `item-${result.artifact_id}`,
        thread_id: "thread-ga",
        turn_id: retouchTurn.turn_id,
        kind: "artifact",
        status: "completed",
        content: {
          retouch_job_id: `retouch-${state.seq + 1}`,
          artifact: result,
          change_summary: "已移除标注区域中的干扰物，保留主体轮廓与整体光线。",
          inspection_regions: [
            { normalized_geometry: { x: 0.12, y: 0.18, width: 0.3, height: 0.24 }, summary: "主体区域已检查" },
            { normalized_geometry: { x: 0.72, y: 0.22 }, summary: "标题边缘已检查" },
          ],
          preview: { artifact_id: result.artifact_id, revision_id: result.revision_id, mime_type: result.mime_type },
        },
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      state.projection.items.push(artifactItem);
      emit(state, envelope(state, "item.created", { turnId: retouchTurn.turn_id, itemId: artifactItem.item_id, payload: { kind: "artifact", status: "completed", content: artifactItem.content } }));
      emit(state, envelope(state, "artifact.retouch.completed", { turnId: retouchTurn.turn_id, itemId: artifactItem.item_id, payload: { artifact_id: result.artifact_id, revision_id: result.revision_id, change_summary: artifactItem.content.change_summary, inspection_regions: artifactItem.content.inspection_regions } }));
      retouchTurn.status = "completed";
      retouchTurn.terminal_reason = "completed";
      emit(state, envelope(state, "turn.status_changed", { turnId: retouchTurn.turn_id, payload: { from: "tool_running", to: "completed", reason: "completed" } }));
    }, 350);
    return json(res, 202, {
      job_id: `job-retouch-${state.seq}`,
      artifact_id: source.artifact_id,
      base_revision_id: source.revision_id,
      status: "queued",
      client_request_id: request.client_request_id,
      created_at: NOW,
      result_revision_id: null,
      change_summary: null,
      inspection_regions: [],
      failure_reason: null,
    });
  }

  const sharesMatch = path.match(/^\/api\/v1\/threads\/([^/]+)\/shares$/);
  if (sharesMatch && req.method === "GET") return json(res, 200, { items: state.shares, count: state.shares.length });
  if (sharesMatch && req.method === "POST") {
    const request = await body(req);
    state.shareCounter += 1;
    const shareId = `share-ga-${state.shareCounter}`;
    const created = {
      share_id: shareId,
      thread_id: decodeURIComponent(sharesMatch[1]),
      source_watermark: state.seq,
      status: "published",
      public_url: `https://share.example.test/s/${shareId}`,
      expires_at: "2026-07-17T07:34:00.000Z",
      created_at: NOW,
      updated_at: NOW,
      revoked_at: null,
      error_code: null,
      client_request_id: request.client_request_id,
    };
    state.shares.unshift(created);
    return json(res, 201, created);
  }
  const shareMatch = path.match(/^\/api\/v1\/shares\/([^/]+)$/);
  if (shareMatch && req.method === "GET") {
    const found = state.shares.find((candidate) => candidate.share_id === decodeURIComponent(shareMatch[1]));
    return found ? json(res, 200, found) : apiError(res, 404, "share_not_found", "Share not found");
  }
  const revokeMatch = path.match(/^\/api\/v1\/shares\/([^/]+)\/revoke$/);
  if (revokeMatch && req.method === "POST") {
    const found = state.shares.find((candidate) => candidate.share_id === decodeURIComponent(revokeMatch[1]));
    if (!found) return apiError(res, 404, "share_not_found", "Share not found");
    found.status = "revoked";
    found.revoked_at = new Date().toISOString();
    found.updated_at = found.revoked_at;
    return json(res, 200, found);
  }

  return apiError(res, 404, "ga_route_not_found", `GA harness has no route for ${req.method} ${path}`);
}

function assertGaMethod(req, method = "GET") {
  if (req.method !== method) {
    throw new GaHarnessRequestError(
      "ga_method_not_allowed",
      `This GA harness route only accepts ${method}`,
      405,
    );
  }
}

function assertGaQuery(url, allowedNames) {
  const allowed = new Set(allowedNames);
  for (const name of url.searchParams.keys()) {
    if (!allowed.has(name)) {
      throw new GaHarnessRequestError(
        "ga_unknown_parameter",
        `Unsupported GA harness parameter: ${name}`,
      );
    }
  }
  for (const name of allowed) {
    if (url.searchParams.getAll(name).length > 1) {
      throw new GaHarnessRequestError(
        "ga_duplicate_parameter",
        `GA harness parameter may only be supplied once: ${name}`,
      );
    }
  }
}

function gaViewportOptions(url) {
  assertGaQuery(url, ["viewport", "theme", "scenario"]);
  const viewportId = url.searchParams.get("viewport") || "1440x900";
  const theme = url.searchParams.get("theme") || "light";
  const scenario = url.searchParams.get("scenario") || "artifact";
  const viewport = GA_VIEWPORTS[viewportId];
  if (!viewport) {
    throw new GaHarnessRequestError("ga_unknown_viewport", `Unknown GA viewport: ${viewportId}`);
  }
  if (!GA_THEMES.has(theme)) {
    throw new GaHarnessRequestError("ga_unknown_theme", `Unknown GA theme: ${theme}`);
  }
  if (!SCENARIOS.has(scenario)) {
    throw new GaHarnessRequestError("ga_unknown_scenario", `Unknown GA scenario: ${scenario}`);
  }
  return { viewportId, viewport, theme, scenario };
}

function gaFrameOptions(url) {
  assertGaQuery(url, ["theme", "scenario"]);
  const theme = url.searchParams.get("theme") || "light";
  const scenario = url.searchParams.get("scenario") || "artifact";
  if (!GA_THEMES.has(theme)) {
    throw new GaHarnessRequestError("ga_unknown_theme", `Unknown GA theme: ${theme}`);
  }
  if (!SCENARIOS.has(scenario)) {
    throw new GaHarnessRequestError("ga_unknown_scenario", `Unknown GA scenario: ${scenario}`);
  }
  return { theme, scenario };
}

function gaViewportRoute(viewportId, theme, scenario = "artifact") {
  return `/__ga/viewport?viewport=${encodeURIComponent(viewportId)}&theme=${encodeURIComponent(theme)}&scenario=${encodeURIComponent(scenario)}`;
}

function gaViewportMatrix() {
  const entries = [];
  for (const [viewportId, viewport] of Object.entries(GA_VIEWPORTS)) {
    for (const theme of GA_THEMES) {
      entries.push({
        matrix_id: `${viewportId}-${theme}`,
        viewport_id: viewportId,
        width: viewport.width,
        height: viewport.height,
        label: viewport.label,
        theme,
        scenario: "artifact",
        url: gaViewportRoute(viewportId, theme),
      });
    }
  }
  return {
    contract_version: "1.0",
    entries,
    checks: {
      viewport: "iframe contentWindow.innerWidth/innerHeight exactly match the declared CSS viewport",
      horizontal_overflow: "document and body content width do not exceed the iframe viewport by more than one CSS pixel",
      clickable_labels: "visible button, link, summary and role=button text occupies one rendered line",
      key_controls: ["navigation", "model_selector", "composer", "task_type", "artifact_shelf"],
    },
  };
}

function sendGaText(res, contentType, content, extraHeaders = {}) {
  const payload = Buffer.from(content, "utf8");
  res.writeHead(200, {
    "Cache-Control": "no-store",
    "Content-Type": contentType,
    "Content-Length": payload.length,
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  res.end(payload);
  return true;
}

function viewportHtml({ viewportId, viewport, theme, scenario }) {
  const frameQuery = `scenario=${encodeURIComponent(scenario)}&amp;theme=${encodeURIComponent(theme)}`;
  return `<!doctype html>
<html lang="zh-CN" data-theme="${theme}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>EcoreX GA · ${viewportId} · ${theme}</title>
    <link rel="stylesheet" href="/__ga/viewport.css" />
    <script src="/__ga/viewport.js" defer></script>
  </head>
  <body data-ga-status="pending">
    <header class="ga-header">
      <h1>EcoreX 响应式验收 · ${viewportId} · ${theme}</h1>
      <p>${viewport.label} · scenario=${scenario} · iframe 使用固定 CSS viewport；下方报告来自同源 frame 的实际布局。</p>
    </header>
    <main class="ga-stage">
      <iframe
        id="ga-viewport-frame"
        class="ga-viewport-frame"
        title="EcoreX ${viewportId} ${theme} 验收视口"
        width="${viewport.width}"
        height="${viewport.height}"
        data-expected-width="${viewport.width}"
        data-expected-height="${viewport.height}"
        data-theme="${theme}"
        data-scenario="${scenario}"
        referrerpolicy="no-referrer"
        src="/__ga/frame-app?${frameQuery}"
      ></iframe>
    </main>
    <pre id="ga-viewport-report" class="ga-report" aria-live="polite">正在等待 EcoreX 完成首屏渲染…</pre>
  </body>
</html>`;
}

function serveGaFrameApp(holder, distDir, res, url) {
  const { theme, scenario } = gaFrameOptions(url);
  const indexFile = resolve(distDir, "index.html");
  if (!existsSync(indexFile) || !statSync(indexFile).isFile()) {
    return apiError(res, 503, "dist_missing", "Run npm run build before npm run ga:serve");
  }
  const marker = "<!--__ECOREX_RUNTIME_CONFIG__-->";
  const source = readFileSync(indexFile, "utf8");
  if (!source.includes(marker)) {
    return apiError(res, 503, "ga_frame_marker_missing", "The built WebUI has no GA bootstrap insertion marker");
  }
  resetScenario(holder, scenario);
  const html = source
    .replace(
      marker,
      `<script src="/__ga/axe.js"></script><script src="/__ga/frame-bootstrap.js?theme=${encodeURIComponent(theme)}"></script>`,
    )
    .replace(/(["'])\.\/assets\//g, "$1/assets/");
  return sendGaText(res, "text/html; charset=utf-8", html, {
    "Content-Security-Policy": FRAME_APP_CSP,
    "X-Frame-Options": "SAMEORIGIN",
  });
}

function handleGaViewportHarness(holder, distDir, req, res, url) {
  const path = url.pathname;
  const viewportPaths = new Set([
    "/__ga/viewport-matrix",
    "/__ga/viewport",
    "/__ga/frame-app",
    "/__ga/frame-bootstrap.js",
    "/__ga/axe.js",
    "/__ga/viewport.css",
    "/__ga/viewport.js",
  ]);
  if (!viewportPaths.has(path)) {
    if (path.startsWith("/__ga/") && path !== "/__ga/state" && path !== "/__ga/reset") {
      return apiError(res, 404, "ga_harness_route_not_found", "Unknown GA harness route");
    }
    return false;
  }
  try {
    assertGaMethod(req);
    if (path === "/__ga/viewport-matrix") {
      assertGaQuery(url, []);
      return json(res, 200, gaViewportMatrix(), {
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
      });
    }
    if (path === "/__ga/viewport") {
      const options = gaViewportOptions(url);
      return sendGaText(res, "text/html; charset=utf-8", viewportHtml(options), {
        "Content-Security-Policy": VIEWPORT_CSP,
        "X-Frame-Options": "DENY",
      });
    }
    if (path === "/__ga/frame-app") return serveGaFrameApp(holder, distDir, res, url);
    if (path === "/__ga/frame-bootstrap.js") {
      assertGaQuery(url, ["theme"]);
      const theme = url.searchParams.get("theme") || "light";
      if (!GA_THEMES.has(theme)) {
        throw new GaHarnessRequestError("ga_unknown_theme", `Unknown GA theme: ${theme}`);
      }
      const script = `(() => { const theme = ${JSON.stringify(theme)}; document.documentElement.dataset.theme = theme; try { window.localStorage.setItem("ecorex-theme", theme); } catch {} Object.defineProperty(window, "__ECOREX_GA_FRAME__", { value: Object.freeze({ contract_version: "1.0", theme }), configurable: false }); })();\n`;
      return sendGaText(res, "text/javascript; charset=utf-8", script);
    }
    assertGaQuery(url, []);
    if (path === "/__ga/axe.js") {
      if (!existsSync(AXE_CORE) || !statSync(AXE_CORE).isFile()) {
        return apiError(res, 503, "axe_core_missing", "Run npm ci before the GA browser matrix");
      }
      return sendGaText(
        res,
        "text/javascript; charset=utf-8",
        readFileSync(AXE_CORE, "utf8"),
      );
    }
    if (path === "/__ga/viewport.css") return sendGaText(res, "text/css; charset=utf-8", VIEWPORT_CSS);
    return sendGaText(res, "text/javascript; charset=utf-8", VIEWPORT_JS);
  } catch (error) {
    if (error instanceof GaHarnessRequestError) {
      if (error.status === 405) res.setHeader("Allow", "GET");
      return apiError(
        res,
        error.status,
        error.code,
        error.message,
        error.status === 405 ? { allowed: ["GET"] } : {},
      );
    }
    throw error;
  }
}

function serveStatic(distDir, req, res, url) {
  const requested = url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname);
  const target = resolve(distDir, `.${requested}`);
  if (target !== distDir && !target.startsWith(`${distDir}${sep}`)) {
    return apiError(res, 400, "invalid_path", "Invalid static path");
  }
  const file = existsSync(target) && statSync(target).isFile() ? target : resolve(distDir, "index.html");
  if (!existsSync(file)) return apiError(res, 503, "dist_missing", "Run npm run build before npm run ga:serve");
  const stats = statSync(file);
  res.writeHead(200, {
    "Cache-Control": "no-store",
    "Content-Security-Policy": APP_CSP,
    "Content-Type": MIME[extname(file).toLowerCase()] || "application/octet-stream",
    "Content-Length": stats.size,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
  });
  createReadStream(file).pipe(res);
  return true;
}

export async function createGaMockServer({
  host = "127.0.0.1",
  port = 0,
  distDir = DEFAULT_DIST,
  scenario = "empty",
} = {}) {
  if (!SCENARIOS.has(scenario)) throw new Error(`Unknown GA scenario: ${scenario}`);
  const holder = { state: scenarioState(scenario) };
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", `http://${req.headers.host || `${host}:${port}`}`);
      const gaHandled = handleGaViewportHarness(holder, distDir, req, res, url);
      if (gaHandled !== false) return;
      const handled = await handleApi(holder, req, res, url);
      if (handled !== false) return;
      serveStatic(distDir, req, res, url);
    } catch (error) {
      if (!res.headersSent) apiError(res, 500, "ga_harness_error", error instanceof Error ? error.message : "GA harness failed");
      else res.end();
    }
  });
  await new Promise((resolvePromise, rejectPromise) => {
    server.once("error", rejectPromise);
    server.listen(port, host, resolvePromise);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("GA server did not expose a TCP address");
  return {
    server,
    url: `http://${host}:${address.port}`,
    reset(name) { return resetScenario(holder, name); },
    async close() {
      closeState(holder.state);
      await new Promise((resolvePromise, rejectPromise) => server.close((error) => error ? rejectPromise(error) : resolvePromise()));
    },
  };
}

const invokedPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (import.meta.url === invokedPath) {
  const portArgument = process.argv.find((value) => value.startsWith("--port="));
  const scenarioArgument = process.argv.find((value) => value.startsWith("--scenario="));
  const port = portArgument ? Number(portArgument.slice("--port=".length)) : 4179;
  const scenario = scenarioArgument ? scenarioArgument.slice("--scenario=".length) : "unauthenticated";
  const running = await createGaMockServer({ port, scenario });
  console.log(`EcoreX GA mock Runtime: ${running.url} (scenario=${scenario})`);
}

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const targetUrl = process.env.WEBUI_HANDTEST_URL || "";
const outputPath = process.env.ECOREX_CDP_OUTPUT_PATH || "";
const screenshotPath = process.env.ECOREX_CDP_SCREENSHOT_PATH || "";
const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
].filter(Boolean);

const imageSvgA = `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="960" viewBox="0 0 720 960">
  <rect width="720" height="960" fill="#efe7da"/>
  <rect x="72" y="90" width="576" height="740" rx="8" fill="#c3aa88"/>
  <rect x="120" y="218" width="480" height="118" fill="#7d5c44"/>
  <text x="110" y="178" font-family="Arial, sans-serif" font-size="56" font-weight="700" fill="#fff">上海中高端设计</text>
  <text x="110" y="256" font-family="Arial, sans-serif" font-size="48" font-weight="700" fill="#fff">公司地自我介绍</text>
  <rect x="120" y="410" width="210" height="260" fill="#ead8be"/>
  <rect x="360" y="410" width="210" height="260" fill="#5f3f2a"/>
  <circle cx="500" cy="736" r="48" fill="#e67818"/>
  <text x="110" y="870" font-family="Arial, sans-serif" font-size="28" fill="#fff">v0.3.1 CDP 发布验收图 A</text>
</svg>`;
const imageSvgB = `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="960" viewBox="0 0 720 960">
  <rect width="720" height="960" fill="#e8edf3"/>
  <rect x="84" y="120" width="552" height="700" rx="10" fill="#dee7ef"/>
  <text x="112" y="202" font-family="Arial, sans-serif" font-size="52" font-weight="700" fill="#263547">办公空间提案</text>
  <rect x="128" y="270" width="190" height="220" fill="#6a8faf"/>
  <rect x="352" y="270" width="190" height="220" fill="#90b77d"/>
  <rect x="128" y="550" width="414" height="110" fill="#f3c86b"/>
  <text x="112" y="870" font-family="Arial, sans-serif" font-size="28" fill="#263547">v0.3.1 CDP 发布验收图 B</text>
</svg>`;
const imageDataUrlA = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(imageSvgA)}`;
const imageDataUrlB = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(imageSvgB)}`;
const fixtureDir = process.env.ECOREX_CDP_WORKSPACE_DIR
  ? path.resolve(process.env.ECOREX_CDP_WORKSPACE_DIR, "v030-cdp")
  : path.join(process.cwd(), "tmp", "v030-cdp");
fs.mkdirSync(fixtureDir, { recursive: true });
const imagePathA = path.join(fixtureDir, "image-a.svg").replace(/\\/g, "/");
const imagePathB = path.join(fixtureDir, "image-b.svg").replace(/\\/g, "/");
fs.writeFileSync(imagePathA, imageSvgA);
fs.writeFileSync(imagePathB, imageSvgB);

const bridgeSource = `
(function () {
  "use strict";
  var now = Math.floor(Date.now() / 1000);
  var imgA = ${JSON.stringify(imageDataUrlA)};
  var imgB = ${JSON.stringify(imageDataUrlB)};
  var imagePathA = ${JSON.stringify(imagePathA)};
  var imagePathB = ${JSON.stringify(imagePathB)};
  var sessions = [
    { session_id: "v030-retouch-release", title: "v0.3.1 精修发布验收", updatedAt: new Date().toISOString(), created_at: new Date(Date.now() - 3600000).toISOString(), last_active: new Date().toISOString() },
    { session_id: "v030-active-turn", title: "v0.3.1 Active Turn Control", updatedAt: new Date(Date.now() - 1200000).toISOString(), created_at: new Date(Date.now() - 7200000).toISOString(), last_active: new Date(Date.now() - 1200000).toISOString() },
    { session_id: "v030-more-01", title: "会话更多 01", updatedAt: new Date(Date.now() - 2400000).toISOString(), created_at: new Date(Date.now() - 7400000).toISOString(), last_active: new Date(Date.now() - 2400000).toISOString() },
    { session_id: "v030-more-02", title: "会话更多 02", updatedAt: new Date(Date.now() - 2500000).toISOString(), created_at: new Date(Date.now() - 7500000).toISOString(), last_active: new Date(Date.now() - 2500000).toISOString() },
    { session_id: "v030-more-03", title: "会话更多 03", updatedAt: new Date(Date.now() - 2600000).toISOString(), created_at: new Date(Date.now() - 7600000).toISOString(), last_active: new Date(Date.now() - 2600000).toISOString() },
    { session_id: "v030-more-04", title: "会话更多 04", updatedAt: new Date(Date.now() - 2700000).toISOString(), created_at: new Date(Date.now() - 7700000).toISOString(), last_active: new Date(Date.now() - 2700000).toISOString() },
    { session_id: "v030-more-05", title: "会话更多 05", updatedAt: new Date(Date.now() - 2800000).toISOString(), created_at: new Date(Date.now() - 7800000).toISOString(), last_active: new Date(Date.now() - 2800000).toISOString() },
    { session_id: "v030-more-06", title: "会话更多 06", updatedAt: new Date(Date.now() - 2900000).toISOString(), created_at: new Date(Date.now() - 7900000).toISOString(), last_active: new Date(Date.now() - 2900000).toISOString() }
  ];
  var messageCalls = [];
  var queueActions = [];
  var activeRunning = true;
  var queuedRequestCounter = 0;
  var retouchArtifacts = [
    { id: "v030-retouch-a", requestId: "v030-retouch-request", request_id: "v030-retouch-request", kind: "image", intent: "deliverable", operation: "created", status: "ready", title: "上海中高端设计封面 A.png", path: imagePathA, previewUrl: imgA, preview_url: imgA, thumbnailUrl: imgA, thumbnail_url: imgA, mimeType: "image/svg+xml", mime_type: "image/svg+xml", taskIndex: 0, artifactIndex: 0, task_index: 0, artifact_index: 0 },
    { id: "v030-retouch-b", requestId: "v030-retouch-request", request_id: "v030-retouch-request", kind: "image", intent: "deliverable", operation: "created", status: "ready", title: "办公空间提案 B.png", path: imagePathB, previewUrl: imgB, preview_url: imgB, thumbnailUrl: imgB, thumbnail_url: imgB, mimeType: "image/svg+xml", mime_type: "image/svg+xml", taskIndex: 0, artifactIndex: 1, task_index: 0, artifact_index: 1 }
  ];
  var histories = {
    "v030-retouch-release": [
      { role: "user", content: "请基于这两张图片进行精准修图。", created_at: now - 420, seq: 1 },
      { role: "assistant", content: "已生成两张图片产物，可进入无限画布精修。\\n\\n生成产物：\\n" + imagePathA + "\\n" + imagePathB, created_at: now - 400, seq: 2, request_id: "v030-retouch-request", artifacts: retouchArtifacts, extras: { request_id: "v030-retouch-request", artifacts: retouchArtifacts } }
    ],
    "v030-active-turn": [
      { role: "user", content: "运行中我会插入新消息。", created_at: now - 320, seq: 1 },
      { role: "assistant", content: "Active turn control mock ready.", created_at: now - 300, seq: 2 }
    ]
  };
  function ok(payload) { return Object.assign({ status: "success" }, payload || {}); }
  function pathOf(input) { return typeof input === "string" ? input : input && input.path ? String(input.path) : "/"; }
  async function apiJson(input) {
    var rawPath = pathOf(input);
    var body = input && typeof input === "object" && input.body && typeof input.body === "object" ? input.body : {};
    var url = new URL(rawPath, "http://ecorex.local");
    var p = url.pathname;
    if (p === "/api/version") return ok({ version: "0.3.1", updateState: { status: "installed", version: "0.3.1", mode: "background" } });
    if (p === "/api/update-check") return ok({ currentVersion: "0.3.1", latestVersion: "0.3.1", hasUpdate: false, update: { webui: { connectorHealthCheck: { required: true, preserve: ["configured", "connected", "callable"] } } }, artifact: { id: "webui-windows-x64" } });
    if (p === "/api/sessions") return ok({ sessions: sessions, total: sessions.length });
    if (p === "/api/history") {
      var sessionId = url.searchParams.get("session_id") || "v030-retouch-release";
      var messages = histories[sessionId] || [];
      return ok({ messages: messages, context_start_seq: 0, total: messages.length, page: 1, page_size: 50, has_more: false });
    }
    if (p === "/api/active-requests") {
      var activeRequests = activeRunning ? [{
        request_id: "v030-active-request",
        session_id: "v030-active-turn",
        state: "running",
        status: "running",
        phase: "running",
        stream_available: false,
        created_at: Math.floor(Date.now() / 1000) - 30,
        updated_at: Math.floor(Date.now() / 1000),
        age_seconds: 30,
        actions: { stop: true, recover: true }
      }] : [];
      return ok({ requests: activeRequests, recentTerminalRequests: [], runStatusCounts: { running: activeRequests.length }, staleLocks: [] });
    }
    if (p === "/api/runtime-projection") return ok({ events: [], messages: [], requests: [] });
    if (p === "/api/tools") return ok({ tools: [{ name: "imagegen" }, { name: "browser" }] });
    if (p === "/api/skills") return ok({ skills: [] });
    if (p === "/api/extensions") return ok({ extensions: [], count: 0, summary: {} });
    if (p === "/api/channels") return ok({ channels: [] });
    if (p === "/api/models") return ok({ providers: [], capabilities: {}, currentProvider: "openai", currentModel: "gpt-5.6-luna" });
    if (p === "/api/scheduler") return ok({ enabled: false, initialized: false, running: false, serviceStatus: "unavailable", tasks: [], taskCount: 0, counts: { total: 0, enabled: 0, disabled: 0, error: 0 } });
    if (p === "/api/external-connections") return ok({ schema: "ecorex.external-connectors.implemented.v1", connections: [] });
    if (p === "/api/tool-permissions") return ok({ mode: "smart-ask", grantsCount: 0, auditPath: "mock-permissions.json" });
    if (p === "/cancel") {
      activeRunning = false;
      return ok({ cancelled: 1, request_id: String(body.request_id || "v030-active-request") });
    }
    if (p === "/message") {
      messageCalls.push(JSON.parse(JSON.stringify(body || {})));
      var mode = String(body.interrupt_mode || "replace");
      if (mode === "queue") {
        queuedRequestCounter += 1;
        return ok({
          request_id: "v030-queued-request-" + queuedRequestCounter,
          stream: true,
          queued: true,
          queue_position: 1,
          same_session: {
            decision: "queued",
            queue_position: 1,
            interrupt_mode: "queue",
            active_request_ids: ["v030-active-request"],
            queued_request_id: "v030-queued-request-" + queuedRequestCounter
          }
        });
      }
      if (mode === "branch") {
        return ok({
          request_id: "v030-branch-request",
          stream: false,
          inline_reply: "已在新分支按最新消息执行。",
          same_session: { decision: "accepted", interrupt_mode: "branch" }
        });
      }
      activeRunning = false;
      return ok({
        request_id: "v030-replace-request",
        stream: false,
        inline_reply: mode === "amend" ? "已把补充说明合入当前任务。" : "已按最新消息替换旧任务。",
        same_session: {
          decision: "replacement_accepted",
          interrupt_mode: mode,
          active_request_ids: ["v030-active-request"],
          replaced_request_ids: ["v030-active-request"],
          cancelled_requests: 1
        }
      });
    }
    if (new RegExp("^/api/requests/[^/]+/queue-action$").test(p)) {
      queueActions.push(JSON.parse(JSON.stringify(body || {})));
      return ok({ state: String(body.action || "") === "cancel_queued" ? "cancelled" : "queued", cancelled: String(body.action || "") === "cancel_queued" ? 1 : 0 });
    }
    if (p === "/api/ui-state") return ok({ state: {} });
    if (p === "/api/file-stat") return ok({ path: String(body.path || ""), exists: true, isFile: true, isDirectory: false, status: "success" });
    if (p === "/api/memory") return ok({ files: [] });
    if (p === "/api/knowledge/graph") return ok({ nodes: [], links: [] });
    return ok({});
  }
  var mockFns = {
    platform: "v030-release-cdp",
    getEnterpriseSession: async function () { return { status: "authenticated", token: "release-cdp-token", clientKey: "ecorex-web-v0.3.1-cdp", user: { name: "发布验收", email: "release-cdp@example.com" }, quota: { allowed: true } }; },
    enterpriseLogout: async function () { return ok({}); },
    enterpriseLogin: async function () { return this.getEnterpriseSession(); },
    checkEnterpriseQuota: async function () { return { ok: true, quota: { allowed: true } }; },
    apiJson: apiJson,
    chooseLocalFiles: async function () { return []; },
    openPath: async function () { return ok({}); },
    reportDesktopEvent: async function () { return ok({}); },
    onSidecarStatus: function (callback) {
      var status = { state: "running", message: "release package runtime", webPort: Number(window.location.port || 80) };
      setTimeout(function () { callback(status); }, 0);
      return function () {};
    }
  };
  var desktop = Object.assign({}, mockFns);
  function installMockBridge() {
    try {
      Object.assign(desktop, mockFns);
      if (!window.ecorexDesktop || typeof window.ecorexDesktop !== "object") {
        try { window.ecorexDesktop = desktop; } catch (error) {}
      } else {
        Object.keys(mockFns).forEach(function (key) {
          try { window.ecorexDesktop[key] = mockFns[key]; } catch (error) {}
        });
      }
    } catch (error) {}
  }
  try {
    Object.defineProperty(window, "ecorexDesktop", { configurable: false, enumerable: true, get: function () { return desktop; }, set: function () {} });
  } catch (error) {
    try { window.ecorexDesktop = desktop; } catch (assignError) {}
  }
  window.__ecorexV030InstallMockBridge = installMockBridge;
  window.__ecorexV030SmokeState = {
    messageCalls: messageCalls,
    queueActions: queueActions,
    setActiveRunning: function (value) { activeRunning = Boolean(value); },
    activeRunning: function () { return activeRunning; }
  };
  installMockBridge();
  var reinstallTicks = 0;
  var reinstallTimer = setInterval(function () {
    installMockBridge();
    reinstallTicks += 1;
    if (reinstallTicks > 600) clearInterval(reinstallTimer);
  }, 100);
})();
`;

try {
  // Fail fast before launching Chrome if the injected bridge script is malformed.
  new Function(bridgeSource);
} catch (error) {
  throw new Error(`Injected bridge script is not valid JavaScript: ${error.message || String(error)}`);
}

function assert(condition, message, detail = undefined) {
  if (!condition) {
    const suffix = detail === undefined ? "" : `\n${JSON.stringify(detail, null, 2)}`;
    throw new Error(`${message}${suffix}`);
  }
}

function findChrome() {
  const chrome = chromeCandidates.find((candidate) => fs.existsSync(candidate));
  if (!chrome) throw new Error("Chrome/Edge executable not found; set CHROME_PATH to run CDP smoke.");
  return chrome;
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

async function waitJson(url, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
      lastError = new Error(`${response.status} ${response.statusText}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw lastError || new Error(`Timed out waiting for ${url}`);
}

async function connectCdp(webSocketDebuggerUrl) {
  const socket = new WebSocket(webSocketDebuggerUrl);
  const pending = new Map();
  let nextId = 1;
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (!payload.id || !pending.has(payload.id)) return;
    const { resolve, reject } = pending.get(payload.id);
    pending.delete(payload.id);
    if (payload.error) reject(new Error(payload.error.message || "CDP command failed"));
    else resolve(payload.result || {});
  });
  return {
    send(method, params = {}) {
      const id = nextId++;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    },
    close() {
      socket.close();
    }
  };
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Runtime.evaluate exception");
  return result.result?.value;
}

async function waitFor(cdp, expression, label, timeoutMs = 12_000) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await evaluate(cdp, expression);
    if (lastValue) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 180));
  }
  throw new Error(`Timed out waiting for ${label}. Last value: ${JSON.stringify(lastValue)}`);
}

async function clickByText(cdp, needle, selector = "button,a,[role='button']") {
  const result = await evaluate(cdp, `(() => {
    const needle = ${JSON.stringify(needle)};
    const nodes = Array.from(document.querySelectorAll(${JSON.stringify(selector)}));
    const visible = (node) => {
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const node = nodes.find((item) => {
      const text = [item.innerText, item.textContent, item.getAttribute("title"), item.getAttribute("aria-label")]
        .filter(Boolean).join(" ");
      return visible(item) && text.includes(needle);
    });
    if (!node) return { clicked: false, available: nodes.slice(0, 24).map((item) => item.innerText || item.title || item.getAttribute("aria-label") || item.tagName) };
    node.click();
    return { clicked: true, text: node.innerText || node.title || node.getAttribute("aria-label") || node.tagName };
  })()`);
  assert(result?.clicked, `Could not click ${needle}`, result);
  await new Promise((resolve) => setTimeout(resolve, 240));
  return result;
}

async function dragOnOverlay(cdp, startRatio, endRatio, steps = 8) {
  const rect = await waitFor(cdp, `(() => {
    const overlay = document.querySelector(".image-retouch-overlay");
    const image = document.querySelector(".image-retouch-image-wrap img");
    if (!overlay || !image) return null;
    const r = image.getBoundingClientRect();
    return r.width && r.height ? { left: r.left, top: r.top, width: r.width, height: r.height } : null;
  })()`, "retouch image rect");
  const start = { x: rect.left + rect.width * startRatio.x, y: rect.top + rect.height * startRatio.y };
  const end = { x: rect.left + rect.width * endRatio.x, y: rect.top + rect.height * endRatio.y };
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: start.x, y: start.y, button: "left", clickCount: 1 });
  for (let index = 1; index <= steps; index += 1) {
    const t = index / steps;
    await cdp.send("Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: start.x + (end.x - start.x) * t,
      y: start.y + (end.y - start.y) * t,
      button: "left"
    });
  }
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: end.x, y: end.y, button: "left", clickCount: 1 });
}

async function commitDraftText(cdp, text) {
  await waitFor(cdp, `Boolean(document.querySelector(".image-retouch-tex…653 tokens truncated…;
    });
  })()`);
}

async function waitForMessageCall(cdp, mode, minimumCount = 1) {
  return waitFor(cdp, `(() => {
    const calls = window.__ecorexV030SmokeState?.messageCalls || [];
    const matches = calls.filter((call) => String(call.interrupt_mode || "replace") === ${JSON.stringify(mode)});
    return matches.length >= ${Number(minimumCount)} ? matches[matches.length - 1] : null;
  })()`, `message interrupt_mode ${mode}`, 20_000);
}

async function waitForQueueAction(cdp, action) {
  return waitFor(cdp, `(() => {
    const actions = window.__ecorexV030SmokeState?.queueActions || [];
    const matches = actions.filter((item) => String(item.action || "") === ${JSON.stringify(action)});
    return matches.length ? matches[matches.length - 1] : null;
  })()`, `queue action ${action}`, 20_000);
}

async function run() {
  assert(targetUrl, "WEBUI_HANDTEST_URL is required for release CDP smoke.");
  const chrome = findChrome();
  const port = await freePort();
  const profile = path.join(os.tmpdir(), `ecorex-v030-release-cdp-${Date.now()}`);
  const chromeProcess = spawn(chrome, [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--disable-extensions",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "about:blank"
  ], { stdio: "ignore" });

  let cdp;
  const result = {
    schema: "ecorex.v0.3.1.release-cdp-smoke.v1",
    status: "FAIL",
    version: "0.3.1",
    targetUrl,
    generatedAt: new Date().toISOString(),
    checks: {}
  };
  try {
    const list = await waitJson(`http://127.0.0.1:${port}/json/list`);
    const target = list.find((item) => item.type === "page") || list[0];
    assert(target?.webSocketDebuggerUrl, "No debuggable page target found", list);
    cdp = await connectCdp(target.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: bridgeSource });
    await cdp.send("Page.navigate", { url: targetUrl });

    await waitFor(cdp, `Boolean(window.ecorexDesktop)`, "desktop bridge bootstrap");
    await evaluate(cdp, `window.__ecorexV030InstallMockBridge && window.__ecorexV030InstallMockBridge()`);
    const bridgeProbe = await waitFor(cdp, `(async () => {
      try {
        const payload = await window.ecorexDesktop.apiJson({ path: "/api/sessions", method: "GET" });
        return payload && Array.isArray(payload.sessions) && payload.sessions[0] && payload.sessions[0].title;
      } catch (error) {
        return "";
      }
    })()`, "mock bridge sessions", 30_000);
    result.checks.bridgeProbe = bridgeProbe;
    await waitFor(cdp, `document.body && document.body.innerText.includes("v0.3.1 精修发布验收")`, "mock release session", 45_000);
    const landing = await evaluate(cdp, `(() => ({
      hasRuntimeConnected: document.body.innerText.includes("发布验收") || document.body.innerText.includes("v0.3.1"),
      hasRetouchSession: document.body.innerText.includes("v0.3.1 精修发布验收"),
      hasComposer: Boolean(document.querySelector(".composer textarea")),
      topbarText: document.querySelector(".app-topbar")?.innerText || ""
    }))()`);
    assert(landing.hasRetouchSession && landing.hasComposer, "Release WebUI should load through CDP with session and composer.", landing);
    result.checks.landing = landing;

    const sessionListBefore = await evaluate(cdp, `(() => ({
      visibleRows: document.querySelectorAll(".session-row").length,
      hasMore: Boolean(document.querySelector(".session-list-more")),
      moreText: document.querySelector(".session-list-more")?.innerText || ""
    }))()`);
    assert(sessionListBefore.hasMore && /查看更多/.test(sessionListBefore.moreText), "Session list should expose 查看更多(N) when there are hidden sessions.", sessionListBefore);
    await clickByText(cdp, "查看更多");
    await waitFor(cdp, `document.body.innerText.includes("收起")`, "session list collapse button");
    const sessionListExpanded = await evaluate(cdp, `(() => ({
      visibleRows: document.querySelectorAll(".session-row").length,
      hasCollapse: Array.from(document.querySelectorAll(".session-list-more")).some((node) => /收起/.test(node.innerText || ""))
    }))()`);
    assert(sessionListExpanded.visibleRows >= sessionListBefore.visibleRows && sessionListExpanded.hasCollapse, "Expanded session list should expose 收起.", sessionListExpanded);
    await clickByText(cdp, "收起");
    await waitFor(cdp, `document.querySelector(".session-list-more")?.innerText.includes("查看更多")`, "session list more restored");
    result.checks.sessionList = { before: sessionListBefore, expanded: sessionListExpanded };

    await clickByText(cdp, "v0.3.1 精修发布验收");
    await waitFor(cdp, `document.body.innerText.includes("已生成两张图片产物")`, "retouch release history");
    await waitFor(cdp, `(() => {
      const previews = Array.from(document.querySelectorAll(".markdown-local-image-preview"));
      return previews.length >= 2 && previews.every((img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
    })()`, "inline local image previews loaded", 30_000);
    const inlinePreviews = await evaluate(cdp, `(() => Array.from(document.querySelectorAll(".markdown-local-image-preview")).map((img) => ({
      src: img.getAttribute("src") || "",
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      complete: img.complete
    })))()`);
    assert(inlinePreviews.length >= 2 && inlinePreviews.every((item) => item.complete && item.naturalWidth > 0 && item.naturalHeight > 0), "Inline local image previews must render in release CDP smoke.", inlinePreviews);
    result.checks.inlinePreviews = inlinePreviews.map((item) => ({ naturalWidth: item.naturalWidth, naturalHeight: item.naturalHeight, complete: item.complete }));
    await waitFor(cdp, `(() => {
      const buttons = Array.from(document.querySelectorAll("button,a,[role='button']"));
      return buttons.some((item) => [item.innerText, item.textContent, item.title, item.getAttribute("aria-label")].filter(Boolean).join(" ").includes("精准修图"));
    })()`, "retouch artifact action", 30_000);
    await clickByText(cdp, "精准修图");
    await waitFor(cdp, `Boolean(document.querySelector(".image-retouch-sheet.is-editor"))`, "retouch editor");

    const editorInitial = await evaluate(cdp, `(() => ({
      hasSidePanel: Boolean(document.querySelector(".image-retouch-side-panel")),
      imagePickerCount: document.querySelectorAll(".image-retouch-image-picker button").length,
      hasRectTool: Boolean(Array.from(document.querySelectorAll(".image-retouch-bottom-toolbar button")).find((button) => /框选|Square/.test([button.title, button.getAttribute("aria-label"), button.innerText].filter(Boolean).join(" ")))),
      hasLassoTool: Boolean(Array.from(document.querySelectorAll(".image-retouch-bottom-toolbar button")).find((button) => /圈选/.test([button.title, button.getAttribute("aria-label"), button.innerText].filter(Boolean).join(" ")))),
      hasTextTool: Boolean(Array.from(document.querySelectorAll(".image-retouch-bottom-toolbar button")).find((button) => /文字修改/.test([button.title, button.getAttribute("aria-label"), button.innerText].filter(Boolean).join(" ")))),
      countText: document.querySelector(".image-retouch-count")?.innerText || ""
    }))()`);
    assert(editorInitial.hasSidePanel && editorInitial.imagePickerCount >= 2 && editorInitial.hasRectTool && editorInitial.hasLassoTool && editorInitial.hasTextTool, "Retouch editor must expose real v0.3.1 tools.", editorInitial);
    result.checks.editorInitial = editorInitial;

    await evaluate(cdp, `(() => {
      const buttons = Array.from(document.querySelectorAll(".image-retouch-image-picker button"));
      if (buttons[1]) buttons[1].click();
      return buttons.length;
    })()`);
    await waitFor(cdp, `document.querySelector(".image-retouch-side-head")?.innerText.includes("2/2") || document.querySelector(".image-retouch-count")?.innerText.includes("2 张图")`, "multi-image selected");

    await clickByText(cdp, "文字修改");
    await dragOnOverlay(cdp, { x: 0.50, y: 0.17 }, { x: 0.50, y: 0.17 }, 1);
    await commitDraftText(cdp, "把“地”改成“的”，字体样式不变");

    await clickByText(cdp, "框选");
    await dragOnOverlay(cdp, { x: 0.16, y: 0.40 }, { x: 0.45, y: 0.63 });
    await commitDraftText(cdp, "局部提亮，保持材质和构图");

    await clickByText(cdp, "圈选");
    await dragOnOverlay(cdp, { x: 0.66, y: 0.46 }, { x: 0.82, y: 0.72 }, 12);
    await commitDraftText(cdp, "参考暖色调微调");

    await syntheticUploadReference(cdp);
    const editorAfter = await evaluate(cdp, `(() => ({
      countText: document.querySelector(".image-retouch-count")?.innerText || "",
      sideHead: document.querySelector(".image-retouch-side-head")?.innerText || "",
      rectCount: document.querySelectorAll(".image-retouch-selection-shape").length,
      textTargets: document.querySelectorAll(".image-retouch-text-target-shape").length,
      stickerCount: document.querySelectorAll(".image-retouch-sticker-layer img").length,
      labels: Array.from(document.querySelectorAll(".image-retouch-svg-label")).map((node) => node.textContent || "")
    }))()`);
    assert(editorAfter.countText.includes("4 处标注") && editorAfter.sideHead.includes("2/2") && editorAfter.rectCount >= 2 && editorAfter.textTargets >= 1 && editorAfter.stickerCount >= 1, "Retouch annotations should include text, rectangle/lasso, upload reference, and multi-image selection.", editorAfter);
    result.checks.editorAfter = editorAfter;

    await clickByText(cdp, "加入聊天框");
    await waitFor(cdp, `!document.querySelector(".image-retouch-sheet.is-editor")`, "retouch modal closed");
    await waitFor(cdp, `document.body.innerText.includes("已加入聊天框")`, "retouch toast");
    const draft = await evaluate(cdp, `(() => {
      const textarea = document.querySelector(".composer textarea");
      const attachment = document.querySelector(".attachment-tray article");
      return {
        hasAttachment: Boolean(attachment),
        hasThumbnail: Boolean(document.querySelector(".attachment-tray img")),
        attachmentText: attachment ? attachment.innerText : "",
        draft: textarea ? textarea.value : "",
        draftLength: textarea ? textarea.value.length : 0
      };
    })()`);
    assert(draft.hasAttachment && draft.hasThumbnail && draft.draft.includes("本轮选中原图") && draft.draft.includes("文字修改约束") && draft.draft.includes("必须走 imagegen"), "Retouch submit should create pending composer draft with marker attachment and imagegen constraints.", draft);
    result.checks.draft = { ...draft, draft: draft.draft.slice(0, 800), redacted: true };

    await clickByText(cdp, "v0.3.1 Active Turn Control");
    await waitFor(cdp, `document.body.innerText.includes("Active turn control mock ready.")`, "active turn session history");
    await evaluate(cdp, `window.__ecorexV030SmokeState?.setActiveRunning(true)`);
    await waitFor(cdp, `Boolean(document.querySelector(".send-button.stop"))`, "active stop button");
    const stopNoJump = await evaluate(cdp, `(() => {
      const beforeScrollY = window.scrollY;
      const button = document.querySelector(".send-button.stop");
      if (!button) return { clicked: false, beforeScrollY, afterScrollY: window.scrollY };
      button.click();
      return new Promise((resolve) => requestAnimationFrame(() => resolve({
        clicked: true,
        beforeScrollY,
        afterScrollY: window.scrollY
      })));
    })()`);
    assert(stopNoJump.clicked && Math.abs(stopNoJump.afterScrollY - stopNoJump.beforeScrollY) <= 1, "Stopping an active task should not jump the page.", stopNoJump);

    await evaluate(cdp, `window.__ecorexV030SmokeState?.setActiveRunning(true)`);
    const longComposerText = [
      "这是 v0.3.1 发布包 active turn 验收长输入。",
      "请用最新意图替换旧任务，并保持页面不跳动。",
      "这段内容用于触发 autosize，多行输入应该稳定在最大高度内。"
    ].join("\\n").repeat(24);
    const composerAutosize = await setComposerText(cdp, longComposerText);
    assert(
      composerAutosize.valueLength === longComposerText.length
      && composerAutosize.afterHeight <= composerAutosize.maxHeight + 2
      && Math.abs(composerAutosize.afterScrollY - composerAutosize.beforeScrollY) <= 1,
      "Long composer input should autosize without page jump.",
      composerAutosize
    );
    await waitFor(cdp, `Boolean(document.querySelector(".active-turn-trigger"))`, "active turn trigger for default replace");
    await evaluate(cdp, `document.querySelector(".send-button:not(.stop)")?.click()`);
    const replaceCall = await waitForMessageCall(cdp, "replace");
    assert(
      replaceCall.session_id === "v030-active-turn"
      && String(replaceCall.message || "").includes("active turn 验收长输入"),
      "Default send during active turn should replace/update the current task.",
      replaceCall
    );

    await evaluate(cdp, `window.__ecorexV030SmokeState?.setActiveRunning(true)`);
    await setComposerText(cdp, "请把这条消息明确排队稍后执行，用来验收队列不是默认入口。");
    await waitFor(cdp, `Boolean(document.querySelector(".active-turn-trigger"))`, "active turn trigger for queue");
    await evaluate(cdp, `document.querySelector(".active-turn-trigger")?.click()`);
    await waitFor(cdp, `document.body.innerText.includes("排队稍后执行")`, "queue menu item");
    await clickByText(cdp, "排队稍后执行");
    const queueCall = await waitForMessageCall(cdp, "queue");
    await waitFor(cdp, `document.body.innerText.includes("提到队首") && document.body.innerText.includes("取消排队")`, "queued message actions");
    await clickByText(cdp, "取消排队");
    const cancelQueueAction = await waitForQueueAction(cdp, "cancel_queued");
    assert(
      queueCall.session_id === "v030-active-turn"
      && cancelQueueAction.request_id && cancelQueueAction.action === "cancel_queued",
      "Explicit queue should send queue mode and expose cancellable queued action.",
      { queueCall, cancelQueueAction }
    );

    await evaluate(cdp, `window.__ecorexV030SmokeState?.setActiveRunning(true)`);
    await setComposerText(cdp, "请把这条新消息放到新开分支里执行，原任务继续运行。");
    await waitFor(cdp, `Boolean(document.querySelector(".active-turn-trigger"))`, "active turn trigger for branch");
    await evaluate(cdp, `document.querySelector(".active-turn-trigger")?.click()`);
    await waitFor(cdp, `document.body.innerText.includes("新开分支")`, "branch menu item");
    await clickByText(cdp, "新开分支");
    const branchCall = await waitForMessageCall(cdp, "branch");
    assert(
      String(branchCall.interrupt_mode || "") === "branch"
      && String(branchCall.message || "").includes("新开分支"),
      "Explicit branch should send branch mode from the active-turn menu.",
      branchCall
    );
    result.checks.activeTurn = {
      stopNoJump,
      composerAutosize,
      replace: {
        session_id: replaceCall.session_id,
        interrupt_mode: replaceCall.interrupt_mode,
        interrupts_request_id: replaceCall.interrupts_request_id
      },
      queue: {
        session_id: queueCall.session_id,
        interrupt_mode: queueCall.interrupt_mode,
        interrupts_request_id: queueCall.interrupts_request_id,
        cancelAction: cancelQueueAction.action
      },
      branch: {
        session_id: branchCall.session_id,
        interrupt_mode: branchCall.interrupt_mode
      }
    };

    if (screenshotPath) {
      const screenshot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.mkdirSync(path.dirname(screenshotPath), { recursive: true });
      fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data || "", "base64"));
      result.screenshot = screenshotPath;
    }
    result.status = "PASS";
  } finally {
    if (cdp) {
      try {
        result.debug = await evaluate(cdp, `(() => ({
          bodyText: (document.body && document.body.innerText ? document.body.innerText.slice(0, 2000) : ""),
          title: document.title,
          artifactShelves: document.querySelectorAll(".artifact-shelf").length,
          artifactRows: document.querySelectorAll(".artifact-row").length,
          retouchButtons: Array.from(document.querySelectorAll("button,a,[role='button']")).filter((item) => [item.innerText, item.textContent, item.title, item.getAttribute("aria-label")].filter(Boolean).join(" ").includes("精准修图")).length,
          messageContentHtml: Array.from(document.querySelectorAll(".message-content")).map((node) => node.innerHTML.slice(0, 4000)),
          messageArticles: Array.from(document.querySelectorAll("article")).map((node) => ({
            className: node.className,
            text: (node.innerText || "").slice(0, 1000),
            html: node.innerHTML.slice(0, 2000)
          })),
          localFileLinks: Array.from(document.querySelectorAll("[data-ecorex-file-path]")).map((node) => ({
            tag: node.tagName,
            className: node.className,
            text: node.textContent,
            path: node.getAttribute("data-ecorex-file-path")
          })),
          looseImagePathMatches: Array.from(((document.body && document.body.innerText) || "").matchAll(/((?:[A-Za-z]:[\\\\/]|\\\\\\\\|\\/)[^\\s\`<>]*?\\.(?:png|jpe?g|gif|webp|bmp|svg))(?:[\\s)\\]'"\\\`,.;:!?]|$)/gi)).map((match) => match[1]),
          scriptAssets: Array.from(document.scripts).map((script) => script.src || script.getAttribute("src") || "").filter(Boolean),
          buttons: Array.from(document.querySelectorAll("button,a,[role='button']")).slice(0, 40).map((item) => [item.innerText, item.textContent, item.title, item.getAttribute("aria-label")].filter(Boolean).join(" ").slice(0, 160)),
          hasMockInstall: typeof window.__ecorexV030InstallMockBridge === "function",
          bridgeVersion: window.ecorexDesktop && window.ecorexDesktop.__ecorexWebBridgeVersion,
          bridgePlatform: window.ecorexDesktop && window.ecorexDesktop.platform
        }))()`);
      } catch (error) {
        result.debugError = error.message || String(error);
      }
      if (screenshotPath && result.status !== "PASS") {
        try {
          const screenshot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
          fs.mkdirSync(path.dirname(screenshotPath), { recursive: true });
          fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data || "", "base64"));
          result.screenshot = screenshotPath;
        } catch (error) {
          result.screenshotError = error.message || String(error);
        }
      }
    }
    if (outputPath) {
      fs.mkdirSync(path.dirname(outputPath), { recursive: true });
      fs.writeFileSync(outputPath, `${JSON.stringify(result, null, 2)}\n`);
    }
    if (cdp) cdp.close();
    chromeProcess.kill();
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch {
      // Windows can keep the profile locked briefly after Chrome exits.
    }
  }
  console.log(JSON.stringify(result, null, 2));
}

run().catch((error) => {
  const payload = { status: "FAIL", error: error.stack || error.message || String(error), generatedAt: new Date().toISOString() };
  if (outputPath && !fs.existsSync(outputPath)) {
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`);
  }
  console.error(payload.error);
  process.exit(1);
});

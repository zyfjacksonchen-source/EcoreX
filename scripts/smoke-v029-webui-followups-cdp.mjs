import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const distDir = path.join(repoRoot, "desktop", "dist");
const providedTargetUrl = process.env.WEBUI_HANDTEST_URL || "";
const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

const retouchSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="820" viewBox="0 0 640 820">
  <rect width="640" height="820" fill="#ece7dc"/>
  <rect x="74" y="78" width="492" height="664" fill="#f7f2e8" stroke="#d8ccb9" stroke-width="2"/>
  <rect x="110" y="220" width="210" height="118" fill="#9b5d30"/>
  <rect x="132" y="244" width="76" height="68" fill="#6d3c1f"/>
  <rect x="224" y="244" width="76" height="68" fill="#6d3c1f"/>
  <rect x="338" y="310" width="128" height="154" rx="8" fill="#6f5a3a"/>
  <rect x="386" y="180" width="82" height="110" rx="6" fill="#3c2d24"/>
  <path d="M420 190h86l-18 38h-98z" fill="#cab886"/>
  <rect x="418" y="228" width="8" height="82" fill="#8b754e"/>
  <circle cx="166" cy="420" r="62" fill="#5c3d28"/>
  <rect x="96" y="124" width="190" height="18" fill="#3d3328"/>
  <rect x="96" y="154" width="132" height="10" fill="#8a7a68"/>
  <rect x="96" y="174" width="154" height="8" fill="#b4a794"/>
  <text x="92" y="118" font-family="Arial, sans-serif" font-size="42" fill="#3d3328">中古风家居</text>
  <text x="92" y="784" font-family="Arial, sans-serif" font-size="18" fill="#837263">手测封面图</text>
</svg>`;
const retouchDataUrl = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(retouchSvg)}`;

const bridgeSource = String.raw`
(function () {
  "use strict";
  var now = Math.floor(Date.now() / 1000);
  var retouchPreviewUrl = __ECOREX_RETOUCH_DATA_URL__;
  var sessions = [
    { session_id: "mock-tencent-docs", title: "腾讯文档：项目复盘", updatedAt: new Date().toISOString(), created_at: new Date(Date.now() - 3600000).toISOString(), last_active: new Date().toISOString() },
    { session_id: "mock-normal-session", title: "你现在是什么模型", updatedAt: new Date(Date.now() - 1800000).toISOString(), created_at: new Date(Date.now() - 7200000).toISOString(), last_active: new Date(Date.now() - 1800000).toISOString() },
    { session_id: "mock-retouch-session", title: "圣都装饰封面图", updatedAt: new Date(Date.now() - 1200000).toISOString(), created_at: new Date(Date.now() - 5400000).toISOString(), last_active: new Date(Date.now() - 1200000).toISOString() }
  ];
  var graph = {
    nodes: [
      { id: "docs/knowledge/tencent-docs.md", label: "腾讯文档连接", category: "external" },
      { id: "docs/knowledge/retouch-editor.md", label: "精准修图编辑器", category: "image" },
      { id: "docs/knowledge/session-recovery.md", label: "会话恢复", category: "session" },
      { id: "docs/knowledge/memory-graph.md", label: "知识图谱独立页", category: "memory" }
    ],
    links: [
      { source: "docs/knowledge/tencent-docs.md", target: "docs/knowledge/session-recovery.md" },
      { source: "docs/knowledge/retouch-editor.md", target: "docs/knowledge/memory-graph.md" },
      { source: "docs/knowledge/memory-graph.md", target: "docs/knowledge/session-recovery.md" }
    ]
  };
  var histories = {
    "mock-tencent-docs": [
      { role: "user", content: "请帮我连接腾讯文档，并读取项目复盘。", created_at: now - 420, seq: 1, extras: { attachments: [{ provider: "tencent-docs", source: "tencent-docs", remote: true, file_path: "tencent-docs://mock-doc-1", file_name: "项目复盘", file_type: "file", url: "https://docs.qq.com/doc/mock" }] } },
      { role: "assistant", content: "已进入 Tencent Docs MCP 会话引导。当前手测桥接会展示远程文档附件和会话恢复效果。", created_at: now - 400, seq: 2 }
    ],
    "mock-normal-session": [
      { role: "user", content: "你现在是什么模型", created_at: now - 2400, seq: 1 },
      { role: "assistant", content: "当前为 WebUI 手测环境，用于检查 v0.2.9 UI 变更。", created_at: now - 2390, seq: 2 }
    ],
    "mock-retouch-session": [
      { role: "user", content: "请帮我基于这张封面做精准修图。", created_at: now - 1300, seq: 1 },
      { role: "assistant", content: "已生成手测图片产物，可点击精准修图按钮打开编辑器。", created_at: now - 1280, seq: 2, artifacts: [{ id: "mock-retouch-artifact", requestId: "mock-retouch-request", kind: "image", intent: "deliverable", operation: "created", status: "ready", title: "圣都装饰封面图", url: "https://ecorex-handtest.invalid/retouch.svg", previewUrl: retouchPreviewUrl, thumbnailUrl: retouchPreviewUrl, mimeType: "image/svg+xml" }] }
    ]
  };
  function ok(payload) { return Object.assign({ status: "success" }, payload || {}); }
  function pathOf(input) { return typeof input === "string" ? input : input && input.path ? String(input.path) : "/"; }
  async function apiJson(input) {
    var rawPath = pathOf(input);
    var url = new URL(rawPath, "http://ecorex.local");
    var p = url.pathname;
    if (p === "/api/version") return ok({ version: "0.2.9-handtest", releaseNotes: null, updateState: { status: "idle", version: "0.2.9" } });
    if (p === "/api/sessions") return ok({ sessions: sessions, total: sessions.length });
    if (p === "/api/active-requests") return ok({ requests: [], recentTerminalRequests: [], runStatusCounts: {}, staleLocks: [] });
    if (p === "/api/tools") return ok({ tools: [{ name: "mcp__tencent-docs__search" }, { name: "image_retouch" }] });
    if (p === "/api/skills") return ok({ skills: [] });
    if (p === "/api/extensions") return ok({ extensions: [], count: 0, summary: {} });
    if (p === "/api/channels") return ok({ channels: [] });
    if (p === "/api/models") return ok({ providers: [], capabilities: {}, currentProvider: "openai", currentModel: "gpt-5.5" });
    if (p === "/api/scheduler") return ok({ enabled: false, initialized: false, running: false, serviceStatus: "unavailable", tasks: [], taskCount: 0, counts: { total: 0, enabled: 0, disabled: 0, error: 0 } });
    if (p === "/api/external-connections") return ok({ connections: [] });
    if (p === "/api/tencent-docs/status") return ok({ message: "手测桥接：腾讯文档 mock 已配置", capability: { configured: true, connected: true, toolCount: 2, contentToolCount: 1, endpoint: "https://docs.qq.com/openapi/mcp", redacted: true } });
    if (p === "/api/tool-permissions") return ok({ mode: "smart-ask", grantsCount: 0, auditPath: "mock-permissions.json" });
    if (p === "/api/ui-state") return ok({ state: {} });
    if (p === "/api/memory") return ok({ files: [{ filename: "project-memory.md", category: "memory", updated_at: "2026-07-05 15:35" }, { filename: "dream-distillation.md", category: "dream", updated_at: "2026-07-05 15:35" }] });
    if (p === "/api/knowledge/graph") return ok(graph);
    if (p === "/api/knowledge/read") {
      var target = url.searchParams.get("path") || "docs/knowledge/mock.md";
      return ok({ path: target, content: "# " + target.split("/").pop().replace(/\\.md$/, "") + "\n\n这是手测桥接返回的知识页正文，用于检查独立知识图谱页面的详情和摘要展示。" });
    }
    if (p === "/api/history") {
      var sessionId = url.searchParams.get("session_id") || "";
      var messages = histories[sessionId] || [];
      return ok({ messages: messages, context_start_seq: 0, total: messages.length, page: 1, page_size: 50, has_more: false });
    }
    if (p.indexOf("/api/runtime-projection") === 0) return ok({ events: [], messages: [], requests: [] });
    return ok({});
  }
  var handtestDesktop = {
    platform: "web-handtest",
    getEnterpriseSession: async function () { return { status: "authenticated", token: "handtest-token", user: { name: "手测同学", email: "handtest@example.com" }, quota: { allowed: true } }; },
    enterpriseLogout: async function () { return ok({}); },
    enterpriseLogin: async function () { return this.getEnterpriseSession(); },
    checkEnterpriseQuota: async function () { return { ok: true, quota: { allowed: true } }; },
    apiJson: apiJson,
    chooseLocalFiles: async function () { return []; },
    openPath: async function () { return ok({}); },
    reportDesktopEvent: async function () { return ok({}); },
    onSidecarStatus: function (callback) {
      var defaultPort = window.location.protocol === "https:" ? 443 : 80;
      var status = { state: "running", message: "handtest runtime", webPort: Number(window.location.port || defaultPort) };
      setTimeout(function () { callback(status); }, 0);
      return function () {};
    }
  };
  try {
    Object.defineProperty(window, "ecorexDesktop", {
      configurable: false,
      enumerable: true,
      get: function () { return handtestDesktop; },
      set: function () {}
    });
  } catch (error) {
    window.ecorexDesktop = handtestDesktop;
  }
})();
`.replace("__ECOREX_RETOUCH_DATA_URL__", JSON.stringify(retouchDataUrl));

function assert(condition, message, detail = undefined) {
  if (!condition) {
    const suffix = detail === undefined ? "" : `\n${JSON.stringify(detail, null, 2)}`;
    throw new Error(`${message}${suffix}`);
  }
}

function findChrome() {
  const chrome = chromeCandidates.find((candidate) => fs.existsSync(candidate));
  if (!chrome) {
    throw new Error("Chrome/Edge executable not found; set CHROME_PATH to run the CDP smoke.");
  }
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

function contentType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".png")) return "image/png";
  if (filePath.endsWith(".svg")) return "image/svg+xml; charset=utf-8";
  return "application/octet-stream";
}

async function startHandtestServer() {
  const port = await freePort();
  const server = http.createServer((request, response) => {
    const url = new URL(request.url || "/", `http://127.0.0.1:${port}`);
    if (request.method === "POST" && url.pathname === "/upload") {
      request.resume();
      response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({
        status: "success",
        file_path: path.join(repoRoot, "docs", "v0.2.9", "artifacts", "handtest-retouch-marker.png"),
        file_name: "精修标注-手测.png",
        file_type: "image",
        preview_url: "/uploads/retouch-marker.png"
      }));
      return;
    }
    if (url.pathname === "/" || url.pathname === "/index.html") {
      const indexPath = path.join(distDir, "index.html");
      let html = fs.readFileSync(indexPath, "utf-8");
      html = html.replace("</head>", `    <script>${bridgeSource}</script>\n  </head>`);
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(html);
      return;
    }
    if (url.pathname === "/handtest-retouch.svg") {
      response.writeHead(200, { "Content-Type": "image/svg+xml; charset=utf-8" });
      response.end(retouchSvg);
      return;
    }
    if (url.pathname === "/uploads/retouch-marker.png") {
      response.writeHead(200, { "Content-Type": "image/svg+xml; charset=utf-8" });
      response.end(retouchSvg);
      return;
    }
    const relativePath = decodeURIComponent(url.pathname).replace(/^\/+/, "");
    const filePath = path.normalize(path.join(distDir, relativePath));
    if (!filePath.startsWith(distDir) || !fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("Not found");
      return;
    }
    response.writeHead(200, { "Content-Type": contentType(filePath) });
    fs.createReadStream(filePath).pipe(response);
  });
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  return {
    url: `http://127.0.0.1:${port}/`,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
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
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
  throw lastError || new Error(`Timed out waiting for ${url}`);
}

async function connectCdp(webSocketDebuggerUrl) {
  const socket = new WebSocket(webSocketDebuggerUrl);
  const pending = new Map();
  const handlers = new Map();
  let nextId = 1;
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (!payload.id) {
      const callbacks = handlers.get(payload.method) || [];
      for (const callback of callbacks) {
        try {
          Promise.resolve(callback(payload.params || {})).catch(() => undefined);
        } catch {
          // Event callbacks should not break pending CDP commands.
        }
      }
      return;
    }
    if (!pending.has(payload.id)) return;
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
    },
    on(method, callback) {
      const callbacks = handlers.get(method) || [];
      callbacks.push(callback);
      handlers.set(method, callbacks);
    },
  };
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Runtime.evaluate exception");
  }
  return result.result?.value;
}

async function waitFor(cdp, expression, label, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await evaluate(cdp, expression);
    if (lastValue) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 180));
  }
  throw new Error(`Timed out waiting for ${label}. Last value: ${JSON.stringify(lastValue)}`);
}

async function clickByText(cdp, needle, selector = "button,a") {
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
        .filter(Boolean)
        .join(" ");
      return visible(item) && text.includes(needle);
    });
    if (!node) return { clicked: false, available: nodes.slice(0, 20).map((item) => item.innerText || item.title || item.getAttribute("aria-label") || item.tagName) };
    node.click();
    return { clicked: true, text: node.innerText || node.title || node.getAttribute("aria-label") || node.tagName };
  })()`);
  assert(result?.clicked, `Could not click ${needle}`, result);
  await new Promise((resolve) => setTimeout(resolve, 240));
  return result;
}

async function closeSettings(cdp) {
  await evaluate(cdp, `(() => {
    const button = Array.from(document.querySelectorAll("button")).find((item) => {
      const label = [item.getAttribute("aria-label"), item.title, item.innerText].filter(Boolean).join(" ");
      return label.includes("关闭设置");
    });
    if (button) button.click();
    return Boolean(button);
  })()`);
  await new Promise((resolve) => setTimeout(resolve, 180));
}

async function drawRetouchAnnotation(cdp) {
  const rect = await waitFor(cdp, `(() => {
    const overlay = document.querySelector(".image-retouch-overlay");
    const image = document.querySelector(".image-retouch-image-wrap img");
    if (!overlay || !image) return null;
    const overlayRect = overlay.getBoundingClientRect();
    const imageRect = image.getBoundingClientRect();
    return overlayRect.width && overlayRect.height && imageRect.width && imageRect.height ? {
      x: overlayRect.left,
      y: overlayRect.top,
      width: overlayRect.width,
      height: overlayRect.height,
      imageX: imageRect.left,
      imageY: imageRect.top,
      imageWidth: imageRect.width,
      imageHeight: imageRect.height
    } : null;
  })()`, "retouch overlay");
  const start = { x: rect.imageX + rect.imageWidth * 0.62, y: rect.imageY + rect.imageHeight * 0.26 };
  const end = { x: Math.max(rect.x + 18, rect.imageX - rect.width * 0.18), y: rect.imageY + rect.imageHeight * 0.24 };
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: start.x, y: start.y, button: "left", clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: end.x, y: end.y, button: "left" });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: end.x, y: end.y, button: "left", clickCount: 1 });
  await waitFor(cdp, `Boolean(document.querySelector(".image-retouch-text-draft textarea"))`, "retouch text draft");
  await evaluate(cdp, `(() => {
    const textarea = document.querySelector(".image-retouch-text-draft textarea");
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(textarea, "不要挡住标题");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    return textarea.value;
  })()`);
  await clickByText(cdp, "完成标注");
}

async function run() {
  const chrome = findChrome();
  const handtestServer = providedTargetUrl ? null : await startHandtestServer();
  const targetUrl = providedTargetUrl || handtestServer.url;
  const port = await freePort();
  const profile = path.join(os.tmpdir(), `ecorex-v029-cdp-${Date.now()}`);
  const chromeProcess = spawn(chrome, [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--disable-extensions",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: "ignore" });

  let cdp;
  try {
    const list = await waitJson(`http://127.0.0.1:${port}/json/list`);
    const target = list.find((item) => item.type === "page") || list[0];
    assert(target?.webSocketDebuggerUrl, "No debuggable page target found", list);
    cdp = await connectCdp(target.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1366,
      height: 768,
      deviceScaleFactor: 1,
      mobile: false,
    });
    if (providedTargetUrl) {
      await cdp.send("Fetch.enable", {
        patterns: [
          { urlPattern: "*", requestStage: "Request" },
        ],
      });
      cdp.on("Fetch.requestPaused", async (event) => {
        const requestUrl = event.request?.url || "";
        let requestPath = "";
        try {
          requestPath = new URL(requestUrl).pathname;
        } catch {
          await cdp.send("Fetch.continueRequest", { requestId: event.requestId });
          return;
        }
        if (!/\/upload(?:\?|$)/.test(requestPath)) {
          await cdp.send("Fetch.continueRequest", { requestId: event.requestId });
          return;
        }
        const body = Buffer.from(JSON.stringify({
          status: "success",
          file_path: path.join(repoRoot, "docs", "v0.2.9", "artifacts", "server-handtest-retouch-marker.png"),
          file_name: "精修标注-线上手测.png",
          file_type: "image",
          preview_url: retouchDataUrl,
        })).toString("base64");
        await cdp.send("Fetch.fulfillRequest", {
          requestId: event.requestId,
          responseCode: 200,
          responseHeaders: [
            { name: "Content-Type", value: "application/json; charset=utf-8" },
            { name: "Access-Control-Allow-Origin", value: "*" },
          ],
          body,
        });
      });
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: bridgeSource });
    }
    await cdp.send("Page.navigate", { url: targetUrl });

    await waitFor(cdp, `document.body && document.body.innerText.includes("手测同学")`, "handtest user");
    const initial = await evaluate(cdp, `(() => {
      const composerButtons = Array.from(document.querySelectorAll(".composer button"))
        .map((button) => [button.innerText, button.title, button.getAttribute("aria-label")].filter(Boolean).join(" "));
      const composer = document.querySelector(".composer");
      const zone = document.querySelector(".composer-zone");
      const textarea = document.querySelector(".composer textarea");
      const modelButton = Array.from(document.querySelectorAll(".composer button")).find((button) => (button.innerText || "").includes("gpt-"));
      const sendButton = Array.from(document.querySelectorAll(".composer button")).find((button) => /发送/.test([button.title, button.getAttribute("aria-label")].filter(Boolean).join(" ")));
      const rectOf = (node) => {
        if (!node) return null;
        const rect = node.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, width: rect.width, height: rect.height };
      };
      const composerRect = rectOf(composer);
      const textareaRect = rectOf(textarea);
      const modelRect = rectOf(modelButton);
      const sendRect = rectOf(sendButton);
      const zonePaddingBottom = zone ? parseFloat(getComputedStyle(zone).paddingBottom || "0") : -1;
      return {
        hasTencentSession: document.body.innerText.includes("腾讯文档：项目复盘"),
        hasRetouchSession: document.body.innerText.includes("圣都装饰封面图"),
        composerButtons,
        composerHasTencentDocs: composerButtons.some((label) => /腾讯文档|Tencent Docs/i.test(label)),
        composerHasLocalFile: composerButtons.some((label) => /添加本地文件/.test(label)),
        composerLayout: {
          zonePaddingBottom,
          composerHeight: composerRect ? composerRect.height : 0,
          textareaInside: Boolean(composerRect && textareaRect && textareaRect.top >= composerRect.top - 1 && textareaRect.bottom <= composerRect.bottom + 1),
          modelInside: Boolean(composerRect && modelRect && modelRect.top >= composerRect.top - 1 && modelRect.bottom <= composerRect.bottom + 1),
          sendInside: Boolean(composerRect && sendRect && sendRect.top >= composerRect.top - 1 && sendRect.bottom <= composerRect.bottom + 1),
          modelSendAligned: Boolean(modelRect && sendRect && Math.abs(((modelRect.top + modelRect.bottom) / 2) - ((sendRect.top + sendRect.bottom) / 2)) < 28)
        },
      };
    })()`);
    assert(initial.hasTencentSession, "Tencent Docs session row should be readable", initial);
    assert(initial.hasRetouchSession, "Retouch session row should be visible", initial);
    assert(initial.composerHasLocalFile, "Composer should keep local file entry", initial);
    assert(!initial.composerHasTencentDocs, "Composer should not contain Tencent Docs entry", initial);
    assert(
      initial.composerLayout.zonePaddingBottom <= 24
        && initial.composerLayout.composerHeight <= 96
        && initial.composerLayout.textareaInside
        && initial.composerLayout.modelInside
        && initial.composerLayout.sendInside
        && initial.composerLayout.modelSendAligned,
      "Composer should keep the compact v0.2.9 layout without floating controls",
      initial.composerLayout
    );

    await clickByText(cdp, "设置");
    await waitFor(cdp, `document.body.innerText.includes("外部连接")`, "settings nav");
    await clickByText(cdp, "外部连接");
    await waitFor(cdp, `document.body.innerText.includes("在聊天中连接")`, "Tencent Docs external connection card");
    const external = await evaluate(cdp, `(() => {
      const logo = document.querySelector(".connection-logo.is-tencent-docs");
      const body = document.body.innerText;
      return {
        hasTencentCard: body.includes("腾讯文档") && body.includes("在聊天中连接"),
        hasAgentFlow: body.includes("Agent 交互") || body.includes("agent 引导"),
        logoBackground: logo ? getComputedStyle(logo).backgroundImage : "",
      };
    })()`);
    assert(external.hasTencentCard, "External Connections must show Tencent Docs card", external);
    assert(external.hasAgentFlow, "Tencent Docs card must indicate agent flow", external);
    assert(/tencent-docs\.png/.test(external.logoBackground), "Tencent Docs card should use official logo asset", external);

    await clickByText(cdp, "在聊天中连接");
    await waitFor(cdp, `(() => {
      const textarea = document.querySelector(".composer textarea");
      return textarea && textarea.value.includes("腾讯文档");
    })()`, "agent prompt inserted into composer");

    await clickByText(cdp, "设置");
    await clickByText(cdp, "知识图谱");
    await waitFor(cdp, `document.body.innerText.includes("知识网络")`, "dedicated knowledge graph page");
    const graphDefault = await evaluate(cdp, `(() => {
      const body = document.body.innerText;
      const grid = document.querySelector(".knowledge-graph-grid.is-dedicated");
      return {
        hasDedicatedGrid: Boolean(grid),
        hasSelectionClass: Boolean(grid && grid.classList.contains("has-selection")),
        hasDedicatedCanvas: Boolean(document.querySelector(".knowledge-graph-canvas.is-dedicated svg")),
        hasDetailPanel: Boolean(document.querySelector(".knowledge-node-panel.is-dedicated")),
        hasRelatedCards: Boolean(document.querySelector(".knowledge-related-list")) || body.includes("相邻知识页"),
        hasChineseCategories: body.includes("记忆") && body.includes("会话") && body.includes("图片") && body.includes("外部连接")
      };
    })()`);
    assert(graphDefault.hasDedicatedGrid && graphDefault.hasDedicatedCanvas, "Knowledge Graph should default to a full-width network", graphDefault);
    assert(!graphDefault.hasSelectionClass && !graphDefault.hasDetailPanel, "Knowledge detail panel should stay collapsed until a node is clicked", graphDefault);
    assert(!graphDefault.hasRelatedCards, "Knowledge Graph should not show related-node cards below the detail area", graphDefault);
    assert(graphDefault.hasChineseCategories, "Knowledge Graph category labels should be translated to Chinese", graphDefault);

    await evaluate(cdp, `(() => {
      const node = document.querySelector(".knowledge-graph-node");
      if (!node) return false;
      node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      return true;
    })()`);
    await waitFor(cdp, `Boolean(document.querySelector(".knowledge-graph-grid.is-dedicated.has-selection") && document.querySelector(".knowledge-node-panel.is-dedicated"))`, "knowledge detail after node click");
    const graph = await evaluate(cdp, `(() => ({
      hasSelectionClass: Boolean(document.querySelector(".knowledge-graph-grid.is-dedicated.has-selection")),
      hasDetailPanel: Boolean(document.querySelector(".knowledge-node-panel.is-dedicated")),
      hasSummary: document.body.innerText.includes("正文摘要"),
      hasRelatedCards: Boolean(document.querySelector(".knowledge-related-list")) || document.body.innerText.includes("相邻知识页")
    }))()`);
    assert(graph.hasSelectionClass && graph.hasDetailPanel && graph.hasSummary, "Knowledge detail should expand only after clicking a node", graph);
    assert(!graph.hasRelatedCards, "Knowledge detail should not render the removed related-node card list", graph);

    await evaluate(cdp, `(() => {
      const canvas = document.querySelector(".knowledge-graph-canvas.is-dedicated");
      if (!canvas) return false;
      canvas.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      return true;
    })()`);
    await waitFor(cdp, `Boolean(document.querySelector(".knowledge-graph-grid.is-dedicated") && !document.querySelector(".knowledge-graph-grid.is-dedicated.has-selection") && !document.querySelector(".knowledge-node-panel.is-dedicated"))`, "knowledge detail closes on blank canvas click");
    const graphClosed = await evaluate(cdp, `(() => ({
      hasSelectionClass: Boolean(document.querySelector(".knowledge-graph-grid.is-dedicated.has-selection")),
      hasDetailPanel: Boolean(document.querySelector(".knowledge-node-panel.is-dedicated"))
    }))()`);
    assert(!graphClosed.hasSelectionClass && !graphClosed.hasDetailPanel, "Knowledge detail should close when clicking canvas whitespace", graphClosed);

    await clickByText(cdp, "记忆");
    const memory = await evaluate(cdp, `(() => {
      const section = document.querySelector(".settings-section");
      return {
        hasMemorySection: Boolean(section && section.innerText.includes("项目记忆")),
        hasGraphInsideMemory: Boolean(section && section.querySelector(".knowledge-graph-grid")),
        text: section ? section.innerText : ""
      };
    })()`);
    assert(memory.hasMemorySection, "Memory section should remain visible", memory);
    assert(!memory.hasGraphInsideMemory, "Memory section should not host knowledge graph", memory);

    await closeSettings(cdp);
    await clickByText(cdp, "圣都装饰封面图");
    await waitFor(cdp, `document.body.innerText.includes("已生成手测图片产物")`, "retouch mock session history");
    await clickByText(cdp, "精准修图");
    await waitFor(cdp, `Boolean(document.querySelector(".image-retouch-sheet.is-editor"))`, "retouch editor modal");
    await drawRetouchAnnotation(cdp);
    await waitFor(cdp, `document.querySelectorAll(".image-retouch-overlay path").length >= 2`, "curved retouch annotation path");
    const retouch = await evaluate(cdp, `(() => ({
      hasEditor: Boolean(document.querySelector(".image-retouch-sheet.is-editor")),
      hasToolbar: Boolean(document.querySelector(".image-retouch-bottom-toolbar")),
      hasStylePanel: Boolean(document.querySelector(".image-retouch-style-panel")),
      hasStageBoard: Boolean(document.querySelector(".image-retouch-image-wrap.has-stage")),
      headerHeight: Math.round(document.querySelector(".image-retouch-header")?.getBoundingClientRect().height || 0),
      pathCount: document.querySelectorAll(".image-retouch-overlay path").length,
      lineCount: document.querySelectorAll(".image-retouch-overlay line").length,
      labelText: document.querySelector(".image-retouch-svg-label")?.innerText || "",
      labelOutsideImage: (() => {
        const label = document.querySelector(".image-retouch-svg-label");
        const image = document.querySelector(".image-retouch-image-wrap img");
        if (!label || !image) return false;
        const a = label.getBoundingClientRect();
        const b = image.getBoundingClientRect();
        return a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom;
      })()
    }))()`);
    assert(retouch.hasEditor && retouch.hasToolbar && retouch.hasStylePanel, "Retouch editor should match reference layout controls", retouch);
    assert(retouch.hasStageBoard && retouch.headerHeight <= 42, "Retouch editor should use thin Cowart-like canvas chrome", retouch);
    assert(retouch.pathCount >= 2 && retouch.lineCount === 0, "Retouch annotation should use curved path/open arrow, not SVG line", retouch);
    assert(retouch.labelOutsideImage, "Retouch annotation label should be allowed outside the image bounds", retouch);
    assert(retouch.labelText.includes("不要挡住标题"), "Retouch label should commit typed annotation", retouch);

    await clickByText(cdp, "加入聊天框");
    try {
      await waitFor(cdp, `!document.querySelector(".image-retouch-sheet.is-editor")`, "retouch modal closed after preparing draft");
    } catch (error) {
      const failureState = await evaluate(cdp, `(() => ({
        modalText: document.querySelector(".image-retouch-sheet.is-editor")?.innerText || "",
        errorText: document.querySelector(".image-retouch-error")?.innerText || "",
        busyButton: Array.from(document.querySelectorAll("button")).find((button) => /加入中|加入聊天框/.test(button.innerText || ""))?.innerText || "",
        hasSession: Boolean(localStorage.getItem("ecorex-web-enterprise-session")),
        sessionKeys: Object.keys(localStorage).filter((key) => key.includes("ecorex-web")).sort()
      }))()`);
      throw new Error(`${error.message}\nRetouch failure state: ${JSON.stringify(failureState, null, 2)}`);
    }
    await waitFor(cdp, `document.body.innerText.includes("已加入聊天框")`, "retouch prepared toast");
    await waitFor(cdp, `Boolean(document.querySelector(".attachment-tray img"))`, "retouch marker attachment thumbnail");
    const retouchDraft = await evaluate(cdp, `(() => {
      const textarea = document.querySelector(".composer textarea");
      const attachment = document.querySelector(".attachment-tray article");
      return {
        hasAttachment: Boolean(attachment),
        attachmentText: attachment ? attachment.innerText : "",
        hasThumbnail: Boolean(document.querySelector(".attachment-tray img")),
        draft: textarea ? textarea.value : "",
        sendEnabled: !document.querySelector(".send-button")?.disabled
      };
    })()`);
    assert(retouchDraft.hasAttachment && retouchDraft.hasThumbnail, "Retouch submission should prepare an annotated image thumbnail in composer", retouchDraft);
    assert(retouchDraft.attachmentText.includes("精修标注"), "Retouch attachment should be named as an edit marker", retouchDraft);
    assert(retouchDraft.draft.includes("请批量处理以下精准修图任务"), "Retouch draft should include batch edit instruction", retouchDraft);
    assert(retouchDraft.draft.includes("不要挡住标题"), "Retouch draft should include annotation text", retouchDraft);

    const result = {
      status: "PASS",
      targetUrl,
      checks: {
        initial,
        external,
        graphDefault,
        graph,
        graphClosed,
        memory,
        retouch,
        retouchDraft,
      },
    };
    console.log(JSON.stringify(result, null, 2));
  } finally {
    if (cdp) cdp.close();
    chromeProcess.kill();
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch {
      // Chrome can keep the profile locked briefly on Windows; this should not fail the UI smoke.
    }
    if (handtestServer) await handtestServer.close();
  }
}

run().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});

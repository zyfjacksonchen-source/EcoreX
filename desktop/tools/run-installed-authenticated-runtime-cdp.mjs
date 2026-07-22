#!/usr/bin/env node

/**
 * Real-user acceptance against an already-running, installed EcoreX Runtime.
 *
 * This runner never starts a fixture Runtime and never accepts credentials on
 * argv.  It intentionally returns only bounded status facts and hashes.
 */

import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { chromium } from "@playwright/test";

const ALL_GROUPS = Object.freeze(["attachments", "tools", "image", "models", "ui"]);
const TERMINAL_TURN_STATES = new Set(["completed", "failed", "cancelled", "interrupted", "superseded"]);
const MAX_TIMEOUT_MS = 30 * 60 * 1000;
const SAFE_VERSION = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$/u;
const SAFE_RELEASE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;

function argument(name) {
  const prefix = `${name}=`;
  const item = process.argv.slice(2).find((value) => value.startsWith(prefix));
  return item ? item.slice(prefix.length) : null;
}

function requiredArgument(name) {
  const value = argument(name);
  if (!value) throw new Error(`${name.slice(2).replaceAll("-", "_")}_missing`);
  return value;
}

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name.toLowerCase()}_missing`);
  delete process.env[name];
  return value;
}

function selectedGroups() {
  const raw = argument("--groups");
  if (!raw) return new Set(ALL_GROUPS);
  const groups = raw.split(",").map((item) => item.trim()).filter(Boolean);
  if (!groups.length || groups.some((item) => !ALL_GROUPS.includes(item))) {
    throw new Error("groups_invalid");
  }
  return new Set(groups);
}

function runtimeOrigin() {
  const parsed = new URL(requiredArgument("--base-url"));
  if (
    parsed.protocol !== "http:"
    || parsed.hostname !== "127.0.0.1"
    || !parsed.port
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
    || parsed.username
    || parsed.password
  ) throw new Error("runtime_origin_invalid");
  return parsed.origin;
}

function expectedIdentity() {
  const releaseId = requiredArgument("--expected-release-id");
  const version = requiredArgument("--expected-version");
  if (!SAFE_RELEASE_ID.test(releaseId) || !SAFE_VERSION.test(version)) {
    throw new Error("runtime_identity_invalid");
  }
  return { releaseId, version };
}

function timeoutMilliseconds() {
  const value = Number(argument("--timeout-ms") || "1200000");
  if (!Number.isSafeInteger(value) || value < 60_000 || value > MAX_TIMEOUT_MS) {
    throw new Error("timeout_invalid");
  }
  return value;
}

function digest(value) {
  return createHash("sha256").update(String(value)).digest("hex");
}

function id(prefix) {
  return `${prefix}_${randomUUID().replaceAll("-", "")}`;
}

function chromeExecutable() {
  if (process.platform !== "win32") throw new Error("windows_chrome_required");
  const candidates = [
    process.env.PROGRAMFILES,
    process.env["PROGRAMFILES(X86)"],
    process.env.LOCALAPPDATA,
  ].filter(Boolean).map((root) => join(root, "Google", "Chrome", "Application", "chrome.exe"));
  const selected = candidates.find((candidate) => existsSync(candidate));
  if (!selected) throw new Error("chrome_unavailable");
  return resolve(selected);
}

async function freePort() {
  return await new Promise((resolvePort, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => error ? reject(error) : resolvePort(port));
    });
  });
}

async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(1_000) });
      if (response.ok) return await response.json();
    } catch { /* startup race */ }
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error("cdp_startup_timeout");
}

function terminate(child) {
  if (!child || child.exitCode !== null) return;
  const taskkill = join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
  spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
}

function check(condition, code) {
  if (!condition) throw new Error(code);
}

async function api(page, path, options = {}) {
  const response = await page.evaluate(async ({ requestPath, requestOptions }) => {
    const bridge = window.__ECOREX_RUNTIME__;
    if (!bridge?.bearerToken || bridge.apiBase !== "/api/v1") throw new Error("runtime_bridge_missing");
    const authorization = { Authorization: `Bearer ${bridge.bearerToken}`, Accept: "application/json" };
    const bootstrapResponse = await fetch("/api/v1/bootstrap", {
      headers: authorization,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!bootstrapResponse.ok) throw new Error(`bootstrap_${bootstrapResponse.status}`);
    const bootstrap = await bootstrapResponse.json();
    const headers = { ...authorization, ...(requestOptions.headers || {}) };
    const method = requestOptions.method || "GET";
    let body;
    if (requestOptions.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(requestOptions.json);
    }
    if (method !== "GET" && method !== "HEAD") headers["X-EcoreX-CSRF"] = bootstrap.csrf_token;
    const result = await fetch(requestPath, {
      method,
      headers,
      body,
      cache: "no-store",
      credentials: "same-origin",
    });
    const contentType = result.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await result.json().catch(() => null) : null;
    return { ok: result.ok, status: result.status, payload, contentType };
  }, { requestPath: path, requestOptions: options });
  if (!response.ok) throw new Error(`api_${response.status}`);
  return response.payload;
}

async function binaryFact(page, path) {
  const fact = await page.evaluate(async (requestPath) => {
    const bridge = window.__ECOREX_RUNTIME__;
    const response = await fetch(requestPath, {
      headers: { Authorization: `Bearer ${bridge.bearerToken}` },
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) return { ok: false, status: response.status };
    const bytes = await response.arrayBuffer();
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return {
      ok: true,
      status: response.status,
      size: bytes.byteLength,
      contentType: response.headers.get("content-type") || "",
      sha256: [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, "0")).join(""),
    };
  }, path);
  check(fact.ok && fact.size > 0 && SHA256.test(fact.sha256), "binary_fact_invalid");
  return fact;
}

async function bootstrap(page) {
  return await api(page, "/api/v1/bootstrap");
}

async function login(page, origin, identifier, password, timeoutMs) {
  let snapshot = await bootstrap(page);
  if (snapshot.login?.authenticated === true) return snapshot;
  await page.getByLabel("账号或邮箱").fill(identifier);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  password = "";
  const deadline = Date.now() + Math.min(timeoutMs, 120_000);
  while (Date.now() < deadline) {
    try {
      await page.waitForTimeout(750);
      await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 10_000 });
      snapshot = await bootstrap(page);
      if (snapshot.login?.authenticated === true) {
        await page.getByRole("button", { name: "新建任务" }).waitFor({ state: "visible", timeout: 15_000 });
        return snapshot;
      }
    } catch { /* Runtime bearer rotation and restart are expected. */ }
  }
  throw new Error("login_restart_timeout");
}

async function createThread(page, title) {
  return await api(page, "/api/v1/threads", {
    method: "POST",
    json: { title, metadata: {}, client_request_id: id("accept_thread") },
  });
}

async function createTurn(page, threadId, input, models, explicitToolIds = [], attachmentIds = []) {
  return await api(page, `/api/v1/threads/${encodeURIComponent(threadId)}/turns`, {
    method: "POST",
    json: {
      input,
      agent_model_id: models.agent,
      image_model_id: models.image,
      explicit_tool_ids: explicitToolIds,
      attachment_ids: attachmentIds,
      client_message_id: id("accept_message"),
      metadata: {},
    },
  });
}

async function eventPage(page, threadId) {
  return await api(page, `/api/v1/threads/${encodeURIComponent(threadId)}/events?after_seq=0&limit=1000`);
}

async function projection(page, threadId) {
  return await api(page, `/api/v1/threads/${encodeURIComponent(threadId)}/projection`);
}

async function waitForTurn(page, threadId, turnId, timeoutMs) {
  const started = performance.now();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const events = await eventPage(page, threadId);
    const terminal = events.events.findLast((event) => (
      event.turn_id === turnId
      && event.event_type === "turn.status_changed"
      && TERMINAL_TURN_STATES.has(event.payload?.to)
    ));
    if (terminal) {
      check(terminal.payload.to === "completed", "turn_not_completed");
      return { events: events.events, terminal, durationMs: Math.round(performance.now() - started) };
    }
    await page.waitForTimeout(500);
  }
  throw new Error("turn_timeout");
}

function toolFacts(events, toolId) {
  const requested = events.filter((event) => event.event_type === "tool.call_requested" && event.payload?.activity?.tool_id === toolId);
  const completed = events.filter((event) => event.event_type === "tool.result" && event.payload?.activity?.tool_id === toolId && event.payload?.activity?.status === "completed");
  check(requested.length > 0 && completed.length > 0, `${toolId}_tool_event_missing`);
  const requestIds = new Set(requested.map((event) => event.tool_call_id));
  check(completed.some((event) => requestIds.has(event.tool_call_id)), `${toolId}_tool_identity_mismatch`);
  const activity = completed.at(-1).payload.activity;
  check(SHA256.test(activity.argument_sha256) && SHA256.test(activity.result_sha256), `${toolId}_tool_digest_invalid`);
  return { tool: toolId, calls: completed.length, event_digest: digest(completed.map((item) => item.event_id).join("\0")) };
}

function assistantText(projectionValue, turnId) {
  return projectionValue.items
    .filter((item) => item.turn_id === turnId && item.kind === "message" && item.content?.role === "assistant")
    .map((item) => String(item.content?.text || ""))
    .join("\n");
}

async function runTool(page, models, toolId, input, attachmentIds, expected, timeoutMs) {
  const thread = await createThread(page, `验收 ${toolId}`);
  const mutation = await createTurn(page, thread.thread_id, input, models, [toolId], attachmentIds);
  const result = await waitForTurn(page, thread.thread_id, mutation.turn.turn_id, timeoutMs);
  const facts = toolFacts(result.events, toolId);
  const state = await projection(page, thread.thread_id);
  const text = assistantText(state, mutation.turn.turn_id);
  if (expected) check(text.toLocaleLowerCase().includes(expected.toLocaleLowerCase()), `${toolId}_expected_output_missing`);
  return {
    ...facts,
    thread_sha256: digest(thread.thread_id),
    turn_sha256: digest(mutation.turn.turn_id),
    response_sha256: digest(text),
    duration_milliseconds: result.durationMs,
  };
}

async function progressiveDiscoveryScenario(page, models, attachmentId, expected, timeoutMs) {
  const thread = await createThread(page, "渐进式工具发现验收");
  const mutation = await createTurn(
    page,
    thread.thread_id,
    "不要猜测图片内容。先使用 tool_search 搜索能读取图片文字的能力，再调用搜索到的 OCR 能力读取唯一验收代码并原样返回。",
    models,
    [],
    [attachmentId],
  );
  const result = await waitForTurn(page, thread.thread_id, mutation.turn.turn_id, timeoutMs);
  const search = toolFacts(result.events, "tool_search");
  const ocr = toolFacts(result.events, "ocr");
  const searchSequence = result.events.find((event) => (
    event.event_type === "tool.result"
    && event.payload?.activity?.tool_id === "tool_search"
    && event.payload?.activity?.status === "completed"
  ))?.seq;
  const ocrSequence = result.events.find((event) => (
    event.event_type === "tool.result"
    && event.payload?.activity?.tool_id === "ocr"
    && event.payload?.activity?.status === "completed"
  ))?.seq;
  check(Number.isSafeInteger(searchSequence) && Number.isSafeInteger(ocrSequence) && searchSequence < ocrSequence, "progressive_discovery_order_invalid");
  const text = assistantText(await projection(page, thread.thread_id), mutation.turn.turn_id);
  check(text.toLocaleLowerCase().includes(expected.toLocaleLowerCase()), "progressive_discovery_output_missing");
  return {
    status: "passed",
    thread_sha256: digest(thread.thread_id),
    turn_sha256: digest(mutation.turn.turn_id),
    response_sha256: digest(text),
    event_digest: digest(`${search.event_digest}:${ocr.event_digest}`),
    discovered_tool: ocr.tool,
    duration_milliseconds: result.durationMs,
  };
}

async function uploadWithPreview(page, imagePath, timeoutMs) {
  await page.getByRole("button", { name: "新建任务" }).click();
  const input = page.locator('input[type="file"][aria-label="选择要添加的文件"]');
  await input.waitFor({ state: "attached", timeout: 10_000 });
  let delayed = false;
  await page.route("**/api/v1/input-attachments", async (route) => {
    if (!delayed && route.request().method() === "POST") {
      delayed = true;
      await new Promise((resolveWait) => setTimeout(resolveWait, 400));
    }
    await route.continue();
  });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/v1/input-attachments"
  ), { timeout: Math.min(timeoutMs, 60_000) });
  await input.setInputFiles(imagePath);
  const pending = page.locator(".ex-input-attachment.is-uploading");
  await pending.waitFor({ state: "visible", timeout: 5_000 });
  check(await pending.locator("img").count() === 1, "local_upload_thumbnail_missing");
  const uploadResponse = await responsePromise;
  check(uploadResponse.ok(), "attachment_upload_failed");
  const attachment = await uploadResponse.json();
  const preview = page.getByRole("button", { name: `完整预览：${attachment.display_name}` });
  await preview.waitFor({ state: "visible", timeout: 20_000 });
  await page.getByText("已就绪", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
  check(await preview.isEnabled(), "authenticated_upload_thumbnail_missing");
  await preview.click();
  const dialog = page.getByRole("dialog", { name: attachment.display_name });
  await dialog.waitFor({ state: "visible" });
  const naturalWidth = await dialog.locator("img").evaluate((image) => image.naturalWidth);
  check(naturalWidth > 0, "attachment_full_preview_invalid");
  await dialog.getByRole("button", { name: "关闭图片预览" }).click();
  return {
    attachment,
    evidence: {
      attachment_sha256: digest(attachment.attachment_id),
      source_sha256: attachment.sha256,
      media_kind: attachment.media_kind,
      local_thumbnail: true,
      authenticated_thumbnail: true,
      full_preview: true,
    },
  };
}

async function imageScenario(page, snapshot, models, timeoutMs) {
  const submissions = await Promise.all(Array.from({ length: 4 }, async (_, index) => {
    const thread = await createThread(page, `并发生图 ${index + 1}`);
    const mutation = await createTurn(
      page,
      thread.thread_id,
      `生成一张编号 ${index + 1} 的极简办公验收图片，画面包含清晰的 ECX-${index + 1}。`,
      models,
    );
    return { thread, mutation, index };
  }));
  const normalThread = await createThread(page, "并发普通任务");
  const normalMutation = await createTurn(page, normalThread.thread_id, "只回复 NORMAL-READY。", models);
  const normalPromise = waitForTurn(page, normalThread.thread_id, normalMutation.turn.turn_id, Math.min(timeoutMs, 120_000));
  const imagePromises = submissions.map(async ({ thread, mutation, index }) => {
    const result = await waitForTurn(page, thread.thread_id, mutation.turn.turn_id, timeoutMs);
    const tool = toolFacts(result.events, "imagegen");
    const artifacts = await api(page, `/api/v1/artifacts?thread_id=${encodeURIComponent(thread.thread_id)}`);
    const image = artifacts.items.find((item) => item.family === "image" && item.visibility === "primary");
    check(image && image.actions.includes("preview") && SHA256.test(image.sha256), "image_artifact_missing");
    const preview = await binaryFact(page, `/api/v1/artifacts/${encodeURIComponent(image.artifact_id)}/preview`);
    check(preview.contentType.startsWith("image/"), "image_preview_mime_invalid");
    return { index, thread, mutation, result, tool, image, preview };
  });
  const normal = await normalPromise;
  const images = await Promise.all(imagePromises);
  const normalText = assistantText(await projection(page, normalThread.thread_id), normalMutation.turn.turn_id);
  check(normalText.includes("NORMAL-READY"), "normal_concurrent_task_invalid");
  const hashes = images.map((item) => item.image.sha256);
  check(new Set(hashes).size === 4, "concurrent_image_artifacts_not_unique");
  check(normal.durationMs < Math.max(...images.map((item) => item.result.durationMs)), "normal_task_blocked_by_image_pool");
  return {
    status: "passed",
    completed_requests: 4,
    unique_artifacts: new Set(hashes).size,
    artifact_sha256: hashes,
    normal_duration_milliseconds: normal.durationMs,
    image_duration_milliseconds: images.map((item) => item.result.durationMs),
    image_model_sha256: digest(models.image),
    first: images[0],
    service_state: snapshot.model_service.state,
  };
}

async function retouchScenario(page, source, models, timeoutMs) {
  let workspace = await api(page, `/api/v1/artifacts/${encodeURIComponent(source.image.artifact_id)}/retouch-workspaces`, {
    method: "POST",
    json: { base_revision_id: source.image.revision_id, client_request_id: id("accept_retouch_open") },
  });
  workspace = await api(page, `/api/v1/retouch-workspaces/${encodeURIComponent(workspace.workspace_id)}`, {
    method: "PATCH",
    json: {
      expected_version: workspace.version,
      annotations: [{
        kind: "rectangle",
        normalized_geometry: { x: 0.08, y: 0.08, width: 0.28, height: 0.28 },
        instruction: "只把矩形内的编号改成 ECX-RET，矩形外保持不变。",
        annotation_id: id("annotation"),
      }],
      reference_artifact_ids: [],
      global_instruction: "只修改标注区域。",
      view_state: {},
      client_request_id: id("accept_retouch_save"),
    },
  });
  workspace = await api(page, `/api/v1/retouch-workspaces/${encodeURIComponent(workspace.workspace_id)}/submit`, {
    method: "POST",
    json: {
      expected_version: workspace.version,
      agent_model_id: models.agent,
      image_model_id: models.image,
      client_request_id: id("accept_retouch_submit"),
    },
  });
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    workspace = await api(page, `/api/v1/retouch-workspaces/${encodeURIComponent(workspace.workspace_id)}`);
    if (workspace.job?.status === "completed") break;
    if (["failed", "cancelled"].includes(workspace.job?.status)) throw new Error("retouch_not_completed");
    await page.waitForTimeout(750);
  }
  check(workspace.job?.status === "completed" && workspace.result, "retouch_timeout");
  check(workspace.result.revision_id !== source.image.revision_id, "retouch_revision_unchanged");
  check(workspace.result.sha256 !== source.image.sha256, "retouch_digest_unchanged");
  const result = await binaryFact(page, `/api/v1/retouch-workspaces/${encodeURIComponent(workspace.workspace_id)}/result`);
  check(result.contentType.startsWith("image/"), "retouch_result_mime_invalid");
  const events = await eventPage(page, source.thread.thread_id);
  check(events.events.some((event) => event.event_type === "artifact.retouch.requested"), "retouch_requested_event_missing");
  check(events.events.some((event) => event.event_type === "artifact.retouch.completed"), "retouch_completed_event_missing");
  return {
    status: "passed",
    workspace_sha256: digest(workspace.workspace_id),
    base_revision_sha256: digest(source.image.revision_id),
    result_revision_sha256: digest(workspace.result.revision_id),
    result_sha256: result.sha256,
    inspection_regions: workspace.job.inspection_regions?.length || 0,
  };
}

async function modelScenarios(page, snapshot, timeoutMs) {
  check(snapshot.model_service.state === "ready", "model_service_unavailable");
  check(snapshot.models?.snapshot_id && snapshot.models.chat.length > 0, "model_catalog_empty");
  const gpt = snapshot.models.chat.find((item) => item.model_policy?.upstream_model_id === "gpt-5.6-sol");
  check(gpt, "gpt_56_catalog_entry_missing");
  check(gpt.model_policy.reasoning_effort === "medium", "gpt_56_reasoning_invalid");
  check(gpt.model_policy.context_management?.compact_threshold_tokens === 272000, "gpt_56_compaction_invalid");
  const results = [];
  check(snapshot.models.chat.length <= 16, "model_catalog_too_large_for_acceptance");
  for (const model of snapshot.models.chat) {
    const thread = await createThread(page, `模型验收 ${model.display_name}`);
    const mutation = await createTurn(page, thread.thread_id, "只回复 MODEL-READY。", { agent: model.model_id, image: null });
    const terminal = await waitForTurn(page, thread.thread_id, mutation.turn.turn_id, timeoutMs);
    check(mutation.turn.agent_model_id === model.model_id, "model_turn_identity_mismatch");
    check(terminal.events.some((event) => event.event_type === "model.response_completed"), "model_completion_event_missing");
    results.push({ model_sha256: digest(model.model_id), duration_milliseconds: terminal.durationMs });
  }
  await page.getByRole("button", { name: "新建任务" }).click();
  const trigger = page.getByRole("button", { name: "选择模型" });
  await trigger.waitFor({ state: "visible", timeout: 15_000 });
  await trigger.click();
  const switchedModel = snapshot.models.chat[1] || snapshot.models.chat[0];
  await page.getByRole("menuitemradio", { name: switchedModel.display_name, exact: true }).click();
  check((await trigger.textContent())?.includes(switchedModel.display_name), "model_ui_switch_failed");
  return {
    status: "passed",
    snapshot_sha256: digest(snapshot.models.snapshot_id),
    tested: results,
    tested_count: results.length,
    visible_count: snapshot.models.chat.length,
    ui_switch: true,
  };
}

async function uiScenarios(page, origin, imageThreadId, timeoutMs) {
  await page.goto(origin, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "新建任务" }).waitFor({ state: "visible" });
  const themeButton = page.getByRole("button", { name: /切换到(?:明亮|暗色)模式/u });
  const before = await page.evaluate(() => localStorage.getItem("ecorex-theme"));
  await themeButton.click();
  const after = await page.evaluate(() => localStorage.getItem("ecorex-theme"));
  check(after && after !== before, "theme_switch_failed");
  await page.reload({ waitUntil: "domcontentloaded" });
  check(await page.evaluate(() => localStorage.getItem("ecorex-theme")) === after, "theme_persistence_failed");

  const first = await api(page, `/api/v1/threads/${encodeURIComponent(imageThreadId)}/shares`, {
    method: "POST",
    json: { expires_in_hours: 24, client_request_id: id("accept_share") },
  });
  const otherThread = await createThread(page, "分享唯一性验收");
  const second = await api(page, `/api/v1/threads/${encodeURIComponent(otherThread.thread_id)}/shares`, {
    method: "POST",
    json: { expires_in_hours: 24, client_request_id: id("accept_share") },
  });
  check(first.share_id !== second.share_id, "share_identity_not_unique");
  const deadline = Date.now() + Math.min(timeoutMs, 120_000);
  let published = first;
  while (Date.now() < deadline && published.status === "publishing") {
    await page.waitForTimeout(750);
    published = await api(page, `/api/v1/shares/${encodeURIComponent(first.share_id)}`);
  }
  check(published.status === "published" && published.public_url, "share_not_published");
  const publicPage = await page.context().newPage();
  try {
    await publicPage.goto(published.public_url, { waitUntil: "domcontentloaded", timeout: Math.min(timeoutMs, 60_000) });
    check(await publicPage.locator("article.message.user").count() > 0, "share_user_message_missing");
    check(await publicPage.locator("article.message.assistant").count() > 0, "share_assistant_message_missing");
    const sharedImage = publicPage.locator("figure.image-artifact img").first();
    await sharedImage.waitFor({ state: "visible", timeout: 20_000 });
    check(await sharedImage.evaluate((image) => image.complete && image.naturalWidth > 0), "share_image_preview_invalid");
  } finally {
    await publicPage.close();
  }
  return {
    status: "passed",
    theme_before: before === "light" ? "light" : "dark",
    theme_after: after,
    share_sha256: digest(first.share_id),
    second_share_sha256: digest(second.share_id),
    share_unique: true,
    transcript_roles: true,
    image_preview: true,
    public_url_sha256: digest(published.public_url),
  };
}

async function run() {
  const started = performance.now();
  const origin = runtimeOrigin();
  const expected = expectedIdentity();
  const timeoutMs = timeoutMilliseconds();
  const groups = selectedGroups();
  let identifier = requiredEnvironment("ECOREX_ACCEPTANCE_IDENTIFIER");
  let password = requiredEnvironment("ECOREX_ACCEPTANCE_PASSWORD");
  const imagePath = (groups.has("attachments") || groups.has("tools"))
    ? resolve(requiredEnvironment("ECOREX_ACCEPTANCE_IMAGE_FIXTURE")) : null;
  const ocrExpected = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_OCR_EXPECTED") : null;
  const visionExpected = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_VISION_EXPECTED") : null;
  const readPath = groups.has("tools") ? resolve(requiredEnvironment("ECOREX_ACCEPTANCE_READ_FIXTURE")) : null;
  const readExpected = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_READ_EXPECTED") : null;
  const fetchUrl = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_FETCH_URL") : null;
  const fetchExpected = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_FETCH_EXPECTED") : null;
  const cdpUrl = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_CDP_URL") : null;
  const cdpExpected = groups.has("tools") ? requiredEnvironment("ECOREX_ACCEPTANCE_CDP_EXPECTED") : null;
  if (imagePath) {
    check(existsSync(imagePath), "image_fixture_missing");
    check((await stat(imagePath)).size <= 64 * 1024 * 1024, "image_fixture_too_large");
  }
  if (readPath) {
    check(existsSync(readPath), "read_fixture_missing");
    check((await stat(readPath)).size <= 64 * 1024 * 1024, "read_fixture_too_large");
  }

  const profileRoot = await mkdtemp(join(tmpdir(), "ecorex-installed-auth-cdp-"));
  check(resolve(profileRoot).startsWith(resolve(tmpdir()) + sep), "profile_root_invalid");
  const cdpPort = await freePort();
  let chrome = null;
  let browser = null;
  const diagnostics = { console_errors: 0, page_errors: 0, failed_runtime_requests: 0 };
  try {
    chrome = spawn(chromeExecutable(), [
      "--headless=new", `--remote-debugging-port=${cdpPort}`, "--remote-debugging-address=127.0.0.1",
      `--user-data-dir=${profileRoot}`, "--no-first-run", "--no-default-browser-check",
      "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
      "--disable-sync", "--metrics-recording-only", "about:blank",
    ], { stdio: ["ignore", "ignore", "ignore"], windowsHide: true });
    const versionProjection = await waitForJson(`http://127.0.0.1:${cdpPort}/json/version`, 20_000);
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`, { timeout: 20_000 });
    const context = browser.contexts()[0];
    check(context, "cdp_context_missing");
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(20_000);
    page.setDefaultNavigationTimeout(20_000);
    page.on("console", (message) => { if (message.type() === "error") diagnostics.console_errors += 1; });
    page.on("pageerror", () => { diagnostics.page_errors += 1; });
    page.on("requestfailed", (request) => {
      try {
        const requestUrl = new URL(request.url());
        const expectedStreamAbort = requestUrl.origin === origin
          && requestUrl.pathname.endsWith("/events/stream")
          && /ERR_(?:ABORTED|FAILED)/u.test(request.failure()?.errorText || "");
        if (requestUrl.origin === origin && !expectedStreamAbort) diagnostics.failed_runtime_requests += 1;
      } catch { /* ignored */ }
    });
    await page.goto(origin, { waitUntil: "domcontentloaded" });
    let snapshot = await login(page, origin, identifier, password, timeoutMs);
    identifier = "";
    password = "";
    check(await windowIdentity(page, expected), "runtime_identity_mismatch");
    snapshot = await bootstrap(page);
    diagnostics.console_errors = 0;
    diagnostics.page_errors = 0;
    diagnostics.failed_runtime_requests = 0;
    const defaultChat = snapshot.models.chat.find((item) => item.is_default) || snapshot.models.chat[0];
    const defaultImage = snapshot.models.image.find((item) => item.is_default) || snapshot.models.image[0];
    check(defaultChat, "default_chat_model_missing");
    const models = { agent: defaultChat.model_id, image: defaultImage?.model_id ?? null };
    const report = {
      schema_version: 1,
      evidence_type: "ecorex-installed-authenticated-runtime-cdp",
      status: "passed",
      runtime: { release_id: expected.releaseId, version: expected.version, model_snapshot_sha256: digest(snapshot.models.snapshot_id || "none") },
      browser: { engine: "chrome", version_sha256: digest(versionProjection.Browser || "unknown"), protocol: "cdp", isolated_profile: true },
      groups: [...groups],
      executions: {},
      diagnostics,
    };
    let uploaded = null;
    if (groups.has("attachments")) {
      uploaded = await uploadWithPreview(page, imagePath, timeoutMs);
      report.executions.attachments = uploaded.evidence;
    }
    if (groups.has("tools")) {
      if (!uploaded) uploaded = await uploadWithPreview(page, imagePath, timeoutMs);
      report.executions.tools = await Promise.all([
        runTool(page, models, "ocr", "使用 OCR 工具读取图片中的唯一验收代码，并原样返回。", [uploaded.attachment.attachment_id], ocrExpected, timeoutMs),
        runTool(page, models, "vision", "使用 vision 工具识别图片主体和唯一验收代码。", [uploaded.attachment.attachment_id], visionExpected, timeoutMs),
        runTool(page, models, "read", `使用 read 工具读取这个文件并原样返回其中的验收代码：${readPath}`, [], readExpected, timeoutMs),
        runTool(page, models, "shell", "使用 shell 工具执行只读命令输出 ECX-SHELL-READY，并原样返回。", [], "ECX-SHELL-READY", timeoutMs),
        runTool(page, models, "fetch", `使用 fetch 工具读取 ${fetchUrl} 并返回唯一验收代码。`, [], fetchExpected, timeoutMs),
        runTool(page, models, "cdp", `使用 cdp 工具打开 ${cdpUrl} 并读取页面中的唯一验收代码。`, [], cdpExpected, timeoutMs),
      ]);
      report.executions.progressive_discovery = await progressiveDiscoveryScenario(
        page,
        models,
        uploaded.attachment.attachment_id,
        ocrExpected,
        timeoutMs,
      );
    }
    let images = null;
    if (groups.has("image")) {
      check(models.image, "default_image_model_missing");
      images = await imageScenario(page, snapshot, models, timeoutMs);
      report.executions.image = {
        status: images.status,
        completed_requests: images.completed_requests,
        unique_artifacts: images.unique_artifacts,
        artifact_sha256: images.artifact_sha256,
        normal_duration_milliseconds: images.normal_duration_milliseconds,
        image_duration_milliseconds: images.image_duration_milliseconds,
        image_model_sha256: images.image_model_sha256,
        service_state: images.service_state,
      };
      report.executions.retouch = await retouchScenario(page, images.first, models, timeoutMs);
    }
    if (groups.has("models")) report.executions.models = await modelScenarios(page, snapshot, timeoutMs);
    if (groups.has("ui")) {
      if (!images) throw new Error("ui_group_requires_image_group");
      report.executions.ui = await uiScenarios(page, origin, images.first.thread.thread_id, timeoutMs);
    }
    check(Object.values(diagnostics).every((value) => value === 0), "browser_diagnostics_failed");
    const screenshot = await page.screenshot({ type: "png" });
    report.screenshot_sha256 = createHash("sha256").update(screenshot).digest("hex");
    report.duration_milliseconds = Math.round(performance.now() - started);
    return report;
  } finally {
    identifier = "";
    password = "";
    if (browser) await browser.close().catch(() => undefined);
    terminate(chrome);
    await rm(profileRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

async function windowIdentity(page, expected) {
  const value = await page.evaluate(() => ({
    releaseId: window.__ECOREX_RUNTIME__?.releaseId,
    version: window.__ECOREX_RUNTIME__?.version,
    frozen: Object.isFrozen(window.__ECOREX_RUNTIME__),
  }));
  return value.releaseId === expected.releaseId && value.version === expected.version && value.frozen === true;
}

try {
  const result = await run();
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  const message = error instanceof Error ? error.message : "installed_authenticated_cdp_failed";
  const code = /^[a-z][a-z0-9_]{2,127}$/u.test(message) ? message : "installed_authenticated_cdp_failed";
  process.stderr.write(`${JSON.stringify({ ok: false, error: code })}\n`);
  process.exitCode = 1;
}

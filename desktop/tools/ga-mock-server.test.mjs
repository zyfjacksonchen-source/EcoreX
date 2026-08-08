import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer as createHttpServer, get as httpGet } from "node:http";
import test from "node:test";

import { createGaMockServer } from "./ga-mock-server.mjs";

const PRODUCT_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;

const MUTATION_HEADERS = {
  "Content-Type": "application/json",
  "X-EcoreX-CSRF": "ga-csrf-token-0123456789abcdef0123456789abcdef",
};

function snapshotResponseHeaders(response) {
  const copied = new Map();
  const distinct = response.headersDistinct;
  if (distinct && typeof distinct === "object") {
    for (const [name, rawValues] of Object.entries(distinct)) {
      const values = Array.isArray(rawValues) ? rawValues : [rawValues];
      copied.set(
        name.toLowerCase(),
        Object.freeze(values.map((value) => String(value))),
      );
    }
  }

  // headersDistinct is available on supported Node 22, but retain an exact
  // wire-level fallback so this test helper remains diagnostic on older Node.
  if (copied.size === 0) {
    const rawHeaders = [...response.rawHeaders];
    for (let index = 0; index < rawHeaders.length; index += 2) {
      const name = String(rawHeaders[index]).toLowerCase();
      const values = copied.get(name) || [];
      copied.set(name, [...values, String(rawHeaders[index + 1])]);
    }
  }
  if (copied.size === 0) {
    for (const [name, rawValue] of Object.entries(response.headers)) {
      const values = Array.isArray(rawValue) ? rawValue : [rawValue];
      copied.set(name.toLowerCase(), values.map((value) => String(value)));
    }
  }
  for (const [name, values] of copied) {
    copied.set(name, Object.freeze([...values]));
  }

  return Object.freeze({
    get(name) {
      const values = copied.get(String(name).toLowerCase());
      return values ? values.join(", ") : null;
    },
    getSetCookie() {
      return [...(copied.get("set-cookie") || [])];
    },
  });
}

async function directGet(url, { headers = {} } = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const request = httpGet(url, { headers }, (response) => {
      // IncomingMessage and its socket are lifecycle-owned by Node. Copy every
      // value before attaching body listeners so macOS teardown/reuse cannot
      // alter the assertions that run after end.
      const status = response.statusCode ?? 0;
      const responseHeaders = snapshotResponseHeaders(response);
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("error", rejectPromise);
      response.once("end", () => {
        const payload = Buffer.concat(chunks);
        resolvePromise({
          status,
          headers: responseHeaders,
          async json() { return JSON.parse(payload.toString("utf8")); },
          async text() { return payload.toString("utf8"); },
        });
      });
    });
    request.once("error", rejectPromise);
  });
}

test("direct GA client keeps a normalized header snapshot after response teardown", async (context) => {
  const cookies = [
    "ecorex_ga_session=1; Path=/; SameSite=Strict",
    "ecorex_ga_expiry=1; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/; HttpOnly",
  ];
  const server = createHttpServer((_request, response) => {
    response.writeHead(200, {
      "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
      "Set-Cookie": cookies,
    });
    response.end("headers-ok");
  });
  await new Promise((resolvePromise, rejectPromise) => {
    server.once("error", rejectPromise);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  context.after(() => new Promise((resolvePromise, rejectPromise) => {
    server.close((error) => error ? rejectPromise(error) : resolvePromise());
  }));

  const address = server.address();
  assert.ok(address && typeof address === "object");
  const response = await directGet(`http://127.0.0.1:${address.port}/`);
  assert.equal(await response.text(), "headers-ok");
  assert.equal(
    response.headers.get("CONTENT-SECURITY-POLICY"),
    "default-src 'none'; frame-ancestors 'none'",
  );
  assert.deepEqual(response.headers.getSetCookie(), cookies);
  const mutableCopy = response.headers.getSetCookie();
  mutableCopy.pop();
  assert.deepEqual(response.headers.getSetCookie(), cookies);
});

async function readUntil(response, marker) {
  assert.ok(response.body);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  const deadline = Date.now() + 2_000;
  while (!text.includes(marker) && Date.now() < deadline) {
    const { done, value } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
  }
  await reader.cancel();
  return text;
}

test("GA harness exposes managed bootstrap, strict CSRF, state reset, and unique shares", async (context) => {
  const harness = await createGaMockServer({ scenario: "unauthenticated" });
  context.after(() => harness.close());

  const page = await directGet(harness.url);
  const html = await page.text();
  assert.equal(page.headers.get("cache-control"), "no-store");
  assert.match(page.headers.get("content-security-policy") || "", /script-src 'self'/);
  assert.match(html, /assets\/index\.[0-9a-f]{16}\.js/);
  assert.doesNotMatch(html, /unhashed/);

  const unauthenticated = await fetch(`${harness.url}/api/v1/bootstrap`).then((response) => response.json());
  assert.equal(unauthenticated.login.authenticated, false);
  assert.equal(unauthenticated.policy_lease, null);
  assert.deepEqual(unauthenticated.quota.limits, {});
  assert.ok(unauthenticated.csrf_token.length >= 32);

  const deviceStarted = await fetch(`${harness.url}/api/v1/session/device`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ client_request_id: "device-login-request-1" }),
  }).then((response) => response.json());
  assert.equal(deviceStarted.status, "pending");
  assert.match(deviceStarted.verification_url, /^https:\/\//);
  assert.equal("device_code" in deviceStarted, false);
  assert.equal("access_token" in deviceStarted, false);
  const devicePolled = await fetch(`${harness.url}/api/v1/session/device/${deviceStarted.flow_id}/poll`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ client_request_id: "device-poll-request-1" }),
  }).then((response) => response.json());
  assert.equal(devicePolled.status, "authorized");
  assert.equal(devicePolled.restart_required, true);
  assert.equal(devicePolled.restart_scheduled, true);

  const reset = await fetch(`${harness.url}/__ga/reset?scenario=artifact`, { method: "POST" });
  assert.equal(reset.status, 200);
  const authenticated = await fetch(`${harness.url}/api/v1/bootstrap`).then((response) => response.json());
  assert.equal(authenticated.update.current_version, PRODUCT_VERSION);
  assert.equal(authenticated.login.organization_id, "org-ga");
  assert.deepEqual(authenticated.login.roles, ["member"]);
  assert.equal(authenticated.models.chat[0].model_id, "ecorex-chat");
  assert.equal(authenticated.models.chat[0].model_policy.upstream_model_id, "gpt-5.6-luna");
  assert.equal(authenticated.models.chat[0].model_policy.reasoning_effort, "max");
  assert.equal(
    authenticated.models.chat[0].model_policy.context_management.compact_threshold_tokens,
    272_000,
  );
  assert.equal(authenticated.models.image[0].model_id, "gpt-image-2");
  assert.equal(authenticated.models.image[0].model_policy, null);
  assert.equal(authenticated.extensions.contract_version, "1.0");
  assert.equal(authenticated.extensions.items.length, 3);
  const mentions = await fetch(`${harness.url}/api/v1/capability-mentions`).then((response) => response.json());
  assert.deepEqual(mentions.items.map((item) => item.reference), [
    "skill:office-documents",
    "collaboration:feishu",
  ]);
  const connectors = await fetch(`${harness.url}/api/v1/connectors`).then((response) => response.json());
  assert.deepEqual(connectors.items.slice(0, 4).map((item) => item.definition.connector_id), [
    "feishu",
    "tencent-docs",
    "dingtalk",
    "wecom_bot",
  ]);
  assert.equal(connectors.items.find((item) => item.definition.connector_id === "telegram").definition.tier, "beta");

  const hub = await fetch(`${harness.url}/api/v1/skill-hub/skills?category=office_productivity`)
    .then((response) => response.json());
  assert.equal(hub.schema_version, 1);
  assert.deepEqual(hub.items.map((item) => item.slug), ["office-documents"]);
  assert.equal(hub.items[0].provenance.brand, "e-Mate");
  assert.equal(hub.items[0].installation_status, "installed_enabled");
  const plannerDetail = await fetch(`${harness.url}/api/v1/skill-hub/skills/content-planner`)
    .then((response) => response.json());
  assert.equal(plannerDetail.skill.slug, "content-planner");
  const installedPlanner = await fetch(
    `${harness.url}/api/v1/skill-hub/skills/content-planner/install`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        version: plannerDetail.skill.version,
        package_sha256: plannerDetail.skill.package_sha256,
        client_request_id: "skill-install-ga",
      }),
    },
  ).then((response) => response.json());
  assert.equal(installedPlanner.extension.extension_id, "hub.content-planner");
  assert.equal(installedPlanner.extension.status, "enabled");
  const refreshedHub = await fetch(`${harness.url}/api/v1/skill-hub/skills`)
    .then((response) => response.json());
  assert.equal(
    refreshedHub.items.find((item) => item.slug === "content-planner").installation_status,
    "installed_enabled",
  );

  const usage = await fetch(`${harness.url}/api/v1/threads/thread-ga/usage`).then((response) => response.json());
  assert.deepEqual(usage.today, { input_tokens: 4280, output_tokens: 960, total_tokens: 5240 });
  assert.deepEqual(usage.context, {
    used_tokens: 42180,
    window_tokens: 272000,
    model_id: "ecorex-chat",
    model_display_name: "GPT-5.6 Luna · 最大推理",
    model_catalog_snapshot_id: "models-ga",
    measured_at: usage.context.measured_at,
  });

  const health = await fetch(`${harness.url}/api/v1/system/health`).then((response) => response.json());
  const technicalHealth = await fetch(`${harness.url}/api/v1/system/health?technical=true`).then((response) => response.json());
  assert.equal(health.summary, "e-Mate 运行正常");
  assert.equal("metrics" in health, false);
  assert.equal(technicalHealth.metrics.services.extensions.total, 4);

  const memoryBefore = await fetch(`${harness.url}/api/v1/memory`).then((response) => response.json());
  assert.equal(memoryBefore.resettable_count, 2);
  const resetMemory = await fetch(`${harness.url}/api/v1/memory/reset`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ confirmed: true, client_request_id: "memory-reset-ga" }),
  }).then((response) => response.json());
  assert.equal(resetMemory.memory.resettable_count, 0);
  assert.equal(resetMemory.reset.can_undo, true);

  const outputBefore = await fetch(`${harness.url}/api/v1/output/preference`).then((response) => response.json());
  assert.equal(outputBefore.location_alias, "documents");
  const outputAfter = await fetch(`${harness.url}/api/v1/output/preference`, {
    method: "PUT",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      location_alias: "downloads",
      expected_revision: outputBefore.revision,
      client_request_id: "output-location-ga",
    }),
  }).then((response) => response.json());
  assert.equal(outputAfter.location_alias, "downloads");
  const materialized = await fetch(`${harness.url}/api/v1/output/artifacts/artifact-ga-source/materialize`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ revision_id: "revision-ga-source", client_request_id: "materialize-ga" }),
  }).then((response) => response.json());
  assert.equal(materialized.status, "completed");
  assert.equal(materialized.location_alias, "downloads");
  assert.equal("path" in materialized, false);

  const extensions = await fetch(`${harness.url}/api/v1/extensions`).then((response) => response.json());
  const feishu = extensions.items.find((item) => item.extension_id === "ecorex.feishu-mcp");
  assert.equal(feishu.health, "degraded");
  assert.equal(feishu.category, "collaboration");
  assert.equal(feishu.icon_key, "feishu");
  assert.equal("rollback_version" in feishu, false);
  assert.equal("command" in feishu, false);
  assert.equal("path" in feishu, false);
  const checkHealth = () => fetch(
    `${harness.url}/api/v1/extensions/ecorex.feishu-mcp/health`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        expected_revision: feishu.revision,
        client_request_id: "extension-health-ga",
      }),
    },
  );
  const checked = await checkHealth().then((response) => response.json());
  const duplicateCheck = await checkHealth().then((response) => response.json());
  assert.equal(checked.extension.health, "healthy");
  assert.equal(checked.extension.revision, feishu.revision + 1);
  assert.deepEqual(duplicateCheck, checked);
  const staleExtension = await fetch(
    `${harness.url}/api/v1/extensions/ecorex.feishu-mcp/disable`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        expected_revision: feishu.revision,
        client_request_id: "extension-disable-stale-ga",
      }),
    },
  );
  assert.equal(staleExtension.status, 409);
  const staleExtensionError = await staleExtension.json();
  assert.equal(staleExtensionError.detail.code, "extension_revision_conflict");
  assert.equal(staleExtensionError.detail.current_revision, feishu.revision + 1);

  const denied = await fetch(`${harness.url}/api/v1/threads/thread-ga/shares`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_request_id: "share-denied" }),
  });
  assert.equal(denied.status, 403);

  const createShare = async (clientRequestId) => fetch(`${harness.url}/api/v1/threads/thread-ga/shares`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ client_request_id: clientRequestId }),
  }).then((response) => response.json());
  const first = await createShare("share-request-1");
  const second = await createShare("share-request-2");
  assert.notEqual(first.share_id, second.share_id);
  assert.notEqual(first.public_url, second.public_url);
});

test("GA frame reload preserves one browser session while a fresh session resets state", async (context) => {
  const harness = await createGaMockServer({ scenario: "artifact" });
  context.after(() => harness.close());
  const frameUrl = `${harness.url}/__ga/frame-app?scenario=artifact&theme=light`;
  const firstFrame = await directGet(frameUrl);
  const responseCookies = firstFrame.headers.getSetCookie();
  assert.equal(responseCookies.length, 1);
  const sessionCookie = responseCookies[0]?.split(";", 1)[0] || "";
  assert.equal(sessionCookie, "ecorex_ga_session=1");

  const pinned = await fetch(`${harness.url}/api/v1/threads/thread-ga/pin`, {
    method: "PUT",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ pinned: true, client_request_id: "pin-ga-reload" }),
  });
  assert.equal(pinned.status, 200);

  const reloaded = await fetch(frameUrl, { headers: { Cookie: sessionCookie } });
  assert.equal(reloaded.status, 200);
  const retained = await fetch(`${harness.url}/api/v1/threads`).then((response) => response.json());
  assert.equal(retained.items[0].pinned, true);

  const freshSession = await fetch(frameUrl);
  assert.equal(freshSession.status, 200);
  const reset = await fetch(`${harness.url}/api/v1/threads`).then((response) => response.json());
  assert.equal(reset.items[0].pinned, false);
});

test("GA task catalog supports archive, restore, and logical delete", async (context) => {
  const harness = await createGaMockServer({ scenario: "artifact" });
  context.after(() => harness.close());
  const mutate = (suffix, method = "POST") => fetch(`${harness.url}/api/v1/threads/thread-ga${suffix}`, {
    method,
    headers: MUTATION_HEADERS,
    body: JSON.stringify({ client_request_id: `ga-${method}-${suffix}` }),
  });

  assert.equal((await mutate("/archive")).status, 200);
  assert.equal((await fetch(`${harness.url}/api/v1/threads?status=archived`).then((response) => response.json())).items.length, 1);
  assert.equal((await mutate("/restore").then((response) => response.json())).status, "active");
  assert.equal((await mutate("/archive")).status, 200);
  assert.equal((await mutate("", "DELETE").then((response) => response.json())).status, "deleted");
  assert.equal((await fetch(`${harness.url}/api/v1/threads?status=all`).then((response) => response.json())).items.length, 0);
  assert.equal((await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`)).status, 404);
});

test("GA-created turns use live acceptance and terminal timestamps", async (context) => {
  const harness = await createGaMockServer({ scenario: "artifact" });
  context.after(() => harness.close());

  const acceptedAfter = Date.now();
  const created = await fetch(`${harness.url}/api/v1/threads/thread-ga/turns`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      input: "整理当前周报",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "duration-live-clock-ga",
      metadata: {},
    }),
  }).then((response) => response.json());
  assert.ok(Date.parse(created.turn.created_at) >= acceptedAfter);

  await new Promise((resolve) => setTimeout(resolve, 450));
  const projection = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/projection`,
  ).then((response) => response.json());
  const completed = projection.turns.find((turn) => turn.turn_id === created.turn.turn_id);
  assert.equal(completed.status, "completed");
  const elapsed = Date.parse(completed.updated_at) - Date.parse(completed.created_at);
  assert.ok(elapsed >= 0 && elapsed < 5_000, `unexpected fixture duration: ${elapsed}`);
});

test("GA task-switch scenario serves independent projections and preserves missing-task 404", async (context) => {
  const harness = await createGaMockServer({ scenario: "thread-switch" });
  context.after(() => harness.close());

  const catalog = await fetch(`${harness.url}/api/v1/threads`).then((response) => response.json());
  assert.deepEqual(
    catalog.items.map((item) => item.thread_id),
    ["thr_current_ga", "thr_target_ga", "thr_slow_ga", "thr_fast_ga"],
  );
  const current = await fetch(`${harness.url}/api/v1/threads/thr_current_ga/projection`).then((response) => response.json());
  const target = await fetch(`${harness.url}/api/v1/threads/thr_target_ga/projection`).then((response) => response.json());
  assert.equal(current.thread.title, "当前季度任务");
  assert.equal(current.items[0].content.text, "当前任务原始内容");
  assert.equal(target.thread.title, "已恢复的年度任务");
  assert.equal(target.items[0].content.text, "年度任务已从恢复点载入");
  assert.equal(target.turns[0].thread_id, "thr_target_ga");
  assert.equal(target.items[0].thread_id, "thr_target_ga");

  const missing = await fetch(`${harness.url}/api/v1/threads/thr_missing_ga/projection`);
  assert.equal(missing.status, 404);
  assert.equal((await missing.json()).detail.code, "thread_not_found");
});

test("responsive GA matrix uses exact same-origin frame viewports without weakening the production CSP", async (context) => {
  const harness = await createGaMockServer({ scenario: "empty" });
  context.after(() => harness.close());

  const matrixResponse = await directGet(`${harness.url}/__ga/viewport-matrix`);
  const matrix = await matrixResponse.json();
  assert.equal(matrixResponse.status, 200);
  assert.equal(matrixResponse.headers.get("cache-control"), "no-store");
  assert.match(matrixResponse.headers.get("content-security-policy") || "", /default-src 'none'/);
  assert.equal(matrix.contract_version, "1.0");
  assert.equal(matrix.entries.length, 10);
  assert.deepEqual(
    [...new Set(matrix.entries.map((entry) => `${entry.width}x${entry.height}`))],
    ["1440x900", "1024x768", "768x900", "390x844", "320x568"],
  );
  assert.deepEqual([...new Set(matrix.entries.map((entry) => entry.theme))], ["light", "dark"]);
  assert.equal(new Set(matrix.entries.map((entry) => entry.matrix_id)).size, matrix.entries.length);
  for (const entry of matrix.entries) {
    assert.match(entry.url, /^\/__ga\/viewport\?viewport=\d+x\d+&theme=(?:light|dark)&scenario=artifact$/);
  }

  const wrapperResponse = await directGet(
    `${harness.url}/__ga/viewport?viewport=390x844&theme=dark&scenario=artifact`,
  );
  const wrapper = await wrapperResponse.text();
  assert.equal(wrapperResponse.status, 200);
  assert.equal(wrapperResponse.headers.get("cache-control"), "no-store");
  assert.equal(wrapperResponse.headers.get("x-frame-options"), "DENY");
  assert.match(wrapperResponse.headers.get("content-security-policy") || "", /frame-src 'self'/);
  assert.match(wrapperResponse.headers.get("content-security-policy") || "", /frame-ancestors 'none'/);
  assert.match(wrapper, /width="390"/);
  assert.match(wrapper, /height="844"/);
  assert.match(wrapper, /data-theme="dark"/);
  assert.match(wrapper, /src="\/__ga\/frame-app\?scenario=artifact&amp;theme=dark"/);
  assert.match(wrapper, /src="\/__ga\/viewport\.js"/);
  assert.match(wrapper, /href="\/__ga\/viewport\.css"/);
  assert.doesNotMatch(wrapper, /<style(?:\s|>)/i);
  assert.doesNotMatch(wrapper, /<script(?![^>]+\bsrc=)/i);
  assert.doesNotMatch(wrapper, /\son\w+=/i);

  const productionResponse = await directGet(harness.url);
  const productionCsp = productionResponse.headers.get("content-security-policy") || "";
  assert.match(productionCsp, /frame-ancestors 'none'/);
  assert.equal(productionResponse.headers.get("x-frame-options"), "DENY");

  const frameResponse = await directGet(
    `${harness.url}/__ga/frame-app?scenario=artifact&theme=dark`,
  );
  const frame = await frameResponse.text();
  const frameCsp = frameResponse.headers.get("content-security-policy") || "";
  assert.equal(frameResponse.status, 200);
  assert.equal(frameResponse.headers.get("cache-control"), "no-store");
  assert.equal(frameResponse.headers.get("x-frame-options"), "SAMEORIGIN");
  assert.match(frameCsp, /frame-ancestors 'self'/);
  assert.doesNotMatch(frameCsp, /frame-ancestors 'none'/);
  assert.match(frameCsp, /frame-src 'none'/);
  assert.match(frame, /src="\/__ga\/axe\.js"/);
  assert.match(frame, /src="\/__ga\/frame-bootstrap\.js\?theme=dark"/);
  assert.match(frame, /src="\/assets\/index\.[0-9a-f]{16}\.js"/);
  assert.doesNotMatch(frame, /(?:src|href)="\.\/assets\//);
  assert.ok(
    frame.indexOf("/__ga/frame-bootstrap.js?theme=dark") < frame.indexOf("/assets/index."),
    "theme bootstrap must execute before the production module",
  );
  const state = await fetch(`${harness.url}/__ga/state`).then((response) => response.json());
  assert.equal(state.scenario, "artifact");

  const bootstrapResponse = await directGet(`${harness.url}/__ga/frame-bootstrap.js?theme=dark`);
  const bootstrap = await bootstrapResponse.text();
  assert.equal(bootstrapResponse.headers.get("cache-control"), "no-store");
  assert.match(bootstrap, /const theme = "dark"/);
  assert.match(bootstrap, /document\.documentElement\.dataset\.theme = theme/);

  const viewportScript = await fetch(`${harness.url}/__ga/viewport.js`).then((response) => response.text());
  assert.match(viewportScript, /contentWindow\.innerWidth|view\.innerWidth/);
  assert.match(viewportScript, /horizontal_overflow/);
  assert.match(viewportScript, /wrapped_clickable_labels/);
  assert.match(viewportScript, /key_controls/);
  assert.match(viewportScript, /__ECOREX_GA_VIEWPORT_REPORT__/);
  assert.match(viewportScript, /view\.axe\.run/);
  assert.match(viewportScript, /accessibility\.violations\.length === 0/);

  const axeResponse = await directGet(`${harness.url}/__ga/axe.js`);
  const axeSource = await axeResponse.text();
  assert.equal(axeResponse.status, 200);
  assert.equal(axeResponse.headers.get("cache-control"), "no-store");
  assert.match(axeResponse.headers.get("content-type") || "", /^text\/javascript/);
  assert.match(axeSource, /axe\.version/);
});

test("responsive GA routes reject unknown, duplicate, recursive, and injected parameters", async (context) => {
  const harness = await createGaMockServer({ scenario: "empty" });
  context.after(() => harness.close());

  const cases = [
    ["/__ga/viewport?viewport=999x999&theme=light&scenario=artifact", "ga_unknown_viewport"],
    ["/__ga/viewport?viewport=390x844&theme=system&scenario=artifact", "ga_unknown_theme"],
    ["/__ga/viewport?viewport=390x844&theme=light&scenario=unknown", "ga_unknown_scenario"],
    ["/__ga/viewport?viewport=390x844&theme=light&scenario=artifact&scenario=empty", "ga_duplicate_parameter"],
    ["/__ga/frame-app?scenario=artifact&theme=light&src=%2F__ga%2Fviewport", "ga_unknown_parameter"],
    ["/__ga/frame-bootstrap.js?theme=%3Cscript%3E", "ga_unknown_theme"],
  ];
  for (const [route, code] of cases) {
    const response = await fetch(`${harness.url}${route}`);
    const payload = await response.json();
    assert.equal(response.status, 422, route);
    assert.equal(payload.detail.code, code, route);
  }

  const recursive = await fetch(`${harness.url}/__ga/viewport/recursive`);
  const recursivePayload = await recursive.json();
  assert.equal(recursive.status, 404);
  assert.equal(recursivePayload.detail.code, "ga_harness_route_not_found");

  const mutation = await fetch(`${harness.url}/__ga/viewport-matrix`, { method: "POST" });
  const mutationPayload = await mutation.json();
  assert.equal(mutation.status, 405);
  assert.equal(mutation.headers.get("allow"), "GET");
  assert.equal(mutationPayload.detail.code, "ga_method_not_allowed");
  assert.deepEqual(mutationPayload.detail.allowed, ["GET"]);
});

test("GA harness models thinking terminal, HITL, retry, and retouch result scenarios", async (context) => {
  const harness = await createGaMockServer({ scenario: "thinking" });
  context.after(() => harness.close());

  const thinking = await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`).then((response) => response.json());
  assert.equal(thinking.turns[0].status, "model_requested");
  const stream = await fetch(`${harness.url}/api/v1/threads/thread-ga/events/stream?after_seq=2&follow=true`);
  const terminal = await readUntil(stream, "turn.status_changed");
  assert.match(terminal, /\"to\":\"completed\"/);
  assert.match(terminal, /\"extension_snapshot_id\":\"extensions-ga-/);

  await fetch(`${harness.url}/__ga/reset?scenario=thinking`, { method: "POST" });
  const replacedThinkingStream = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/events/stream?after_seq=2&follow=true`,
  );
  const replacedBeforeTimer = await fetch(`${harness.url}/api/v1/turns/turn-ga/replace`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      input: "改为先输出风险清单",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "message-replace-before-thinking-timer",
      metadata: {},
      reason: "replaced_by_user",
    }),
  }).then((response) => response.json());
  assert.equal(replacedBeforeTimer.superseded_turn.status, "superseded");
  await new Promise((resolve) => setTimeout(resolve, 1_300));
  const afterThinkingTimer = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/projection`,
  ).then((response) => response.json());
  assert.equal(afterThinkingTimer.turns[0].status, "superseded");
  assert.equal(afterThinkingTimer.turns[1].status, "queued");
  assert.equal(afterThinkingTimer.turns[1].metadata.replaces_turn_id, "turn-ga");
  await replacedThinkingStream.body?.cancel();

  await fetch(`${harness.url}/__ga/reset?scenario=hitl`, { method: "POST" });
  const hitl = await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`).then((response) => response.json());
  assert.equal(hitl.interactions[0].status, "pending");
  const resolved = await fetch(`${harness.url}/api/v1/interactions/interaction-ga/respond`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      response: { action_id: "approve_once", values: {} },
      client_request_id: "interaction-request-1",
    }),
  }).then((response) => response.json());
  assert.equal(resolved.interaction.status, "resolved");
  assert.equal(resolved.interaction.response_client_request_id, "interaction-request-1");

  await fetch(`${harness.url}/__ga/reset?scenario=retry`, { method: "POST" });
  const retry = await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`).then((response) => response.json());
  assert.equal(retry.turns[0].status, "retry_wait");
  const steered = await fetch(`${harness.url}/api/v1/turns/turn-ga/steer`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      input: "补充华东区域同比数据",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "message-steer-ga",
      metadata: {},
    }),
  }).then((response) => response.json());
  assert.equal(steered.turn.status, "retry_wait");
  const queued = await fetch(`${harness.url}/api/v1/threads/thread-ga/queue`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      input: "下一轮处理附件",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "message-queue-ga",
      metadata: {},
    }),
  }).then((response) => response.json());
  assert.equal(queued.turn.status, "queued");
  const replaced = await fetch(`${harness.url}/api/v1/turns/turn-ga/replace`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      input: "改为生成完整季度复盘",
      agent_model_id: "ecorex-chat",
      image_model_id: "gpt-image-2",
      client_message_id: "message-replace-ga",
      metadata: {},
      reason: "replaced_by_user",
    }),
  }).then((response) => response.json());
  assert.equal(replaced.superseded_turn.status, "superseded");
  assert.equal(replaced.replacement_turn.status, "queued");
  const operationProjection = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/projection`,
  ).then((response) => response.json());
  assert.deepEqual(
    operationProjection.items.slice(-3).map((candidate) => candidate.client_message_id),
    ["message-steer-ga", "message-queue-ga", "message-replace-ga"],
  );
  const operationEvents = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/events?after_seq=0&limit=1000`,
  ).then((response) => response.json());
  assert.equal(operationEvents.has_more, false);
  assert.equal(operationEvents.watermark, operationProjection.watermark);
  assert.deepEqual(
    operationEvents.events
      .map((event) => event.client_message_id)
      .filter(Boolean),
    ["message-steer-ga", "message-queue-ga", "message-queue-ga", "message-replace-ga"],
  );

  await fetch(`${harness.url}/__ga/reset?scenario=artifact`, { method: "POST" });
  const artifacts = await fetch(`${harness.url}/api/v1/artifacts?thread_id=thread-ga`).then((response) => response.json());
  assert.equal(artifacts.items[0].visibility, "primary");
  assert.equal(artifacts.items[0].family, "image");
  assert.equal(artifacts.items[0].display_name.endsWith(".png"), true);
  const opened = await fetch(
    `${harness.url}/api/v1/artifacts/artifact-ga-source/actions/open`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ client_request_id: "artifact-open-1" }),
    },
  ).then((response) => response.json());
  assert.equal(opened.status, "completed");
  assert.equal(opened.action, "open");
  assert.equal("path" in opened, false);
  const forgedPath = await fetch(
    `${harness.url}/api/v1/artifacts/artifact-ga-source/actions/reveal`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ client_request_id: "artifact-reveal-1", path: "/forged" }),
    },
  );
  assert.equal(forgedPath.status, 422);
  const retouch = await fetch(`${harness.url}/api/v1/artifacts/artifact-ga-source/retouch`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      base_revision_id: "revision-ga-source",
      selected_artifact_ids: ["artifact-ga-source"],
      annotations: [],
      reference_artifact_ids: [],
      global_instruction: "移除干扰物",
      client_request_id: "retouch-request-1",
    }),
  });
  assert.equal(retouch.status, 202);
  await new Promise((resolve) => setTimeout(resolve, 500));
  const retouchedArtifacts = await fetch(`${harness.url}/api/v1/artifacts?thread_id=thread-ga`).then((response) => response.json());
  assert.equal(retouchedArtifacts.count, 2);
  const retouchedProjection = await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`).then((response) => response.json());
  assert.equal(retouchedProjection.turns.at(-1).status, "completed");
  assert.match(retouchedProjection.items.at(-1).content.change_summary, /移除干扰物/);
  assert.doesNotMatch(retouchedProjection.items.at(-1).content.change_summary, /主体轮廓/);
  assert.equal(retouchedProjection.items.at(-1).content.inspection_regions.length, 0);

  const workspace = await fetch(
    `${harness.url}/api/v1/artifacts/artifact-ga-source/retouch-workspaces`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        base_revision_id: "revision-ga-source",
        client_request_id: "workspace-open-1",
      }),
    },
  ).then((response) => response.json());
  assert.equal(workspace.edit_surface.coordinate_space_version, "oriented-normalized-v1");
  const savedWorkspace = await fetch(
    `${harness.url}/api/v1/retouch-workspaces/${workspace.workspace_id}`,
    {
      method: "PATCH",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        expected_version: workspace.version,
        annotations: [{
          annotation_id: "ann-ga",
          kind: "rectangle",
          normalized_geometry: { x: 0.1, y: 0.2, width: 0.3, height: 0.2 },
          instruction: "移除标记物",
        }],
        reference_artifact_ids: [],
        global_instruction: "保持其他区域稳定",
        view_state: { zoom: 2, pan_x: 0.5, pan_y: 0.5, tool: "select" },
        client_request_id: "workspace-save-1",
      }),
    },
  ).then((response) => response.json());
  assert.equal(savedWorkspace.mask.coordinate_space_version, "oriented-normalized-v1");
  const beforeWorkspaceSubmit = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/projection`,
  ).then((response) => response.json());
  const workspaceEvents = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/events/stream?after_seq=${beforeWorkspaceSubmit.watermark}&follow=true`,
  );
  const submittedWorkspace = await fetch(
    `${harness.url}/api/v1/retouch-workspaces/${workspace.workspace_id}/submit`,
    {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        expected_version: savedWorkspace.version,
        client_request_id: "workspace-submit-1",
      }),
    },
  ).then((response) => response.json());
  assert.equal(submittedWorkspace.status, "submitted");
  const workspaceCompletionEvents = await readUntil(
    workspaceEvents,
    "artifact.retouch.completed",
  );
  assert.match(workspaceCompletionEvents, /event: item\.created/);
  assert.match(workspaceCompletionEvents, /event: artifact\.retouch\.completed/);
  assert.match(workspaceCompletionEvents, /"turn_id":"turn-retouch-workspace-job-/);
  assert.match(workspaceCompletionEvents, /"retouch_job_id":"retouch-workspace-job-/);
  const completedWorkspace = await fetch(
    `${harness.url}/api/v1/retouch-workspaces/${workspace.workspace_id}`,
  ).then((response) => response.json());
  assert.equal(completedWorkspace.job.status, "completed");
  assert.equal(completedWorkspace.result.artifact_id, "artifact-ga-source");
  assert.equal(completedWorkspace.job.inspection_regions[0].summary, "移除标记物");
  const workspaceProjection = await fetch(
    `${harness.url}/api/v1/threads/thread-ga/projection`,
  ).then((response) => response.json());
  assert.equal(workspaceProjection.turns.at(-1).status, "completed");
  assert.equal(workspaceProjection.items.at(-1).turn_id, workspaceProjection.turns.at(-1).turn_id);
  assert.match(workspaceProjection.items.at(-1).content.change_summary, /移除标记物/);
  assert.doesNotMatch(workspaceProjection.items.at(-1).content.change_summary, /主体轮廓/);
});

test("GA viewport matrix includes the 320px Hallmark floor in both themes", async (context) => {
  const harness = await createGaMockServer({ scenario: "artifact" });
  context.after(() => harness.close());

  const matrix = await fetch(`${harness.url}/__ga/viewport-matrix`).then((response) => response.json());
  const narrow = matrix.entries.filter((entry) => entry.viewport_id === "320x568");
  assert.deepEqual(
    narrow.map((entry) => [entry.width, entry.height, entry.theme]).sort(),
    [[320, 568, "dark"], [320, 568, "light"]],
  );
});

test("GA Replay scenario keeps Mock read-only and Live idempotent with explicit confirmation", async (context) => {
  const harness = await createGaMockServer({ scenario: "replay" });
  context.after(() => harness.close());

  const before = await fetch(`${harness.url}/__ga/state`).then((response) => response.json());
  const mock = await fetch(`${harness.url}/api/v1/threads/thread-ga/replay`).then((response) => response.json());
  const afterMock = await fetch(`${harness.url}/__ga/state`).then((response) => response.json());
  assert.equal(afterMock.seq, before.seq);
  assert.deepEqual(mock.live_replay_turn_ids, ["turn-ga"]);
  assert.equal(mock.event_count, 2);
  assert.match(mock.event_digest, /^[0-9a-f]{64}$/);

  const rejected = await fetch(`${harness.url}/api/v1/threads/thread-ga/replay/live`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      source_turn_id: "turn-ga",
      confirmed: false,
      client_request_id: "live-replay-ga",
    }),
  });
  assert.equal(rejected.status, 422);

  const submit = () => fetch(`${harness.url}/api/v1/threads/thread-ga/replay/live`, {
    method: "POST",
    headers: MUTATION_HEADERS,
    body: JSON.stringify({
      source_turn_id: "turn-ga",
      confirmed: true,
      client_request_id: "live-replay-ga",
    }),
  }).then((response) => response.json());
  const first = await submit();
  const duplicate = await submit();
  assert.equal(first.replay.turn.turn_id, duplicate.replay.turn.turn_id);
  assert.equal(first.replay.turn.metadata._replay.reuse_external_side_effects, false);

  const projection = await fetch(`${harness.url}/api/v1/threads/thread-ga/projection`).then((response) => response.json());
  assert.equal(projection.turns.length, 2);
  assert.equal(projection.turns.at(-1).turn_id, first.replay.turn.turn_id);
});

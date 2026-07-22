#!/usr/bin/env node

// This runner never starts a fixture server. Its only accepted target is an
// already-running loopback e-Mate Runtime whose signed identity is supplied by
// the Python candidate supervisor. The browser profile and CDP endpoint exist
// only for this bounded acceptance window.

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, sep } from "node:path";
import { chromium } from "@playwright/test";

const MAX_HTML_BYTES = 2 * 1024 * 1024;
const MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024;
const MAX_TIMEOUT_MILLISECONDS = 180_000;
const SAFE_RELEASE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u;
const SAFE_VERSION = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$/u;

function argument(name) {
  const prefix = `${name}=`;
  const value = process.argv.slice(2).find((item) => item.startsWith(prefix));
  return value ? value.slice(prefix.length) : null;
}

function requiredArgument(name) {
  const value = argument(name);
  if (!value) throw new Error(`${name.slice(2).replaceAll("-", "_")}_missing`);
  return value;
}

function runtimeOrigin() {
  const raw = requiredArgument("--base-url");
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("runtime_origin_invalid");
  }
  if (
    parsed.protocol !== "http:"
    || parsed.hostname !== "127.0.0.1"
    || !parsed.port
    || parsed.username
    || parsed.password
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
  ) throw new Error("runtime_origin_not_loopback");
  const port = Number(parsed.port);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new Error("runtime_port_invalid");
  }
  return parsed.origin;
}

function expectedIdentity() {
  const releaseId = requiredArgument("--expected-release-id");
  const version = requiredArgument("--expected-version");
  if (!SAFE_RELEASE_ID.test(releaseId)) throw new Error("release_id_invalid");
  if (!SAFE_VERSION.test(version)) throw new Error("version_invalid");
  return { releaseId, version };
}

function timeoutMilliseconds() {
  const raw = argument("--timeout-ms") || "120000";
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 10_000 || value > MAX_TIMEOUT_MILLISECONDS) {
    throw new Error("timeout_invalid");
  }
  return value;
}

function chromeExecutable() {
  if (process.platform !== "win32") throw new Error("windows_chrome_required");
  const candidates = [
    process.env.PROGRAMFILES,
    process.env["PROGRAMFILES(X86)"],
    process.env.LOCALAPPDATA,
  ].filter(Boolean).map((root) => join(root, "Google", "Chrome", "Application", "chrome.exe"));
  const selected = candidates.find((candidate) => existsSync(candidate));
  if (!selected || selected.toLowerCase().split(/[\\/]/u).at(-1) !== "chrome.exe") {
    throw new Error("chrome_unavailable");
  }
  return resolve(selected);
}

async function boundedBody(response, limit, code) {
  const declared = Number(response.headers.get("content-length") || "0");
  if (Number.isFinite(declared) && declared > limit) throw new Error(code);
  if (!response.body) return Buffer.alloc(0);
  const chunks = [];
  let length = 0;
  for await (const chunk of response.body) {
    length += chunk.byteLength;
    if (length > limit) {
      try { await response.body.cancel(); } catch { /* The limit remains authoritative. */ }
      throw new Error(code);
    }
    chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks, length);
}

async function waitForDevToolsEndpoint(profileRoot, timeout) {
  const expiresAt = Date.now() + timeout;
  while (Date.now() < expiresAt) {
    try {
      const payload = await readFile(join(profileRoot, "DevToolsActivePort"));
      if (payload.byteLength > 4096) throw new Error("cdp_endpoint_invalid");
      const [rawPort, browserPath] = payload.toString("utf8").trim().split(/\r?\n/u);
      const port = Number(rawPort);
      if (
        Number.isSafeInteger(port)
        && port >= 1
        && port <= 65_535
        && /^\/devtools\/browser\/[0-9a-f-]{8,128}$/iu.test(browserPath || "")
      ) return { port, browserPath };
    } catch {
      // The bounded caller owns the startup race.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error("cdp_startup_timeout");
}

async function boundedJson(url, timeout) {
  const response = await fetch(url, {
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) throw new Error("cdp_endpoint_invalid");
  const body = await boundedBody(response, 64 * 1024, "cdp_endpoint_invalid");
  try { return JSON.parse(body.toString("utf8")); } catch { throw new Error("cdp_endpoint_invalid"); }
}

function parseRuntimeConfiguration(html) {
  const match = html.match(
    /window\.__ECOREX_RUNTIME__=Object\.freeze\((\{[\s\S]*?\})\);Object\.defineProperty/u,
  );
  if (!match) throw new Error("runtime_bridge_missing");
  let value;
  try {
    value = JSON.parse(match[1]);
  } catch {
    throw new Error("runtime_bridge_invalid");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("runtime_bridge_invalid");
  }
  if (
    value.apiBase !== "/api/v1"
    || typeof value.bearerToken !== "string"
    || value.bearerToken.length < 32
    || value.bearerToken.length > 512
    || typeof value.releaseId !== "string"
    || typeof value.version !== "string"
  ) throw new Error("runtime_bridge_contract_invalid");
  return value;
}

function terminate(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") {
    const taskkill = join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
    spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
  } else {
    try { process.kill(-child.pid, "SIGTERM"); } catch { child.kill("SIGTERM"); }
  }
}

async function run() {
  const startedAt = performance.now();
  const origin = runtimeOrigin();
  const expected = expectedIdentity();
  const timeout = timeoutMilliseconds();
  const rootResponse = await fetch(`${origin}/`, {
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(Math.min(timeout, 10_000)),
  });
  if (!rootResponse.ok) throw new Error("runtime_root_unavailable");
  if (!(rootResponse.headers.get("cache-control") || "").toLowerCase().includes("no-store")) {
    throw new Error("runtime_root_cache_policy_invalid");
  }
  const rootBody = await boundedBody(rootResponse, MAX_HTML_BYTES, "runtime_root_too_large");
  const bridge = parseRuntimeConfiguration(rootBody.toString("utf8"));
  if (bridge.releaseId !== expected.releaseId || bridge.version !== expected.version) {
    throw new Error("runtime_identity_mismatch");
  }

  const bootstrapResponse = await fetch(`${origin}/api/v1/bootstrap`, {
    cache: "no-store",
    redirect: "error",
    headers: {
      Authorization: `Bearer ${bridge.bearerToken}`,
      "Cache-Control": "no-store",
      Origin: origin,
    },
    signal: AbortSignal.timeout(Math.min(timeout, 10_000)),
  });
  const bootstrapBody = await boundedBody(
    bootstrapResponse,
    MAX_BOOTSTRAP_BYTES,
    "runtime_bootstrap_too_large",
  );
  if (!bootstrapResponse.ok) throw new Error("runtime_bootstrap_unavailable");
  let bootstrap;
  try {
    bootstrap = JSON.parse(bootstrapBody.toString("utf8"));
  } catch {
    throw new Error("runtime_bootstrap_invalid");
  }
  if (
    bootstrap?.api_version !== "v1"
    || bootstrap?.event_schema_version !== 1
    || bootstrap?.storage_schema_version !== 1
    || bootstrap?.update?.current_version !== expected.version
  ) throw new Error("runtime_bootstrap_contract_invalid");

  const profileRoot = await mkdtemp(join(tmpdir(), "ecorex-v1-signed-cdp-"));
  const expectedPrefix = resolve(tmpdir()) + sep;
  if (!resolve(profileRoot).startsWith(expectedPrefix)) {
    await rm(profileRoot, { recursive: true, force: false });
    throw new Error("profile_root_invalid");
  }
  const screenshotPath = join(profileRoot, "runtime.png");
  let chrome = null;
  let browser = null;
  const diagnostics = {
    console_errors: 0,
    page_errors: 0,
    failed_requests: 0,
    external_requests: 0,
  };
  const requestedPaths = new Set();
  let chromeProjection = null;
  try {
    chrome = spawn(chromeExecutable(), [
      "--headless=new",
      "--remote-debugging-port=0",
      "--remote-debugging-address=127.0.0.1",
      `--user-data-dir=${profileRoot}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-sync",
      "--metrics-recording-only",
      "about:blank",
    ], {
      stdio: ["ignore", "ignore", "ignore"],
      windowsHide: true,
      detached: false,
    });
    const cdp = await waitForDevToolsEndpoint(profileRoot, Math.min(timeout, 20_000));
    chromeProjection = await boundedJson(
      `http://127.0.0.1:${cdp.port}/json/version`,
      Math.min(timeout, 5_000),
    );
    if (typeof chromeProjection.webSocketDebuggerUrl !== "string") {
      throw new Error("cdp_endpoint_missing");
    }
    const debuggerUrl = new URL(chromeProjection.webSocketDebuggerUrl);
    if (
      debuggerUrl.protocol !== "ws:"
      || debuggerUrl.hostname !== "127.0.0.1"
      || Number(debuggerUrl.port) !== cdp.port
      || debuggerUrl.pathname !== cdp.browserPath
    ) throw new Error("cdp_endpoint_invalid");
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdp.port}`, {
      timeout: Math.min(timeout, 20_000),
    });
    const context = browser.contexts()[0];
    if (!context) throw new Error("cdp_context_missing");
    await context.route("**/*", async (route) => {
      const raw = route.request().url();
      let parsed;
      try {
        parsed = new URL(raw);
      } catch {
        diagnostics.external_requests += 1;
        await route.abort("blockedbyclient");
        return;
      }
      if (parsed.origin === origin) {
        requestedPaths.add(parsed.pathname);
        await route.continue();
        return;
      }
      if (parsed.protocol === "blob:" || parsed.protocol === "data:") {
        await route.continue();
        return;
      }
      diagnostics.external_requests += 1;
      await route.abort("blockedbyclient");
    });
    const page = context.pages()[0] || await context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.setDefaultTimeout(Math.min(timeout, 20_000));
    page.setDefaultNavigationTimeout(Math.min(timeout, 20_000));
    page.on("console", (message) => {
      if (message.type() === "error") diagnostics.console_errors += 1;
    });
    page.on("pageerror", () => { diagnostics.page_errors += 1; });
    page.on("requestfailed", (request) => {
      let parsed;
      try { parsed = new URL(request.url()); } catch { return; }
      if (parsed.origin === origin) diagnostics.failed_requests += 1;
    });
    await page.goto(origin, { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "新建任务" }).waitFor({ state: "visible" });
    await page.getByText("需要登录 e-Mate 账号", { exact: true }).waitFor({ state: "visible" });
    await page.getByText("e-Mate", { exact: true }).first().waitFor({ state: "visible" });
    const browserBridge = await page.evaluate(() => ({
      apiBase: window.__ECOREX_RUNTIME__?.apiBase,
      releaseId: window.__ECOREX_RUNTIME__?.releaseId,
      version: window.__ECOREX_RUNTIME__?.version,
      frozen: Object.isFrozen(window.__ECOREX_RUNTIME__),
    }));
    if (
      browserBridge.apiBase !== "/api/v1"
      || browserBridge.releaseId !== expected.releaseId
      || browserBridge.version !== expected.version
      || browserBridge.frozen !== true
    ) throw new Error("browser_runtime_identity_mismatch");
    if (await page.locator("[data-ga-status]").count() !== 0) {
      throw new Error("ga_fixture_surface_detected");
    }
    await page.waitForTimeout(250);
    if (Array.from(requestedPaths).some((path) => /^\/__ga(?:\/|$)/u.test(path))) {
      throw new Error("ga_fixture_endpoint_detected");
    }
    if (
      diagnostics.console_errors !== 0
      || diagnostics.page_errors !== 0
      || diagnostics.failed_requests !== 0
      || diagnostics.external_requests !== 0
    ) throw new Error("browser_diagnostics_failed");
    await page.screenshot({ path: screenshotPath, fullPage: true, type: "png" });
    const screenshot = await readFile(screenshotPath);
    return {
      schema_version: 1,
      status: "passed",
      evidence_class: "installed-signed-runtime-cdp",
      acceptance_scope: "unauthenticated-shell-smoke",
      transport: "google-chrome-cdp",
      mock_server_spawned: false,
      ga_endpoint_contacted: false,
      runtime: {
        origin,
        release_id: expected.releaseId,
        version: expected.version,
        api_version: bootstrap.api_version,
        event_schema_version: bootstrap.event_schema_version,
        storage_schema_version: bootstrap.storage_schema_version,
        authenticated: bootstrap.login?.authenticated === true,
        index_cache_control: rootResponse.headers.get("cache-control"),
      },
      browser: {
        product: typeof chromeProjection.Browser === "string" ? chromeProjection.Browser : "Google Chrome",
        protocol_version: typeof chromeProjection["Protocol-Version"] === "string"
          ? chromeProjection["Protocol-Version"]
          : null,
        isolated_profile: true,
        external_network_blocked: true,
      },
      ui: {
        viewport: "1440x900",
        brand_visible: true,
        new_task_visible: true,
        managed_login_boundary_visible: true,
        runtime_bridge_frozen: true,
      },
      full_office_scenario_acceptance_claimed: false,
      promotion_claimed: false,
      diagnostics,
      requested_path_count: requestedPaths.size,
      screenshot_sha256: createHash("sha256").update(screenshot).digest("hex"),
      elapsed_milliseconds: Math.round(performance.now() - startedAt),
    };
  } finally {
    if (browser) {
      try { await browser.close(); } catch { /* Job cleanup remains authoritative. */ }
    }
    terminate(chrome);
    await rm(profileRoot, { recursive: true, force: false });
  }
}

try {
  const result = await run();
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  const code = error instanceof Error && /^[a-z][a-z0-9_]{2,127}$/u.test(error.message)
    ? error.message
    : "installed_signed_runtime_cdp_failed";
  process.stderr.write(`${code}\n`);
  process.exitCode = 1;
}

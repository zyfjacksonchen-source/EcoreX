#!/usr/bin/env node
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(desktopRoot, "..");
const outputPath = path.resolve(process.argv[2] || path.join(repoRoot, "docs", "v0.1.18", "sidecar-lifecycle-smoke.json"));

class FakeChild extends EventEmitter {
  constructor(pid) {
    super();
    this.pid = pid;
    this.stderr = new EventEmitter();
    this.killedWith = [];
  }

  kill(signal = "SIGTERM") {
    this.killedWith.push(signal);
    this.emit("exit", null, signal);
    return true;
  }
}

function response(payload, ok = true, status = ok ? 200 : 503) {
  return {
    ok,
    status,
    json: async () => payload
  };
}

async function waitFor(predicate, label, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

function makeTimerHarness() {
  const intervals = [];
  return {
    intervals,
    setTimeoutFn(fn, ms) {
      if (Number(ms) >= 1000) {
        return { inertTimeout: true };
      }
      return setTimeout(fn, 0);
    },
    clearTimeoutFn(handle) {
      if (handle && !handle.inertTimeout) {
        clearTimeout(handle);
      }
    },
    setIntervalFn(fn) {
      intervals.push(fn);
      return { intervalIndex: intervals.length - 1 };
    },
    clearIntervalFn() {
      // The lifecycle smoke drives health checks directly.
    }
  };
}

const { SidecarManager } = await import(pathToFileURL(path.join(desktopRoot, "dist-electron", "sidecar.js")).href);
const { fetchSidecarJson } = await import(pathToFileURL(path.join(desktopRoot, "dist-electron", "apiBridge.js")).href);

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ecorex-sidecar-lifecycle-"));
const userData = path.join(tmpRoot, "user-data");
fs.mkdirSync(userData, { recursive: true });
fs.writeFileSync(path.join(tmpRoot, "config-template.json"), "{}\n", "utf8");

const children = [];
const taskkillCalls = [];
const statuses = [];
let pid = 4100;
let probeMode = "ready";
let fetchCalls = 0;
const timers = makeTimerHarness();

const previousPython = process.env.ECOREX_PYTHON;
const previousWebPort = process.env.ECOREX_WEB_PORT;
process.env.ECOREX_PYTHON = process.execPath;
process.env.ECOREX_WEB_PORT = "19317";

try {
  const manager = new SidecarManager(tmpRoot, {
    appGetPath: () => userData,
    broadcastStatus: (status) => statuses.push(status),
    fetchImpl: async () => {
      fetchCalls += 1;
      return response({ version: "0.1.18", desktopRuntimeVerified: probeMode === "ready" }, probeMode === "ready");
    },
    spawnProcess: (command, args) => {
      if (String(command).toLowerCase().includes("taskkill")) {
        const taskkill = new FakeChild(++pid);
        taskkillCalls.push({ command, args });
        return taskkill;
      }
      const child = new FakeChild(++pid);
      children.push(child);
      return child;
    },
    ...timers
  });

  manager.start();
  manager.start();
  assert.equal(children.length, 1, "single-flight start must spawn only one runtime child");
  const startupWaiters = Promise.all([manager.waitUntilReady(5000), manager.waitUntilReady(5000)]);
  assert.equal(fetchCalls, 0, "waitUntilReady must share startup latch before spawn and avoid probe storms");

  const firstChild = children[0];
  firstChild.emit("spawn");
  assert.deepEqual(await startupWaiters, [true, true], "concurrent startup waiters should resolve from the same ready probe");
  await waitFor(() => manager.getStatus().phase === "ready", "first child ready");

  manager.stop();
  manager.start();
  assert.equal(children.length, 2, "second runtime start should spawn a replacement child");

  const secondChild = children[1];
  secondChild.emit("spawn");
  await waitFor(() => manager.getStatus().phase === "ready" && manager.getStatus().pid === secondChild.pid, "replacement ready");

  firstChild.stderr.emit("data", Buffer.from("stale child should not replace ready status"));
  firstChild.emit("exit", 9, null);
  assert.equal(manager.getStatus().phase, "ready", "stale child events must not overwrite replacement ready status");
  assert.equal(manager.getStatus().pid, secondChild.pid, "stale child exit must not clear replacement pid");

  const token = manager.getRuntimeToken();
  secondChild.stderr.emit(
    "data",
    Buffer.from(`X-EcoreX-Runtime-Token: ${token} Authorization: Bearer ${token} sk-abcdefghijklmnop user@example.com C:\\Users\\alice\\secret`)
  );
  const redacted = manager.getStatus();
  assert.equal(redacted.phase, "ready", "stderr should preserve ready phase");
  assert(!redacted.message.includes(token), "runtime token must be redacted");
  assert(!redacted.message.includes("sk-abcdefghijklmnop"), "API key must be redacted");
  assert(!redacted.message.includes("user@example.com"), "email must be redacted");
  assert(!redacted.message.includes("C:\\Users\\alice"), "Windows user path must be redacted");

  probeMode = "fail";
  await manager.checkRuntimeHealth(19317, secondChild.pid);
  assert.equal(manager.getStatus().phase, "degraded", "first health failure should enter degraded phase");
  await manager.checkRuntimeHealth(19317, secondChild.pid);
  await manager.checkRuntimeHealth(19317, secondChild.pid);
  assert.equal(manager.getStatus().phase, "restarting", "third health failure should enter restarting phase");
  assert(taskkillCalls.length >= 1, "health restart should terminate the unhealthy child");

  const bridgeSidecar = {
    waitUntilReady: async () => true,
    getBaseUrl: () => "http://127.0.0.1:19317",
    getRuntimeToken: () => token,
    getStatus: () => ({
      state: "running",
      phase: "ready",
      message: "ready",
      webPort: 19317,
      diagnostics: manager.getStatus().diagnostics
    })
  };
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("x".repeat(2 * 1024 * 1024 + 1), { status: 200 });
    const oversized = await fetchSidecarJson(bridgeSidecar, { path: "/api/version" });
    assert.equal(oversized.status, "error", "oversized sidecar response must fail");
    assert.match(oversized.message, /exceeded/i);

    globalThis.fetch = async () => new Response(`bad ${token} sk-abcdefghijklmnop user@example.com C:\\Users\\alice\\secret`, { status: 500 });
    const parseError = await fetchSidecarJson(bridgeSidecar, { path: "/api/version" });
    assert.equal(parseError.status, "error", "non-JSON sidecar response must fail cleanly");
    assert(!parseError.message.includes(token), "bridge parse error must redact runtime token");
    assert(!parseError.message.includes("sk-abcdefghijklmnop"), "bridge parse error must redact API key");
    assert(!parseError.message.includes("user@example.com"), "bridge parse error must redact email");
    assert(!parseError.message.includes("C:\\Users\\alice"), "bridge parse error must redact user path");

    let bridgeStatus = {
      state: "running",
      phase: "ready",
      message: "ready",
      webPort: 19317,
      diagnostics: manager.getStatus().diagnostics
    };
    const degradingBridgeSidecar = {
      waitUntilReady: async () => true,
      getBaseUrl: () => "http://127.0.0.1:19317",
      getRuntimeToken: () => token,
      getStatus: () => bridgeStatus,
      reportApiFailure: (reason) => {
        bridgeStatus = {
          ...bridgeStatus,
          phase: "degraded",
          message: reason,
          diagnostics: manager.getStatus().diagnostics
        };
        return bridgeStatus;
      }
    };
    globalThis.fetch = async () => {
      const error = new Error("The operation was aborted");
      error.name = "AbortError";
      throw error;
    };
    const timeoutError = await fetchSidecarJson(degradingBridgeSidecar, { path: "/api/version" });
    assert.equal(timeoutError.status, "error", "sidecar request timeout must fail cleanly");
    assert.equal(timeoutError.sidecarPhase, "degraded", "bridge timeout must report degraded phase instead of stale ready");
    assert.match(timeoutError.message, /timed out/i);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const payload = {
    status: "pass",
    version: "0.1.18",
    generatedAt: new Date().toISOString(),
    changeIds: ["STAB-004"],
    checks: [
      { name: "single-flight startup", status: "pass", evidence: "two start calls spawned one child" },
      { name: "startup waiters share startup latch", status: "pass", evidence: "two waitUntilReady calls before spawn made zero direct probes and resolved from one startup promise" },
      { name: "stale child isolation", status: "pass", evidence: "old stderr/exit did not overwrite replacement ready status" },
      { name: "diagnostic redaction", status: "pass", evidence: "token, API key, email, and user path absent from current status" },
      { name: "health degraded restart", status: "pass", evidence: "three failed health checks moved degraded -> restarting and terminated child" },
      { name: "api bridge body cap", status: "pass", evidence: "oversized sidecar response rejected" },
      { name: "api bridge parse redaction", status: "pass", evidence: "invalid JSON response snippet redacted secrets" },
      { name: "api bridge timeout degrades ready status", status: "pass", evidence: "AbortError returned sidecarPhase=degraded instead of stale ready" }
    ]
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  console.log(JSON.stringify(payload, null, 2));
} finally {
  if (previousPython === undefined) {
    delete process.env.ECOREX_PYTHON;
  } else {
    process.env.ECOREX_PYTHON = previousPython;
  }
  if (previousWebPort === undefined) {
    delete process.env.ECOREX_WEB_PORT;
  } else {
    process.env.ECOREX_WEB_PORT = previousWebPort;
  }
  fs.rmSync(tmpRoot, { recursive: true, force: true });
}

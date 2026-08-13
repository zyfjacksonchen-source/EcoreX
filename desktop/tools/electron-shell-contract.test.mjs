import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { link, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import backendContract from "../electron/backend.cjs";
import navigationPolicy from "../electron/navigation-policy.cjs";
import updateContract from "../electron/update-contract.cjs";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (relative) => readFile(path.join(desktop, relative), "utf8");
const productVersion = async () => (await readFile(
  path.resolve(desktop, "../ecorex/_version.py"),
  "utf8",
)).match(/__version__ = "([^"]+)"/)?.[1];

const runtimeIdentity = (digit = "a") => ({
  release_id: `release-test-${digit}`,
  build_digest: digit.repeat(64),
  artifact_id: `core-test-${digit}`,
  artifact_sha256: digit.repeat(64),
  payload_digest: digit.repeat(64),
});

const ownedRuntimeProgram = String.raw`
const crypto = require("node:crypto");
const http = require("node:http");
const nonce = process.env.TEST_RUNTIME_NONCE || process.env.ECOREX_RUNTIME_OWNER_NONCE;
const portArgument = process.argv.indexOf("--port");
const port = Number(process.env.TEST_RUNTIME_PORT || process.argv[portArgument + 1]);
const ownerReadyAt = Date.now() + Number(process.env.TEST_RUNTIME_OWNER_DELAY_MS || 0);
const server = http.createServer((request, response) => {
  if (process.env.TEST_RUNTIME_UNKNOWN === "1") {
    response.writeHead(404);
    response.end();
    return;
  }
  if (Date.now() < ownerReadyAt) {
    response.writeHead(503);
    response.end();
    return;
  }
  const challenge = request.headers["x-ecorex-owner-challenge"];
  const proof = crypto.createHmac("sha256", Buffer.from(nonce, "base64url"))
    .update("e-mate.runtime-owner.v1\0", "ascii")
    .update(challenge, "ascii")
    .digest("base64url");
  response.writeHead(204, { "X-EcoreX-Runtime-Owner": proof });
  response.end();
});
server.listen(port, "127.0.0.1", () => console.log(server.address().port));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
`;

const stageFailureProgram = String.raw`
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.EMATE_DATA_DIR;
const attemptsPath = path.join(root, "startup-test-attempts");
const attempts = fs.existsSync(attemptsPath) ? Number(fs.readFileSync(attemptsPath, "utf8")) : 0;
if (attempts < 2) {
  const token = process.env.ECOREX_RUNTIME_STARTUP_DIAGNOSTIC_TOKEN;
  const stage = attempts === 0 ? "credential_vault" : "legacy_desktop_data_migration";
  fs.writeFileSync(attemptsPath, String(attempts + 1));
  fs.writeFileSync(path.join(root, ".runtime-startup", token + ".json"), JSON.stringify({
    schema_version: 1,
    stage,
    token,
  }), { flag: "wx" });
  process.exit(64);
}
${ownedRuntimeProgram}
`;

const bindRaceRuntimeProgram = String.raw`
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.EMATE_DATA_DIR;
const marker = path.join(root, "startup-test-bind-race");
if (!fs.existsSync(marker)) {
  const token = process.env.ECOREX_RUNTIME_STARTUP_DIAGNOSTIC_TOKEN;
  fs.writeFileSync(marker, "observed");
  fs.writeFileSync(path.join(root, ".runtime-startup", token + ".json"), JSON.stringify({
    schema_version: 1,
    stage: "http_server_bind",
    token,
  }), { flag: "wx" });
  process.exit(64);
}
${ownedRuntimeProgram}
`;

const startOwnedRuntime = (nonce, { port = 0, unknown = false, ownerDelayMs = 0 } = {}) => new Promise((resolve, reject) => {
  const child = spawn(process.execPath, ["-e", ownedRuntimeProgram], {
    detached: true,
    env: {
      ...process.env,
      TEST_RUNTIME_NONCE: nonce,
      TEST_RUNTIME_PORT: String(port),
      TEST_RUNTIME_UNKNOWN: unknown ? "1" : "0",
      TEST_RUNTIME_OWNER_DELAY_MS: String(ownerDelayMs),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.once("error", reject);
  child.once("exit", (code) => reject(new Error(`test Runtime exited early (${code}): ${stderr}`)));
  child.stdout.setEncoding("utf8");
  child.stdout.once("data", (value) => resolve({ child, port: Number(value.trim()) }));
});

const stopTestRuntime = async (child) => {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  try { child.kill("SIGKILL"); } catch { return; }
  await exited;
};

const loopbackPortOpen = (port) => new Promise((resolve) => {
  const socket = net.connect({ host: "127.0.0.1", port });
  socket.once("connect", () => { socket.destroy(); resolve(true); });
  socket.once("error", () => resolve(false));
});

const stageTestRuntime = async (resources, identity, program = ownedRuntimeProgram) => {
  const runtime = path.join(resources, "runtime");
  const payload = path.join(runtime, "payload");
  const command = path.join(payload, "bin", process.platform === "win32" ? "ecorex.exe" : "ecorex");
  await mkdir(path.dirname(command), { recursive: true });
  await link(process.execPath, command);
  await Promise.all([
    writeFile(path.join(payload, "serve"), program),
    writeFile(path.join(runtime, ".slot.json"), JSON.stringify(identity)),
    writeFile(path.join(runtime, "release-manifest.json"), "{}"),
  ]);
  return runtime;
};

test("desktop identity and unsigned release targets are explicit", async () => {
  const pkg = JSON.parse(await load("package.json"));
  assert.equal(pkg.name, "e-mate-desktop");
  assert.equal(pkg.version, await productVersion());
  assert.equal(pkg.main, "electron/main.cjs");
  assert.equal(pkg.build.appId, "net.ecoremedia.emate");
  assert.equal(pkg.build.productName, "e-Mate");
  assert.equal(pkg.build.directories.buildResources, "build");
  assert.equal(pkg.build.mac.icon, "icon.icns");
  assert.equal(pkg.build.win.icon, "icon.ico");
  assert.equal(pkg.build.publish.url, "https://dl.ecoremedia.net/e-mate/update/");
  assert.equal(pkg.build.mac.identity, null);
  assert.equal(pkg.build.mac.hardenedRuntime, false);
  assert.equal(pkg.build.mac.notarize, false);
  assert.equal(pkg.build.win.forceCodeSigning, false);
  assert.deepEqual(pkg.build.mac.target, ["dmg", "zip"]);
  assert.deepEqual(pkg.build.win.target[0].arch, ["x64"]);
  assert.ok(pkg.build.files.includes("src/v1/assets/emate-logo.png"));
});

test("desktop starts the Runtime before waiting for the startup page", async () => {
  const main = await load("electron/main.cjs");
  const launchBody = main.match(/async function launch\(\) \{(?<body>[\s\S]*?)\n\}\n\nconst singleInstance/)?.groups?.body ?? "";
  assert.match(launchBody, /const runtimeStartup = startBackendWithRetry\(window\);/);
  assert.ok(launchBody.indexOf("const runtimeStartup") < launchBody.indexOf("await window.loadURL(startupPage())"));
  assert.match(launchBody, /const runtimeOrigin = await runtimeStartup;/);
});

test("desktop loads the existing loopback Runtime and never packages a second renderer", async () => {
  const [main, backend, preload, staging] = await Promise.all([
    load("electron/main.cjs"),
    load("electron/backend.cjs"),
    load("electron/preload.cjs"),
    load("tools/stage-electron-runtime.mjs"),
  ]);
  assert.match(main, /loadURL\(runtimeOrigin\)/);
  assert.match(main, /loadURL\(startupPage\(\)\)/);
  assert.match(main, /startBackendWithRetry/);
  assert.match(main, /backend\.on\("exit", \(code\) =>/);
  assert.match(main, /code === 86\) void restartRuntime\(\)/);
  assert.match(main, /await mainWindow\?\.loadURL\(startupPage\(\)\)/);
  assert.match(main, /if \(runtimeRestart\) return runtimeRestart/);
  assert.match(main, /app\.on\("before-quit", \(event\) =>/);
  assert.match(main, /event\.preventDefault\(\)/);
  assert.match(main, /await backend\?\.stop\(\)/);
  assert.match(main, /finally \{\s+shutdownComplete = true;\s+app\.quit\(\)/);
  assert.match(main, /buttons: \["重试", "退出"\]/);
  assert.match(main, /console\.error\(`\[e-Mate\] Runtime startup failed \(\$\{diagnosticCode\}\)\.`\)/);
  assert.match(main, /function createWindow\(runtimeOrigin = \(\) => backend\.origin\)/);
  assert.match(main, /const origin = runtimeOrigin\(\)/);
  assert.match(main, /src", "v1", "assets", "emate-logo\.png"/);
  assert.match(main, /titleBarStyle: "hiddenInset"/);
  assert.match(main, /trafficLightPosition: \{ x: 14, y: 18 \}/);
  assert.match(main, /if \(process\.platform === "win32"\)/);
  assert.match(main, /titleBarOverlay: \{ color: "#1c1c1e", symbolColor: "#c7c7cc", height: 48 \}/);
  assert.match(main, /webContents\.on\("context-menu"/);
  assert.match(main, /backgroundColor: "#171719"/);
  assert.match(main, /copyImageAt\(params\.x, params\.y\)/);
  assert.match(main, /label: "复制链接地址"/);
  assert.match(main, /label: "保存并复制文件路径"/);
  assert.match(main, /event\.sender !== mainWindow\?\.webContents/);
  assert.match(main, /stat\.isFile\(\) && !stat\.isSymbolicLink\(\)/);
  assert.match(main, /<img class="logo" src="\$\{logo\}"/);
  assert.doesNotMatch(main, />ϟ</);
  assert.doesNotMatch(main, /loadFile\(/);
  assert.match(backend, /-m", "ecorex\.server\.cli", "serve"/);
  assert.match(backend, /ecorex\.exe/);
  assert.match(backend, /127\.0\.0\.1/);
  assert.match(backend, /packagedRuntimeSpec/);
  assert.match(backend, /EMATE_PACKAGED_RUNTIME: "1"/);
  assert.match(backend, /EMATE_DATA_DIR: dataDir/);
  assert.match(backend, /COW_DATA_DIR: dataDir/);
  assert.match(backend, /COW_DESKTOP: "1"/);
  assert.match(backend, /PLAYWRIGHT_BROWSERS_PATH: path\.join\(payload, "ms-playwright"\)/);
  assert.match(backend, /ECOREX_RUNTIME_OWNER_NONCE/);
  assert.doesNotMatch(backend, /--local-release/);
  assert.doesNotMatch(backend, /--launch-installed/);
  assert.doesNotMatch(backend, /installedReleaseMatches/);
  assert.match(backend, /runtime-owner\.json/);
  assert.match(backend, /x-ecorex-runtime-owner/);
  assert.match(backend, /ECOREX_BOOTSTRAPPED === "1"/);
  assert.match(backend, /stage \? `runtime_stage_\$\{stage\}` : runtimeExitDiagnosticCode\(code\)/);
  assert.doesNotMatch(backend, /if \(code !== 0\) startupFailure/);
  assert.match(backend, /while \(!startupFailure\)/);
  assert.doesNotMatch(backend, /Date\.now\(\) \+ 5 \* 60_000/);
  assert.doesNotMatch(backend, /exited before the Runtime became ready/);
  assert.match(staging, /stage-direct-runtime\.py/);
  assert.match(staging, /release-manifest\.json/);
  assert.match(preload, /contextBridge\.exposeInMainWorld\("eMateDesktop"/);
  assert.match(preload, /data-emate-artifact-id/);
  assert.match(preload, /onCopyArtifactPath/);
  assert.match(preload, /copyMaterializedPath/);
  assert.doesNotMatch(preload, /require\(["']\.\//u);
  const pkg = JSON.parse(await load("package.json"));
  assert.equal(pkg.build.files.some((entry) => entry.includes("renderer")), false);
  assert.equal(pkg.build.extraResources[0].from, "runtime-bundle");
});

test("loopback owner proof never discloses its secret to an untrusted listener", async () => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), "emate-owner-proof-"));
  const nonce = Buffer.alloc(32, 7).toString("base64url");
  backendContract.issueRuntimeOwnerReceipt(dataDir, process.pid, runtimeIdentity(), nonce);
  const listen = async (server) => {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    return server.address().port;
  };
  const close = (server) => new Promise((resolve) => server.close(resolve));
  const servers = [];
  try {
    let disclosed;
    let markAttackerClosed;
    const attackerClosed = new Promise((resolve) => { markAttackerClosed = resolve; });
    const attacker = http.createServer((request, response) => {
      disclosed = request.headers["x-ecorex-owner-nonce"];
      request.socket.on("error", () => {});
      request.socket.once("close", markAttackerClosed);
      response.writeHead(200, { "X-EcoreX-Runtime-Owner": "verified" });
      response.write("untrusted body stays open");
    });
    servers.push(attacker);
    const attackerPort = await listen(attacker);
    assert.equal(await backendContract.runtimeResponds(attackerPort, dataDir), false);
    assert.equal(disclosed, undefined);
    let closeDeadline;
    const closed = await Promise.race([
      attackerClosed.then(() => true),
      new Promise((resolve) => { closeDeadline = setTimeout(() => resolve(false), 500); }),
    ]);
    clearTimeout(closeDeadline);
    assert.equal(closed, true);

    const runtime = http.createServer((request, response) => {
      const challenge = request.headers["x-ecorex-owner-challenge"];
      const proof = createHmac("sha256", Buffer.from(nonce, "base64url"))
        .update("e-mate.runtime-owner.v1\0", "ascii")
        .update(challenge, "ascii")
        .digest("base64url");
      response.writeHead(204, { "X-EcoreX-Runtime-Owner": proof });
      response.end();
    });
    servers.push(runtime);
    const runtimePort = await listen(runtime);
    assert.equal(await backendContract.runtimeResponds(runtimePort, dataDir), true);

    const drip = net.createServer((socket) => {
      socket.on("error", () => {});
      socket.once("data", () => {
        socket.write("HTTP/1.1 204 No Content\r\nX-Drip: ");
        const interval = setInterval(() => {
          if (!socket.destroyed) socket.write("x");
        }, 100);
        socket.once("close", () => clearInterval(interval));
      });
    });
    servers.push(drip);
    const dripPort = await listen(drip);
    const started = Date.now();
    assert.equal(await backendContract.runtimeResponds(dripPort, dataDir), false);
    assert.ok(Date.now() - started < 2_500);
  } finally {
    await Promise.all(servers.filter((server) => server.listening).map(close));
    await rm(dataDir, { recursive: true, force: true });
  }
});

test("desktop adopts an exact crash-left Runtime before rotating its owner receipt", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-adopt-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const identity = runtimeIdentity();
  const nonce = Buffer.alloc(32, 11).toString("base64url");
  let owned;
  try {
    await stageTestRuntime(resources, identity);
    owned = await startOwnedRuntime(nonce);
    backendContract.issueRuntimeOwnerReceipt(dataDir, owned.child.pid, identity, nonce);
    const backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: owned.port,
    });

    assert.equal(await backend.start(), `http://127.0.0.1:${owned.port}`);
    assert.equal(backend.child, null);
    assert.equal(backendContract.runtimeOwnerNonce(dataDir), nonce);
    await backend.stop();
    assert.equal(await loopbackPortOpen(owned.port), false);
  } finally {
    await stopTestRuntime(owned?.child);
    await rm(root, { recursive: true, force: true });
  }
});

test("desktop waits for an exact owned Runtime whose owner proof is still starting", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-starting-owner-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const identity = runtimeIdentity();
  const nonce = Buffer.alloc(32, 15).toString("base64url");
  let owned;
  try {
    await stageTestRuntime(resources, identity);
    owned = await startOwnedRuntime(nonce, { ownerDelayMs: 2000 });
    backendContract.issueRuntimeOwnerReceipt(dataDir, owned.child.pid, identity, nonce);
    const backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: owned.port,
    });

    assert.equal(await backend.start(), `http://127.0.0.1:${owned.port}`);
    assert.equal(backend.child, null);
    await backend.stop();
    assert.equal(await loopbackPortOpen(owned.port), false);
  } finally {
    await stopTestRuntime(owned?.child);
    await rm(root, { recursive: true, force: true });
  }
});

test("desktop replaces an owned Runtime only when its immutable identity changed", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-replace-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const oldIdentity = runtimeIdentity("a");
  const newIdentity = runtimeIdentity("b");
  const nonce = Buffer.alloc(32, 12).toString("base64url");
  let oldRuntime;
  let backend;
  try {
    await stageTestRuntime(resources, newIdentity);
    oldRuntime = await startOwnedRuntime(nonce);
    backendContract.issueRuntimeOwnerReceipt(dataDir, oldRuntime.child.pid, oldIdentity, nonce);
    backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: oldRuntime.port,
    });

    assert.equal(await backend.start(), `http://127.0.0.1:${oldRuntime.port}`);
    const receipt = backendContract.runtimeOwnerReceipt(dataDir);
    assert.deepEqual(receipt.runtime_identity, newIdentity);
    assert.notEqual(receipt.pid, oldRuntime.child.pid);
    assert.equal(await backendContract.runtimeResponds(oldRuntime.port, dataDir, newIdentity), true);
    await backend.stop();
    assert.equal(await loopbackPortOpen(oldRuntime.port), false);
  } finally {
    await backend?.stop().catch(() => {});
    await stopTestRuntime(oldRuntime?.child);
    await rm(root, { recursive: true, force: true });
  }
});

test("desktop never rotates a receipt or kills an unknown loopback listener", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-unknown-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const identity = runtimeIdentity();
  const nonce = Buffer.alloc(32, 13).toString("base64url");
  let unknown;
  try {
    await stageTestRuntime(resources, identity);
    unknown = await startOwnedRuntime(nonce, { unknown: true });
    backendContract.issueRuntimeOwnerReceipt(dataDir, unknown.child.pid, identity, nonce);
    const receiptPath = path.join(dataDir, "bootstrap", "runtime-owner.json");
    const receipt = await readFile(receiptPath, "utf8");
    const backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: unknown.port,
    });

    await assert.rejects(
      backend.start(),
      (error) => error?.diagnosticCode === "runtime_port_occupied",
    );
    assert.equal(await readFile(receiptPath, "utf8"), receipt);
    assert.equal(await loopbackPortOpen(unknown.port), true);
  } finally {
    await stopTestRuntime(unknown?.child);
    await rm(root, { recursive: true, force: true });
  }
});

test("desktop moves an unknown occupied port and one bind race to a working loopback origin", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-port-fallback-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const identity = runtimeIdentity();
  const unknown = http.createServer((_request, response) => {
    response.writeHead(404);
    response.end();
  });
  let backend;
  try {
    await stageTestRuntime(resources, identity, bindRaceRuntimeProgram);
    await new Promise((resolve) => unknown.listen(0, "127.0.0.1", resolve));
    const occupiedPort = unknown.address().port;
    backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: occupiedPort,
      allowPortFallback: true,
    });

    const origin = await backend.start();

    assert.notEqual(origin, `http://127.0.0.1:${occupiedPort}`);
    assert.equal(origin, backend.origin);
    assert.equal(await loopbackPortOpen(occupiedPort), true);
    assert.equal(await readFile(path.join(dataDir, "startup-test-bind-race"), "utf8"), "observed");
    assert.equal(navigationPolicy.externalHttpUrl(`${origin}/threads/1`, origin), null);
    assert.equal(navigationPolicy.externalHttpUrl("https://example.com/", origin), "https://example.com/");
  } finally {
    await backend?.stop().catch(() => {});
    if (unknown.listening) await new Promise((resolve) => unknown.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});

test("desktop replaces safe startup diagnostics on retry and removes them after success", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-diagnostics-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const diagnosticPath = path.join(dataDir, "diagnostics", "runtime-startup.json");
  let backend;
  try {
    await stageTestRuntime(resources, runtimeIdentity(), stageFailureProgram);
    const port = await backendContract.availableLoopbackPort();
    backend = new backendContract.BackendManager({ packaged: true, resourcesPath: resources, dataDir, port });

    await assert.rejects(backend.start(), (error) => error?.diagnosticCode === "runtime_stage_credential_vault");
    const first = JSON.parse(await readFile(diagnosticPath, "utf8"));
    assert.deepEqual(first, {
      schema_version: 1,
      status: "failed",
      diagnostic_code: "runtime_stage_credential_vault",
      phase: "runtime",
      stage: "credential_vault",
      exit_code: 64,
    });

    await assert.rejects(
      backend.start(),
      (error) => error?.diagnosticCode === "runtime_stage_legacy_desktop_data_migration",
    );
    const second = JSON.parse(await readFile(diagnosticPath, "utf8"));
    assert.equal(second.diagnostic_code, "runtime_stage_legacy_desktop_data_migration");
    assert.equal(JSON.stringify(second).includes(root), false);

    assert.equal(await backend.start(), backend.origin);
    await assert.rejects(readFile(diagnosticPath, "utf8"), (error) => error?.code === "ENOENT");
  } finally {
    await backend?.stop().catch(() => {});
    await rm(root, { recursive: true, force: true });
  }
});

test("Runtime spawn errors reject without an unhandled error race", async (context) => {
  if (process.platform === "win32") {
    context.skip("POSIX executable permissions are not portable to Windows");
    return;
  }
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-spawn-error-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const runtime = path.join(resources, "runtime");
  const command = path.join(runtime, "payload", "bin", "ecorex");
  try {
    await mkdir(path.dirname(command), { recursive: true });
    await Promise.all([
      writeFile(command, "not executable", { mode: 0o600 }),
      writeFile(path.join(runtime, ".slot.json"), JSON.stringify(runtimeIdentity())),
      writeFile(path.join(runtime, "release-manifest.json"), "{}"),
    ]);
    const backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: 0,
    });

    await assert.rejects(
      backend.start(),
      (error) => error?.diagnosticCode === "runtime_spawn_eacces",
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(backendContract.runtimeOwnerReceipt(dataDir), null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("legacy owner receipts are neither adopted nor killed", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-legacy-owner-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const nonce = Buffer.alloc(32, 14).toString("base64url");
  let legacy;
  try {
    await stageTestRuntime(resources, runtimeIdentity());
    legacy = await startOwnedRuntime(nonce);
    await mkdir(path.join(dataDir, "bootstrap"), { recursive: true });
    await writeFile(
      path.join(dataDir, "bootstrap", "runtime-owner.json"),
      JSON.stringify({ schema_version: 1, nonce }),
    );
    const backend = new backendContract.BackendManager({
      packaged: true,
      resourcesPath: resources,
      dataDir,
      port: legacy.port,
    });

    await assert.rejects(
      backend.start(),
      (error) => error?.diagnosticCode === "runtime_port_occupied",
    );
    assert.equal(await loopbackPortOpen(legacy.port), true);
  } finally {
    await stopTestRuntime(legacy?.child);
    await rm(root, { recursive: true, force: true });
  }
});

test("packaged desktop directly launches the immutable Runtime on macOS and Windows", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-direct-"));
  const resources = path.join(root, "resources");
  const dataDir = path.join(root, "data");
  const runtime = path.join(resources, "runtime");
  const identity = runtimeIdentity();
  try {
    await mkdir(path.join(runtime, "payload", "bin"), { recursive: true });
    await Promise.all([
      writeFile(path.join(runtime, "payload", "bin", "ecorex"), "mac runtime"),
      writeFile(path.join(runtime, "payload", "bin", "ecorex.exe"), "windows runtime"),
      writeFile(path.join(runtime, ".slot.json"), JSON.stringify(identity)),
      writeFile(path.join(runtime, "release-manifest.json"), "{}"),
    ]);

    const mac = backendContract.packagedRuntimeSpec(resources, dataDir, 8765, "darwin");
    assert.equal(mac.command, path.join(runtime, "payload", "bin", "ecorex"));
    assert.deepEqual(mac.args, ["serve", "--host", "127.0.0.1", "--port", "8765"]);
    assert.equal(mac.cwd, path.join(runtime, "payload"));
    assert.equal(mac.environment.EMATE_DATA_DIR, dataDir);
    assert.equal(mac.environment.COW_DATA_DIR, dataDir);
    assert.equal(mac.environment.COW_DESKTOP, "1");
    assert.ok(mac.environment.PATH.split(":").includes("/opt/homebrew/bin"));
    assert.ok(mac.environment.PATH.split(":").includes(path.join(os.homedir(), ".local/bin")));
    assert.equal(mac.environment.PLAYWRIGHT_BROWSERS_PATH, path.join(runtime, "payload", "ms-playwright"));
    assert.equal(mac.environment.EMATE_PACKAGED_RUNTIME, "1");
    assert.equal(mac.windowsHide, true);
    assert.equal(mac.detached, true);
    assert.deepEqual(mac.runtimeIdentity, identity);

    const windows = backendContract.packagedRuntimeSpec(resources, dataDir, 9988, "win32");
    assert.equal(windows.command, path.join(runtime, "payload", "bin", "ecorex.exe"));
    assert.deepEqual(windows.args, ["serve", "--host", "127.0.0.1", "--port", "9988"]);
    assert.equal(windows.environment.EMATE_DATA_DIR, dataDir);
    assert.equal(windows.environment.COW_DATA_DIR, dataDir);
    assert.equal(windows.environment.PATH, process.env.PATH || "");
    assert.equal(windows.environment.PLAYWRIGHT_BROWSERS_PATH, path.join(runtime, "payload", "ms-playwright"));
    assert.equal(windows.windowsHide, true);
    assert.equal(windows.detached, false);
    assert.deepEqual(windows.runtimeIdentity, identity);

    const nonce = backendContract.issueRuntimeOwnerReceipt(dataDir, 4312, identity);
    assert.match(nonce, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(backendContract.runtimeOwnerNonce(dataDir), nonce);
    assert.deepEqual(backendContract.runtimeOwnerReceipt(dataDir), {
      nonce,
      pid: 4312,
      runtime_identity: identity,
    });
    assert.equal(await readdir(dataDir).then((items) => items.includes("slots")), false);

    await rm(path.join(runtime, ".slot.json"));
    assert.equal(backendContract.packagedRuntimeSpec(resources, dataDir, 8765, "darwin"), null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Runtime shutdown targets the exact macOS process group and Windows process tree", () => {
  assert.deepEqual(
    backendContract.runtimeTerminationSpec(4312, false, "darwin"),
    { pid: -4312, signal: "SIGTERM" },
  );
  assert.deepEqual(
    backendContract.runtimeTerminationSpec(4312, true, "darwin"),
    { pid: -4312, signal: "SIGKILL" },
  );
  assert.deepEqual(
    backendContract.runtimeTerminationSpec(4312, false, "win32", "C:\\Windows"),
    { command: path.join("C:\\Windows", "System32", "taskkill.exe"), args: ["/PID", "4312", "/T"] },
  );
  assert.deepEqual(
    backendContract.runtimeTerminationSpec(4312, true, "win32", "C:\\Windows"),
    { command: path.join("C:\\Windows", "System32", "taskkill.exe"), args: ["/PID", "4312", "/T", "/F"] },
  );
  assert.equal(backendContract.runtimeTerminationSpec(0, false, "win32"), null);
});

test("public download index parsing only offers newer stable releases", () => {
  const version = "2.0.1";
  const download = (target, platform, architecture, fileName) => ({
    target,
    platform,
    architecture,
    file_name: fileName,
    url: `https://dl.ecoremedia.net/e-mate/update/${fileName}`,
    size_bytes: 123456,
    sha256: "a".repeat(64),
  });
  const index = {
    schema_version: 2,
    product: "e-Mate",
    version,
    distribution_mode: "unsigned-manual",
    released_at: "2026-08-11T00:00:00Z",
    downloads: [
      download("windows-x64", "windows", "x64", `e-Mate-Setup-${version}-x64.exe`),
      download("macos-arm64", "macos", "arm64", `e-Mate-${version}-arm64.dmg`),
      download("macos-x64", "macos", "x64", `e-Mate-${version}-x64.dmg`),
    ],
  };
  assert.equal(updateContract.parseDownloadIndex(JSON.stringify(index))?.version, version);
  const forged = structuredClone(index);
  forged.downloads[1].url = "https://example.invalid/forged.dmg";
  assert.equal(updateContract.parseDownloadIndex(JSON.stringify(forged)), null);
  assert.equal(updateContract.parseDownloadIndex(JSON.stringify({ ...index, distribution_mode: "signed" })), null);
  assert.equal(updateContract.isNewerStableVersion("2.0.1", "2.0.0"), true);
  assert.equal(updateContract.isNewerStableVersion("2.0.0", "2.0.0"), false);
  assert.equal(updateContract.isNewerStableVersion("1.9.9", "2.0.0"), false);
  assert.equal(updateContract.isNewerStableVersion("2.1.0-beta.1", "2.0.0"), false);
});

test("unsigned mac updates remain manual while Windows retains electron-updater", async () => {
  const updater = await load("electron/updater.cjs");
  assert.match(updater, /process\.platform !== "win32"/);
  assert.match(updater, /shell\.openExternal\(DOWNLOAD_URL\)/);
  assert.match(updater, /parseDownloadIndex/);
  assert.match(updater, /download-index\.json/);
  assert.doesNotMatch(updater, /latest-mac\.yml/);
  assert.match(updater, /autoUpdater\.autoDownload = false/);
  assert.match(updater, /autoUpdater\.quitAndInstall/);
  assert.match(updater, /manualInstall: true/);
});

test("native completion notifications are Runtime-driven and deep-link through preload", async () => {
  const [main, notifications, contract, preload, app] = await Promise.all([
    load("electron/main.cjs"),
    load("electron/task-notifications.cjs"),
    load("electron/notification-contract.cjs"),
    load("electron/preload.cjs"),
    load("src/v1/AppV1.tsx"),
  ]);
  assert.match(contract, /turn\.status_changed/);
  assert.match(contract, /status === "completed"/);
  assert.match(notifications, /\/api\/v1\/threads/);
  assert.match(main, /new TaskNotificationMonitor/);
  assert.match(main, /webContents\.send\("emate:open-thread"/);
  assert.match(preload, /validThreadId\(threadId\)/);
  assert.match(app, /runtime\.openThread\(threadId\)/);
});

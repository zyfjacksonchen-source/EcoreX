import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHmac } from "node:crypto";
import { copyFile, cp, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import backendContract from "../electron/backend.cjs";
import updateContract from "../electron/update-contract.cjs";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (relative) => readFile(path.join(desktop, relative), "utf8");
const run = promisify(execFile);
const productVersion = async () => (await readFile(
  path.resolve(desktop, "../ecorex/_version.py"),
  "utf8",
)).match(/__version__ = "([^"]+)"/)?.[1];

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
  assert.equal(pkg.build.publish.url, "https://mvdcm.ecoremedia.net/e-mate/update/");
  assert.equal(pkg.build.mac.identity, null);
  assert.equal(pkg.build.mac.hardenedRuntime, false);
  assert.equal(pkg.build.mac.notarize, false);
  assert.equal(pkg.build.win.forceCodeSigning, false);
  assert.deepEqual(pkg.build.mac.target, ["dmg", "zip"]);
  assert.deepEqual(pkg.build.win.target[0].arch, ["x64"]);
  assert.ok(pkg.build.files.includes("src/v1/assets/emate-logo.png"));
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
  assert.match(main, /buttons: \["重试", "退出"\]/);
  assert.match(main, /src", "v1", "assets", "emate-logo\.png"/);
  assert.match(main, /<img class="logo" src="\$\{logo\}"/);
  assert.doesNotMatch(main, />ϟ</);
  assert.doesNotMatch(main, /loadFile\(/);
  assert.match(backend, /-m", "ecorex\.server\.cli", "serve"/);
  assert.match(backend, /emate-backend/);
  assert.match(backend, /127\.0\.0\.1/);
  assert.match(backend, /--local-release/);
  assert.match(backend, /--launch-installed/);
  assert.match(backend, /--no-open/);
  assert.match(backend, /runtime-owner\.json/);
  assert.match(backend, /x-ecorex-runtime-owner/);
  assert.match(backend, /ECOREX_BOOTSTRAPPED === "1"/);
  assert.doesNotMatch(backend, /exited before the Runtime became ready/);
  assert.match(staging, /ecorex-bootstrap/);
  assert.match(staging, /release-manifest\.json/);
  assert.match(staging, /release-metadata\.json/);
  assert.match(staging, /sbom\.cdx\.json/);
  assert.match(preload, /contextBridge\.exposeInMainWorld\("eMateDesktop"/);
  assert.doesNotMatch(preload, /require\(["']\.\//u);
  const pkg = JSON.parse(await load("package.json"));
  assert.equal(pkg.build.files.some((entry) => entry.includes("renderer")), false);
  assert.equal(pkg.build.extraResources[0].from, "runtime-bundle");
});

test("loopback owner proof never discloses its secret to an untrusted listener", async () => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), "emate-owner-proof-"));
  const nonce = Buffer.alloc(32, 7).toString("base64url");
  await mkdir(path.join(dataDir, "bootstrap"));
  await writeFile(
    path.join(dataDir, "bootstrap", "runtime-owner.json"),
    JSON.stringify({ schema_version: 1, nonce }),
  );
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

test("packaged desktop installs a different signed Runtime release before launch", async () => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-current-"));
  const releaseDir = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-seed-"));
  const slotId = "r-current";
  const identity = {
    release_id: "release-stable-" + "a".repeat(24),
    version: "2.0.1",
    build_digest: "b".repeat(64),
  };
  try {
    const pythonPath = process.platform === "win32"
      ? path.join(dataDir, "slots", slotId, "payload", "bin", "pack-python", "python.exe")
      : path.join(dataDir, "slots", slotId, "payload", "bin", "pack-python", "bin", "python3");
    await mkdir(path.dirname(pythonPath), { recursive: true });
    await writeFile(pythonPath, "runtime");
    await writeFile(path.join(dataDir, "slot-pointers.json"), JSON.stringify({
      current: slotId,
      known_good: [slotId],
    }));
    await writeFile(path.join(dataDir, "slots", slotId, ".slot.json"), JSON.stringify(identity));
    await writeFile(path.join(releaseDir, "release-manifest.json"), JSON.stringify(identity));
    assert.equal(backendContract.installedReleaseMatches(dataDir, releaseDir), true);

    await writeFile(path.join(dataDir, "slot-pointers.json"), JSON.stringify({
      current: slotId,
      previous: null,
      known_good: [slotId],
      unexpected: true,
    }));
    assert.equal(backendContract.installedReleaseMatches(dataDir, releaseDir), false);
    await writeFile(path.join(dataDir, "slot-pointers.json"), JSON.stringify({
      current: slotId,
      previous: null,
    }));
    assert.equal(backendContract.installedReleaseMatches(dataDir, releaseDir), false);
    await writeFile(path.join(dataDir, "slot-pointers.json"), JSON.stringify({
      current: slotId,
      previous: null,
      known_good: [slotId],
    }));

    await writeFile(path.join(releaseDir, "release-manifest.json"), JSON.stringify({
      ...identity,
      version: "2.0.2",
    }));
    assert.equal(backendContract.installedReleaseMatches(dataDir, releaseDir), false);
  } finally {
    await Promise.all([
      rm(dataDir, { recursive: true, force: true }),
      rm(releaseDir, { recursive: true, force: true }),
    ]);
  }
});

test("Windows in-place upgrades select a clean release identity namespace", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "emate-runtime-upgrade-"));
  const isolatedDesktop = path.join(root, "desktop");
  const bootstrap = path.join(root, "bootstrap");
  const release = path.join(root, "release");
  const resources = path.join(root, "installed", "resources");
  const runtime = path.join(resources, "runtime");
  const releaseId = `release-stable-${"c".repeat(24)}`;
  try {
    await Promise.all([
      mkdir(path.join(isolatedDesktop, "tools"), { recursive: true }),
      mkdir(path.join(bootstrap, "bin"), { recursive: true }),
      mkdir(release, { recursive: true }),
      mkdir(path.join(runtime, "release"), { recursive: true }),
    ]);
    await Promise.all([
      copyFile(
        path.join(desktop, "tools", "stage-electron-runtime.mjs"),
        path.join(isolatedDesktop, "tools", "stage-electron-runtime.mjs"),
      ),
      writeFile(path.join(bootstrap, "bin", "ecorex-bootstrap.exe"), "bootstrap"),
      writeFile(path.join(bootstrap, "bin", "ecorex-sandbox-host.exe"), "sandbox"),
      writeFile(path.join(bootstrap, "bootstrap-config.json"), "{}"),
      writeFile(path.join(release, "core.zip"), "current"),
      writeFile(path.join(release, "release-manifest.json"), JSON.stringify({
        release_id: releaseId,
        artifacts: [{
          platform: "windows",
          architecture: "x64",
          file_name: "core.zip",
        }],
      })),
      writeFile(path.join(runtime, "release", "release-manifest.json"), "legacy"),
      writeFile(path.join(runtime, "release", "stale-2.0.1.zip"), "stale"),
    ]);
    await run(process.execPath, [
      path.join(isolatedDesktop, "tools", "stage-electron-runtime.mjs"),
      "--platform", "windows",
      "--arch", "x64",
    ], {
      env: {
        ...process.env,
        EMATE_BOOTSTRAP_DIR_WINDOWS_X64: bootstrap,
        EMATE_RELEASE_DIR: release,
      },
    });
    for (const name of await readdir(path.join(isolatedDesktop, "runtime-bundle"))) {
      await cp(
        path.join(isolatedDesktop, "runtime-bundle", name),
        path.join(runtime, name),
        { recursive: true, force: true },
      );
    }

    const selected = backendContract.packagedReleasePath(resources);
    assert.equal(selected, path.join(runtime, "releases", releaseId));
    assert.deepEqual((await readdir(selected)).sort(), ["core.zip", "release-manifest.json"]);
    assert.equal(await readFile(path.join(runtime, "release", "stale-2.0.1.zip"), "utf8"), "stale");

    await writeFile(path.join(runtime, "current-release"), "../release\n");
    assert.equal(backendContract.packagedReleasePath(resources), null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("public download index parsing only offers newer stable releases", () => {
  const version = "2.0.1";
  const download = (target, platform, architecture, fileName) => ({
    target,
    platform,
    architecture,
    file_name: fileName,
    url: `https://mvdcm.ecoremedia.net/e-mate/update/${fileName}`,
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

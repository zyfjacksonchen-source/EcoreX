import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import updateContract from "../electron/update-contract.cjs";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (relative) => readFile(path.join(desktop, relative), "utf8");

test("desktop identity and unsigned release targets are explicit", async () => {
  const pkg = JSON.parse(await load("package.json"));
  assert.equal(pkg.name, "e-mate-desktop");
  assert.equal(pkg.version, "2.0.0");
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
  const pkg = JSON.parse(await load("package.json"));
  assert.equal(pkg.build.files.some((entry) => entry.includes("renderer")), false);
  assert.equal(pkg.build.extraResources[0].from, "runtime-bundle");
});

test("mac metadata parsing only offers newer stable releases", () => {
  assert.equal(updateContract.parseUpdateVersion("version: 2.0.1\nfiles: []\n"), "2.0.1");
  const digest = Buffer.alloc(64, 7).toString("base64");
  assert.deepEqual(updateContract.parseMacUpdateMetadata([
    "version: 2.0.1",
    "files:",
    "  - url: e-Mate-2.0.1-arm64.zip",
    `    sha512: ${digest}`,
    "    size: 123456",
    "",
  ].join("\n")), {
    version: "2.0.1",
    files: [{ url: "e-Mate-2.0.1-arm64.zip", sha512: digest, size: 123456 }],
  });
  assert.equal(updateContract.parseMacUpdateMetadata([
    "version: 2.0.1",
    "files:",
    "  - url: ../forged.zip",
    `    sha512: ${digest}`,
    "    size: 1",
  ].join("\n")), null);
  assert.equal(updateContract.parseMacUpdateMetadata([
    "version: 2.0.1",
    "files:",
    "  - url: e-Mate-2.0.1-arm64.zip",
    "    sha512: not-a-digest",
    "    size: 1",
  ].join("\n")), null);
  assert.equal(updateContract.isNewerStableVersion("2.0.1", "2.0.0"), true);
  assert.equal(updateContract.isNewerStableVersion("2.0.0", "2.0.0"), false);
  assert.equal(updateContract.isNewerStableVersion("1.9.9", "2.0.0"), false);
  assert.equal(updateContract.isNewerStableVersion("2.1.0-beta.1", "2.0.0"), false);
});

test("unsigned mac updates remain manual while Windows retains electron-updater", async () => {
  const updater = await load("electron/updater.cjs");
  assert.match(updater, /process\.platform !== "win32"/);
  assert.match(updater, /shell\.openExternal\(UPDATE_URL\)/);
  assert.match(updater, /parseMacUpdateMetadata/);
  assert.match(updater, /SHA-512/);
  assert.match(updater, /autoUpdater\.autoDownload = true/);
  assert.match(updater, /autoUpdater\.quitAndInstall/);
  assert.match(updater, /当前 macOS 版本暂未签名/);
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

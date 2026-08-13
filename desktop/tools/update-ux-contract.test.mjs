import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/v1/AppV1.tsx", import.meta.url), "utf8");
const settings = await readFile(
  new URL("../src/v1/components/SettingsDialog.tsx", import.meta.url),
  "utf8",
);
const desktopUpdater = await readFile(new URL("../electron/updater.cjs", import.meta.url), "utf8");
const desktopMain = await readFile(new URL("../electron/main.cjs", import.meta.url), "utf8");
const desktopPreload = await readFile(new URL("../electron/preload.cjs", import.meta.url), "utf8");

test("packaged desktop exposes Electron updater as the only update UI and action", () => {
  assert.doesNotMatch(app, /hasPendingRuntimeUpdate|updateBannerVisible|runtime\.activateUpdate/u);
  assert.doesNotMatch(app, /新版下载与安装进度|label="关闭更新提示"/u);
  assert.doesNotMatch(settings, /runtimeUpdateStatusText|onCheckUpdate|onActivateUpdate/u);
  assert.match(settings, /window\.eMateDesktop\?\.checkForUpdates\?\.\(\)/u);
  assert.match(app, /data-desktop-update-state/u);
});

test("desktop updates keep one feed and require a platform-correct user action", () => {
  assert.match(desktopUpdater, /autoUpdater\.autoDownload = false/u);
  assert.doesNotMatch(desktopUpdater, /autoUpdater\.autoDownload = true/u);
  assert.match(desktopUpdater, /setFeedURL\(\{ provider: "generic", url: UPDATE_URL \}\)/u);
  assert.match(desktopUpdater, /const UPDATE_POLL_MS = 4 \* 60 \* 60 \* 1000/u);
  assert.match(desktopUpdater, /manualInstall: true/u);
  assert.match(desktopUpdater, /UPDATE_URL = "https:\/\/dl\.ecoremedia\.net\/e-mate\/update\/"/u);
  assert.match(desktopMain, /"emate:download-update"/u);
  assert.match(desktopMain, /"emate:install-update"/u);
  assert.match(desktopPreload, /onDesktopUpdateStatus/u);
  assert.match(app, /当前 macOS 版本未签名/u);
  assert.match(app, /打开官方下载页/u);
  assert.match(app, /下载更新/u);
  assert.match(app, /重启并更新/u);
});

const { app, dialog, net, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const { isNewerStableVersion, parseMacUpdateMetadata } = require("./update-contract.cjs");

const UPDATE_URL = "https://mvdcm.ecoremedia.net/e-mate/update/";
let manualWindowsCheck = false;

function windowFor(getWindow) {
  const window = getWindow();
  return window && !window.isDestroyed() ? window : undefined;
}

async function showUpdatePage() {
  await shell.openExternal(UPDATE_URL);
}

async function checkMacUpdate(getWindow, manual = false) {
  try {
    const response = await net.fetch(new URL("latest-mac.yml", UPDATE_URL));
    if (!response.ok) throw new Error("update metadata unavailable");
    const metadata = parseMacUpdateMetadata(await response.text());
    if (!metadata) throw new Error("update metadata is invalid");
    const { version } = metadata;
    if (!version || !isNewerStableVersion(version, app.getVersion())) {
      if (manual) {
        await dialog.showMessageBox(windowFor(getWindow), {
          type: "info",
          title: "e-Mate 更新",
          message: "当前已是最新版本。",
        });
      }
      return;
    }
    const result = await dialog.showMessageBox(windowFor(getWindow), {
      type: "info",
      title: "e-Mate 更新",
      message: `e-Mate ${version} 已发布`,
      detail: `已校验 ${metadata.files.length} 个下载项的文件名、大小和 SHA-512 信息。当前 macOS 版本暂未签名，请打开下载页并手动安装。`,
      buttons: ["打开下载页", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (result.response === 0) await showUpdatePage();
  } catch {
    if (manual) {
      await dialog.showMessageBox(windowFor(getWindow), {
        type: "warning",
        title: "e-Mate 更新",
        message: "暂时无法检查更新。",
      });
    }
  }
}

function installWindowsUpdate() {
  if (process.platform !== "win32") return;
  setImmediate(() => autoUpdater.quitAndInstall(true, true));
}

function initWindowsUpdater(getWindow) {
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;
  autoUpdater.allowPrerelease = false;
  autoUpdater.setFeedURL({ provider: "generic", url: UPDATE_URL });

  autoUpdater.on("update-not-available", async () => {
    if (!manualWindowsCheck) return;
    manualWindowsCheck = false;
    await dialog.showMessageBox(windowFor(getWindow), {
      type: "info",
      title: "e-Mate 更新",
      message: "当前已是最新版本。",
    });
  });
  autoUpdater.on("update-downloaded", async (info) => {
    manualWindowsCheck = false;
    const result = await dialog.showMessageBox(windowFor(getWindow), {
      type: "info",
      title: "e-Mate 更新",
      message: `e-Mate ${info.version} 已准备好`,
      detail: "立即重启以完成更新，或关闭窗口后自动安装。",
      buttons: ["立即重启", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (result.response === 0) installWindowsUpdate();
  });
  autoUpdater.on("error", async () => {
    if (!manualWindowsCheck) return;
    manualWindowsCheck = false;
    await dialog.showMessageBox(windowFor(getWindow), {
      type: "warning",
      title: "e-Mate 更新",
      message: "暂时无法检查更新。",
    });
  });
}

function initDesktopUpdater(getWindow) {
  if (!app.isPackaged) {
    return {
      check: async () => dialog.showMessageBox(windowFor(getWindow), {
        type: "info",
        title: "e-Mate 更新",
        message: "开发模式不检查桌面更新。",
      }),
      openPage: showUpdatePage,
    };
  }
  if (process.platform === "win32") initWindowsUpdater(getWindow);

  const check = async (manual = true) => {
    if (process.platform === "win32") {
      manualWindowsCheck = manual;
      await autoUpdater.checkForUpdates();
      return;
    }
    await checkMacUpdate(getWindow, manual);
  };
  const timer = setTimeout(() => void check(false), 8_000);
  timer.unref();
  return { check, openPage: showUpdatePage };
}

module.exports = { initDesktopUpdater, UPDATE_URL };

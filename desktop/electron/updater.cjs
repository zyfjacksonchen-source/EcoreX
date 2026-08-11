const { app, dialog, net, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const { isNewerStableVersion, parseDownloadIndex } = require("./update-contract.cjs");

const UPDATE_URL = "https://mvdcm.ecoremedia.net/e-mate/update/";
const DOWNLOAD_URL = "https://mvdcm.ecoremedia.net/e-mate/";
const UPDATE_POLL_MS = 4 * 60 * 60 * 1000;
let manualWindowsCheck = false;
let windowsDownloadRequested = false;
let windowsVersion = null;
let currentStatus = null;

function windowFor(getWindow) {
  const window = getWindow();
  return window && !window.isDestroyed() ? window : undefined;
}

async function showUpdatePage() {
  await shell.openExternal(DOWNLOAD_URL);
}

function send(getWindow, status) {
  currentStatus = status;
  windowFor(getWindow)?.webContents.send("emate:update-status", status);
}

async function checkMacUpdate(getWindow, manual = false) {
  send(getWindow, { state: "checking", userInitiated: manual });
  try {
    const response = await net.fetch(new URL("download-index.json", UPDATE_URL));
    if (!response.ok) throw new Error("update metadata unavailable");
    const metadata = parseDownloadIndex(await response.text());
    if (!metadata) throw new Error("update metadata is invalid");
    const { version } = metadata;
    if (!version || !isNewerStableVersion(version, app.getVersion())) {
      send(getWindow, { state: "not-available", userInitiated: manual });
      if (manual) {
        await dialog.showMessageBox(windowFor(getWindow), {
          type: "info",
          title: "e-Mate 更新",
          message: "当前已是最新版本。",
        });
      }
      return;
    }
    send(getWindow, {
      state: "available",
      version,
      platform: "macos",
      manualInstall: true,
      userInitiated: manual,
    });
  } catch (error) {
    send(getWindow, {
      state: "error",
      version: null,
      message: error?.message || "暂时无法检查更新。",
      userInitiated: manual,
    });
  }
}

function installWindowsUpdate() {
  if (process.platform !== "win32") return;
  setImmediate(() => autoUpdater.quitAndInstall(true, true));
}

function initWindowsUpdater(getWindow) {
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;
  autoUpdater.allowPrerelease = false;
  autoUpdater.setFeedURL({ provider: "generic", url: UPDATE_URL });

  autoUpdater.on("checking-for-update", () => {
    send(getWindow, { state: "checking", userInitiated: manualWindowsCheck });
  });
  autoUpdater.on("update-available", (info) => {
    windowsVersion = info.version;
    send(getWindow, {
      state: "available",
      version: info.version,
      platform: "windows",
      manualInstall: false,
      userInitiated: manualWindowsCheck,
    });
    manualWindowsCheck = false;
  });
  autoUpdater.on("update-not-available", async () => {
    const manual = manualWindowsCheck;
    manualWindowsCheck = false;
    send(getWindow, { state: "not-available", userInitiated: manual });
    if (!manual) return;
    await dialog.showMessageBox(windowFor(getWindow), {
      type: "info",
      title: "e-Mate 更新",
      message: "当前已是最新版本。",
    });
  });
  autoUpdater.on("download-progress", (progress) => {
    send(getWindow, {
      state: "downloading",
      version: windowsVersion,
      percent: Math.max(0, Math.min(100, Math.round(progress.percent))),
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    windowsVersion = info.version;
    windowsDownloadRequested = false;
    send(getWindow, { state: "downloaded", version: info.version });
  });
  autoUpdater.on("error", (error) => {
    const userInitiated = manualWindowsCheck || windowsDownloadRequested;
    manualWindowsCheck = false;
    windowsDownloadRequested = false;
    send(getWindow, {
      state: "error",
      version: windowsVersion,
      message: error?.message || "暂时无法检查更新。",
      userInitiated,
    });
  });
}

async function downloadWindowsUpdate(getWindow) {
  if (process.platform !== "win32") return;
  windowsDownloadRequested = true;
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    if (currentStatus?.state !== "error") {
      windowsDownloadRequested = false;
      send(getWindow, {
        state: "error",
        version: windowsVersion,
        message: error?.message || "更新下载失败。",
        userInitiated: true,
      });
    }
  }
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
      download: async () => undefined,
      install: () => undefined,
      status: () => currentStatus,
    };
  }
  if (process.platform === "win32") initWindowsUpdater(getWindow);

  const check = async (manual = true) => {
    if (process.platform === "win32") {
      manualWindowsCheck = manual;
      try {
        await autoUpdater.checkForUpdates();
      } catch (error) {
        if (currentStatus?.state !== "error") {
          manualWindowsCheck = false;
          send(getWindow, {
            state: "error",
            version: windowsVersion,
            message: error?.message || "暂时无法检查更新。",
            userInitiated: manual,
          });
        }
      }
      return;
    }
    await checkMacUpdate(getWindow, manual);
  };
  const firstCheck = setTimeout(() => void check(false), 5_000);
  firstCheck.unref();
  const poll = setInterval(() => void check(false), UPDATE_POLL_MS);
  poll.unref();
  return {
    check,
    openPage: showUpdatePage,
    download: () => downloadWindowsUpdate(getWindow),
    install: installWindowsUpdate,
    status: () => currentStatus,
  };
}

module.exports = { DOWNLOAD_URL, initDesktopUpdater, UPDATE_URL };

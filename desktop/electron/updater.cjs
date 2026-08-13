const { execFile } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { Readable, Transform } = require("node:stream");
const { pipeline } = require("node:stream/promises");
const { promisify } = require("node:util");
const { app, dialog, net } = require("electron");
const { autoUpdater } = require("electron-updater");
const { isNewerStableVersion, parseDownloadIndex } = require("./update-contract.cjs");

const execFileAsync = promisify(execFile);
const UPDATE_URL = "https://dl.ecoremedia.net/e-mate/update/";
const UPDATE_POLL_MS = 4 * 60 * 60 * 1000;
const MAC_DOWNLOAD_TIMEOUT_MS = 30 * 60 * 1000;
let automaticRequest = false;
let manualWindowsCheck = false;
let windowsDownloadRequested = false;
let windowsVersion = null;
let macCandidate = null;
let macDownloaded = null;
let currentStatus = null;

function windowFor(getWindow) {
  const window = getWindow();
  return window && !window.isDestroyed() ? window : undefined;
}

function send(getWindow, status) {
  currentStatus = status;
  windowFor(getWindow)?.webContents.send("emate:update-status", status);
}

function macTarget() {
  return process.arch === "arm64" ? "macos-arm64" : "macos-x64";
}

async function fetchMacCandidate(getWindow, manual = false) {
  send(getWindow, { state: "checking", userInitiated: manual });
  const response = await net.fetch(new URL("download-index.json", UPDATE_URL), {
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error("update metadata unavailable");
  const metadata = parseDownloadIndex(await response.text());
  const download = metadata?.downloads?.find((item) => item.target === macTarget());
  if (!metadata || !download) throw new Error("update metadata is invalid");
  if (!isNewerStableVersion(metadata.version, app.getVersion())) {
    macCandidate = null;
    send(getWindow, { state: "not-available", userInitiated: manual });
    if (manual) {
      await dialog.showMessageBox(windowFor(getWindow), {
        type: "info",
        title: "e-Mate 更新",
        message: "当前已是最新版本。",
      });
    }
    return null;
  }
  macCandidate = { version: metadata.version, download };
  send(getWindow, {
    state: "available",
    version: metadata.version,
    platform: "macos",
    userInitiated: manual,
  });
  return macCandidate;
}

async function checkMacUpdate(getWindow, manual = false) {
  try {
    return await fetchMacCandidate(getWindow, manual);
  } catch (error) {
    send(getWindow, {
      state: "error",
      version: null,
      message: error?.message || "暂时无法检查更新。",
      userInitiated: manual || automaticRequest,
    });
    automaticRequest = false;
    return null;
  }
}

async function downloadMacUpdate(getWindow, candidate = macCandidate) {
  if (process.platform !== "darwin") return;
  if (!candidate) candidate = await checkMacUpdate(getWindow, true);
  if (!candidate) return;
  const { download, version } = candidate;
  const directory = path.join(app.getPath("userData"), "desktop-update", version);
  const target = path.join(directory, download.file_name);
  const temporary = `${target}.part`;
  fs.rmSync(directory, { recursive: true, force: true });
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const hash = crypto.createHash("sha256");
  let received = 0;
  let lastPercent = -1;
  try {
    const response = await net.fetch(download.url, {
      signal: AbortSignal.timeout(MAC_DOWNLOAD_TIMEOUT_MS),
    });
    if (!response.ok || !response.body) throw new Error("update download unavailable");
    const meter = new Transform({
      transform(chunk, _encoding, callback) {
        received += chunk.length;
        if (received > download.size_bytes) return callback(new Error("update download exceeded declared size"));
        hash.update(chunk);
        const percent = Math.floor((received * 100) / download.size_bytes);
        if (percent !== lastPercent) {
          lastPercent = percent;
          send(getWindow, { state: "downloading", version, percent });
        }
        callback(null, chunk);
      },
    });
    await pipeline(
      Readable.fromWeb(response.body),
      meter,
      fs.createWriteStream(temporary, { flags: "wx", mode: 0o600 }),
    );
    if (received !== download.size_bytes || hash.digest("hex") !== download.sha256) {
      throw new Error("update download integrity check failed");
    }
    fs.renameSync(temporary, target);
    macDownloaded = { ...candidate, path: target };
    send(getWindow, { state: "downloaded", version });
  } catch (error) {
    try { fs.rmSync(temporary, { force: true }); } catch { /* No partial download remains. */ }
    send(getWindow, {
      state: "error",
      version,
      message: error?.message || "更新下载失败。",
      userInitiated: true,
    });
    automaticRequest = false;
    throw error;
  }
  if (automaticRequest) await installMacUpdate(getWindow);
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function installedMacAppPath() {
  const current = path.resolve(app.getPath("exe"), "../../..");
  return path.basename(current) === "e-Mate.app" && !current.startsWith("/Volumes/")
    ? current
    : "/Applications/e-Mate.app";
}

function macInstallCommand(staged, target) {
  const incoming = `${target}.emate-update-new`;
  const backup = `${target}.emate-update-backup`;
  return [
    "set -eu",
    `[ -e ${shellQuote(target)} ] || [ ! -e ${shellQuote(backup)} ] || /bin/mv ${shellQuote(backup)} ${shellQuote(target)}`,
    `/bin/rm -rf ${shellQuote(incoming)} ${shellQuote(backup)}`,
    `/usr/bin/ditto --rsrc --extattr --acl ${shellQuote(staged)} ${shellQuote(incoming)}`,
    `[ ! -e ${shellQuote(target)} ] || /bin/mv ${shellQuote(target)} ${shellQuote(backup)}`,
    `if /bin/mv ${shellQuote(incoming)} ${shellQuote(target)}; then /bin/rm -rf ${shellQuote(backup)}; else [ ! -e ${shellQuote(backup)} ] || /bin/mv ${shellQuote(backup)} ${shellQuote(target)}; exit 1; fi`,
  ].join("; ");
}

async function replaceMacApp(staged, target) {
  const command = macInstallCommand(staged, target);
  let parentWritable = true;
  try {
    await fs.promises.access(path.dirname(target), fs.constants.W_OK);
  } catch {
    parentWritable = false;
  }
  if (parentWritable) {
    await execFileAsync("/bin/sh", ["-c", command], { timeout: 5 * 60 * 1000 });
    return;
  }
  const script = "on run argv\ndo shell script (item 1 of argv) with administrator privileges\nend run";
  await execFileAsync("/usr/bin/osascript", ["-e", script, command], { timeout: 5 * 60 * 1000 });
}

async function installMacUpdate(getWindow) {
  if (process.platform !== "darwin" || !macDownloaded) return;
  const update = macDownloaded;
  const root = path.dirname(update.path);
  const mount = path.join(root, "mount");
  const staged = path.join(root, "staged", "e-Mate.app");
  fs.rmSync(mount, { recursive: true, force: true });
  fs.rmSync(path.dirname(staged), { recursive: true, force: true });
  fs.mkdirSync(mount, { recursive: true, mode: 0o700 });
  let mounted = false;
  try {
    await execFileAsync("/usr/bin/hdiutil", ["attach", "-nobrowse", "-readonly", "-mountpoint", mount, update.path], { timeout: 2 * 60 * 1000 });
    mounted = true;
    const source = path.join(mount, "e-Mate.app");
    const sourceMetadata = fs.lstatSync(source);
    if (!sourceMetadata.isDirectory() || sourceMetadata.isSymbolicLink()) throw new Error("update application bundle is invalid");
    fs.mkdirSync(path.dirname(staged), { recursive: true, mode: 0o700 });
    await execFileAsync("/usr/bin/ditto", ["--rsrc", "--extattr", "--acl", source, staged], { timeout: 5 * 60 * 1000 });
    const plist = path.join(staged, "Contents", "Info.plist");
    const [{ stdout: version }, { stdout: bundleId }] = await Promise.all([
      execFileAsync("/usr/libexec/PlistBuddy", ["-c", "Print :CFBundleShortVersionString", plist]),
      execFileAsync("/usr/libexec/PlistBuddy", ["-c", "Print :CFBundleIdentifier", plist]),
    ]);
    if (version.trim() !== update.version || bundleId.trim() !== "net.ecoremedia.emate") {
      throw new Error("update application identity is invalid");
    }
    if (!fs.statSync(path.join(staged, "Contents", "MacOS", "e-Mate")).isFile()) {
      throw new Error("update application executable is missing");
    }
    const target = installedMacAppPath();
    await replaceMacApp(staged, target);
    const executable = path.join(target, "Contents", "MacOS", "e-Mate");
    if (!fs.statSync(executable).isFile()) throw new Error("installed update is incomplete");
    macDownloaded = null;
    automaticRequest = false;
    app.relaunch({ execPath: executable });
    app.quit();
  } catch (error) {
    send(getWindow, {
      state: "error",
      version: update.version,
      message: error?.message || "更新安装失败。",
      userInitiated: true,
    });
    automaticRequest = false;
    throw error;
  } finally {
    if (mounted) {
      try { await execFileAsync("/usr/bin/hdiutil", ["detach", mount, "-force"], { timeout: 60_000 }); } catch { /* OS releases stale mounts on logout/reboot. */ }
    }
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
    send(getWindow, { state: "checking", userInitiated: manualWindowsCheck || automaticRequest });
  });
  autoUpdater.on("update-available", (info) => {
    windowsVersion = info.version;
    send(getWindow, {
      state: "available",
      version: info.version,
      platform: "windows",
      userInitiated: manualWindowsCheck || automaticRequest,
    });
    manualWindowsCheck = false;
    if (automaticRequest) void downloadWindowsUpdate(getWindow);
  });
  autoUpdater.on("update-not-available", async () => {
    const manual = manualWindowsCheck;
    manualWindowsCheck = false;
    automaticRequest = false;
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
    if (automaticRequest) {
      automaticRequest = false;
      installWindowsUpdate();
    }
  });
  autoUpdater.on("error", (error) => {
    const userInitiated = manualWindowsCheck || windowsDownloadRequested || automaticRequest;
    manualWindowsCheck = false;
    windowsDownloadRequested = false;
    automaticRequest = false;
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
  if (windowsDownloadRequested) return;
  windowsDownloadRequested = true;
  try {
    await autoUpdater.downloadUpdate();
  } catch (error) {
    if (currentStatus?.state !== "error") {
      windowsDownloadRequested = false;
      automaticRequest = false;
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
    const unavailable = async () => dialog.showMessageBox(windowFor(getWindow), {
      type: "info",
      title: "e-Mate 更新",
      message: "开发模式不检查桌面更新。",
    });
    return { check: unavailable, download: unavailable, install: unavailable, requestAutomatic: unavailable, status: () => currentStatus };
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
          automaticRequest = false;
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
  const requestAutomatic = async () => {
    automaticRequest = true;
    if (process.platform === "win32") {
      if (currentStatus?.state === "downloaded") return installWindowsUpdate();
      if (currentStatus?.state === "available") return downloadWindowsUpdate(getWindow);
      return check(false);
    }
    if (currentStatus?.state === "downloaded" && macDownloaded) return installMacUpdate(getWindow);
    if (currentStatus?.state === "downloading") return;
    const candidate = await checkMacUpdate(getWindow, false);
    if (!candidate) {
      automaticRequest = false;
      return;
    }
    await downloadMacUpdate(getWindow, candidate);
  };
  const firstCheck = setTimeout(() => void check(false), 5_000);
  firstCheck.unref();
  const poll = setInterval(() => void check(false), UPDATE_POLL_MS);
  poll.unref();
  return {
    check,
    download: () => process.platform === "win32" ? downloadWindowsUpdate(getWindow) : downloadMacUpdate(getWindow),
    install: () => process.platform === "win32" ? installWindowsUpdate() : installMacUpdate(getWindow),
    requestAutomatic,
    status: () => currentStatus,
  };
}

module.exports = {
  initDesktopUpdater,
  installedMacAppPath,
  macInstallCommand,
  UPDATE_URL,
};

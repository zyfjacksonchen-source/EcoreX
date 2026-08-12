const { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, Notification, shell, Tray } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { BackendManager, runtimeResponds } = require("./backend.cjs");
const { externalHttpUrl } = require("./navigation-policy.cjs");
const { readRuntimeBearerToken, TaskNotificationMonitor } = require("./task-notifications.cjs");
const { initDesktopUpdater } = require("./updater.cjs");

const PRODUCT_NAME = "e-Mate";
const DATA_DIR = path.join(os.homedir(), ".emate");
app.setName(PRODUCT_NAME);
app.setPath("userData", DATA_DIR);

let backend = null;
let mainWindow = null;
let tray = null;
let updater = null;
let taskNotifications = null;
let quitting = false;
let runtimeRestart = null;

function eMateIcon() {
  const iconPath = path.join(app.getAppPath(), "src", "v1", "assets", "emate-mark.png");
  if (!fs.existsSync(iconPath)) return undefined;
  const icon = nativeImage.createFromPath(iconPath);
  return icon.isEmpty() ? undefined : icon;
}

function eMateLogoDataUrl() {
  const logoPath = path.join(app.getAppPath(), "src", "v1", "assets", "emate-logo.png");
  return `data:image/png;base64,${fs.readFileSync(logoPath).toString("base64")}`;
}

function focusWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function startupPage() {
  const logo = eMateLogoDataUrl();
  const document = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>e-Mate</title><style>
:root{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;color:#171717}
main{text-align:center;padding:32px}.logo{width:126px;height:40px;margin:0 auto 20px;display:block;object-fit:contain}.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
p{margin:0;color:#6b6b6b;font-size:14px}
.spinner{width:18px;height:18px;margin:22px auto 0;border:2px solid #ff8a0033;border-top-color:#ff8a00;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.spinner{animation:none;border-top-color:#ff8a00}}
@media(prefers-color-scheme:dark){body{background:#171719;color:#f5f5f5}p{color:#a9a9ad}.logo{filter:invert(1) hue-rotate(180deg)}}
</style></head><body><main role="status" aria-live="polite"><img class="logo" src="${logo}" alt="" aria-hidden="true"><h1 class="visually-hidden">e-Mate</h1><p>正在验证并启动企业工作伙伴…</p><div class="spinner" aria-hidden="true"></div></main></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(document)}`;
}

async function startBackendWithRetry(window) {
  while (!quitting) {
    try {
      return await backend.start();
    } catch {
      const result = await dialog.showMessageBox(window, {
        type: "error",
        title: `${PRODUCT_NAME} 启动失败`,
        message: `${PRODUCT_NAME} Runtime 无法启动。`,
        detail: "安装包和原有数据保持不变。你可以重试，或退出后联系企业管理员。",
        buttons: ["重试", "退出"],
        defaultId: 0,
        cancelId: 1,
      });
      if (result.response !== 0) {
        app.quit();
        return null;
      }
      await window.loadURL(startupPage());
    }
  }
  return null;
}

async function restartRuntime() {
  if (runtimeRestart) return runtimeRestart;
  runtimeRestart = (async () => {
    try {
      taskNotifications?.stop();
      taskNotifications = null;
      await mainWindow?.loadURL(startupPage());
      const runtimeOrigin = await backend.restart();
      await mainWindow?.loadURL(runtimeOrigin);
      await startTaskNotifications(runtimeOrigin);
      return true;
    } catch {
      await dialog.showMessageBox(mainWindow ?? undefined, {
        type: "error",
        title: `${PRODUCT_NAME} Runtime`,
        message: "Runtime 重启失败。",
      });
      return false;
    } finally {
      runtimeRestart = null;
    }
  })();
  return runtimeRestart;
}

async function openThreadFromNotification(threadId) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    const window = createWindow(backend.origin);
    await window.loadURL(backend.origin);
  }
  focusWindow();
  mainWindow?.webContents.send("emate:open-thread", threadId);
}

async function startTaskNotifications(runtimeOrigin) {
  taskNotifications?.stop();
  taskNotifications = null;
  try {
    const bearerToken = await readRuntimeBearerToken(runtimeOrigin);
    const refreshBearerToken = async () => {
      if (!await runtimeResponds(backend.port, DATA_DIR)) throw new Error("Runtime owner proof failed.");
      return readRuntimeBearerToken(runtimeOrigin);
    };
    const monitor = new TaskNotificationMonitor({
      origin: runtimeOrigin,
      bearerToken,
      refreshBearerToken,
      NotificationClass: Notification,
      onOpenThread: openThreadFromNotification,
    });
    taskNotifications = monitor;
    await monitor.start();
  } catch {
    taskNotifications = null;
  }
}

function createWindow(runtimeOrigin) {
  const window = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    title: PRODUCT_NAME,
    backgroundColor: "#f6f7f9",
    icon: eMateIcon(),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (url !== runtimeOrigin && !url.startsWith(`${runtimeOrigin}/`)) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    const externalUrl = externalHttpUrl(url, runtimeOrigin);
    if (externalUrl) void shell.openExternal(externalUrl);
    return { action: "deny" };
  });
  window.webContents.on("did-fail-load", (_event, code, description) => {
    console.error(`[e-Mate] Renderer failed to load (${code}: ${description}).`);
  });
  window.once("ready-to-show", () => window.show());
  window.on("close", (event) => {
    if (!quitting && process.platform !== "darwin") {
      event.preventDefault();
      window.hide();
    }
  });
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  mainWindow = window;
  return window;
}

function buildMenu() {
  const appMenu = {
    label: PRODUCT_NAME,
    submenu: [
      {
        label: `关于 ${PRODUCT_NAME}`,
        click: () => void dialog.showMessageBox(mainWindow ?? undefined, {
          type: "info",
          title: PRODUCT_NAME,
          message: `${PRODUCT_NAME} ${app.getVersion()}`,
          detail: "企业智能工作伙伴",
        }),
      },
      { label: "检查更新…", click: () => void updater?.check(true) },
      { label: "重启 Runtime", click: () => void restartRuntime() },
      { type: "separator" },
      ...(process.platform === "darwin" ? [{ role: "hide" }, { role: "hideOthers" }, { type: "separator" }] : []),
      { label: `退出 ${PRODUCT_NAME}`, accelerator: process.platform === "darwin" ? "Cmd+Q" : "Alt+F4", click: () => app.quit() },
    ],
  };
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    appMenu,
    { label: "编辑", submenu: [{ role: "undo" }, { role: "redo" }, { type: "separator" }, { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" }] },
    { label: "窗口", submenu: [{ role: "minimize" }, { role: "zoom" }, ...(process.platform === "darwin" ? [{ role: "front" }] : [{ role: "close" }])] },
  ]));
}

function createTray() {
  if (process.platform === "darwin" || tray) return;
  const icon = eMateIcon();
  if (!icon) return;
  tray = new Tray(icon.resize({ width: 20, height: 20 }));
  tray.setToolTip(PRODUCT_NAME);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `打开 ${PRODUCT_NAME}`, click: focusWindow },
    { label: "检查更新…", click: () => void updater?.check(true) },
    { type: "separator" },
    { label: "退出", click: () => app.quit() },
  ]));
  tray.on("click", focusWindow);
}

function setupIpc() {
  ipcMain.handle("emate:version", () => app.getVersion());
  ipcMain.handle("emate:restart-runtime", restartRuntime);
  ipcMain.handle("emate:check-for-updates", () => updater?.check(true));
  ipcMain.handle("emate:open-update-page", () => updater?.openPage());
  ipcMain.handle("emate:download-update", () => updater?.download());
  ipcMain.handle("emate:install-update", () => updater?.install());
  ipcMain.handle("emate:desktop-update-status", () => updater?.status() ?? null);
}

async function launch() {
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
  if (process.platform === "win32") app.setAppUserModelId("net.ecoremedia.emate");
  backend = new BackendManager({
    packaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    dataDir: DATA_DIR,
  });
  backend.on("exit", (code) => {
    if (!quitting && code === 86) void restartRuntime();
  });
  const window = createWindow(backend.origin);
  await window.loadURL(startupPage());
  updater = initDesktopUpdater(() => mainWindow);
  setupIpc();
  buildMenu();
  createTray();
  const runtimeOrigin = await startBackendWithRetry(window);
  if (!runtimeOrigin) return;
  await window.loadURL(runtimeOrigin);
  await startTaskNotifications(runtimeOrigin);
}

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", focusWindow);
  app.whenReady().then(launch).catch(async () => {
    await dialog.showMessageBox({
      type: "error",
      title: `${PRODUCT_NAME} 启动失败`,
      message: `${PRODUCT_NAME} 桌面壳无法启动。`,
      detail: "安装包和原有数据保持不变，请联系企业管理员。",
    });
    app.quit();
  });
}

app.on("activate", () => {
  if (mainWindow) focusWindow();
  else if (backend) {
    const window = createWindow(backend.origin);
    void window.loadURL(backend.origin);
  }
});

app.on("before-quit", () => {
  quitting = true;
  tray?.destroy();
  tray = null;
  taskNotifications?.stop();
  taskNotifications = null;
  backend?.stop();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && quitting) app.quit();
});

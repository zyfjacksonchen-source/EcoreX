const { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, Notification, shell, Tray } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { fileURLToPath } = require("node:url");
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
let shutdownComplete = false;
let shutdown = null;
const contextTargets = new WeakMap();

const ARTIFACT_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$/;
const DISPLAY_NAME = /^[^/\\\u0000-\u001f\u007f]{1,255}$/u;
const CONTEXT_PARAMS = new WeakMap();

function nativeWindowChrome() {
  if (process.platform === "darwin") {
    return {
      titleBarStyle: "hiddenInset",
      trafficLightPosition: { x: 14, y: 18 },
    };
  }
  if (process.platform === "win32") {
    return {
      titleBarStyle: "hidden",
      titleBarOverlay: { color: "#1c1c1e", symbolColor: "#c7c7cc", height: 48 },
    };
  }
  return {};
}

function safeContextTarget(value) {
  if (!value || typeof value !== "object") return null;
  const artifactId = typeof value.artifactId === "string" ? value.artifactId : "";
  const revisionId = typeof value.revisionId === "string" ? value.revisionId : "";
  const displayName = typeof value.displayName === "string" ? value.displayName : "";
  if (!ARTIFACT_ID.test(artifactId) || !ARTIFACT_ID.test(revisionId) || !DISPLAY_NAME.test(displayName)) return null;
  return { artifactId, revisionId, displayName };
}

function materializedOutputPath(value) {
  if (!value || typeof value !== "object" || !DISPLAY_NAME.test(value.displayName ?? "")) return null;
  const root = value.locationAlias === "documents"
    ? path.join(app.getPath("documents"), "EcoreX")
    : value.locationAlias === "downloads"
      ? path.join(app.getPath("downloads"), "EcoreX")
      : null;
  if (!root) return null;
  const candidate = path.join(root, value.displayName);
  try {
    const stat = fs.lstatSync(candidate);
    return stat.isFile() && !stat.isSymbolicLink() ? candidate : null;
  } catch {
    return null;
  }
}

function nativeContextMenu(window, params) {
  const template = [];
  const separator = () => {
    if (template.length && template.at(-1)?.type !== "separator") template.push({ type: "separator" });
  };
  if (params.isEditable) {
    template.push(
      { role: "undo", enabled: params.editFlags.canUndo },
      { role: "redo", enabled: params.editFlags.canRedo },
      { type: "separator" },
      { role: "cut", enabled: params.editFlags.canCut },
      { role: "copy", enabled: params.editFlags.canCopy },
      { role: "paste", enabled: params.editFlags.canPaste },
      { role: "delete", enabled: params.editFlags.canDelete },
      { type: "separator" },
      { role: "selectAll", enabled: params.editFlags.canSelectAll },
    );
  } else if (params.selectionText) {
    template.push({ role: "copy", label: "复制" });
  }
  if (params.hasImageContents) {
    separator();
    template.push({
      label: "复制图片",
      click: () => window.webContents.copyImageAt(params.x, params.y),
    });
  }
  if (params.linkURL) {
    separator();
    template.push({ label: "复制链接地址", click: () => clipboard.writeText(params.linkURL) });
  }
  const fileUrl = [params.linkURL, params.srcURL].find((value) => value?.startsWith("file:"));
  if (fileUrl) {
    try {
      const filePath = fileURLToPath(fileUrl);
      separator();
      template.push({ label: "复制文件路径", click: () => clipboard.writeText(filePath) });
    } catch {
      // Invalid file URLs are never copied.
    }
  }
  const contextTarget = contextTargets.get(window.webContents);
  if (contextTarget) {
    separator();
    template.push(
      { label: "复制文件名", click: () => clipboard.writeText(contextTarget.displayName) },
      {
        label: "保存并复制文件路径",
        click: () => window.webContents.send("emate:copy-artifact-path", contextTarget),
      },
    );
  }
  contextTargets.delete(window.webContents);
  while (template.at(-1)?.type === "separator") template.pop();
  if (template.length) Menu.buildFromTemplate(template).popup({ window, frame: params.frame ?? undefined });
}

function popupPendingContextMenu(webContents) {
  const pending = CONTEXT_PARAMS.get(webContents);
  if (!pending) return;
  CONTEXT_PARAMS.delete(webContents);
  clearImmediate(pending.fallback);
  nativeContextMenu(pending.window, pending.params);
}

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
:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#171719;color:#f5f5f5}
main{text-align:center;padding:32px}.logo{width:126px;height:40px;margin:0 auto 20px;display:block;object-fit:contain}.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
p{margin:0;color:#6b6b6b;font-size:14px}
.spinner{width:18px;height:18px;margin:22px auto 0;border:2px solid #ff8a0033;border-top-color:#ff8a00;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.spinner{animation:none;border-top-color:#ff8a00}}
p{color:#a9a9ad}.logo{filter:invert(1) hue-rotate(180deg)}
</style></head><body><main role="status" aria-live="polite"><img class="logo" src="${logo}" alt="" aria-hidden="true"><h1 class="visually-hidden">e-Mate</h1><p>正在验证并启动企业工作伙伴…</p><div class="spinner" aria-hidden="true"></div></main></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(document)}`;
}

async function startBackendWithRetry(window) {
  while (!quitting) {
    try {
      return await backend.start();
    } catch (error) {
      console.error(`[e-Mate] Runtime startup failed: ${error instanceof Error ? error.message : "Unknown failure."}`);
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
    backgroundColor: "#171719",
    icon: eMateIcon(),
    show: false,
    ...nativeWindowChrome(),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.on("context-menu", (_event, params) => {
    const pending = { window, params, fallback: null };
    pending.fallback = setImmediate(() => popupPendingContextMenu(window.webContents));
    CONTEXT_PARAMS.set(window.webContents, pending);
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
  ipcMain.on("emate:context-target", (event, value) => {
    if (event.sender !== mainWindow?.webContents) return;
    const target = safeContextTarget(value);
    if (target) contextTargets.set(event.sender, target);
    else contextTargets.delete(event.sender);
    popupPendingContextMenu(event.sender);
  });
  ipcMain.handle("emate:copy-materialized-path", (event, value) => {
    if (event.sender !== mainWindow?.webContents) return false;
    const outputPath = materializedOutputPath(value);
    if (!outputPath) return false;
    // Electron has no cross-platform file-list clipboard API. Copying the
    // canonical path lets Finder/Explorer and native apps resolve the file.
    clipboard.writeText(outputPath);
    return true;
  });
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
  const runtimeStartup = startBackendWithRetry(window);
  await window.loadURL(startupPage());
  updater = initDesktopUpdater(() => mainWindow);
  setupIpc();
  buildMenu();
  createTray();
  const runtimeOrigin = await runtimeStartup;
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

app.on("before-quit", (event) => {
  if (shutdownComplete) return;
  event.preventDefault();
  if (shutdown) return;
  quitting = true;
  tray?.destroy();
  tray = null;
  taskNotifications?.stop();
  taskNotifications = null;
  shutdown = (async () => {
    try {
      await backend?.stop();
    } finally {
      shutdownComplete = true;
      app.quit();
    }
  })();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && quitting) app.quit();
});

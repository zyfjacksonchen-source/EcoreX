import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, shell } from "electron";
import fsp from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { fetchSidecarJson } from "./apiBridge.js";
import { CapabilityManager } from "./capabilities.js";
import { EnterpriseAuthManager } from "./enterpriseAuth.js";
import { PermissionManager, type PermissionMode } from "./permissions.js";
import { resolveRepoRoot, SidecarManager } from "./sidecar.js";
import { TelemetryReporter } from "./telemetry.js";
import { EcorexUpdateManager } from "./updater.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = Boolean(process.env.VITE_DEV_SERVER_URL);
const hasSingleInstanceLock = app.requestSingleInstanceLock();
const runtimeRoot = resolveRepoRoot(__dirname);
const sidecar = new SidecarManager(runtimeRoot);
const capabilities = new CapabilityManager(runtimeRoot, __dirname);
const telemetry = new TelemetryReporter(runtimeRoot);
const permissions = new PermissionManager();
const enterpriseAuth = new EnterpriseAuthManager(runtimeRoot);
const updates = new EcorexUpdateManager(sidecar);
nativeTheme.themeSource = "dark";

if (!hasSingleInstanceLock) {
  app.quit();
}

const imageExtensions = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"]);
const videoExtensions = new Set([".mp4", ".webm", ".avi", ".mov", ".mkv"]);
const externalUrlProtocols = new Set(["http:", "https:", "mailto:"]);

function toAttachment(filePath: string) {
  const ext = path.extname(filePath).toLowerCase();
  const fileType = imageExtensions.has(ext) ? "image" : videoExtensions.has(ext) ? "video" : "file";
  return {
    file_path: filePath,
    file_name: path.basename(filePath),
    file_type: fileType
  };
}

function safeFileName(name: string, fallback: string) {
  const cleaned = path.basename(name || fallback).replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_");
  return cleaned || fallback;
}

function stableProjectId(folderPath: string) {
  const normalized = path.resolve(folderPath).toLowerCase();
  return `project-${createHash("sha1").update(normalized).digest("hex").slice(0, 16)}`;
}

function applyWindowTheme(theme: "light" | "dark") {
  nativeTheme.themeSource = theme;
  const color = theme === "dark" ? "#17110d" : "#fff9f2";
  const symbolColor = theme === "dark" ? "#f8efe7" : "#1d140e";
  for (const win of BrowserWindow.getAllWindows()) {
    win.setBackgroundColor(color);
    if (process.platform === "win32") {
      try {
        win.setTitleBarOverlay({ color, symbolColor, height: 32 });
      } catch {
        // Older Electron/window modes may not support overlay updates.
      }
    }
  }
}

function safeOpenExternal(rawUrl: string) {
  try {
    const parsed = new URL(String(rawUrl || ""));
    if (!externalUrlProtocols.has(parsed.protocol)) {
      console.warn(`[EcoreX] blocked external URL protocol: ${parsed.protocol}`);
      return false;
    }
    void shell.openExternal(parsed.toString());
    return true;
  } catch {
    console.warn("[EcoreX] blocked malformed external URL");
    return false;
  }
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 760,
    minWidth: 960,
    minHeight: 660,
    title: "EcoreX",
    autoHideMenuBar: process.platform !== "darwin",
    show: false,
    backgroundColor: "#17110d",
    ...(process.platform === "win32"
      ? {
          titleBarStyle: "hidden" as const,
          titleBarOverlay: { color: "#17110d", symbolColor: "#f8efe7", height: 32 }
        }
      : process.platform === "darwin"
        ? {
            titleBarStyle: "hiddenInset" as const,
            trafficLightPosition: { x: 14, y: 10 }
          }
        : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  win.once("ready-to-show", () => {
    win.show();
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    safeOpenExternal(url);
    return { action: "deny" };
  });

  if (isDev && process.env.VITE_DEV_SERVER_URL) {
    void win.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    void win.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

function focusMainWindow() {
  const win = BrowserWindow.getAllWindows()[0];
  if (!win) {
    return;
  }
  if (win.isMinimized()) {
    win.restore();
  }
  win.show();
  win.focus();
}

function installApplicationMenu() {
  const isMac = process.platform === "darwin";
  const template: Electron.MenuItemConstructorOptions[] = [
    ...(isMac
      ? [{
          label: "EcoreX",
          submenu: [
            { role: "about", label: "关于 EcoreX" },
            { type: "separator" },
            { role: "services", label: "服务" },
            { type: "separator" },
            { role: "hide", label: "隐藏 EcoreX" },
            { role: "hideOthers", label: "隐藏其他" },
            { role: "unhide", label: "全部显示" },
            { type: "separator" },
            { role: "quit", label: "退出 EcoreX" }
          ]
        } satisfies Electron.MenuItemConstructorOptions]
      : []),
    {
      label: "文件",
      submenu: [
        { role: "close", label: isMac ? "关闭窗口" : "退出" }
      ]
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" }
      ]
    },
    {
      label: "视图",
      submenu: [
        { role: "reload", label: "重新载入" },
        { role: "forceReload", label: "强制重新载入" },
        { role: "toggleDevTools", label: "开发者工具" },
        { type: "separator" },
        { role: "resetZoom", label: "实际大小" },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "进入/退出全屏" }
      ]
    },
    {
      label: "窗口",
      submenu: [
        { role: "minimize", label: "最小化" },
        { role: "zoom", label: "缩放" },
        ...(isMac
          ? [
              { type: "separator" as const },
              { role: "front" as const, label: "前置所有窗口" }
            ]
          : [
              { role: "close" as const, label: "关闭" }
            ])
      ]
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "EcoreX 官网",
          click: () => {
            safeOpenExternal("https://www.ecoreai.cn/ecorex-agent/");
          }
        }
      ]
    }
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.on("second-instance", () => {
  focusMainWindow();
});

app.whenReady().then(async () => {
  if (!hasSingleInstanceLock) {
    return;
  }
  app.setName("EcoreX");
  installApplicationMenu();
  updates.init();
  ipcMain.handle("ecorex:get-sidecar-status", () => sidecar.getStatus());
  ipcMain.handle("ecorex:set-window-theme", (_event, theme: "light" | "dark") => {
    applyWindowTheme(theme === "dark" ? "dark" : "light");
    return { ok: true };
  });
  ipcMain.handle("ecorex:sidecar-json", (_event, request) => fetchSidecarJson(sidecar, request));
  ipcMain.handle("ecorex:check-for-updates", () => updates.checkForUpdates());
  ipcMain.handle("ecorex:get-update-status", () => updates.getStatus());
  ipcMain.handle("ecorex:install-downloaded-update", () => updates.installDownloadedUpdate());
  ipcMain.handle("ecorex:open-download-page", () => updates.openDownloadPage());
  ipcMain.handle("ecorex:list-capability-packs", () => capabilities.listPacks());
  ipcMain.handle("ecorex:get-telemetry-state", () => telemetry.getState());
  ipcMain.handle("ecorex:report-telemetry", (_event, event) => telemetry.report(event));
  ipcMain.handle("ecorex:refresh-enterprise-policy", () => sidecar.refreshEnterpriseModelConfig());
  ipcMain.handle("ecorex:get-enterprise-session", () => enterpriseAuth.getSessionView());
  ipcMain.handle("ecorex:enterprise-login", async (_event, input: { email: string; password: string }) => {
    const session = await enterpriseAuth.login(input);
    await sidecar.refreshEnterpriseModelConfig();
    return enterpriseAuth.toSessionView(session);
  });
  ipcMain.handle("ecorex:enterprise-logout", async () => {
    await enterpriseAuth.logout();
    sidecar.clearEnterpriseModelConfig("已退出登录，模型策略已清空");
    return { ok: true };
  });
  ipcMain.handle("ecorex:enterprise-change-password", async (_event, input: { oldPassword: string; newPassword: string }) => {
    const session = await enterpriseAuth.changePassword(input);
    return enterpriseAuth.toSessionView(session);
  });
  ipcMain.handle("ecorex:check-enterprise-quota", (_event, estimatedTokens: number) =>
    enterpriseAuth.checkQuota(estimatedTokens)
  );
  ipcMain.handle("ecorex:get-permission-state", () => permissions.getState());
  ipcMain.handle("ecorex:set-permission-mode", (_event, mode: PermissionMode) => permissions.setMode(mode));
  ipcMain.handle("ecorex:reset-permission-grants", () => permissions.resetGrants());
  ipcMain.handle("ecorex:choose-files", async () => {
    const result = await dialog.showOpenDialog({
      title: "选择要交给 EcoreX 的文件",
      properties: ["openFile", "multiSelections"]
    });
    if (result.canceled) {
      return [];
    }
    await permissions.rememberSelectedPaths(result.filePaths);
    return result.filePaths.map(toAttachment);
  });
  ipcMain.handle("ecorex:choose-project-folder", async () => {
    const result = await dialog.showOpenDialog({
      title: "选择项目文件夹",
      properties: ["openDirectory", "createDirectory"]
    });
    if (result.canceled || !result.filePaths[0]) {
      return null;
    }
    const folderPath = await fsp.realpath(result.filePaths[0]).catch(() => path.resolve(result.filePaths[0]));
    const projectStateDir = path.join(folderPath, ".ecorex");
    const projectMemoryPath = path.join(projectStateDir, "project-memory.md");
    const projectDreamsPath = path.join(projectStateDir, "dreams");
    await fsp.mkdir(projectDreamsPath, { recursive: true });
    await fsp.writeFile(
      projectMemoryPath,
      `# Project Memory\n\nEcoreX stores project-specific summaries here. Keep this file concise and do not duplicate global user memory.\n`,
      { encoding: "utf8", flag: "wx" }
    ).catch(() => undefined);
    await permissions.rememberSelectedPaths([folderPath]);
    return {
      id: stableProjectId(folderPath),
      name: path.basename(folderPath) || folderPath,
      path: folderPath,
      memoryPath: projectMemoryPath,
      dreamsPath: projectDreamsPath,
      updatedAt: new Date().toISOString()
    };
  });
  ipcMain.handle("ecorex:save-pasted-file", async (_event, input: { fileName?: string; mimeType?: string; dataBase64: string }) => {
    const approxBytes = Math.ceil((input.dataBase64.length * 3) / 4);
    if (approxBytes > 50 * 1024 * 1024) {
      throw new Error("粘贴文件超过 50 MB，请使用附件按钮选择文件。");
    }
    const pastedDir = path.join(app.getPath("userData"), "pasted-files");
    await fsp.mkdir(pastedDir, { recursive: true });
    const fileName = safeFileName(input.fileName || `paste-${Date.now()}`, `paste-${Date.now()}`);
    const filePath = path.join(pastedDir, `${Date.now()}-${fileName}`);
    await fsp.writeFile(filePath, Buffer.from(input.dataBase64, "base64"));
    await permissions.rememberSelectedPaths([filePath]);
    return toAttachment(filePath);
  });
  ipcMain.handle("ecorex:open-path", async (event, filePath: string, action: "open" | "reveal" | "openWith" = "open") => {
    if (!filePath || !path.isAbsolute(filePath)) {
      return "invalid path";
    }
    const decision = await permissions.authorizeOpenPath(event, filePath);
    if (!decision.allowed) {
      return `denied: ${decision.reason}`;
    }
    let result = "";
    if (action === "reveal") {
      shell.showItemInFolder(filePath);
    } else if (action === "openWith" && process.platform === "win32") {
      const { spawn } = await import("node:child_process");
      spawn("rundll32.exe", ["shell32.dll,OpenAs_RunDLL", filePath], {
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }).unref();
    } else {
      result = await shell.openPath(filePath);
    }
    await permissions.writeOpenResult(filePath, result);
    return result;
  });

  await sidecar.refreshEnterpriseModelConfig();
  sidecar.start();
  createMainWindow();
  setTimeout(() => {
    void updates.checkForUpdates().catch(() => undefined);
  }, 5000);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    sidecar.stop();
    app.quit();
  }
});

app.on("before-quit", () => {
  sidecar.stop();
});

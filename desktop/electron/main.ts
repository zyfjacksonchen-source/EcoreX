import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import fsp from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { fetchSidecarJson } from "./apiBridge.js";
import { CapabilityManager } from "./capabilities.js";
import { EnterpriseAuthManager } from "./enterpriseAuth.js";
import { PermissionManager, type PermissionMode } from "./permissions.js";
import { resolveRepoRoot, SidecarManager } from "./sidecar.js";
import { TelemetryReporter } from "./telemetry.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = Boolean(process.env.VITE_DEV_SERVER_URL);
const runtimeRoot = resolveRepoRoot(__dirname);
const sidecar = new SidecarManager(runtimeRoot);
const capabilities = new CapabilityManager(runtimeRoot, __dirname);
const telemetry = new TelemetryReporter(runtimeRoot);
const permissions = new PermissionManager();
const enterpriseAuth = new EnterpriseAuthManager(runtimeRoot);

const imageExtensions = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"]);
const videoExtensions = new Set([".mp4", ".webm", ".avi", ".mov", ".mkv"]);

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

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 760,
    minWidth: 960,
    minHeight: 660,
    title: "EcoreX",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  win.once("ready-to-show", () => {
    win.show();
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  if (isDev && process.env.VITE_DEV_SERVER_URL) {
    void win.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    void win.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

app.whenReady().then(async () => {
  app.setName("EcoreX");
  ipcMain.handle("ecorex:get-sidecar-status", () => sidecar.getStatus());
  ipcMain.handle("ecorex:sidecar-json", (_event, request) => fetchSidecarJson(sidecar, request));
  ipcMain.handle("ecorex:list-capability-packs", () => capabilities.listPacks());
  ipcMain.handle("ecorex:install-capability-pack", (_event, packId: string) => capabilities.installPack(packId));
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
  ipcMain.handle("ecorex:open-path", async (event, filePath: string) => {
    if (!filePath || !path.isAbsolute(filePath)) {
      return "invalid path";
    }
    const result = await shell.openPath(filePath);
    await permissions.writeOpenResult(filePath, result);
    return result;
  });

  await sidecar.refreshEnterpriseModelConfig();
  sidecar.start();
  createMainWindow();

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

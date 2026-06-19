import { contextBridge, ipcRenderer, nativeTheme } from "electron";
import type { CapabilityPack } from "./capabilities.js";
import type { PermissionMode, PermissionState } from "./permissions.js";
import type { SidecarStatus } from "./sidecar.js";

contextBridge.exposeInMainWorld("ecorexDesktop", {
  platform: process.platform,
  shouldUseDarkColors: nativeTheme?.shouldUseDarkColors ?? false,
  setWindowTheme: (theme: "light" | "dark") => ipcRenderer.invoke("ecorex:set-window-theme", theme) as Promise<unknown>,
  getSidecarStatus: () => ipcRenderer.invoke("ecorex:get-sidecar-status") as Promise<SidecarStatus>,
  checkForUpdates: () => ipcRenderer.invoke("ecorex:check-for-updates") as Promise<unknown>,
  getUpdateStatus: () => ipcRenderer.invoke("ecorex:get-update-status") as Promise<unknown>,
  installDownloadedUpdate: () => ipcRenderer.invoke("ecorex:install-downloaded-update") as Promise<unknown>,
  openDownloadPage: () => ipcRenderer.invoke("ecorex:open-download-page") as Promise<unknown>,
  listCapabilityPacks: () => ipcRenderer.invoke("ecorex:list-capability-packs") as Promise<CapabilityPack[]>,
  getPermissionState: () => ipcRenderer.invoke("ecorex:get-permission-state") as Promise<PermissionState>,
  setPermissionMode: (mode: PermissionMode) =>
    ipcRenderer.invoke("ecorex:set-permission-mode", mode) as Promise<PermissionState>,
  resetPermissionGrants: () => ipcRenderer.invoke("ecorex:reset-permission-grants") as Promise<PermissionState>,
  getTelemetryState: () => ipcRenderer.invoke("ecorex:get-telemetry-state") as Promise<{
    configured: boolean;
    eventsUrl: string;
    deviceId: string;
    userEmail?: string;
    orgId?: string;
    lastError?: string;
  }>,
  getEnterpriseSession: () => ipcRenderer.invoke("ecorex:get-enterprise-session") as Promise<unknown>,
  enterpriseLogin: (input: { email: string; password: string }) =>
    ipcRenderer.invoke("ecorex:enterprise-login", input) as Promise<unknown>,
  enterpriseLogout: () => ipcRenderer.invoke("ecorex:enterprise-logout") as Promise<unknown>,
  enterpriseChangePassword: (input: { oldPassword: string; newPassword: string }) =>
    ipcRenderer.invoke("ecorex:enterprise-change-password", input) as Promise<unknown>,
  checkEnterpriseQuota: (estimatedTokens: number) =>
    ipcRenderer.invoke("ecorex:check-enterprise-quota", estimatedTokens) as Promise<unknown>,
  refreshEnterprisePolicy: () => ipcRenderer.invoke("ecorex:refresh-enterprise-policy") as Promise<{
    configured: boolean;
    changed: boolean;
    restarted: boolean;
    message: string;
    model?: string;
    provider?: string;
    updatedAt?: string;
  }>,
  reportTelemetry: (event: {
    type: "usage" | "error" | "warn" | "info";
    source?: string;
    message?: string;
    category?: string;
    label?: string;
    amount?: number;
    sessionId?: string;
    tool?: string;
    detail?: Record<string, unknown>;
  }) => ipcRenderer.invoke("ecorex:report-telemetry", event) as Promise<unknown>,
  chooseFiles: () =>
    ipcRenderer.invoke("ecorex:choose-files") as Promise<
      Array<{ file_path: string; file_name: string; file_type: "image" | "video" | "file" }>
    >,
  chooseProjectFolder: () => ipcRenderer.invoke("ecorex:choose-project-folder") as Promise<{
    id: string;
    name: string;
    path: string;
    memoryPath?: string;
    dreamsPath?: string;
    updatedAt: string;
  } | null>,
  savePastedFile: (input: { fileName?: string; mimeType?: string; dataBase64: string }) =>
    ipcRenderer.invoke("ecorex:save-pasted-file", input) as Promise<{
      file_path: string;
      file_name: string;
      file_type: "image" | "video" | "file" | "directory";
    }>,
  statPath: (filePath: string) =>
    ipcRenderer.invoke("ecorex:stat-path", filePath) as Promise<{
      status?: string;
      message?: string;
      path: string;
      exists: boolean;
      isFile?: boolean;
      isDirectory?: boolean;
      mimeType?: string;
      sizeBytes?: number;
    }>,
  openPath: (filePath: string, action?: "open" | "reveal" | "openWith") =>
    ipcRenderer.invoke("ecorex:open-path", filePath, action || "open") as Promise<string>,
  apiJson: (request: { path: string; method?: "GET" | "POST" | "PUT" | "DELETE"; body?: unknown }) =>
    ipcRenderer.invoke("ecorex:sidecar-json", request) as Promise<unknown>,
  onSidecarStatus: (listener: (status: SidecarStatus) => void) => {
    const wrapped = (_event: Electron.IpcRendererEvent, status: SidecarStatus) => listener(status);
    ipcRenderer.on("ecorex:sidecar-status", wrapped);
    return () => ipcRenderer.off("ecorex:sidecar-status", wrapped);
  },
  onUpdateStatus: (listener: (status: unknown) => void) => {
    const wrapped = (_event: Electron.IpcRendererEvent, status: unknown) => listener(status);
    ipcRenderer.on("ecorex:update-status", wrapped);
    return () => ipcRenderer.off("ecorex:update-status", wrapped);
  }
});

import { contextBridge, ipcRenderer, nativeTheme } from "electron";
import type { CapabilityPack } from "./capabilities.js";
import type { PermissionMode, PermissionState } from "./permissions.js";
import type { SidecarStatus } from "./sidecar.js";

contextBridge.exposeInMainWorld("ecorexDesktop", {
  platform: process.platform,
  shouldUseDarkColors: nativeTheme.shouldUseDarkColors,
  getSidecarStatus: () => ipcRenderer.invoke("ecorex:get-sidecar-status") as Promise<SidecarStatus>,
  listCapabilityPacks: () => ipcRenderer.invoke("ecorex:list-capability-packs") as Promise<CapabilityPack[]>,
  installCapabilityPack: (packId: string) =>
    ipcRenderer.invoke("ecorex:install-capability-pack", packId) as Promise<CapabilityPack>,
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
  savePastedFile: (input: { fileName?: string; mimeType?: string; dataBase64: string }) =>
    ipcRenderer.invoke("ecorex:save-pasted-file", input) as Promise<{
      file_path: string;
      file_name: string;
      file_type: "image" | "video" | "file" | "directory";
    }>,
  openPath: (filePath: string) => ipcRenderer.invoke("ecorex:open-path", filePath) as Promise<string>,
  apiJson: (request: { path: string; method?: "GET" | "POST" | "PUT" | "DELETE"; body?: unknown }) =>
    ipcRenderer.invoke("ecorex:sidecar-json", request) as Promise<unknown>,
  onSidecarStatus: (listener: (status: SidecarStatus) => void) => {
    const wrapped = (_event: Electron.IpcRendererEvent, status: SidecarStatus) => listener(status);
    ipcRenderer.on("ecorex:sidecar-status", wrapped);
    return () => ipcRenderer.off("ecorex:sidecar-status", wrapped);
  }
});

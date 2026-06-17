import { app, BrowserWindow, shell } from "electron";
import electronUpdater from "electron-updater";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import type { SidecarManager } from "./sidecar.js";
import { fetchSidecarJson } from "./apiBridge.js";

const { autoUpdater } = electronUpdater;

type UpdateState =
  | "idle"
  | "checking"
  | "available"
  | "not-available"
  | "downloading"
  | "downloaded"
  | "blocked"
  | "installing"
  | "error";

export type DesktopUpdateStatus = {
  state: UpdateState;
  platform: NodeJS.Platform;
  currentVersion: string;
  version?: string;
  message: string;
  downloadUrl?: string;
  releaseDate?: string;
  progress?: number;
  activeRequests?: number;
  checkedAt?: string;
};

const DOWNLOAD_PAGE_URL = "https://www.ecoreai.cn/ecorex-agent/";
const MANIFEST_URL = "https://www.ecoreai.cn/ecorex-agent/manifest.json";
const UPDATE_FEED_URL = "https://www.ecoreai.cn/ecorex-agent/downloads/";

type UpdateEndpointPolicy = {
  updateFeedUrl?: string;
  updateManifestUrl?: string;
  downloadPageUrl?: string;
};

function readUpdateEndpointPolicy(filePath: string): UpdateEndpointPolicy {
  if (!filePath) return {};
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "")) as UpdateEndpointPolicy;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return {
      updateFeedUrl: stringValue(parsed.updateFeedUrl),
      updateManifestUrl: stringValue(parsed.updateManifestUrl),
      downloadPageUrl: stringValue(parsed.downloadPageUrl)
    };
  } catch {
    return {};
  }
}

function stringValue(value: unknown) {
  const text = String(value || "").trim();
  return text || undefined;
}

function normalizeUrl(value?: string, options: { directory?: boolean } = {}) {
  const text = stringValue(value);
  if (!text) return "";
  if (/^[a-z][a-z0-9+.-]*:/i.test(text)) {
    return options.directory && !text.endsWith("/") ? `${text}/` : text;
  }
  if (path.isAbsolute(text)) {
    const target = options.directory && !/[\\/]$/.test(text) ? `${text}${path.sep}` : text;
    return pathToFileURL(target).href;
  }
  return text;
}

function isMissingAppUpdateConfig(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || "");
  return /app-update\.ya?ml/i.test(message) && /ENOENT|no such file/i.test(message);
}

function isMissingUpdateFeed(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || "");
  const statusCode = Number((error as { statusCode?: unknown } | null)?.statusCode || 0);
  return (
    statusCode === 404 ||
    (/latest\.ya?ml/i.test(message) && /404|not found|Cannot find channel/i.test(message))
  );
}

function publicUpdateUnavailableStatus(): Partial<DesktopUpdateStatus> {
  return {
    state: "not-available",
    message: "当前自动更新通道暂未发布，请打开下载页获取最新版本"
  };
}

function safeUpdateErrorMessage(error: unknown) {
  if (isMissingAppUpdateConfig(error)) {
    return "当前测试包未配置自动更新通道";
  }
  if (isMissingUpdateFeed(error)) {
    return publicUpdateUnavailableStatus().message || "当前自动更新通道暂未发布";
  }
  const message = error instanceof Error ? error.message : String(error || "");
  if (/HttpError|at ElectronHttpExecutor|builder-util-runtime|app\.asar|latest\.ya?ml/i.test(message)) {
    return "更新检查失败，请打开下载页获取最新版本";
  }
  return message || "更新检查失败";
}

function compareVersions(left: string, right: string) {
  const a = String(left || "0").split(/[.-]/).map((part) => Number.parseInt(part, 10) || 0);
  const b = String(right || "0").split(/[.-]/).map((part) => Number.parseInt(part, 10) || 0);
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    const diff = (a[index] || 0) - (b[index] || 0);
    if (diff !== 0) return diff > 0 ? 1 : -1;
  }
  return 0;
}

function platformArtifactId(platform: NodeJS.Platform) {
  if (platform === "win32") return "windows-x64";
  if (platform === "darwin") return process.arch === "x64" ? "macos-x64-dmg" : "macos-arm64-dmg";
  return "";
}

function absoluteDownloadUrl(href: string | undefined, downloadPageUrl: string) {
  if (!href) return downloadPageUrl;
  try {
    return new URL(href, downloadPageUrl).href;
  } catch {
    return downloadPageUrl;
  }
}

export class EcorexUpdateManager {
  private status: DesktopUpdateStatus = {
    state: "idle",
    platform: process.platform,
    currentVersion: app.getVersion(),
    message: "尚未检查更新"
  };

  private initialized = false;
  private endpoints: UpdateEndpointPolicy | null = null;

  constructor(private readonly sidecar: SidecarManager) {}

  init() {
    if (this.initialized) return;
    this.initialized = true;

    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = false;
    const feedUrl = this.updateFeedUrl();
    if (feedUrl) {
      autoUpdater.setFeedURL({ provider: "generic", url: feedUrl });
    }
    autoUpdater.on("checking-for-update", () => {
      this.setStatus({ state: "checking", message: "正在检查新版本" });
    });
    autoUpdater.on("update-available", (info) => {
      this.setStatus({
        state: "available",
        version: info.version,
        releaseDate: info.releaseDate,
        message: `发现新版本 ${info.version}，正在后台下载`
      });
    });
    autoUpdater.on("update-not-available", (info) => {
      this.setStatus({
        state: "not-available",
        version: info.version,
        releaseDate: info.releaseDate,
        message: "当前已经是最新版本"
      });
    });
    autoUpdater.on("download-progress", (progress) => {
      this.setStatus({
        state: "downloading",
        progress: Math.max(0, Math.min(100, progress.percent || 0)),
        message: `正在下载更新 ${Math.round(progress.percent || 0)}%`
      });
    });
    autoUpdater.on("update-downloaded", (info) => {
      this.setStatus({
        state: "downloaded",
        version: info.version,
        releaseDate: info.releaseDate,
        progress: 100,
        message: `新版本 ${info.version} 已下载，重启后安装`
      });
    });
    autoUpdater.on("error", (error) => {
      if (isMissingAppUpdateConfig(error)) {
        this.setStatus({
          state: "not-available",
          message: "当前测试包未配置自动更新通道"
        });
        return;
      }
      if (isMissingUpdateFeed(error)) {
        this.setStatus(publicUpdateUnavailableStatus());
        return;
      }
      this.setStatus({
        state: "error",
        message: safeUpdateErrorMessage(error)
      });
    });
  }

  getStatus() {
    return this.status;
  }

  async checkForUpdates() {
    this.init();
    this.setStatus({ state: "checking", message: "正在检查新版本" });

    if (process.platform === "win32" && app.isPackaged) {
      try {
        await autoUpdater.checkForUpdates();
        return this.status;
      } catch (error) {
        if (isMissingAppUpdateConfig(error)) {
          this.setStatus({
            state: "not-available",
            message: "当前测试包未配置自动更新通道"
          });
          return this.status;
        }
        if (isMissingUpdateFeed(error)) {
          this.setStatus(publicUpdateUnavailableStatus());
          return this.status;
        }
        this.setStatus({
          state: "error",
          message: safeUpdateErrorMessage(error)
        });
        return this.status;
      }
    }

    return this.checkManifestOnly();
  }

  async installDownloadedUpdate() {
    this.init();
    if (process.platform !== "win32") {
      void shell.openExternal(this.downloadPageUrl());
      this.setStatus({
        state: "available",
        message: "macOS 需要手动下载新版本安装包"
      });
      return this.status;
    }
    if (this.status.state !== "downloaded") {
      return this.checkForUpdates();
    }

    const activeRequests = await this.countActiveRequests();
    if (activeRequests > 0) {
      this.setStatus({
        state: "blocked",
        activeRequests,
        message: `当前还有 ${activeRequests} 个任务运行中，完成或取消后再安装更新`
      });
      return this.status;
    }

    this.setStatus({ state: "installing", activeRequests: 0, message: "正在重启并安装更新" });
    autoUpdater.quitAndInstall(false, true);
    return this.status;
  }

  openDownloadPage() {
    const url = this.downloadPageUrl();
    void shell.openExternal(url);
    return { ok: true, url };
  }

  private async checkManifestOnly() {
    try {
      const separator = this.manifestUrl().includes("?") ? "&" : "?";
      const response = await fetch(`${this.manifestUrl()}${separator}t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`manifest HTTP ${response.status}`);
      }
      const manifest = await response.json() as {
        version?: string;
        artifacts?: Array<{ id?: string; href?: string; status?: string }>;
      };
      const latestVersion = String(manifest.version || "");
      const artifactId = platformArtifactId(process.platform);
      const artifact = (manifest.artifacts || []).find((item) =>
        item.id === artifactId && (item.status === "ready" || item.status === "ready-unsigned")
      );
      const hasUpdate = Boolean(artifact && latestVersion && compareVersions(latestVersion, app.getVersion()) > 0);
      this.setStatus({
        state: hasUpdate ? "available" : "not-available",
        version: latestVersion || app.getVersion(),
        downloadUrl: absoluteDownloadUrl(artifact?.href, this.downloadPageUrl()),
        message: hasUpdate
          ? `发现新版本 ${latestVersion}，请前往下载页安装`
          : "当前已经是最新版本"
      });
      return this.status;
    } catch (error) {
      this.setStatus({
        state: "error",
        message: error instanceof Error ? error.message : String(error)
      });
      return this.status;
    }
  }

  private async countActiveRequests() {
    const payload = await fetchSidecarJson(this.sidecar, { path: "/api/active-requests" });
    if (!payload || typeof payload !== "object") return 0;
    const requests = Array.isArray((payload as { requests?: unknown[] }).requests)
      ? (payload as { requests: unknown[] }).requests
      : [];
    return requests.filter((request) => {
      if (!request || typeof request !== "object") return false;
      const state = String((request as Record<string, unknown>).state || "running");
      return state !== "cancelled" && state !== "done";
    }).length;
  }

  private resolveEndpointPolicy() {
    if (this.endpoints) {
      return this.endpoints;
    }
    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      process.resourcesPath ? path.join(process.resourcesPath, "enterprise-policy.json") : "",
      process.resourcesPath ? path.join(process.resourcesPath, "ecorex-runtime", "enterprise-policy.json") : ""
    ].filter(Boolean);
    const filePolicy = candidates.reduce<UpdateEndpointPolicy>((merged, candidate) => {
      return { ...merged, ...readUpdateEndpointPolicy(candidate) };
    }, {});
    this.endpoints = {
      ...filePolicy,
      updateFeedUrl: stringValue(process.env.ECOREX_UPDATE_FEED_URL) || filePolicy.updateFeedUrl,
      updateManifestUrl: stringValue(process.env.ECOREX_UPDATE_MANIFEST_URL) || filePolicy.updateManifestUrl,
      downloadPageUrl: stringValue(process.env.ECOREX_DOWNLOAD_PAGE_URL) || filePolicy.downloadPageUrl
    };
    return this.endpoints;
  }

  private updateFeedUrl() {
    return normalizeUrl(this.resolveEndpointPolicy().updateFeedUrl, { directory: true }) || UPDATE_FEED_URL;
  }

  private manifestUrl() {
    return normalizeUrl(this.resolveEndpointPolicy().updateManifestUrl) || MANIFEST_URL;
  }

  private downloadPageUrl() {
    return normalizeUrl(this.resolveEndpointPolicy().downloadPageUrl) || DOWNLOAD_PAGE_URL;
  }

  private setStatus(partial: Partial<DesktopUpdateStatus>) {
    this.status = {
      ...this.status,
      ...partial,
      platform: process.platform,
      currentVersion: app.getVersion(),
      checkedAt: new Date().toISOString()
    };
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send("ecorex:update-status", this.status);
    }
  }
}

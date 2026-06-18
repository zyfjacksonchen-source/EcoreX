import { app, BrowserWindow } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { enterpriseClientEventKeys, enterpriseRequestHeaders, normalizeEnterpriseDeviceId, resolveEnterprisePolicy, type EnterprisePolicy } from "./enterprisePolicy.js";

export type SidecarState = "starting" | "running" | "stopped" | "failed" | "skipped";

export type SidecarStatus = {
  state: SidecarState;
  message: string;
  pid?: number;
  webPort: number;
};

type EnterpriseSession = {
  token: string;
  expiresAt: string;
  user?: {
    email?: string;
  };
  deviceId?: string;
};

type ModelPolicyPayload = {
  configured?: boolean;
  provider?: string;
  model?: string;
  updatedAt?: string;
  settings?: Record<string, unknown>;
};

type EnterpriseModelRefresh = {
  configured: boolean;
  changed: boolean;
  restarted: boolean;
  message: string;
  model?: string;
  provider?: string;
  updatedAt?: string;
};

const DEFAULT_WEB_PORT = 9899;

export class SidecarManager {
  private child: ChildProcessWithoutNullStreams | null = null;
  private enterpriseEnv: NodeJS.ProcessEnv = {};
  private enterpriseConfigHash = "";
  private policy: EnterprisePolicy | null = null;
  private stoppingIntentionally = false;
  private intentionallyStoppedChildren = new WeakSet<ChildProcessWithoutNullStreams>();
  private restartAttempts = 0;
  private restartTimer: NodeJS.Timeout | null = null;
  private readonly maxRestartAttempts = 3;
  private status: SidecarStatus = {
    state: "stopped",
    message: "sidecar 未启动",
    webPort: DEFAULT_WEB_PORT
  };

  constructor(private readonly repoRoot: string) {}

  getStatus() {
    return this.status;
  }

  private getState(): SidecarState {
    return this.status.state;
  }

  getBaseUrl() {
    return `http://127.0.0.1:${this.getWebPort()}`;
  }

  async waitUntilReady(timeoutMs = 30000) {
    if (this.getState() === "running") {
      return true;
    }
    if (this.getState() === "failed" || this.getState() === "stopped" || this.getState() === "skipped") {
      return false;
    }

    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (this.getState() === "running") {
        return true;
      }
      if (this.getState() === "failed" || this.getState() === "stopped" || this.getState() === "skipped") {
        return false;
      }
      if (await this.probeHttpReady(this.getWebPort())) {
        this.updateStatus({
          state: "running",
          message: "EcoreX local runtime is ready",
          pid: this.child?.pid,
          webPort: this.getWebPort()
        });
        return true;
      }
      await this.delay(300);
    }
    return this.getState() === "running";
  }

  start() {
    if (process.env.ECOREX_SKIP_SIDECAR === "1") {
      this.updateStatus({
        state: "skipped",
        message: "已按环境变量跳过 sidecar 启动",
        webPort: this.getWebPort()
      });
      return;
    }

    if (this.child) {
      return;
    }

    this.stoppingIntentionally = false;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }

    const python = this.resolvePython();
    if (!python) {
      this.updateStatus({
        state: "failed",
        message: "未找到 EcoreX 内置运行时，请重新安装或联系管理员获取完整安装包",
        webPort: this.getWebPort()
      });
      return;
    }
    const webPort = this.getWebPort();
    this.ensureDesktopRuntimeDefaults();

    this.updateStatus({
      state: "starting",
      message: "正在启动 EcoreX 兼容运行时",
      webPort
    });

    this.child = spawn(python, ["app.py"], {
      cwd: this.repoRoot,
      env: {
        ...process.env,
        ...this.enterpriseEnv,
        ECOREX_DESKTOP: "1",
        ECOREX_DESKTOP_USER_DATA: app.getPath("userData"),
        PATH: [this.resolveExternalCliPath(), process.env.PATH].filter(Boolean).join(path.delimiter),
        PYTHONPATH: [this.repoRoot, this.resolveCapabilityPythonPath(), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        PLAYWRIGHT_BROWSERS_PATH:
          process.platform === "darwin"
            ? path.join(app.getPath("userData"), "capabilities", "playwright-browsers")
            : process.env.PLAYWRIGHT_BROWSERS_PATH,
        PYTHONNOUSERSITE: "1",
        WEB_PORT: String(webPort)
      },
      windowsHide: true
    });

    const launchedChild = this.child;

    launchedChild.once("spawn", () => {
      this.updateStatus({
        state: "starting",
        message: "EcoreX 兼容运行时已启动",
        pid: launchedChild.pid,
        webPort
      });
      void this.markReadyWhenAvailable(webPort, launchedChild.pid);
    });

    launchedChild.once("error", (error) => {
      if (this.child === launchedChild) {
        this.child = null;
      }
      this.updateStatus({
        state: "failed",
        message: `sidecar 启动失败：${error.message}`,
        webPort
      });
    });

    launchedChild.once("exit", (code, signal) => {
      const reason = signal ? String(signal) : String(code ?? "unknown");
      const stoppedIntentionally = this.stoppingIntentionally || this.intentionallyStoppedChildren.has(launchedChild);
      if (this.child === launchedChild) {
        this.child = null;
      }
      if (stoppedIntentionally) {
        if (!this.child) {
          this.updateStatus({
            state: "stopped",
            message: "EcoreX local runtime stopped",
            webPort
          });
        }
        return;
      }
      if (this.restartAttempts < this.maxRestartAttempts) {
        this.scheduleRestart(webPort, reason);
        return;
      }
      this.updateStatus({
        state: "failed",
        message: signal ? `sidecar 已退出：${signal}` : `sidecar 已退出：${code ?? "unknown"}`,
        webPort
      });
    });

    launchedChild.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8").trim();
      if (text) {
        this.updateStatus({
          ...this.status,
          message: text.slice(0, 240)
        });
      }
    });
  }

  stop() {
    this.stoppingIntentionally = true;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (!this.child) {
      return;
    }
    this.intentionallyStoppedChildren.add(this.child);
    this.child.kill();
    this.child = null;
    this.updateStatus({
      state: "stopped",
      message: "sidecar 已停止",
      webPort: this.getWebPort()
    });
  }

  async refreshEnterpriseModelConfig(): Promise<EnterpriseModelRefresh> {
    const policy = this.loadPolicy();
    const modelConfigUrl = policy.modelConfigUrl || this.deriveModelConfigUrl(policy.adminEventsUrl);
    if (!modelConfigUrl || !policy.clientEventKey) {
      return {
        configured: false,
        changed: false,
        restarted: false,
        message: "enterprise model policy is not configured"
      };
    }

    const session = this.loadSession();
    if (!session) {
      return this.clearEnterpriseModelConfig("enterprise model policy requires login");
    }

    try {
      const payload = await this.fetchModelPolicyWithKeyFallback(modelConfigUrl, policy, session);
      await this.saveCachedModelPolicy(payload);
      return this.applyModelPolicyPayload(payload, "enterprise model policy refreshed", "enterprise model policy unchanged");
    } catch (error) {
      const cached = this.readCachedModelPolicy();
      if (cached?.settings) {
        const result = this.applyModelPolicyPayload(cached, "using cached enterprise model policy", "cached enterprise model policy unchanged");
        return {
          ...result,
          message: `${result.message}; refresh failed: ${error instanceof Error ? error.message : String(error)}`
        };
      }
      return {
        configured: true,
        changed: false,
        restarted: false,
        message: error instanceof Error ? error.message : String(error)
      };
    }
  }

  private async fetchModelPolicyWithKeyFallback(modelConfigUrl: string, policy: EnterprisePolicy, session: EnterpriseSession) {
    const keys = enterpriseClientEventKeys(policy);
    for (const [index, clientEventKey] of keys.entries()) {
      const response = await fetch(modelConfigUrl, {
        headers: enterpriseRequestHeaders({
          clientEventKey,
          userEmail: session?.user?.email || policy.userEmail,
          userToken: session?.token,
          deviceId: session?.deviceId || this.resolveDeviceId(policy),
          orgId: policy.orgId,
          authorizationToken: session?.token
        })
      });
      const payload = (await response.json().catch(() => ({}))) as ModelPolicyPayload & { error?: string; message?: string };
      if (response.status === 403 && this.isInvalidClientKeyPayload(payload) && index < keys.length - 1) {
        continue;
      }
      if (!response.ok) {
        throw new Error(`model policy HTTP ${response.status}${payload.error ? `: ${payload.error}` : ""}`);
      }
      return payload;
    }
    throw new Error("model policy HTTP 403: invalid client key");
  }

  private isInvalidClientKeyPayload(payload: { error?: string; message?: string }) {
    return `${payload.error || ""} ${payload.message || ""}`.toLowerCase().includes("invalid client key");
  }

  clearEnterpriseModelConfig(message = "enterprise model policy cleared"): EnterpriseModelRefresh {
    const hadConfig = Boolean(Object.keys(this.enterpriseEnv).length || this.enterpriseConfigHash);
    this.enterpriseEnv = {};
    this.enterpriseConfigHash = "";
    const wasRunning = Boolean(this.child);
    if (wasRunning) {
      this.stop();
      this.start();
    }
    return {
      configured: false,
      changed: hadConfig,
      restarted: wasRunning,
      message
    };
  }

  private applyModelPolicyPayload(payload: ModelPolicyPayload, changedMessage: string, unchangedMessage: string): EnterpriseModelRefresh {
    const settings = payload.configured && payload.settings ? payload.settings : {};
    const nextEnv = Object.fromEntries(
      Object.entries(settings)
        .filter(([, value]) => value !== undefined && value !== null && String(value).length > 0)
        .map(([key, value]) => [key, String(value)])
    );
    const nextHash = JSON.stringify({
      updatedAt: payload.updatedAt || "",
      settings: nextEnv
    });
    const changed = nextHash !== this.enterpriseConfigHash;
    if (!changed) {
      return {
        configured: Boolean(payload.configured),
        changed: false,
        restarted: false,
        message: payload.configured ? unchangedMessage : "enterprise model policy empty",
        model: payload.model,
        provider: payload.provider,
        updatedAt: payload.updatedAt
      };
    }

    this.enterpriseEnv = nextEnv;
    this.enterpriseConfigHash = nextHash;
    const wasRunning = Boolean(this.child);
    if (wasRunning) {
      this.stop();
      this.start();
    }
    return {
      configured: Boolean(payload.configured),
      changed: true,
      restarted: wasRunning,
      message: payload.configured ? changedMessage : "enterprise model policy cleared",
      model: payload.model,
      provider: payload.provider,
      updatedAt: payload.updatedAt
    };
  }

  private getWebPort() {
    const raw = Number(process.env.ECOREX_WEB_PORT || process.env.WEB_PORT || DEFAULT_WEB_PORT);
    return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_WEB_PORT;
  }

  private async markReadyWhenAvailable(webPort: number, pid?: number) {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      if (!this.child || (pid && this.child.pid !== pid)) {
        return;
      }
      if (await this.probeHttpReady(webPort)) {
        this.restartAttempts = 0;
        this.updateStatus({
          state: "running",
          message: "EcoreX local runtime is ready",
          pid,
          webPort
        });
        return;
      }
      await this.delay(500);
    }

    if (this.child && (!pid || this.child.pid === pid)) {
      this.updateStatus({
        state: "starting",
        message: "EcoreX local runtime is still starting",
        pid,
        webPort
      });
    }
  }

  private async probeHttpReady(webPort: number) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1200);
    try {
      const response = await fetch(`http://127.0.0.1:${webPort}/api/version`, {
        signal: controller.signal
      });
      return response.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  private delay(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  private scheduleRestart(webPort: number, reason: string) {
    this.restartAttempts += 1;
    this.updateStatus({
      state: "starting",
      message: `EcoreX local runtime exited (${reason}); restarting ${this.restartAttempts}/${this.maxRestartAttempts}`,
      webPort
    });
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
    }
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      this.start();
    }, 1200);
  }

  private ensureDesktopRuntimeDefaults() {
    const configPath = path.join(this.repoRoot, "config.json");
    const templatePath = path.join(this.repoRoot, "config-template.json");
    try {
      const sourcePath = fs.existsSync(configPath) ? configPath : templatePath;
      const config = JSON.parse(fs.readFileSync(sourcePath, "utf8").replace(/^\uFEFF/, "")) as Record<string, unknown>;
      const oldDefaultPersona = "You are a helpful AI assistant. You aim to answer and solve any questions people have, and can communicate in multiple languages.";
      const desktopPersona = "You are EcoreX, the desktop AI Agent for Yixin Advertising. Keep a professional, rigorous, concise tone. Address the user as tongxue. Always identify as EcoreX. Confirm goals and constraints first, then provide executable steps. When using tools, files, web search, Skills, or MCP, clearly explain the reason and result.";
      const defaults: Record<string, unknown> = {
        channel_type: "web",
        agent: true,
        knowledge: true,
        self_evolution_enabled: false,
        scheduler_enabled: false,
        mcp_auto_start: false,
        agent_max_context_tokens: 258000,
        appdata_dir: path.join(app.getPath("userData"), "runtime-appdata")
      };
      let changed = !fs.existsSync(configPath);
      for (const [key, value] of Object.entries(defaults)) {
        if (config[key] !== value) {
          config[key] = value;
          changed = true;
        }
      }
      const currentPersona = typeof config.character_desc === "string" ? config.character_desc.trim() : "";
      const legacyPersonaMarker = currentPersona.toUpperCase().includes("COW");
      if (!currentPersona || currentPersona === oldDefaultPersona || legacyPersonaMarker) {
        config.character_desc = desktopPersona;
        changed = true;
      }
      if (config.agent_max_context_tokens === 50000) {
        config.agent_max_context_tokens = 258000;
        changed = true;
      }
      const currentWorkspace = typeof config.agent_workspace === "string" ? config.agent_workspace.trim() : "";
      if (!currentWorkspace || currentWorkspace === "~/cow" || currentWorkspace === "~\\cow") {
        config.agent_workspace = "~/EcoreX";
        changed = true;
      }
      const tools = typeof config.tools === "object" && config.tools !== null && !Array.isArray(config.tools)
        ? config.tools as Record<string, unknown>
        : {};
      const browser = typeof tools.browser === "object" && tools.browser !== null && !Array.isArray(tools.browser)
        ? tools.browser as Record<string, unknown>
        : {};
      const browserDefaults: Record<string, unknown> = {
        cdp_endpoint: "http://127.0.0.1:9222",
        cdp_auto_launch: false,
        cdp_fallback: true,
        persistent: true
      };
      for (const [key, value] of Object.entries(browserDefaults)) {
        if (browser[key] === undefined || browser[key] === "") {
          browser[key] = value;
          changed = true;
        }
      }
      if (tools.browser !== browser) {
        tools.browser = browser;
        changed = true;
      }
      const feishuCli = typeof tools.feishu_cli === "object" && tools.feishu_cli !== null && !Array.isArray(tools.feishu_cli)
        ? tools.feishu_cli as Record<string, unknown>
        : {};
      if (!feishuCli.package) {
        feishuCli.package = "@larksuite/cli@1.0.40";
        changed = true;
      }
      if (feishuCli.auto_install === undefined || feishuCli.auto_install === true) {
        feishuCli.auto_install = false;
        changed = true;
      }
      if (tools.feishu_cli !== feishuCli) {
        tools.feishu_cli = feishuCli;
        changed = true;
      }
      if (config.tools !== tools) {
        config.tools = tools;
        changed = true;
      }
      const chromeDevtoolsMcp = {
        name: "chrome-devtools",
        type: "stdio",
        command: process.platform === "win32" ? "npx.cmd" : "npx",
        args: ["chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222", "--no-usage-statistics"],
        timeout: 30
      };
      const mcpServers = Array.isArray(config.mcp_servers) ? config.mcp_servers : [];
      const hasChromeDevtoolsMcp = mcpServers.some((server) => (
        typeof server === "object" &&
        server !== null &&
        !Array.isArray(server) &&
        ((server as Record<string, unknown>).name === "chrome-devtools" ||
          String((server as Record<string, unknown>).command || "").includes("chrome-devtools-mcp"))
      ));
      if (process.platform === "win32") {
        for (const server of mcpServers) {
          if (
            typeof server === "object" &&
            server !== null &&
            !Array.isArray(server) &&
            String((server as Record<string, unknown>).command || "").trim().toLowerCase() === "npx"
          ) {
            (server as Record<string, unknown>).command = "npx.cmd";
            changed = true;
          }
        }
      }
      for (const server of mcpServers) {
        if (typeof server !== "object" || server === null || Array.isArray(server)) {
          continue;
        }
        const item = server as Record<string, unknown>;
        const args = Array.isArray(item.args) ? item.args.map((part) => String(part)) : [];
        const isChromeDevtools =
          item.name === "chrome-devtools" ||
          String(item.command || "").includes("chrome-devtools-mcp") ||
          args.join(" ").includes("chrome-devtools-mcp");
        const usesAutoConnect = args.includes("--autoConnect") || args.includes("--auto-connect");
        if (isChromeDevtools && (!args.length || usesAutoConnect)) {
          item.args = ["chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222", "--no-usage-statistics"];
          changed = true;
        }
      }
      if (!hasChromeDevtoolsMcp) {
        config.mcp_servers = [...mcpServers, chromeDevtoolsMcp];
        changed = true;
      }
      if (changed) {
        fs.writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, "utf8");
      }
    } catch (error) {
      console.warn("[EcoreX] Failed to ensure desktop runtime defaults", error);
    }
  }

  private resolvePython() {
    if (process.env.ECOREX_PYTHON) {
      return process.env.ECOREX_PYTHON;
    }

    const candidates =
      process.platform === "win32"
        ? [
            path.join(this.repoRoot, "python", "python.exe"),
            path.join(this.repoRoot, "desktop", "runtime", "ecorex-runtime", "python", "python.exe"),
            path.join(this.repoRoot, "desktop", "release", "win-unpacked", "resources", "ecorex-runtime", "python", "python.exe")
          ]
        : [
            path.join(this.repoRoot, "python", "bin", "python3"),
            path.join(this.repoRoot, "python", "bin", "python"),
            path.join(this.repoRoot, "desktop", "runtime", "ecorex-runtime", "python", "bin", "python3"),
            path.join(this.repoRoot, "desktop", "runtime", "ecorex-runtime", "python", "bin", "python"),
            path.join(this.repoRoot, "desktop", "release", "mac", "EcoreX.app", "Contents", "Resources", "ecorex-runtime", "python", "bin", "python3"),
            path.join(this.repoRoot, "desktop", "release", "mac", "EcoreX.app", "Contents", "Resources", "ecorex-runtime", "python", "bin", "python")
          ];

    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }

    if (process.env.NODE_ENV !== "production") {
      return process.platform === "win32" ? "python" : "python3";
    }

    return "";
  }

  private loadPolicy(): EnterprisePolicy {
    if (this.policy) {
      return this.policy;
    }
    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      path.join(this.repoRoot, "enterprise-policy.json"),
      process.resourcesPath ? path.join(process.resourcesPath, "enterprise-policy.json") : ""
    ].filter(Boolean);
    this.policy = resolveEnterprisePolicy(candidates);
    return this.policy;
  }

  private loadSession(): EnterpriseSession | null {
    try {
      const raw = fs.readFileSync(path.join(app.getPath("userData"), "enterprise-session.json"), "utf8");
      const session = JSON.parse(raw) as EnterpriseSession;
      if (!session?.token || !session.expiresAt) {
        return null;
      }
      const expiresAt = Date.parse(session.expiresAt);
      if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
        return null;
      }
      return session;
    } catch {
      return null;
    }
  }

  private readCachedModelPolicy(): ModelPolicyPayload | null {
    try {
      return JSON.parse(fs.readFileSync(this.modelPolicyCachePath(), "utf8")) as ModelPolicyPayload;
    } catch {
      return null;
    }
  }

  private async saveCachedModelPolicy(payload: ModelPolicyPayload) {
    try {
      fs.mkdirSync(path.dirname(this.modelPolicyCachePath()), { recursive: true });
      fs.writeFileSync(this.modelPolicyCachePath(), JSON.stringify(payload, null, 2), "utf8");
    } catch {
      // A cache failure must not block startup.
    }
  }

  private modelPolicyCachePath() {
    return path.join(app.getPath("userData"), "enterprise-model-policy.json");
  }

  private resolveDeviceId(policy: EnterprisePolicy) {
    return normalizeEnterpriseDeviceId(policy.deviceId || `${os.hostname()}-${process.platform}`);
  }

  private deriveModelConfigUrl(adminEventsUrl?: string) {
    if (!adminEventsUrl) {
      return "";
    }
    return adminEventsUrl.replace(/\/client\/events\/?$/, "/client/model-config");
  }

  private resolveCapabilityPythonPath() {
    if (process.platform === "darwin") {
      return path.join(app.getPath("userData"), "capabilities", "python-site");
    }
    return "";
  }

  private resolveExternalCliPath() {
    const candidates: string[] = [];
    if (process.platform === "win32") {
      const appData = process.env.APPDATA;
      const localAppData = process.env.LOCALAPPDATA;
      const programFiles = process.env.ProgramFiles;
      const programFilesX86 = process.env["ProgramFiles(x86)"];
      if (appData) candidates.push(path.join(appData, "npm"));
      if (localAppData) candidates.push(path.join(localAppData, "npm"));
      if (programFiles) candidates.push(path.join(programFiles, "nodejs"));
      if (programFilesX86) candidates.push(path.join(programFilesX86, "nodejs"));
    } else {
      candidates.push(
        path.join(os.homedir(), ".npm-global", "bin"),
        path.join(os.homedir(), ".npm", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin"
      );
    }
    candidates.push(
      path.join(this.repoRoot, "bin"),
      path.join(this.repoRoot, "tools", "bin"),
      path.join(this.repoRoot, "node", "bin"),
      path.join(this.repoRoot, "tools", "lark-cli", "bin")
    );
    return candidates.filter((candidate) => {
      try {
        return Boolean(candidate && fs.existsSync(candidate));
      } catch {
        return false;
      }
    }).join(path.delimiter);
  }

  private updateStatus(status: SidecarStatus) {
    this.status = status;
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send("ecorex:sidecar-status", status);
    }
  }
}

export function resolveRepoRoot(electronDir: string) {
  if (process.env.ECOREX_REPO_ROOT) {
    return process.env.ECOREX_REPO_ROOT;
  }
  if (process.env.NODE_ENV === "production" && process.resourcesPath) {
    return path.join(process.resourcesPath, "ecorex-runtime");
  }
  if (app.isPackaged && process.resourcesPath) {
    return path.join(process.resourcesPath, "ecorex-runtime");
  }
  return path.resolve(electronDir, "..", "..");
}

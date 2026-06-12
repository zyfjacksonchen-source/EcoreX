import { app, BrowserWindow } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { resolveEnterprisePolicy, type EnterprisePolicy } from "./enterprisePolicy.js";

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
  private status: SidecarStatus = {
    state: "stopped",
    message: "sidecar 未启动",
    webPort: DEFAULT_WEB_PORT
  };

  constructor(private readonly repoRoot: string) {}

  getStatus() {
    return this.status;
  }

  getBaseUrl() {
    return `http://127.0.0.1:${this.getWebPort()}`;
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

    this.child.once("spawn", () => {
      this.updateStatus({
        state: "running",
        message: "EcoreX 兼容运行时已启动",
        pid: this.child?.pid,
        webPort
      });
    });

    this.child.once("error", (error) => {
      this.child = null;
      this.updateStatus({
        state: "failed",
        message: `sidecar 启动失败：${error.message}`,
        webPort
      });
    });

    this.child.once("exit", (code, signal) => {
      this.child = null;
      this.updateStatus({
        state: code === 0 ? "stopped" : "failed",
        message: signal ? `sidecar 已退出：${signal}` : `sidecar 已退出：${code ?? "unknown"}`,
        webPort
      });
    });

    this.child.stderr.on("data", (chunk: Buffer) => {
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
    if (!this.child) {
      return;
    }
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
      const response = await fetch(modelConfigUrl, {
        headers: {
          "X-EcoreX-Client-Key": policy.clientEventKey,
          "X-EcoreX-User-Email": session?.user?.email || policy.userEmail || "",
          "X-EcoreX-User-Token": session?.token || "",
          "X-EcoreX-Device-Id": session?.deviceId || this.resolveDeviceId(policy),
          "X-EcoreX-Org-Id": policy.orgId || "",
          ...(session?.token ? { Authorization: `Bearer ${session.token}` } : {})
        }
      });
      if (!response.ok) {
        throw new Error(`model policy HTTP ${response.status}`);
      }
      const payload = (await response.json()) as ModelPolicyPayload;
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

  private ensureDesktopRuntimeDefaults() {
    const configPath = path.join(this.repoRoot, "config.json");
    const templatePath = path.join(this.repoRoot, "config-template.json");
    try {
      const sourcePath = fs.existsSync(configPath) ? configPath : templatePath;
      const config = JSON.parse(fs.readFileSync(sourcePath, "utf8").replace(/^\uFEFF/, "")) as Record<string, unknown>;
      const oldDefaultPersona = "You are a helpful AI assistant. You aim to answer and solve any questions people have, and can communicate in multiple languages.";
      const desktopPersona = "你是 EcoreX，亦芯广告的桌面端 AI Agent。默认沟通风格专业、严谨、克制，称呼用户为“同学”。回答时先确认目标和约束，再给出可执行步骤；需要使用工具、读写文件、联网搜索、调用 Skill 或 MCP 时，清晰说明原因与结果。";
      const defaults: Record<string, unknown> = {
        channel_type: "web",
        agent: true,
        knowledge: true,
        self_evolution_enabled: true
      };
      let changed = !fs.existsSync(configPath);
      for (const [key, value] of Object.entries(defaults)) {
        if (config[key] !== value) {
          config[key] = value;
          changed = true;
        }
      }
      const currentPersona = typeof config.character_desc === "string" ? config.character_desc.trim() : "";
      if (!currentPersona || currentPersona === oldDefaultPersona) {
        config.character_desc = desktopPersona;
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
        ? [path.join(this.repoRoot, "python", "python.exe")]
        : [
            path.join(this.repoRoot, "python", "bin", "python3"),
            path.join(this.repoRoot, "python", "bin", "python")
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
    if (policy.deviceId) {
      return policy.deviceId;
    }
    return `${os.hostname()}-${process.platform}`;
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

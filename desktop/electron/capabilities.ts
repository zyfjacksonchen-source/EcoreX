import { app } from "electron";
import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export type CapabilityState =
  | "installed"
  | "not-installed"
  | "checking"
  | "installing"
  | "busy"
  | "failed"
  | "unknown";

type CapabilityPolicyMode = "ask" | "preinstall" | "disabled";

export type CapabilityPack = {
  id: string;
  name: string;
  summary: string;
  installMode: "user-or-admin" | "admin-recommended";
  estimatedSizeMb?: number;
  requirements?: string[];
  moduleChecks?: string[];
  postInstallCommands?: string[];
  failureHint?: string;
  state: CapabilityState;
  message: string;
  installed: boolean;
  logPath?: string;
  missingModules?: string[];
  updatedAt?: string;
  policyMode?: CapabilityPolicyMode;
  policyStatus?: string;
  policyUpdatedAt?: string;
};

type RawPack = Omit<
  CapabilityPack,
  "state" | "message" | "installed" | "logPath" | "missingModules" | "updatedAt" | "policyMode" | "policyStatus" | "policyUpdatedAt"
>;

type StatusFile = {
  state?: CapabilityState;
  message?: string;
  installed?: boolean;
  logPath?: string;
  missingModules?: string[];
  updatedAt?: string;
};

type ProcessResult = {
  exitCode: number | null;
  stdout: string;
  stderr: string;
};

type EnterprisePolicy = {
  adminEventsUrl?: string;
  modelConfigUrl?: string;
  capabilityPolicyUrl?: string;
  clientEventKey?: string;
  userEmail?: string;
  deviceId?: string;
  orgId?: string;
};

type EnterpriseSession = {
  token: string;
  expiresAt: string;
  user?: {
    email?: string;
  };
  deviceId?: string;
};

type CapabilityPolicy = {
  mirror?: string;
  mode?: CapabilityPolicyMode;
  offlineCache?: string;
  updatedAt?: string;
};

type CapabilityPolicyPayload = {
  ok?: boolean;
  policy?: CapabilityPolicy;
  capabilities?: Array<{
    id: string;
    name?: string;
    mode?: string;
    size?: string;
    status?: string;
    updatedAt?: string;
  }>;
  updatedAt?: string;
};

async function fileExists(filePath: string) {
  try {
    await fsp.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function readJson<T>(filePath: string): T | null {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as T;
  } catch {
    return null;
  }
}

function usableIndexUrl(value: unknown) {
  const text = String(value || "").trim();
  return /^https?:\/\//i.test(text) ? text : "";
}

function usableFindLinks(value: unknown) {
  const text = String(value || "").trim();
  if (/^https?:\/\//i.test(text) || /^file:/i.test(text) || path.isAbsolute(text)) {
    return text;
  }
  return "";
}

function runProcess(command: string, args: string[], options: { cwd?: string; env?: NodeJS.ProcessEnv; timeoutMs?: number }) {
  return new Promise<ProcessResult>((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      windowsHide: true
    });
    let stdout = "";
    let stderr = "";
    const timeout = options.timeoutMs
      ? setTimeout(() => {
          child.kill();
          reject(new Error(`Timed out running ${command}`));
        }, options.timeoutMs)
      : null;

    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    child.once("error", (error) => {
      if (timeout) clearTimeout(timeout);
      reject(error);
    });
    child.once("exit", (exitCode) => {
      if (timeout) clearTimeout(timeout);
      resolve({ exitCode, stdout, stderr });
    });
  });
}

export class CapabilityManager {
  private enterprisePolicy: EnterprisePolicy | null = null;
  private capabilityPolicy: CapabilityPolicyPayload | null = null;

  constructor(
    private readonly runtimeRoot: string,
    private readonly electronDir: string
  ) {}

  async listPacks(): Promise<CapabilityPack[]> {
    const enterprisePolicy = await this.refreshEnterpriseCapabilityPolicy();
    const policy = enterprisePolicy.policy || {};
    const policyPacks = new Map((enterprisePolicy.capabilities || []).map((pack) => [pack.id, pack]));
    const manifestPath = await this.resolveManifestPath();
    const raw = JSON.parse(await fsp.readFile(manifestPath, "utf8")) as { packs?: RawPack[] };
    const rawPacks = Array.isArray(raw.packs) ? raw.packs : [];
    const moduleState = await this.checkInstalled(rawPacks);

    return Promise.all(
      rawPacks.map(async (pack) => {
        const status = await this.readStatus(pack.id);
        const check = moduleState.get(pack.id);
        const installed = check?.installed || status.installed || status.state === "installed" || false;
        const missingModules = check?.missingModules || status.missingModules || [];
        const state: CapabilityState = installed ? "installed" : status.state || "not-installed";
        const policyPack = policyPacks.get(pack.id);
        const message =
          policy.mode === "disabled" && !installed
            ? `${pack.name} installation is disabled by the enterprise policy.`
            : installed
              ? `${pack.name} is ready.`
              : status.message || `${pack.name} is not installed. EcoreX can guide installation on first use.`;
        return {
          ...pack,
          state,
          message,
          installed,
          logPath: status.logPath,
          missingModules,
          updatedAt: status.updatedAt,
          policyMode: policy.mode || "ask",
          policyStatus: policyPack?.status,
          policyUpdatedAt: policyPack?.updatedAt || policy.updatedAt
        };
      })
    );
  }

  async installPack(packId: string): Promise<CapabilityPack> {
    const packs = await this.listPacks();
    const target = packs.find((pack) => pack.id === packId);
    if (!target) {
      return {
        id: packId,
        name: packId,
        summary: "Unknown capability pack.",
        installMode: "user-or-admin",
        state: "failed",
        message: `Capability pack not found: ${packId}`,
        installed: false
      };
    }
    if (target.installed) {
      return target;
    }

    const enterprisePolicy = await this.refreshEnterpriseCapabilityPolicy();
    const installPolicy = enterprisePolicy.policy || {};
    if (installPolicy.mode === "disabled") {
      return {
        ...target,
        state: "failed",
        message: "The enterprise administrator has disabled optional capability installation.",
        installed: false
      };
    }

    const python = this.resolvePython();
    const script = await this.resolveInstallScript();
    const manifest = await this.resolveManifestPath();
    const layout = this.resolveInstallLayout();
    if (!python || !(await fileExists(python))) {
      return {
        ...target,
        state: "failed",
        message: "EcoreX runtime Python was not found. Reinstall the full desktop app or ask an administrator to preinstall this pack.",
        installed: false
      };
    }
    if (!script) {
      return {
        ...target,
        state: "failed",
        message: "Capability installer was not found. Reinstall the full desktop app and try again.",
        installed: false
      };
    }

    try {
      await fsp.mkdir(layout.stateDir, { recursive: true });
      if (layout.targetDir) {
        await fsp.mkdir(layout.targetDir, { recursive: true });
      }
      if (layout.playwrightBrowsersDir) {
        await fsp.mkdir(layout.playwrightBrowsersDir, { recursive: true });
      }
      const args = [
        script,
        "--pack-id",
        packId,
        "--runtime-dir",
        this.runtimeRoot,
        "--manifest",
        manifest,
        "--index-dir",
        layout.stateDir
      ];
      if (layout.targetDir) {
        args.push("--target-dir", layout.targetDir);
      }
      if (layout.playwrightBrowsersDir) {
        args.push("--playwright-browsers-dir", layout.playwrightBrowsersDir);
      }
      const mirror = usableIndexUrl(installPolicy.mirror);
      const offlineCache = usableFindLinks(installPolicy.offlineCache);
      if (mirror) {
        args.push("--index-url", mirror);
      }
      if (offlineCache) {
        args.push("--find-links", offlineCache);
      }
      await runProcess(python, args, {
        cwd: this.runtimeRoot,
        env: {
          ...process.env,
          ...this.resolveCapabilityEnv(),
          PYTHONNOUSERSITE: "1"
        },
        timeoutMs: 30 * 60 * 1000
      });
    } catch {
      // The installer writes a structured failed state; read it below.
    }

    const refreshed = await this.listPacks();
    return refreshed.find((pack) => pack.id === packId) || target;
  }

  private resolvePython() {
    if (process.env.ECOREX_PYTHON) {
      return process.env.ECOREX_PYTHON;
    }

    const candidates =
      process.platform === "win32"
        ? [path.join(this.runtimeRoot, "python", "python.exe")]
        : [path.join(this.runtimeRoot, "python", "bin", "python3"), path.join(this.runtimeRoot, "python", "bin", "python")];

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

  private async resolveManifestPath() {
    const candidates = [
      path.join(this.runtimeRoot, "capabilities.json"),
      path.resolve(this.electronDir, "..", "runtime-packs", "capabilities.json")
    ];
    for (const candidate of candidates) {
      if (await fileExists(candidate)) {
        return candidate;
      }
    }
    throw new Error("EcoreX capability manifest not found");
  }

  private async resolveInstallScript() {
    const candidates = [
      path.join(this.runtimeRoot, "scripts", "install-capability.py"),
      path.resolve(this.electronDir, "..", "scripts", "install-capability.py")
    ];
    for (const candidate of candidates) {
      if (await fileExists(candidate)) {
        return candidate;
      }
    }
    return "";
  }

  private async readStatus(packId: string): Promise<StatusFile> {
    const statusPath = path.join(this.resolveInstallLayout().stateDir, `${packId}.json`);
    try {
      return JSON.parse(await fsp.readFile(statusPath, "utf8")) as StatusFile;
    } catch {
      return {};
    }
  }

  private async checkInstalled(packs: RawPack[]) {
    const result = new Map<string, { installed: boolean; missingModules: string[] }>();
    const python = this.resolvePython();
    if (!python) {
      return result;
    }

    const checks = packs.map((pack) => ({
      id: pack.id,
      moduleChecks: pack.moduleChecks || []
    }));
    const code = [
      "import importlib.util, json, sys",
      "def missing_module(name):",
      "    try:",
      "        return importlib.util.find_spec(name) is None",
      "    except ModuleNotFoundError:",
      "        return True",
      "packs=json.loads(sys.stdin.read())",
      "out={}",
      "for pack in packs:",
      "    missing=[m for m in pack.get('moduleChecks', []) if missing_module(m)]",
      "    out[pack['id']]={'installed': not missing, 'missingModules': missing}",
      "print(json.dumps(out, ensure_ascii=False))"
    ].join("\n");

    try {
      const child = spawn(python, ["-c", code], {
        cwd: this.runtimeRoot,
        env: {
          ...process.env,
          ...this.resolveCapabilityEnv(),
          PYTHONNOUSERSITE: "1"
        },
        windowsHide: true
      });
      const completed = new Promise<string>((resolve, reject) => {
        let stdout = "";
        let stderr = "";
        const timer = setTimeout(() => {
          child.kill();
          reject(new Error("Capability module check timed out"));
        }, 8000);
        child.stdout.on("data", (chunk: Buffer) => {
          stdout += chunk.toString("utf8");
        });
        child.stderr.on("data", (chunk: Buffer) => {
          stderr += chunk.toString("utf8");
        });
        child.once("error", (error) => {
          clearTimeout(timer);
          reject(error);
        });
        child.once("exit", (exitCode) => {
          clearTimeout(timer);
          if (exitCode === 0) {
            resolve(stdout);
          } else {
            reject(new Error(stderr || `Module check exited with ${exitCode}`));
          }
        });
      });
      child.stdin.end(JSON.stringify(checks));
      const parsed = JSON.parse(await completed) as Record<string, { installed: boolean; missingModules: string[] }>;
      for (const [id, state] of Object.entries(parsed)) {
        result.set(id, state);
      }
    } catch {
      // Status files still provide useful state when the module probe cannot run.
    }

    return result;
  }

  private resolveInstallLayout() {
    if (process.platform === "darwin") {
      const root = path.join(app.getPath("userData"), "capabilities");
      return {
        stateDir: path.join(root, "state"),
        targetDir: path.join(root, "python-site"),
        playwrightBrowsersDir: path.join(root, "playwright-browsers")
      };
    }

    return {
      stateDir: path.join(this.runtimeRoot, "capability-state"),
      targetDir: "",
      playwrightBrowsersDir: ""
    };
  }

  private resolveCapabilityEnv() {
    const layout = this.resolveInstallLayout();
    const env: NodeJS.ProcessEnv = {};
    if (layout.targetDir) {
      env.PYTHONPATH = [layout.targetDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    }
    if (layout.playwrightBrowsersDir) {
      env.PLAYWRIGHT_BROWSERS_PATH = layout.playwrightBrowsersDir;
    }
    return env;
  }

  private async refreshEnterpriseCapabilityPolicy(): Promise<CapabilityPolicyPayload> {
    const policy = this.loadEnterprisePolicy();
    const cached = await this.loadCachedCapabilityPolicy();
    const capabilityPolicyUrl = policy.capabilityPolicyUrl || this.deriveCapabilityPolicyUrl(policy);
    if (!capabilityPolicyUrl || !policy.clientEventKey) {
      return cached;
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const session = this.loadEnterpriseSession();
      const response = await fetch(capabilityPolicyUrl, {
        headers: {
          "X-EcoreX-Client-Key": policy.clientEventKey,
          "X-EcoreX-User-Email": session?.user?.email || policy.userEmail || "",
          "X-EcoreX-User-Token": session?.token || "",
          "X-EcoreX-Device-Id": session?.deviceId || this.resolveDeviceId(policy),
          "X-EcoreX-Org-Id": policy.orgId || "",
          ...(session?.token ? { Authorization: `Bearer ${session.token}` } : {})
        },
        signal: controller.signal
      });
      if (!response.ok) {
        throw new Error(`capability policy HTTP ${response.status}`);
      }
      const payload = (await response.json()) as CapabilityPolicyPayload;
      this.capabilityPolicy = this.normalizeCapabilityPolicy(payload);
      await this.saveCachedCapabilityPolicy(this.capabilityPolicy);
      return this.capabilityPolicy;
    } catch {
      return cached;
    } finally {
      clearTimeout(timeout);
    }
  }

  private normalizeCapabilityPolicy(payload: CapabilityPolicyPayload): CapabilityPolicyPayload {
    const mode = payload.policy?.mode;
    return {
      ok: Boolean(payload.ok),
      policy: {
        mirror: payload.policy?.mirror || "https://pypi.org/simple",
        mode: mode === "preinstall" || mode === "disabled" ? mode : "ask",
        offlineCache: payload.policy?.offlineCache || "",
        updatedAt: payload.policy?.updatedAt || payload.updatedAt
      },
      capabilities: Array.isArray(payload.capabilities) ? payload.capabilities : [],
      updatedAt: payload.updatedAt || payload.policy?.updatedAt
    };
  }

  private async loadCachedCapabilityPolicy(): Promise<CapabilityPolicyPayload> {
    if (this.capabilityPolicy) {
      return this.capabilityPolicy;
    }
    const cached = readJson<CapabilityPolicyPayload>(this.capabilityPolicyCachePath());
    this.capabilityPolicy = cached
      ? this.normalizeCapabilityPolicy(cached)
      : {
          ok: false,
          policy: {
            mirror: "https://pypi.org/simple",
            mode: "ask",
            offlineCache: ""
          },
          capabilities: []
        };
    return this.capabilityPolicy;
  }

  private async saveCachedCapabilityPolicy(policy: CapabilityPolicyPayload) {
    try {
      const filePath = this.capabilityPolicyCachePath();
      await fsp.mkdir(path.dirname(filePath), { recursive: true });
      await fsp.writeFile(filePath, JSON.stringify(policy, null, 2), "utf8");
    } catch {
      // Capability policy cache must not break optional capability installation.
    }
  }

  private capabilityPolicyCachePath() {
    return path.join(app.getPath("userData"), "capability-policy.json");
  }

  private loadEnterprisePolicy(): EnterprisePolicy {
    if (this.enterprisePolicy) {
      return this.enterprisePolicy;
    }

    const envPolicy: EnterprisePolicy = {
      adminEventsUrl: process.env.ECOREX_ADMIN_EVENTS_URL,
      modelConfigUrl: process.env.ECOREX_MODEL_CONFIG_URL,
      capabilityPolicyUrl: process.env.ECOREX_CAPABILITY_POLICY_URL,
      clientEventKey: process.env.ECOREX_CLIENT_EVENT_KEY,
      userEmail: process.env.ECOREX_USER_EMAIL,
      deviceId: process.env.ECOREX_DEVICE_ID,
      orgId: process.env.ECOREX_ORG_ID
    };

    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      path.join(this.runtimeRoot, "enterprise-policy.json"),
      process.resourcesPath ? path.join(process.resourcesPath, "enterprise-policy.json") : ""
    ].filter(Boolean);

    const filePolicy = candidates.map((candidate) => readJson<EnterprisePolicy>(candidate)).find(Boolean) || {};
    this.enterprisePolicy = {
      ...filePolicy,
      ...Object.fromEntries(Object.entries(envPolicy).filter(([, value]) => Boolean(value)))
    };
    return this.enterprisePolicy;
  }

  private loadEnterpriseSession(): EnterpriseSession | null {
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

  private deriveCapabilityPolicyUrl(policy: EnterprisePolicy) {
    if (policy.adminEventsUrl) {
      return policy.adminEventsUrl.replace(/\/client\/events\/?$/, "/client/capability-policy");
    }
    if (policy.modelConfigUrl) {
      return policy.modelConfigUrl.replace(/\/client\/model-config\/?$/, "/client/capability-policy");
    }
    return "";
  }

  private resolveDeviceId(policy: EnterprisePolicy) {
    if (policy.deviceId) {
      return policy.deviceId;
    }
    return `${os.hostname()}-${process.platform}`;
  }
}

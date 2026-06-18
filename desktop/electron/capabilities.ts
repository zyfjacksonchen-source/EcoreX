import { app } from "electron";
import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { enterpriseClientEventKeys, enterpriseRequestHeaders, normalizeEnterpriseDeviceId, resolveEnterprisePolicy, type EnterprisePolicy } from "./enterprisePolicy.js";

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
    const raw = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
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
        const packPolicyMode = (policyPack?.mode as CapabilityPolicyMode | undefined) || policy.mode || "ask";
        const message =
          packPolicyMode === "disabled" && !installed
            ? `Administrator disabled self-service installation for ${pack.name}.`
            : installed
              ? `${pack.name} is ready.`
              : status.message || `${pack.name} is not installed. EcoreX will ask the current agent session to install it when needed.`;
        return {
          ...pack,
          state,
          message,
          installed,
          logPath: status.logPath,
          missingModules,
          updatedAt: status.updatedAt,
          policyMode: packPolicyMode,
          policyStatus: policyPack?.status,
          policyUpdatedAt: policyPack?.updatedAt || policy.updatedAt
        };
      })
    );
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
    throw new Error("EcoreX capability manifest was not found.");
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
          reject(new Error("Capability module probe timed out."));
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
            reject(new Error(stderr || `Capability module probe exited with ${exitCode}`));
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
      const payload = await this.fetchCapabilityPolicyWithKeyFallback(capabilityPolicyUrl, policy, session, controller.signal);
      this.capabilityPolicy = this.normalizeCapabilityPolicy(payload);
      await this.saveCachedCapabilityPolicy(this.capabilityPolicy);
      return this.capabilityPolicy;
    } catch {
      return cached;
    } finally {
      clearTimeout(timeout);
    }
  }

  private async fetchCapabilityPolicyWithKeyFallback(
    capabilityPolicyUrl: string,
    policy: EnterprisePolicy,
    session: EnterpriseSession | null,
    signal: AbortSignal
  ) {
    const keys = enterpriseClientEventKeys(policy);
    for (const [index, clientEventKey] of keys.entries()) {
      const response = await fetch(capabilityPolicyUrl, {
        headers: enterpriseRequestHeaders({
          clientEventKey,
          userEmail: session?.user?.email || policy.userEmail,
          userToken: session?.token,
          deviceId: session?.deviceId || this.resolveDeviceId(policy),
          orgId: policy.orgId,
          authorizationToken: session?.token
        }),
        signal
      });
      const payload = (await response.json().catch(() => ({}))) as CapabilityPolicyPayload & { error?: string; message?: string };
      if (response.status === 403 && this.isInvalidClientKeyPayload(payload) && index < keys.length - 1) {
        continue;
      }
      if (!response.ok) {
        throw new Error(`capability policy HTTP ${response.status}${payload.error ? `: ${payload.error}` : ""}`);
      }
      return payload;
    }
    throw new Error("capability policy HTTP 403: invalid client key");
  }

  private isInvalidClientKeyPayload(payload: { error?: string; message?: string }) {
    return `${payload.error || ""} ${payload.message || ""}`.toLowerCase().includes("invalid client key");
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
            mode: "preinstall",
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

    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      path.join(this.runtimeRoot, "enterprise-policy.json"),
      process.resourcesPath ? path.join(process.resourcesPath, "enterprise-policy.json") : ""
    ].filter(Boolean);

    this.enterprisePolicy = resolveEnterprisePolicy(candidates);
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
    return normalizeEnterpriseDeviceId(policy.deviceId || `${os.hostname()}-${process.platform}`);
  }
}

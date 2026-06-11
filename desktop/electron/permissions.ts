import { app, BrowserWindow, dialog, type IpcMainInvokeEvent, type MessageBoxOptions } from "electron";
import fsp from "node:fs/promises";
import path from "node:path";

export type PermissionMode = "smart-ask" | "always-ask" | "read-only" | "custom";

export type PermissionState = {
  mode: PermissionMode;
  grantsCount: number;
  auditPath: string;
  updatedAt?: string;
};

type PermissionSettings = {
  mode: PermissionMode;
  alwaysAllow: Record<string, boolean>;
  updatedAt?: string;
};

type PermissionDecision = {
  allowed: boolean;
  reason: string;
  remember?: boolean;
};

const allowedModes = new Set<PermissionMode>(["smart-ask", "always-ask", "read-only", "custom"]);
const dangerousExtensions = new Set([
  ".app",
  ".bat",
  ".cmd",
  ".command",
  ".com",
  ".exe",
  ".js",
  ".jse",
  ".lnk",
  ".msi",
  ".ps1",
  ".reg",
  ".scr",
  ".sh",
  ".vbe",
  ".vbs",
  ".wsf"
]);

function toMode(value: unknown): PermissionMode {
  return allowedModes.has(value as PermissionMode) ? (value as PermissionMode) : "smart-ask";
}

function isInside(parent: string, child: string) {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function safeJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

export class PermissionManager {
  private settings: PermissionSettings | null = null;
  private readonly selectedPaths = new Set<string>();

  private get settingsPath() {
    return path.join(app.getPath("userData"), "permissions.json");
  }

  private get auditPath() {
    return path.join(app.getPath("userData"), "permission-audit.jsonl");
  }

  async getState(): Promise<PermissionState> {
    const settings = await this.loadSettings();
    return {
      mode: settings.mode,
      grantsCount: Object.keys(settings.alwaysAllow || {}).length,
      auditPath: this.auditPath,
      updatedAt: settings.updatedAt
    };
  }

  async setMode(mode: PermissionMode): Promise<PermissionState> {
    const settings = await this.loadSettings();
    settings.mode = toMode(mode);
    settings.updatedAt = new Date().toISOString();
    await this.saveSettings(settings);
    await this.writeAudit("permission.mode.update", "allow", {
      mode: settings.mode
    });
    return this.getState();
  }

  async resetGrants(): Promise<PermissionState> {
    const settings = await this.loadSettings();
    settings.alwaysAllow = {};
    settings.updatedAt = new Date().toISOString();
    this.selectedPaths.clear();
    await this.saveSettings(settings);
    await this.writeAudit("permission.grants.reset", "allow", {
      mode: settings.mode
    });
    return this.getState();
  }

  async rememberSelectedPaths(filePaths: string[]) {
    for (const filePath of filePaths) {
      if (path.isAbsolute(filePath)) {
        this.selectedPaths.add(path.resolve(filePath));
      }
    }
    if (filePaths.length > 0) {
      await this.writeAudit("file.select", "allow", {
        count: filePaths.length
      });
    }
  }

  async authorizeOpenPath(event: IpcMainInvokeEvent, filePath: string): Promise<PermissionDecision> {
    const target = path.resolve(filePath);
    const settings = await this.loadSettings();
    const pathClass = await this.classifyTarget(target);
    const grantKey = `open-local-path:${pathClass}`;

    if (!path.isAbsolute(filePath)) {
      await this.writeAudit("open-local-path", "deny", { reason: "relative-path" });
      return { allowed: false, reason: "EcoreX only opens absolute local paths." };
    }

    if (settings.mode === "read-only" && pathClass === "dangerous") {
      await this.writeAudit("open-local-path", "deny", {
        mode: settings.mode,
        pathClass,
        target
      });
      return { allowed: false, reason: "Read Only mode blocks executable or script-like files." };
    }

    if (settings.alwaysAllow?.[grantKey]) {
      await this.writeAudit("open-local-path", "allow", {
        mode: settings.mode,
        pathClass,
        target,
        reason: "remembered-grant"
      });
      return { allowed: true, reason: "remembered-grant" };
    }

    if (settings.mode === "smart-ask" && this.isPreviouslySelected(target) && pathClass !== "dangerous") {
      await this.writeAudit("open-local-path", "allow", {
        mode: settings.mode,
        pathClass,
        target,
        reason: "selected-by-user"
      });
      return { allowed: true, reason: "selected-by-user" };
    }

    if (settings.mode === "smart-ask" && isInside(app.getPath("userData"), target)) {
      await this.writeAudit("open-local-path", "allow", {
        mode: settings.mode,
        pathClass,
        target,
        reason: "ecorex-user-data"
      });
      return { allowed: true, reason: "ecorex-user-data" };
    }

    const decision = await this.promptOpenPath(event, target, settings.mode, pathClass);
    if (decision.remember) {
      settings.alwaysAllow[grantKey] = true;
      settings.updatedAt = new Date().toISOString();
      await this.saveSettings(settings);
    }
    await this.writeAudit("open-local-path", decision.allowed ? "allow" : "deny", {
      mode: settings.mode,
      pathClass,
      target,
      reason: decision.reason,
      remember: Boolean(decision.remember)
    });
    return decision;
  }

  async writeOpenResult(filePath: string, result: string) {
    await this.writeAudit(result ? "open-local-path.result" : "open-local-path.result", result ? "warn" : "allow", {
      target: path.resolve(filePath),
      result: result || "opened"
    });
  }

  private async loadSettings(): Promise<PermissionSettings> {
    if (this.settings) {
      return this.settings;
    }
    try {
      const raw = await fsp.readFile(this.settingsPath, "utf8");
      const parsed = JSON.parse(raw) as Partial<PermissionSettings>;
      this.settings = {
        mode: toMode(parsed.mode),
        alwaysAllow: parsed.alwaysAllow && typeof parsed.alwaysAllow === "object" ? parsed.alwaysAllow : {},
        updatedAt: parsed.updatedAt
      };
    } catch {
      this.settings = {
        mode: "smart-ask",
        alwaysAllow: {},
        updatedAt: new Date().toISOString()
      };
      await this.saveSettings(this.settings);
    }
    return this.settings;
  }

  private async saveSettings(settings: PermissionSettings) {
    await fsp.mkdir(path.dirname(this.settingsPath), { recursive: true });
    await fsp.writeFile(this.settingsPath, safeJson(settings), "utf8");
    this.settings = settings;
  }

  private async classifyTarget(filePath: string) {
    const ext = path.extname(filePath).toLowerCase();
    if (dangerousExtensions.has(ext)) {
      return "dangerous";
    }
    try {
      const stat = await fsp.stat(filePath);
      if (stat.isDirectory()) {
        return "directory";
      }
    } catch {
      return "missing";
    }
    return ext ? `file:${ext}` : "file";
  }

  private isPreviouslySelected(filePath: string) {
    for (const selectedPath of this.selectedPaths) {
      if (filePath === selectedPath || isInside(selectedPath, filePath)) {
        return true;
      }
    }
    return false;
  }

  private async promptOpenPath(event: IpcMainInvokeEvent, filePath: string, mode: PermissionMode, pathClass: string) {
    const owner = BrowserWindow.fromWebContents(event.sender) || undefined;
    const message =
      pathClass === "dangerous"
        ? "EcoreX wants to open an executable or script-like local file."
        : "EcoreX wants to open a local path with the system application.";
    const detail = `${filePath}\n\nMode: ${mode}. Choose Allow once for this action, Always allow for this kind of local open, or Deny.`;
    const options: MessageBoxOptions = {
      type: pathClass === "dangerous" ? "warning" : "question",
      buttons: ["Allow once", "Always allow this kind", "Deny"],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
      title: "EcoreX permission check",
      message,
      detail
    };
    const result = owner ? await dialog.showMessageBox(owner, options) : await dialog.showMessageBox(options);
    if (result.response === 0) {
      return { allowed: true, reason: "user-allowed-once" };
    }
    if (result.response === 1) {
      return { allowed: true, reason: "user-allowed-kind", remember: true };
    }
    return { allowed: false, reason: "user-denied" };
  }

  private async writeAudit(action: string, decision: "allow" | "deny" | "warn", detail: Record<string, unknown>) {
    try {
      await fsp.mkdir(path.dirname(this.auditPath), { recursive: true });
      const entry = {
        createdAt: new Date().toISOString(),
        action,
        decision,
        detail
      };
      await fsp.appendFile(this.auditPath, `${JSON.stringify(entry)}\n`, "utf8");
    } catch {
      // Local permission audit must never crash the desktop shell.
    }
  }
}

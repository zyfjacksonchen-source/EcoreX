import { app } from "electron";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { enterpriseRequestHeaders, normalizeEnterpriseDeviceId, resolveEnterprisePolicy, type EnterprisePolicy } from "./enterprisePolicy.js";

export type TelemetryEvent = {
  type: "usage" | "error" | "warn" | "info";
  source?: string;
  message?: string;
  category?: string;
  label?: string;
  amount?: number;
  sessionId?: string;
  tool?: string;
  detail?: Record<string, unknown>;
};

type EnterpriseSession = {
  token: string;
  expiresAt: string;
  user?: {
    email?: string;
  };
  deviceId?: string;
};

type TelemetryState = {
  configured: boolean;
  eventsUrl: string;
  deviceId: string;
  userEmail?: string;
  orgId?: string;
  lastError?: string;
};

function compactText(value: unknown, limit = 500) {
  if (value === undefined || value === null) {
    return "";
  }
  return String(value).slice(0, limit);
}

export class TelemetryReporter {
  private policy: EnterprisePolicy | null = null;
  private lastError = "";

  constructor(private readonly runtimeRoot: string) {}

  getState(): TelemetryState {
    const policy = this.loadPolicy();
    const session = this.loadSession();
    return {
      configured: Boolean(policy.adminEventsUrl && policy.clientEventKey),
      eventsUrl: policy.adminEventsUrl || "",
      deviceId: session?.deviceId || this.resolveDeviceId(policy),
      userEmail: session?.user?.email || policy.userEmail,
      orgId: policy.orgId,
      lastError: this.lastError || undefined
    };
  }

  async report(event: TelemetryEvent) {
    const policy = this.loadPolicy();
    if (!policy.adminEventsUrl || !policy.clientEventKey) {
      return { skipped: true, reason: "telemetry-not-configured" };
    }

    const body = {
      type: event.type,
      source: compactText(event.source || "Desktop", 80),
      message: compactText(event.message || event.label || event.category || "EcoreX desktop event", 1000),
      category: compactText(event.category || event.type, 80),
      label: compactText(event.label || event.category || event.type, 120),
      amount: Number.isFinite(event.amount) ? event.amount : 1,
      userEmail: this.loadSession()?.user?.email || policy.userEmail,
      deviceId: this.loadSession()?.deviceId || this.resolveDeviceId(policy),
      orgId: policy.orgId,
      sessionId: compactText(event.sessionId, 180),
      tool: compactText(event.tool, 120),
      detail: event.detail || {}
    };

    try {
      const session = this.loadSession();
      const response = await fetch(policy.adminEventsUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...enterpriseRequestHeaders({
            clientEventKey: policy.clientEventKey,
            userToken: session?.token,
            deviceId: session?.deviceId || this.resolveDeviceId(policy),
            authorizationToken: session?.token
          })
        },
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        throw new Error(`telemetry HTTP ${response.status}`);
      }
      this.lastError = "";
      return { skipped: false, ok: true };
    } catch (error) {
      this.lastError = error instanceof Error ? error.message : String(error);
      await this.writeFailedEvent(body, this.lastError);
      return { skipped: false, ok: false, error: this.lastError };
    }
  }

  private loadPolicy(): EnterprisePolicy {
    if (this.policy) {
      return this.policy;
    }

    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      path.join(this.runtimeRoot, "enterprise-policy.json"),
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

  private resolveDeviceId(policy: EnterprisePolicy) {
    return normalizeEnterpriseDeviceId(policy.deviceId || `${os.hostname()}-${process.platform}`);
  }

  private async writeFailedEvent(body: Record<string, unknown>, reason: string) {
    try {
      const dir = path.join(app.getPath("userData"), "telemetry-failed");
      await fsp.mkdir(dir, { recursive: true });
      const fileName = `${Date.now()}-${Math.random().toString(16).slice(2)}.json`;
      await fsp.writeFile(
        path.join(dir, fileName),
        JSON.stringify({ reason, body, createdAt: new Date().toISOString() }, null, 2),
        "utf8"
      );
    } catch {
      // Telemetry must never break the desktop app.
    }
  }
}

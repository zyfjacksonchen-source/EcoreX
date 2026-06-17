import { app } from "electron";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { enterpriseClientEventKeys, resolveEnterprisePolicy, type EnterprisePolicy } from "./enterprisePolicy.js";

export type EnterpriseSession = {
  token: string;
  expiresAt: string;
  user: {
    id: string;
    name: string;
    email: string;
    role: string;
    status: string;
    mustChangePassword?: boolean;
  };
  quota?: Record<string, unknown>;
  deviceId: string;
  savedAt: string;
};

export type EnterpriseSessionView = Omit<EnterpriseSession, "token"> & {
  authenticated: true;
};

function readJson<T>(filePath: string): T | null {
  try {
    const raw = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function compactText(value: unknown, limit = 500) {
  if (value === undefined || value === null) {
    return "";
  }
  return String(value).trim().slice(0, limit);
}

export class EnterpriseAuthManager {
  constructor(private readonly runtimeRoot: string) {}

  getSession(): EnterpriseSession | null {
    const session = readJson<EnterpriseSession>(this.sessionPath());
    if (!session?.token || !session.expiresAt) {
      return null;
    }
    const expiresAt = Date.parse(session.expiresAt);
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      void this.logout();
      return null;
    }
    return session;
  }

  getSessionView(): EnterpriseSessionView | null {
    const session = this.getSession();
    return session ? this.toSessionView(session) : null;
  }

  getPolicy() {
    return this.loadPolicy();
  }

  getDeviceId(policy = this.loadPolicy()) {
    return policy.deviceId || `${os.hostname()}-${process.platform}`;
  }

  async login(input: { email: string; password: string }) {
    const policy = this.loadPolicy();
    const authUrl = this.deriveClientUrl(policy, "auth/login");
    if (!authUrl || !policy.clientEventKey) {
      throw new Error("企业登录策略未配置，请确认安装包包含 enterprise-policy.json。");
    }
    const { response, payload } = await this.fetchClientJsonWithKeyFallback<EnterpriseSession & { ok?: boolean; error?: string }>(policy, authUrl, (clientEventKey) => ({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EcoreX-Client-Key": clientEventKey,
        "X-EcoreX-Device-Id": this.getDeviceId(policy),
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({
        email: compactText(input.email, 180),
        password: input.password,
        deviceId: this.getDeviceId(policy),
        appVersion: app.getVersion()
      })
    }));
    if (!response.ok || payload.ok === false || !payload.token) {
      throw new Error(payload.error || `企业登录失败：HTTP ${response.status}`);
    }
    const session: EnterpriseSession = {
      token: payload.token,
      expiresAt: payload.expiresAt,
      user: payload.user,
      quota: payload.quota,
      deviceId: this.getDeviceId(policy),
      savedAt: new Date().toISOString()
    };
    await fsp.mkdir(path.dirname(this.sessionPath()), { recursive: true });
    await fsp.writeFile(this.sessionPath(), JSON.stringify(session, null, 2), "utf8");
    return session;
  }

  toSessionView(session: EnterpriseSession): EnterpriseSessionView {
    const { token: _token, ...view } = session;
    return { ...view, authenticated: true };
  }

  async logout() {
    await fsp.rm(this.sessionPath(), { force: true }).catch(() => undefined);
    return { ok: true };
  }

  async changePassword(input: { oldPassword: string; newPassword: string }) {
    const session = this.getSession();
    const policy = this.loadPolicy();
    const changeUrl = this.deriveClientUrl(policy, "auth/change-password");
    if (!session) {
      throw new Error("请先登录。");
    }
    if (!changeUrl || !policy.clientEventKey) {
      throw new Error("企业密码服务未配置。");
    }
    const { response, payload } = await this.fetchClientJsonWithKeyFallback<{ ok?: boolean; error?: string }>(policy, changeUrl, (clientEventKey) => ({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${session.token}`,
        "X-EcoreX-User-Token": session.token,
        "X-EcoreX-Client-Key": clientEventKey,
        "X-EcoreX-Device-Id": session.deviceId,
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({
        oldPassword: input.oldPassword,
        newPassword: input.newPassword,
        deviceId: session.deviceId
      })
    }));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `修改密码失败：HTTP ${response.status}`);
    }
    const nextSession: EnterpriseSession = {
      ...session,
      user: { ...session.user, mustChangePassword: false },
      savedAt: new Date().toISOString()
    };
    await fsp.writeFile(this.sessionPath(), JSON.stringify(nextSession, null, 2), "utf8");
    return nextSession;
  }

  async checkQuota(estimatedTokens = 0) {
    const session = this.getSession();
    const policy = this.loadPolicy();
    const quotaUrl = this.deriveClientUrl(policy, "quota/check");
    if (!session) {
      return { ok: false, quota: { allowed: false, reason: "尚未登录" } };
    }
    if (!quotaUrl || !policy.clientEventKey) {
      return { ok: true, quota: { allowed: true, reason: "企业额度服务未配置，已跳过预检" } };
    }
    const { response, payload } = await this.fetchClientJsonWithKeyFallback<{ ok?: boolean; error?: string; quota?: Record<string, unknown> }>(policy, quotaUrl, (clientEventKey) => ({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${session.token}`,
        "X-EcoreX-User-Token": session.token,
        "X-EcoreX-Client-Key": clientEventKey,
        "X-EcoreX-Device-Id": session.deviceId,
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({ estimatedTokens })
    }));
    if (!response.ok || payload.ok === false) {
      return { ok: false, quota: { allowed: false, reason: payload.error || `额度检查失败：HTTP ${response.status}` } };
    }
    return payload;
  }

  loadPolicy(): EnterprisePolicy {
    const candidates = [
      path.join(app.getPath("userData"), "enterprise-policy.json"),
      path.join(this.runtimeRoot, "enterprise-policy.json"),
      process.resourcesPath ? path.join(process.resourcesPath, "enterprise-policy.json") : ""
    ].filter(Boolean);
    return resolveEnterprisePolicy(candidates);
  }

  private deriveClientUrl(policy: EnterprisePolicy, suffix: string) {
    if (policy.adminEventsUrl) {
      return policy.adminEventsUrl.replace(/\/client\/events\/?$/, `/client/${suffix}`);
    }
    if (policy.modelConfigUrl) {
      return policy.modelConfigUrl.replace(/\/client\/model-config\/?$/, `/client/${suffix}`);
    }
    if (policy.capabilityPolicyUrl) {
      return policy.capabilityPolicyUrl.replace(/\/client\/capability-policy\/?$/, `/client/${suffix}`);
    }
    return "";
  }

  private sessionPath() {
    return path.join(app.getPath("userData"), "enterprise-session.json");
  }

  private async fetchClientJsonWithKeyFallback<T extends { error?: string; message?: string }>(
    policy: EnterprisePolicy,
    url: string,
    buildInit: (clientEventKey: string) => RequestInit
  ) {
    const keys = enterpriseClientEventKeys(policy);
    let lastResponse: Response | null = null;
    let lastPayload = {} as T;
    for (const [index, clientEventKey] of keys.entries()) {
      const response = await fetch(url, buildInit(clientEventKey));
      const payload = (await response.json().catch(() => ({}))) as T;
      lastResponse = response;
      lastPayload = payload;
      if (this.isInvalidClientKey(response, payload) && index < keys.length - 1) {
        continue;
      }
      return { response, payload, clientEventKey };
    }
    return { response: lastResponse as Response, payload: lastPayload, clientEventKey: keys[keys.length - 1] || "" };
  }

  private isInvalidClientKey(response: Response, payload: { error?: string; message?: string }) {
    const text = `${payload.error || ""} ${payload.message || ""}`.toLowerCase();
    return response.status === 403 && text.includes("invalid client key");
  }
}

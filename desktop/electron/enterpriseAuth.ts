import { app } from "electron";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export type EnterprisePolicy = {
  adminEventsUrl?: string;
  modelConfigUrl?: string;
  capabilityPolicyUrl?: string;
  clientEventKey?: string;
  userEmail?: string;
  deviceId?: string;
  orgId?: string;
};

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
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as T;
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
    const response = await fetch(authUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EcoreX-Client-Key": policy.clientEventKey,
        "X-EcoreX-Device-Id": this.getDeviceId(policy),
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({
        email: compactText(input.email, 180),
        password: input.password,
        deviceId: this.getDeviceId(policy),
        appVersion: app.getVersion()
      })
    });
    const payload = (await response.json().catch(() => ({}))) as EnterpriseSession & { ok?: boolean; error?: string };
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
    const response = await fetch(changeUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${session.token}`,
        "X-EcoreX-User-Token": session.token,
        "X-EcoreX-Client-Key": policy.clientEventKey,
        "X-EcoreX-Device-Id": session.deviceId,
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({
        oldPassword: input.oldPassword,
        newPassword: input.newPassword,
        deviceId: session.deviceId
      })
    });
    const payload = (await response.json().catch(() => ({}))) as { ok?: boolean; error?: string };
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
    const response = await fetch(quotaUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${session.token}`,
        "X-EcoreX-User-Token": session.token,
        "X-EcoreX-Client-Key": policy.clientEventKey,
        "X-EcoreX-Device-Id": session.deviceId,
        "X-EcoreX-Org-Id": policy.orgId || ""
      },
      body: JSON.stringify({ estimatedTokens })
    });
    const payload = (await response.json().catch(() => ({}))) as { ok?: boolean; error?: string; quota?: Record<string, unknown> };
    if (!response.ok || payload.ok === false) {
      return { ok: false, quota: { allowed: false, reason: payload.error || `额度检查失败：HTTP ${response.status}` } };
    }
    return payload;
  }

  loadPolicy(): EnterprisePolicy {
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
    return {
      ...filePolicy,
      ...Object.fromEntries(Object.entries(envPolicy).filter(([, value]) => Boolean(value)))
    };
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
}

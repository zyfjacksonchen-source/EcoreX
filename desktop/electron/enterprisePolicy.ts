import fs from "node:fs";
import crypto from "node:crypto";

export type EnterprisePolicy = {
  adminEventsUrl?: string;
  modelConfigUrl?: string;
  capabilityPolicyUrl?: string;
  clientEventKey?: string;
  compatClientEventKeys?: string[];
  userEmail?: string;
  deviceId?: string;
  orgId?: string;
};

const ASCII_HEADER_RE = /^[\x20-\x7E]*$/;
const SAFE_DEVICE_ID_RE = /^[A-Za-z0-9._:@+-]{1,180}$/;

export const DEFAULT_CLIENT_EVENT_KEY = "ecorex-desktop-v0.2.0";
export const DEFAULT_COMPAT_CLIENT_EVENT_KEYS = [
  "ecorex-desktop-v0.2.0",
  "ecorex-desktop-v0.1.19",
  "ecorex-desktop-v0.1.18",
  "ecorex-desktop-v0.1.17",
  "ecorex-desktop-v0.1.16",
  "ecorex-desktop-v0.1.15",
  "ecorex-desktop-v0.1.14",
  "ecorex-desktop-v0.1.13",
  "ecorex-desktop-v0.1.12",
  "ecorex-desktop-v0.1.11",
  "ecorex-desktop-v0.1.10"
];

export const DEFAULT_ENTERPRISE_POLICY: EnterprisePolicy = {
  adminEventsUrl: "https://www.ecoreai.cn/ecorex-agent/client/events",
  modelConfigUrl: "https://www.ecoreai.cn/ecorex-agent/client/model-config",
  capabilityPolicyUrl: "https://www.ecoreai.cn/ecorex-agent/client/capability-policy",
  clientEventKey: DEFAULT_CLIENT_EVENT_KEY,
  compatClientEventKeys: DEFAULT_COMPAT_CLIENT_EVENT_KEYS
};

export function hasEnterpriseTransport(policy: EnterprisePolicy) {
  return Boolean(
    policy.clientEventKey &&
      (policy.adminEventsUrl || policy.modelConfigUrl || policy.capabilityPolicyUrl)
  );
}

export function readEnterprisePolicy(filePath: string): EnterprisePolicy | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "")) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    return parsed as EnterprisePolicy;
  } catch {
    return null;
  }
}

export function enterpriseEnvPolicy(): EnterprisePolicy {
  return {
    adminEventsUrl: process.env.ECOREX_ADMIN_EVENTS_URL,
    modelConfigUrl: process.env.ECOREX_MODEL_CONFIG_URL,
    capabilityPolicyUrl: process.env.ECOREX_CAPABILITY_POLICY_URL,
    clientEventKey: process.env.ECOREX_CLIENT_EVENT_KEY,
    compatClientEventKeys: splitClientEventKeys(process.env.ECOREX_CLIENT_EVENT_KEYS),
    userEmail: process.env.ECOREX_USER_EMAIL,
    deviceId: process.env.ECOREX_DEVICE_ID,
    orgId: process.env.ECOREX_ORG_ID
  };
}

export function resolveEnterprisePolicy(candidates: string[], envPolicy = enterpriseEnvPolicy()): EnterprisePolicy {
  const validFilePolicy = candidates
    .map((candidate) => readEnterprisePolicy(candidate))
    .find((policy): policy is EnterprisePolicy => Boolean(policy && hasEnterpriseTransport(policy)));
  const envOverrides = Object.fromEntries(Object.entries(envPolicy).filter(([, value]) => hasPolicyOverrideValue(value)));
  return {
    ...DEFAULT_ENTERPRISE_POLICY,
    ...(validFilePolicy || {}),
    ...envOverrides
  };
}

function hasPolicyOverrideValue(value: unknown) {
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  return Boolean(value);
}

export function splitClientEventKeys(value?: string) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function enterpriseClientEventKeys(policy: EnterprisePolicy) {
  const keys: string[] = [];
  const add = (value?: string) => {
    const key = String(value || "").trim();
    if (key && !keys.includes(key)) {
      keys.push(key);
    }
  };
  add(policy.clientEventKey);
  for (const key of policy.compatClientEventKeys || []) {
    add(key);
  }
  const primary = String(policy.clientEventKey || "");
  const shouldUseDefaultCompat = !primary || DEFAULT_COMPAT_CLIENT_EVENT_KEYS.includes(primary);
  if (shouldUseDefaultCompat) {
    for (const key of DEFAULT_COMPAT_CLIENT_EVENT_KEYS) {
      add(key);
    }
  }
  return keys;
}

export function toAsciiHeaderValue(value: unknown) {
  const text = String(value ?? "").trim();
  if (!text) {
    return "";
  }
  return ASCII_HEADER_RE.test(text) ? text : encodeURIComponent(text);
}

export function normalizeEnterpriseDeviceId(value: unknown, fallback = "ecorex-device") {
  const raw = String(value ?? "").trim() || fallback;
  if (SAFE_DEVICE_ID_RE.test(raw)) {
    return raw.slice(0, 180);
  }
  const asciiHint = raw
    .normalize("NFKD")
    .replace(/[^\x20-\x7E]/g, "-")
    .replace(/[^A-Za-z0-9._:@+-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  const hash = crypto.createHash("sha256").update(raw, "utf8").digest("hex").slice(0, 24);
  return `ecorex-${asciiHint || "device"}-${hash}`.slice(0, 180);
}

export function enterpriseRequestHeaders(input: {
  clientEventKey?: string;
  userEmail?: string;
  userToken?: string;
  deviceId?: string;
  orgId?: string;
  authorizationToken?: string;
}) {
  const headers: Record<string, string> = {};
  const setHeader = (name: string, value: unknown) => {
    const safeValue = toAsciiHeaderValue(value);
    if (safeValue) {
      headers[name] = safeValue;
    }
  };

  setHeader("X-EcoreX-Client-Key", input.clientEventKey);
  setHeader("X-EcoreX-User-Email", input.userEmail);
  setHeader("X-EcoreX-User-Token", input.userToken);
  setHeader("X-EcoreX-Device-Id", input.deviceId ? normalizeEnterpriseDeviceId(input.deviceId) : "");
  setHeader("X-EcoreX-Org-Id", input.orgId);

  const authorizationToken = toAsciiHeaderValue(input.authorizationToken);
  if (authorizationToken) {
    headers.Authorization = `Bearer ${authorizationToken}`;
  }
  return headers;
}

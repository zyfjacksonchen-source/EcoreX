import fs from "node:fs";

export type EnterprisePolicy = {
  adminEventsUrl?: string;
  modelConfigUrl?: string;
  capabilityPolicyUrl?: string;
  clientEventKey?: string;
  userEmail?: string;
  deviceId?: string;
  orgId?: string;
};

export const DEFAULT_ENTERPRISE_POLICY: EnterprisePolicy = {
  adminEventsUrl: "https://www.ecoreai.cn/ecorex-agent/client/events",
  modelConfigUrl: "https://www.ecoreai.cn/ecorex-agent/client/model-config",
  capabilityPolicyUrl: "https://www.ecoreai.cn/ecorex-agent/client/capability-policy",
  clientEventKey: "ecorex-desktop-v0.1.12"
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
    userEmail: process.env.ECOREX_USER_EMAIL,
    deviceId: process.env.ECOREX_DEVICE_ID,
    orgId: process.env.ECOREX_ORG_ID
  };
}

export function resolveEnterprisePolicy(candidates: string[], envPolicy = enterpriseEnvPolicy()): EnterprisePolicy {
  const validFilePolicy = candidates
    .map((candidate) => readEnterprisePolicy(candidate))
    .find((policy): policy is EnterprisePolicy => Boolean(policy && hasEnterpriseTransport(policy)));
  const envOverrides = Object.fromEntries(Object.entries(envPolicy).filter(([, value]) => Boolean(value)));
  return {
    ...DEFAULT_ENTERPRISE_POLICY,
    ...(validFilePolicy || {}),
    ...envOverrides
  };
}

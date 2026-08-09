import type {
  ConnectorAuthKind,
  ConnectorCatalogItem,
  ConnectorCatalogResponse,
  ConnectorHealth,
  ConnectorTier,
} from "../api/contracts.ts";

export type ConnectorCatalogLoadState = "idle" | "loading" | "ready" | "error";

export type ConnectorOperationKind =
  | "connecting"
  | "reconnecting"
  | "checking"
  | "disconnecting"
  | "saving"
  | "testing"
  | "enabling"
  | "disabling"
  | "retrying";

export interface ConnectorOperationState {
  connectorId: string;
  instanceId: string | null;
  kind: ConnectorOperationKind;
  clientRequestId: string;
}

export interface ConnectorSection {
  tier: ConnectorTier;
  label: string;
  description: string;
  items: ConnectorCatalogItem[];
}

const healthPriority: ConnectorHealth[] = [
  "connected",
  "authenticating",
  "degraded",
  "error",
  "disabled",
  "unconfigured",
];

export function connectorSections(items: ConnectorCatalogItem[]): ConnectorSection[] {
  const definitions: Array<Omit<ConnectorSection, "items">> = [
    {
      tier: "stable",
      label: "正式连接器",
      description: "可用于日常办公任务",
    },
    {
      tier: "beta",
      label: "内置 Beta",
      description: "功能和授权范围仍在验证",
    },
  ];
  return definitions
    .map((section) => ({
      ...section,
      items: items
        .filter((item) => item.definition.tier === section.tier)
        .sort((left, right) => left.definition.display_name.localeCompare(
          right.definition.display_name,
          "zh-CN",
        )),
    }))
    .filter((section) => section.items.length > 0);
}

export function preferredConnectorAuthKind(
  item: ConnectorCatalogItem,
): ConnectorAuthKind | null {
  return item.definition.auth_kinds.includes("oauth2") ? "oauth2" : null;
}

export function connectorOverallHealth(item: ConnectorCatalogItem): ConnectorHealth {
  const values = new Set(item.instances.map((instance) => instance.health));
  return healthPriority.find((health) => values.has(health)) ?? "unconfigured";
}

export function connectorHealthLabel(health: ConnectorHealth): string {
  const labels: Record<ConnectorHealth, string> = {
    unconfigured: "未连接",
    authenticating: "正在授权",
    connected: "已连接",
    degraded: "连接异常",
    error: "连接失败",
    disabled: "已停用",
  };
  return labels[health];
}

export function connectorUnavailableMessage(item: ConnectorCatalogItem): string | null {
  if (!item.adapter_available) {
    return item.unavailable_reason === "adapter_not_installed"
      ? "当前安装未包含这个连接所需的组件。"
      : "当前安装暂不支持这个连接。";
  }
  if (!preferredConnectorAuthKind(item)) {
    return "此连接器需要管理员预先配置凭证，当前页面不会收集密钥。";
  }
  return null;
}

export function safeConnectorAuthorizationUrl(value: string | null): string {
  if (!value) throw new Error("e-Mate 暂时无法打开授权页面，请稍后重试。");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("授权地址无效，已停止打开以保护你的账号。");
  }
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.hash
  ) {
    throw new Error("授权地址未通过安全检查，已停止打开以保护你的账号。");
  }
  return parsed.href;
}

export function connectorAuthorizationCompleted(
  response: ConnectorCatalogResponse,
  connectorId: string,
  previousInstanceIds: ReadonlySet<string>,
  reauthorizedInstanceId: string | null = null,
): boolean {
  const item = response.items.find(
    (candidate) => candidate.definition.connector_id === connectorId,
  );
  if (reauthorizedInstanceId) {
    return Boolean(item?.instances.some(
      (instance) => (
        instance.instance_id === reauthorizedInstanceId
        && instance.health === "connected"
      ),
    ));
  }
  return Boolean(item?.instances.some(
    (instance) => !previousInstanceIds.has(instance.instance_id),
  ));
}

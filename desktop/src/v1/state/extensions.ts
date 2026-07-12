import type {
  ExtensionActionId,
  ExtensionActionProjection,
  ExtensionCatalogSnapshot,
  ExtensionExportProjection,
  ExtensionHealth,
  ExtensionKind,
  ExtensionProjection,
  ExtensionStatus,
  ExtensionTrust,
} from "../api/contracts.ts";

export type ExtensionLoadState = "idle" | "loading" | "ready" | "error";

export interface ExtensionOperationState {
  extensionId: string;
  actionId: ExtensionActionId;
  expectedRevision: number;
  clientRequestId: string;
}

export interface ExtensionCatalogSummary {
  total: number;
  enabled: number;
  quarantined: number;
  degraded: number;
  unhealthy: number;
  circuitOpen: number;
}

export type ExtensionKindFilter = "all" | ExtensionKind;
export type ExtensionStatusFilter = "all" | ExtensionStatus;

export function extensionCatalogSummary(
  snapshot: ExtensionCatalogSnapshot | null,
): ExtensionCatalogSummary {
  const items = snapshot?.items ?? [];
  return {
    total: items.length,
    enabled: items.filter((item) => item.status === "enabled").length,
    quarantined: items.filter((item) => item.status === "quarantined").length,
    degraded: items.filter((item) => item.health === "degraded").length,
    unhealthy: items.filter((item) => item.health === "unhealthy").length,
    circuitOpen: items.filter((item) => item.health === "circuit_open").length,
  };
}

export function extensionRequestKey(
  extension: ExtensionProjection,
  actionId: ExtensionActionId,
): string {
  return `${extension.extension_id}:${actionId}:${extension.revision}`;
}

export function extensionAction(
  extension: ExtensionProjection,
  actionId: ExtensionActionId,
): ExtensionActionProjection | null {
  return extension.actions.find((candidate) => candidate.action_id === actionId) ?? null;
}

export function extensionActionDisabledReason(action: ExtensionActionProjection): string | null {
  if (action.enabled) return null;
  return action.disabled_reason?.trim() || "当前版本没有提供这项操作不可用的原因。";
}

export function extensionKindLabel(kind: ExtensionKind): string {
  return ({
    skill: "技能",
    mcp_server: "扩展服务",
    tool_provider: "工具组件",
    connector_provider: "连接组件",
    capability_pack: "能力组件",
  } satisfies Record<ExtensionKind, string>)[kind];
}

export function extensionStatusLabel(status: ExtensionStatus): string {
  return ({
    staged: "已暂存",
    enabled: "已启用",
    disabled: "已停用",
    quarantined: "已隔离",
  } satisfies Record<ExtensionStatus, string>)[status];
}

export function extensionHealthLabel(health: ExtensionHealth): string {
  return ({
    unknown: "尚未检查",
    healthy: "健康",
    degraded: "部分降级",
    unhealthy: "不可用",
    circuit_open: "熔断中",
  } satisfies Record<ExtensionHealth, string>)[health];
}

export function extensionTrustLabel(trust: ExtensionTrust): string {
  return ({
    builtin: "EcoreX 内置",
    administrator: "管理员批准",
    verified_publisher: "已验证发布方",
    local_untrusted: "本地未受信任",
  } satisfies Record<ExtensionTrust, string>)[trust];
}

export function extensionSourceLabel(source: ExtensionProjection["source"]): string {
  return ({
    core_bundle: "核心内置",
    signed_release: "签名发布",
    capability_pack: "能力组件",
    administrator: "管理员提供",
    local_bundle: "本地技能包",
    legacy_import: "旧版迁移",
  } satisfies Record<ExtensionProjection["source"], string>)[source];
}

export function extensionActionLabel(actionId: ExtensionActionId): string {
  return ({
    enable: "启用",
    disable: "停用",
    health_check: "检查健康",
    rollback: "回滚版本",
  } satisfies Record<ExtensionActionId, string>)[actionId];
}

export function extensionActionConfirmation(
  extension: ExtensionProjection,
  actionId: ExtensionActionId,
): string {
  const verb = extensionActionLabel(actionId);
  return `确认${verb}“${extension.display_name}”？EcoreX 会重新检查来源、依赖、权限和当前版本。`;
}

export function extensionExportKindLabel(kind: ExtensionExportProjection["kind"]): string {
  return ({
    tool: "工具",
    skill: "技能",
    mcp_server: "扩展服务",
    connector: "连接器",
    capability_pack: "能力包",
  } satisfies Record<ExtensionExportProjection["kind"], string>)[kind];
}

export function extensionExposureLabel(exposure: ExtensionExportProjection["exposure"]): string {
  return ({
    direct: "直接暴露",
    deferred: "按需发现",
    hidden: "隐藏",
  } satisfies Record<ExtensionExportProjection["exposure"], string>)[exposure];
}

export function extensionPermissionEffectLabel(effect: string): string {
  const value = effect.trim();
  if (/\p{Script=Han}/u.test(value)) return value;
  const normalized = value.toLowerCase().replaceAll("-", "_");
  return ({
    read: "读取数据",
    write: "修改数据",
    network: "访问网络",
    filesystem_read: "读取工作区文件",
    filesystem_write: "修改工作区文件",
    workspace_read: "读取工作区",
    workspace_write: "修改工作区",
    external_write: "修改外部系统",
    connector_read: "读取连接器数据",
    connector_write: "修改连接器数据",
    browser: "访问浏览器",
    execute: "运行本机能力",
    process: "运行本机能力",
    shell: "运行本机能力",
    ui_automation: "操作用户界面",
    generate_media: "生成媒体",
    subscribe: "订阅事件",
    secrets: "使用受保护凭证",
    credential: "使用受保护凭证",
    image_generation: "生成图片",
  } satisfies Record<string, string>)[normalized] ?? "其他受控权限";
}

export function filterExtensions(
  items: ExtensionProjection[],
  query: string,
  kind: ExtensionKindFilter,
  status: ExtensionStatusFilter,
): ExtensionProjection[] {
  const needle = query.trim().toLocaleLowerCase("zh-CN");
  return items.filter((extension) => {
    if (kind !== "all" && extension.kind !== kind) return false;
    if (status !== "all" && extension.status !== status) return false;
    if (!needle) return true;
    return extension.display_name.toLocaleLowerCase("zh-CN").includes(needle)
      || extension.extension_id.toLocaleLowerCase("zh-CN").includes(needle);
  });
}

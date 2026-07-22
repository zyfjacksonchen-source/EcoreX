import type { ArtifactFamily, InteractionProjection } from "../api/contracts.ts";
import { RuntimeApiError } from "../api/runtimeClient.ts";

const CODE_MESSAGES: Record<string, string> = {
  event_cursor_reset_required: "任务内容正在重新同步，请稍候。",
  thread_not_found: "没有找到这个任务。请核对任务 ID 后重试。",
  turn_not_found: "没有找到要继续的工作步骤。请刷新任务后重试。",
  live_replay_confirmation_required: "请先确认重新运行这一步。",
  turn_not_terminal: "这一步仍在进行中，结束后才能重新运行。",
  extension_revision_conflict: "扩展状态已在其他位置改变。页面已刷新，请重新确认。",
  extension_projection_mismatch: "扩展状态未能同步，请刷新扩展目录。",
  extension_action_unavailable: "这项扩展操作当前不可用。请刷新扩展目录后重试。",
  extension_idempotency_conflict: "扩展操作内容已经改变。请刷新后重新确认。",
  memory_request_conflict: "记忆设置已在其他位置改变，请刷新后重试。",
  memory_undo_expired: "这次记忆重置已超过可撤销时间。",
  output_revision_conflict: "默认保存位置已在其他页面改变，请刷新后重试。",
  output_location_unavailable: "所选保存位置当前不可用，请选择其他位置。",
  stale_permission_revision: "权限设置已在其他页面改变，请刷新后重新确认。",
  migration_quarantine_invalid: "旧版凭证备份状态异常，e-Mate 已停止删除。请保留当前文件并联系管理员。",
  migration_quarantine_confirmation_required: "请先确认永久删除旧版凭证备份。",
  managed_session_unavailable: "账户授权已过期，请重新登录。",
  managed_gateway_not_configured: "模型服务尚未配置，请联系管理员。",
  signed_model_allowlist_empty: "当前账户没有可用模型，请联系管理员。",
  image_capability_pack_not_installed: "图片能力尚未安装完整，请完成组件安装后重试。",
  managed_image_edit_not_configured: "精准修图服务尚未配置，请联系管理员。",
  signed_image_model_not_allowed: "当前账户没有可用的修图模型，请联系管理员。",
  connector_not_found: "没有找到这个连接，请刷新连接器列表。",
  device_authorization_not_found: "这次登录已结束，请重新开始登录。",
  device_authorization_conflict: "登录状态已更新，请刷新后继续。",
  device_authorization_unavailable: "登录服务暂时不可用，请检查网络后重试。",
  session_already_authenticated: "账号已经登录，刷新页面后即可使用。",
  session_login_invalid: "账号或密码不正确，请重试。",
  session_login_locked: "登录尝试过多，请稍后再试。",
  session_login_rate_limited: "登录尝试过多，请稍后再试。",
  session_login_unavailable: "登录服务暂时不可用，请稍后重试。",
  health_check_failed: "最近一次连接检查未通过，请重新连接或稍后再试。",
  credential_cleanup_pending: "账号已断开，e-Mate 正在完成本机清理。",
  disconnect_draining: "正在完成尚未结束的操作，随后会断开连接。",
  remote_revocation_pending: "正在向连接服务确认断开，请稍候。",
  remote_revocation_uncertain: "连接可能已断开，但暂时无法确认。请稍后检查状态。",
  connector_invocation_uncertain: "连接应用可能已完成操作，但暂时无法确认。请先检查结果。",
  provider_timeout: "扩展响应超时，e-Mate 已停止等待。你可以稍后重试。",
  gateway_unavailable: "模型服务暂时不可用，任务会保留并可重试。",
  share_image_preview_missing: "有图片还没有可分享的预览图。请等待图片处理完成后重试。",
  share_image_preview_too_large: "图片预览超过分享上限。请生成较小的预览图后重试。",
  share_image_preview_unsupported: "图片预览格式暂不支持分享。请生成 PNG、JPEG、WebP、GIF 或 AVIF 预览后重试。",
  share_image_preview_invalid: "图片预览未通过完整性检查。请重新生成预览图后重试。",
  share_media_total_too_large: "本次图片预览总量超过分享上限。请减少图片数量或生成较小预览后重试。",
  share_schema_upgrade_required: "这个分享数据版本已停止发布。请从当前会话重新创建分享链接。",
};

const REASON_MESSAGES: Record<string, string> = {
  ...CODE_MESSAGES,
  share_service_not_configured: "分享服务尚未配置。",
  device_authorization_not_configured: "设备登录服务尚未配置。",
  adapter_not_installed: "所需连接组件尚未安装。",
};

const TECHNICAL_WORDS = /\b(?:runtime|snapshot|idempotency|trace|lease|exception|failed|unavailable|danger-full-access|workspace-write)\b|[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}/i;

export function serviceReasonMessage(
  reason: string | null | undefined,
  fallback: string,
): string {
  const value = String(reason ?? "").trim();
  if (!value) return fallback;
  return REASON_MESSAGES[value] ?? fallback;
}

export function userFacingError(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    if (error.code && CODE_MESSAGES[error.code]) return CODE_MESSAGES[error.code];
    if (error.status === 401) return "登录状态已过期，请重新登录。";
    if (error.status === 403) return "当前设置不允许这项操作。";
    if (error.status === 404) return "没有找到要操作的内容。请刷新后重试。";
    if (error.status === 409) return "内容已在其他位置改变。请刷新后重新确认。";
    if (error.status === 422) return "提交的内容不完整或格式不正确，请检查后重试。";
    if (error.status === 429) return "当前请求较多，e-Mate 会稍后继续。";
    if (error.status >= 500) return "e-Mate 暂时无法完成这项操作，当前数据已保留。请稍后重试。";
  }
  if (
    !(error instanceof RuntimeApiError)
    && error instanceof Error
    && /\p{Script=Han}/u.test(error.message)
    && !TECHNICAL_WORDS.test(error.message)
  ) {
    return error.message;
  }
  return "e-Mate 暂时没有响应。当前数据已保留，请稍后重试。";
}

export function technicalErrorCode(error: unknown): string | null {
  if (!(error instanceof RuntimeApiError)) return null;
  const code = String(error.code ?? "").trim();
  return code || null;
}

const ARTIFACT_FAMILY_LABELS: Record<ArtifactFamily, string> = {
  document: "文档",
  spreadsheet: "表格",
  presentation: "演示文稿",
  pdf: "PDF",
  image: "图片",
  audio: "音频",
  video: "视频",
  data_export: "数据文件",
  web_report: "网页报告",
  archive: "压缩包",
  cloud_link: "在线文件",
};

export function artifactFamilyLabel(family: ArtifactFamily): string {
  return ARTIFACT_FAMILY_LABELS[family];
}

export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "大小未知";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"] as const;
  let value = bytes / 1024;
  let unit: (typeof units)[number] = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: value < 10 ? 1 : 0,
  }).format(value)} ${unit}`;
}

export function interactionTitle(kind: InteractionProjection["kind"]): string {
  return ({
    permission_approval: "需要你的允许",
    information: "还需要一点信息",
    connector_login: "需要连接账号",
    conflict_resolution: "请选择如何继续",
    artifact_review: "请检查产物",
  } satisfies Record<InteractionProjection["kind"], string>)[kind];
}

export function interactionOptionLabel(id: string, supplied: unknown): string {
  if (typeof supplied === "string" && /\p{Script=Han}/u.test(supplied)) return supplied;
  return ({
    allow: "允许一次",
    deny: "不允许",
    continue: "继续",
    cancel: "取消任务",
    retry: "安全重试",
    skip: "跳过这一步",
    approve: "确认",
    reject: "返回修改",
    connect: "连接账号",
  } satisfies Record<string, string>)[id] ?? "继续";
}

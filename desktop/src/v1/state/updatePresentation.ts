import type { UpdateSnapshot } from "../api/contracts.ts";

/**
 * The running slot is authoritative for ordinary update presentation. The
 * Runtime converges stale durable state at startup; this is the UI backstop.
 */
export function hasPendingRuntimeUpdate(update: UpdateSnapshot | null | undefined): boolean {
  return Boolean(
    update
    && update.target_version
    && update.target_version !== update.current_version
    && update.state !== "idle",
  );
}

/** A banner is only a user decision after the signed package is fully prepared. */
export function isVerifiedRuntimeUpdateReady(
  update: UpdateSnapshot | null | undefined,
): boolean {
  return hasPendingRuntimeUpdate(update)
    && update?.state === "awaiting_user"
    && update.can_activate;
}

export function runtimeUpdateStatusText(
  update: UpdateSnapshot | null | undefined,
  busy = false,
): string {
  if (!update) return "版本信息尚未读取";
  if (!hasPendingRuntimeUpdate(update)) {
    return busy ? "正在检查更新…" : "当前已是最新可用版本";
  }
  switch (update.state) {
    case "available":
      return `已发现 e-Mate ${update.target_version}，即将后台下载`;
    case "downloading":
      return `正在后台下载并校验 e-Mate ${update.target_version}`;
    case "awaiting_user":
      return update.can_activate
        ? `e-Mate ${update.target_version} 已下载并通过校验`
        : `e-Mate ${update.target_version} 正在完成安装准备`;
    case "activating":
      return `正在切换到 e-Mate ${update.target_version}`;
    case "failed":
      return "更新准备失败，当前版本不受影响，可重新检查";
    default:
      return "当前已是最新可用版本";
  }
}

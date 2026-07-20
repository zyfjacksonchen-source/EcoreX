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

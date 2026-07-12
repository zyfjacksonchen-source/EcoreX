import type {
  MigrationQuarantineItem,
  MigrationQuarantineProjection,
} from "../api/contracts.ts";

const kinds = new Set([
  "api_key",
  "refresh_token",
  "access_token",
  "password",
  "cryptographic_key",
  "client_secret",
  "credential",
]);
const origins = new Set([
  "product_configuration",
  "mcp_configuration",
  "skill_configuration",
  "permission_configuration",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function validateMigrationQuarantineProjection(
  value: unknown,
): MigrationQuarantineProjection {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new TypeError("旧版凭证状态无效。");
  }
  const items: MigrationQuarantineItem[] = value.items.map((item) => {
    if (
      !isRecord(item)
      || Object.keys(item).sort().join(",") !== "count,kind,origin"
      || !kinds.has(String(item.kind))
      || !origins.has(String(item.origin))
      || !Number.isSafeInteger(item.count)
      || Number(item.count) < 1
    ) {
      throw new TypeError("旧版凭证状态无效。");
    }
    return item as unknown as MigrationQuarantineItem;
  });
  const entryCount = value.entry_count;
  const status = value.status;
  const canDelete = value.can_delete;
  const deletedAt = value.deleted_at;
  if (
    Object.keys(value).sort().join(",")
      !== "can_delete,deleted_at,entry_count,items,status"
    || !["absent", "available", "deleted"].includes(String(status))
    || !Number.isSafeInteger(entryCount)
    || Number(entryCount) < 0
    || typeof canDelete !== "boolean"
    || (deletedAt !== null && typeof deletedAt !== "string")
    || items.reduce((total, item) => total + item.count, 0) !== entryCount
    || canDelete !== (status === "available" && Number(entryCount) > 0)
    || (status !== "available" && items.length > 0)
  ) {
    throw new TypeError("旧版凭证状态无效。");
  }
  return {
    status,
    entry_count: entryCount,
    can_delete: canDelete,
    deleted_at: deletedAt,
    items,
  } as MigrationQuarantineProjection;
}

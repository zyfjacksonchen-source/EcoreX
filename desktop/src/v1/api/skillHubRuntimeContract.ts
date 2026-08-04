import type {
  SkillHubCardProjection,
  SkillHubDetailProjection,
  SkillHubListResponse,
} from "./contracts.ts";
type Reject = (message: string, code: string) => never;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function validateSkillHubCardProjection(value: unknown, reject: Reject): SkillHubCardProjection {
  const item = value as Record<string, unknown>;
  if (
    !isRecord(value)
    || typeof item.slug !== "string"
    || !/^[a-z0-9][a-z0-9-]{1,95}$/.test(item.slug)
    || typeof item.title !== "string"
    || typeof item.summary !== "string"
    || typeof item.version !== "string"
    || typeof item.package_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(item.package_sha256)
    || !Number.isSafeInteger(item.package_size_bytes)
    || !Array.isArray(item.tags)
    || !isRecord(item.uploader)
    || typeof item.uploader.nickname !== "string"
    || !isRecord(item.provenance)
    || item.provenance.brand !== "e-Mate"
    || !["third_party", "content_creation", "office_productivity"].includes(String(item.category))
    || !["not_installed", "installed_enabled", "installed_disabled", "uninstalled"].includes(String(item.installation_status))
    || !["ready", "needs_configuration", "missing_runtime", "unsupported"].includes(String(item.readiness))
  ) {
    reject("Runtime returned an invalid Skill Hub card.", "skill_hub_card_invalid");
  }
  return value as unknown as SkillHubCardProjection;
}

export function validateSkillHubListResponse(value: unknown, reject: Reject): SkillHubListResponse {
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || !Array.isArray(value.items)
    || value.items.length > 100
    || (value.next_cursor !== null && typeof value.next_cursor !== "string")
  ) {
    reject("Runtime returned an invalid Skill Hub catalog.", "skill_hub_catalog_invalid");
  }
  value.items.forEach((item) => validateSkillHubCardProjection(item, reject));
  return value as unknown as SkillHubListResponse;
}

export function validateSkillHubDetailProjection(value: unknown, reject: Reject): SkillHubDetailProjection {
  if (!isRecord(value) || value.schema_version !== 1 || !Array.isArray(value.versions)) {
    reject("Runtime returned an invalid Skill Hub detail.", "skill_hub_detail_invalid");
  }
  const skill = validateSkillHubCardProjection(value.skill, reject);
  const versions = value.versions.map((item) => validateSkillHubCardProjection(item, reject));
  if (!versions.length || versions.some((item) => item.slug !== skill.slug)) {
    reject("Runtime returned an invalid Skill Hub detail.", "skill_hub_detail_invalid");
  }
  return { schema_version: 1, skill, versions };
}

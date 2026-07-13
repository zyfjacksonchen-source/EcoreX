import type {
  ArtifactListResponse,
  ArtifactProjection,
  BootstrapResponse,
  ConversationUsageProjection,
  EventEnvelope,
  InputAttachmentProjection,
} from "./contracts.ts";
import { GENERATED_RUNTIME_CONTRACT } from "./generatedRuntimeContract.ts";

type ContractName = keyof typeof GENERATED_RUNTIME_CONTRACT.wireFields;

const ARTIFACT_FIELDS = GENERATED_RUNTIME_CONTRACT.wireFields.ArtifactProjection;
const BOOTSTRAP_FIELDS = GENERATED_RUNTIME_CONTRACT.wireFields.BootstrapResponse;
const CONVERSATION_USAGE_FIELDS = GENERATED_RUNTIME_CONTRACT.wireFields.ConversationUsageProjection;
const EVENT_FIELDS = GENERATED_RUNTIME_CONTRACT.wireFields.EventEnvelope;
const INPUT_ATTACHMENT_FIELDS = GENERATED_RUNTIME_CONTRACT.wireFields.InputAttachmentProjection;
const BOOTSTRAP_VALUES = GENERATED_RUNTIME_CONTRACT.bootstrap;

export const runtimeContractSchemaSha256 = GENERATED_RUNTIME_CONTRACT.schemaSha256;

export class RuntimeContractError extends Error {
  readonly contract: ContractName | "ArtifactListResponse";
  readonly path: string;
  readonly expectation: string;

  constructor(
    contract: ContractName | "ArtifactListResponse",
    path: string,
    expectation: string,
  ) {
    super("运行服务返回的数据版本与当前页面不兼容，请刷新或更新 EcoreX。");
    this.name = "RuntimeContractError";
    this.contract = contract;
    this.path = path;
    this.expectation = expectation;
  }
}

function reject(
  contract: RuntimeContractError["contract"],
  path: string,
  expectation: string,
): never {
  throw new RuntimeContractError(contract, path, expectation);
}

function assertRecord(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject(contract, path, "an object");
  }
}

function assertWireFields(
  value: Record<string, unknown>,
  expectedFields: readonly string[],
  contract: RuntimeContractError["contract"],
  path: string,
): void {
  const expected = new Set(expectedFields);
  for (const field of expectedFields) {
    if (!Object.hasOwn(value, field)) {
      reject(contract, path === "root" ? field : `${path}.${field}`, "a backend-declared wire field");
    }
  }
  for (const field of Object.keys(value)) {
    if (!expected.has(field)) {
      reject(
        contract,
        path === "root" ? field : `${path}.${field}`,
        "no undeclared wire fields",
      );
    }
  }
}

function assertString(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
  allowEmpty = false,
): asserts value is string {
  if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
    reject(contract, path, allowEmpty ? "a string" : "a non-empty string");
  }
}

function assertNullableString(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is string | null {
  if (value !== null) assertString(value, contract, path);
}

function assertBoolean(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is boolean {
  if (typeof value !== "boolean") reject(contract, path, "a boolean");
}

function assertInteger(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
  minimum = 0,
): asserts value is number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    reject(contract, path, `an integer >= ${minimum}`);
  }
}

function assertFiniteNumber(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    reject(contract, path, "a finite number");
  }
}

function assertStringArray(
  value: unknown,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is string[] {
  if (!Array.isArray(value)) reject(contract, path, "an array");
  for (let index = 0; index < value.length; index += 1) {
    assertString(value[index], contract, `${path}[${index}]`);
  }
}

function assertOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  contract: RuntimeContractError["contract"],
  path: string,
): asserts value is T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    reject(contract, path, allowed.join(" | "));
  }
}

function assertModelDescriptor(
  value: unknown,
  contract: "BootstrapResponse",
  path: string,
): void {
  assertRecord(value, contract, path);
  assertString(value.model_id, contract, `${path}.model_id`);
  assertString(value.display_name, contract, `${path}.display_name`);
  assertStringArray(value.capabilities, contract, `${path}.capabilities`);
  assertStringArray(value.aliases, contract, `${path}.aliases`);
  assertBoolean(value.is_default, contract, `${path}.is_default`);
  if (value.model_policy !== null) {
    const policyPath = `${path}.model_policy`;
    assertRecord(value.model_policy, contract, policyPath);
    if (value.model_policy.schema_version !== 1) {
      reject(contract, `${policyPath}.schema_version`, "literal 1");
    }
    for (const field of [
      "policy_id",
      "policy_version",
      "local_model_id",
      "upstream_model_id",
    ] as const) {
      assertString(value.model_policy[field], contract, `${policyPath}.${field}`);
    }
    if (value.model_policy.local_model_id !== value.model_id) {
      reject(contract, policyPath, "a policy matching model_id");
    }
    if (value.model_policy.reasoning_effort !== "medium") {
      reject(contract, `${policyPath}.reasoning_effort`, 'literal "medium"');
    }
    assertRecord(
      value.model_policy.context_management,
      contract,
      `${policyPath}.context_management`,
    );
    if (value.model_policy.context_management.type !== "compaction") {
      reject(
        contract,
        `${policyPath}.context_management.type`,
        'literal "compaction"',
      );
    }
    assertInteger(
      value.model_policy.context_management.compact_threshold_tokens,
      contract,
      `${policyPath}.context_management.compact_threshold_tokens`,
      1_000,
    );
  }
}

function assertModelList(
  value: unknown,
  contract: "BootstrapResponse",
  path: string,
): void {
  if (!Array.isArray(value)) reject(contract, path, "an array");
  value.forEach((item, index) => assertModelDescriptor(item, contract, `${path}[${index}]`));
}

function assertServiceSnapshot(
  value: unknown,
  contract: "BootstrapResponse",
  path: string,
): void {
  assertRecord(value, contract, path);
  assertOneOf(
    value.state,
    GENERATED_RUNTIME_CONTRACT.bootstrap.modelServiceStates,
    contract,
    `${path}.state`,
  );
  assertNullableString(value.reason, contract, `${path}.reason`);
}

function assertBootstrap(value: unknown): asserts value is BootstrapResponse {
  const contract = "BootstrapResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, BOOTSTRAP_FIELDS.BootstrapResponse, contract, "root");
  if (value.api_version !== GENERATED_RUNTIME_CONTRACT.versions.api) {
    reject(contract, "api_version", `literal "${GENERATED_RUNTIME_CONTRACT.versions.api}"`);
  }
  if (value.event_schema_version !== GENERATED_RUNTIME_CONTRACT.versions.eventSchema) {
    reject(
      contract,
      "event_schema_version",
      `literal ${GENERATED_RUNTIME_CONTRACT.versions.eventSchema}`,
    );
  }
  if (value.storage_schema_version !== GENERATED_RUNTIME_CONTRACT.versions.storageSchema) {
    reject(
      contract,
      "storage_schema_version",
      `literal ${GENERATED_RUNTIME_CONTRACT.versions.storageSchema}`,
    );
  }

  assertRecord(value.login, contract, "login");
  assertBoolean(value.login.authenticated, contract, "login.authenticated");
  assertNullableString(value.login.account_id, contract, "login.account_id");
  assertNullableString(value.login.display_name, contract, "login.display_name");
  assertNullableString(value.login.organization_id, contract, "login.organization_id");
  assertStringArray(value.login.roles, contract, "login.roles");
  if (value.login.session_revision !== null) {
    assertInteger(value.login.session_revision, contract, "login.session_revision", 1);
  }

  if (value.policy_lease !== null) {
    assertRecord(value.policy_lease, contract, "policy_lease");
    assertString(value.policy_lease.lease_id, contract, "policy_lease.lease_id");
    assertString(value.policy_lease.issued_at, contract, "policy_lease.issued_at");
    assertString(value.policy_lease.expires_at, contract, "policy_lease.expires_at");
    assertFiniteNumber(value.policy_lease.duration_hours, contract, "policy_lease.duration_hours");
  }

  assertRecord(value.models, contract, "models");
  assertNullableString(value.models.snapshot_id, contract, "models.snapshot_id");
  assertModelList(value.models.chat, contract, "models.chat");
  assertModelList(value.models.image, contract, "models.image");
  assertModelList(value.models.vision, contract, "models.vision");
  assertModelList(value.models.audio, contract, "models.audio");
  assertModelList(value.models.embedding, contract, "models.embedding");

  assertServiceSnapshot(value.model_service, contract, "model_service");
  assertServiceSnapshot(value.login_service, contract, "login_service");
  assertServiceSnapshot(value.share_service, contract, "share_service");
  assertServiceSnapshot(value.retouch_service, contract, "retouch_service");

  assertRecord(value.quota, contract, "quota");
  if (value.quota.remaining !== null) {
    assertFiniteNumber(value.quota.remaining, contract, "quota.remaining");
  }
  assertString(value.quota.unit, contract, "quota.unit");
  assertNullableString(value.quota.resets_at, contract, "quota.resets_at");
  assertRecord(value.quota.limits, contract, "quota.limits");
  for (const [name, limit] of Object.entries(value.quota.limits)) {
    assertFiniteNumber(limit, contract, `quota.limits.${name}`);
  }

  assertRecord(value.permissions, contract, "permissions");
  assertString(value.permissions.snapshot_id, contract, "permissions.snapshot_id");
  assertOneOf(
    value.permissions.profile,
    GENERATED_RUNTIME_CONTRACT.bootstrap.permissionProfiles,
    contract,
    "permissions.profile",
  );
  assertInteger(value.permissions.revision, contract, "permissions.revision", 1);
  assertString(value.permissions.updated_at, contract, "permissions.updated_at");
  assertOneOf(
    value.permissions.sandbox,
    GENERATED_RUNTIME_CONTRACT.bootstrap.permissionSandboxes,
    contract,
    "permissions.sandbox",
  );
  assertOneOf(
    value.permissions.approval,
    GENERATED_RUNTIME_CONTRACT.bootstrap.permissionApprovals,
    contract,
    "permissions.approval",
  );
  assertBoolean(value.permissions.full_access, contract, "permissions.full_access");
  assertStringArray(
    value.permissions.admin_hard_denies,
    contract,
    "permissions.admin_hard_denies",
  );
  if ((value.permissions.profile === "full_access") !== value.permissions.full_access) {
    reject(contract, "permissions", "profile/full_access consistency");
  }

  if (!Array.isArray(value.connectors)) reject(contract, "connectors", "an array");
  value.connectors.forEach((connector, index) => {
    const path = `connectors[${index}]`;
    assertRecord(connector, contract, path);
    assertString(connector.connector_id, contract, `${path}.connector_id`);
    assertString(connector.display_name, contract, `${path}.display_name`);
    assertOneOf(
      connector.tier,
      GENERATED_RUNTIME_CONTRACT.bootstrap.connectorTiers,
      contract,
      `${path}.tier`,
    );
    assertOneOf(
      connector.health,
      GENERATED_RUNTIME_CONTRACT.bootstrap.connectorHealth,
      contract,
      `${path}.health`,
    );
    assertStringArray(connector.capabilities, contract, `${path}.capabilities`);
    assertString(connector.contract_version, contract, `${path}.contract_version`);
    assertBoolean(connector.adapter_available, contract, `${path}.adapter_available`);
    assertNullableString(connector.description, contract, `${path}.description`);
    assertStringArray(connector.auth_kinds, contract, `${path}.auth_kinds`);
    assertNullableString(connector.icon_key, contract, `${path}.icon_key`);
    assertNullableString(connector.unavailable_reason, contract, `${path}.unavailable_reason`);
  });

  assertRecord(value.extensions, contract, "extensions");
  assertString(value.extensions.snapshot_id, contract, "extensions.snapshot_id");
  if (value.extensions.contract_version !== GENERATED_RUNTIME_CONTRACT.versions.extensionContract) {
    reject(
      contract,
      "extensions.contract_version",
      `literal "${GENERATED_RUNTIME_CONTRACT.versions.extensionContract}"`,
    );
  }
  if (!Array.isArray(value.extensions.items)) reject(contract, "extensions.items", "an array");
  value.extensions.items.forEach((extension, index) => {
    const path = `extensions.items[${index}]`;
    assertRecord(extension, contract, path);
    for (const field of ["extension_id", "display_name", "updated_at"] as const) {
      assertString(extension[field], contract, `${path}.${field}`);
    }
    assertString(extension.description, contract, `${path}.description`, true);
    for (const field of [
      "active_revision_id",
      "active_version",
      "active_digest",
      "last_error_code",
    ] as const) {
      assertNullableString(extension[field], contract, `${path}.${field}`);
    }
    if (
      typeof extension.active_digest === "string"
      && !/^[0-9a-f]{64}$/.test(extension.active_digest)
    ) {
      reject(contract, `${path}.active_digest`, "a lowercase SHA-256");
    }
    for (const [field, allowed] of [
      ["kind", BOOTSTRAP_VALUES.extensionKinds],
      ["source", BOOTSTRAP_VALUES.extensionSources],
      ["trust", BOOTSTRAP_VALUES.extensionTrust],
      ["status", BOOTSTRAP_VALUES.extensionStatuses],
      ["health", BOOTSTRAP_VALUES.extensionHealth],
    ] as const) {
      assertOneOf(extension[field], allowed, contract, `${path}.${field}`);
    }
    if (!Array.isArray(extension.dependencies)) {
      reject(contract, `${path}.dependencies`, "an array");
    }
    extension.dependencies.forEach((dependency, dependencyIndex) => {
      const dependencyPath = `${path}.dependencies[${dependencyIndex}]`;
      assertRecord(dependency, contract, dependencyPath);
      for (const field of ["extension_id", "version_range"] as const) {
        assertString(dependency[field], contract, `${dependencyPath}.${field}`);
      }
    });
    if (!Array.isArray(extension.exports)) reject(contract, `${path}.exports`, "an array");
    extension.exports.forEach((item, exportIndex) => {
      const exportPath = `${path}.exports[${exportIndex}]`;
      assertRecord(item, contract, exportPath);
      assertString(item.export_id, contract, `${exportPath}.export_id`);
      for (const [field, allowed] of [
        ["kind", BOOTSTRAP_VALUES.extensionExportKinds],
        ["exposure", BOOTSTRAP_VALUES.extensionExposures],
      ] as const) {
        assertOneOf(item[field], allowed, contract, `${exportPath}.${field}`);
      }
      assertStringArray(item.permission_effects, contract, `${exportPath}.permission_effects`);
    });
    if (!Array.isArray(extension.actions)) reject(contract, `${path}.actions`, "an array");
    extension.actions.forEach((action, actionIndex) => {
      const actionPath = `${path}.actions[${actionIndex}]`;
      assertRecord(action, contract, actionPath);
      assertOneOf(
        action.action_id,
        BOOTSTRAP_VALUES.extensionActionIds,
        contract,
        `${actionPath}.action_id`,
      );
      assertBoolean(action.enabled, contract, `${actionPath}.enabled`);
      assertNullableString(action.disabled_reason, contract, `${actionPath}.disabled_reason`);
      assertBoolean(
        action.requires_confirmation,
        contract,
        `${actionPath}.requires_confirmation`,
      );
    });
    assertInteger(extension.revision, contract, `${path}.revision`, 1);
  });

  assertRecord(value.update, contract, "update");
  assertString(value.update.current_version, contract, "update.current_version");
  assertOneOf(
    value.update.state,
    GENERATED_RUNTIME_CONTRACT.bootstrap.updateStates,
    contract,
    "update.state",
  );
  for (const field of [
    "target_version",
    "release_id",
    "build_digest",
    "transaction_id",
    "error_code",
  ] as const) {
    assertNullableString(value.update[field], contract, `update.${field}`);
  }
  assertBoolean(value.update.can_activate, contract, "update.can_activate");
  assertBoolean(value.update.requires_refresh, contract, "update.requires_refresh");

  assertString(value.csrf_token, contract, "csrf_token");
  if (value.csrf_token.length < 32) reject(contract, "csrf_token", "at least 32 characters");
  assertString(value.server_time, contract, "server_time");
}

export function validateBootstrapResponse(value: unknown): BootstrapResponse {
  assertBootstrap(value);
  return value;
}

function assertArtifact(value: unknown, path = "root"): asserts value is ArtifactProjection {
  const contract = "ArtifactProjection";
  assertRecord(value, contract, path);
  assertWireFields(value, ARTIFACT_FIELDS.ArtifactProjection, contract, path);
  assertString(value.artifact_id, contract, `${path}.artifact_id`);
  assertString(value.revision_id, contract, `${path}.revision_id`);
  assertOneOf(
    value.family,
    GENERATED_RUNTIME_CONTRACT.artifact.families,
    contract,
    `${path}.family`,
  );
  assertOneOf(value.role, GENERATED_RUNTIME_CONTRACT.artifact.roles, contract, `${path}.role`);
  assertOneOf(
    value.visibility,
    GENERATED_RUNTIME_CONTRACT.artifact.visibilities,
    contract,
    `${path}.visibility`,
  );
  assertOneOf(
    value.status,
    GENERATED_RUNTIME_CONTRACT.artifact.statuses,
    contract,
    `${path}.status`,
  );
  assertString(value.display_name, contract, `${path}.display_name`);
  assertString(value.mime_type, contract, `${path}.mime_type`);
  assertInteger(value.size_bytes, contract, `${path}.size_bytes`);
  assertString(value.sha256, contract, `${path}.sha256`);
  if (!/^[0-9a-f]{64}$/.test(value.sha256)) {
    reject(contract, `${path}.sha256`, "a lowercase SHA-256");
  }
  assertString(value.created_at, contract, `${path}.created_at`);

  assertRecord(value.lineage, contract, `${path}.lineage`);
  assertWireFields(
    value.lineage,
    ARTIFACT_FIELDS.ArtifactLineage,
    contract,
    `${path}.lineage`,
  );
  assertStringArray(
    value.lineage.source_artifact_ids,
    contract,
    `${path}.lineage.source_artifact_ids`,
  );
  assertNullableString(
    value.lineage.supersedes_revision_id,
    contract,
    `${path}.lineage.supersedes_revision_id`,
  );

  if (!Array.isArray(value.renditions)) reject(contract, `${path}.renditions`, "an array");
  value.renditions.forEach((rendition, index) => {
    const itemPath = `${path}.renditions[${index}]`;
    assertRecord(rendition, contract, itemPath);
    assertWireFields(rendition, ARTIFACT_FIELDS.RenditionProjection, contract, itemPath);
    assertOneOf(
      rendition.kind,
      GENERATED_RUNTIME_CONTRACT.artifact.renditionKinds,
      contract,
      `${itemPath}.kind`,
    );
    assertString(rendition.mime_type, contract, `${itemPath}.mime_type`);
    assertInteger(rendition.size_bytes, contract, `${itemPath}.size_bytes`);
    assertString(rendition.sha256, contract, `${itemPath}.sha256`);
    if (!/^[0-9a-f]{64}$/.test(rendition.sha256)) {
      reject(contract, `${itemPath}.sha256`, "a lowercase SHA-256");
    }
  });

  if (!Array.isArray(value.actions)) reject(contract, `${path}.actions`, "an array");
  value.actions.forEach((action, index) => {
    assertOneOf(
      action,
      GENERATED_RUNTIME_CONTRACT.artifact.actions,
      contract,
      `${path}.actions[${index}]`,
    );
  });

  if (value.feedback !== null) {
    assertRecord(value.feedback, contract, `${path}.feedback`);
    assertWireFields(
      value.feedback,
      ARTIFACT_FIELDS.FeedbackProjection,
      contract,
      `${path}.feedback`,
    );
    assertString(value.feedback.feedback_id, contract, `${path}.feedback.feedback_id`);
    assertString(value.feedback.revision_id, contract, `${path}.feedback.revision_id`);
    assertOneOf(
      value.feedback.signal,
      GENERATED_RUNTIME_CONTRACT.artifact.feedbackSignals,
      contract,
      `${path}.feedback.signal`,
    );
    assertString(value.feedback.recorded_at, contract, `${path}.feedback.recorded_at`);
  }

  assertRecord(value.quality_evidence, contract, `${path}.quality_evidence`);
  assertWireFields(
    value.quality_evidence,
    ARTIFACT_FIELDS.QualityEvidence,
    contract,
    `${path}.quality_evidence`,
  );
  assertOneOf(
    value.quality_evidence.status,
    GENERATED_RUNTIME_CONTRACT.artifact.qualityStatuses,
    contract,
    `${path}.quality_evidence.status`,
  );
  if (!Array.isArray(value.quality_evidence.checks)) {
    reject(contract, `${path}.quality_evidence.checks`, "an array");
  }
  value.quality_evidence.checks.forEach((check, index) => {
    const checkPath = `${path}.quality_evidence.checks[${index}]`;
    assertRecord(check, contract, checkPath);
    assertWireFields(check, ARTIFACT_FIELDS.QualityCheck, contract, checkPath);
    assertString(check.name, contract, `${checkPath}.name`);
    assertOneOf(
      check.status,
      GENERATED_RUNTIME_CONTRACT.artifact.qualityStatuses,
      contract,
      `${checkPath}.status`,
    );
    assertNullableString(check.detail, contract, `${checkPath}.detail`);
  });
  if (value.quality_evidence.score !== null) {
    assertFiniteNumber(value.quality_evidence.score, contract, `${path}.quality_evidence.score`);
    if (value.quality_evidence.score < 0 || value.quality_evidence.score > 1) {
      reject(contract, `${path}.quality_evidence.score`, "a value from 0 to 1");
    }
  }
  assertNullableString(
    value.quality_evidence.summary,
    contract,
    `${path}.quality_evidence.summary`,
  );
}

export function validateArtifactProjection(value: unknown): ArtifactProjection {
  assertArtifact(value);
  return value;
}

export function validateInputAttachmentProjection(value: unknown): InputAttachmentProjection {
  const contract = "InputAttachmentProjection";
  assertRecord(value, contract, "root");
  assertWireFields(value, INPUT_ATTACHMENT_FIELDS.InputAttachmentProjection, contract, "root");
  assertString(value.attachment_id, contract, "attachment_id");
  assertString(value.revision_id, contract, "revision_id");
  assertString(value.display_name, contract, "display_name");
  assertString(value.mime_type, contract, "mime_type");
  assertInteger(value.size_bytes, contract, "size_bytes");
  assertOneOf(value.media_kind, ["image", "document", "file"] as const, contract, "media_kind");
  assertString(value.sha256, contract, "sha256");
  if (!/^[0-9a-f]{64}$/u.test(value.sha256)) {
    reject(contract, "sha256", "a lowercase SHA-256");
  }
  assertString(value.created_at, contract, "created_at");
  if (!Number.isFinite(Date.parse(value.created_at))) {
    reject(contract, "created_at", "an ISO timestamp");
  }
  return value as unknown as InputAttachmentProjection;
}

function assertUsageWindow(value: unknown, path: string): void {
  const contract = "ConversationUsageProjection";
  assertRecord(value, contract, path);
  assertWireFields(
    value,
    ["input_tokens", "output_tokens", "total_tokens"],
    contract,
    path,
  );
  assertInteger(value.input_tokens, contract, `${path}.input_tokens`);
  assertInteger(value.output_tokens, contract, `${path}.output_tokens`);
  assertInteger(value.total_tokens, contract, `${path}.total_tokens`);
  if (value.total_tokens < value.input_tokens + value.output_tokens) {
    reject(contract, `${path}.total_tokens`, "at least input plus output tokens");
  }
}

export function validateConversationUsageProjection(value: unknown): ConversationUsageProjection {
  const contract = "ConversationUsageProjection";
  assertRecord(value, contract, "root");
  assertWireFields(
    value,
    CONVERSATION_USAGE_FIELDS.ConversationUsageProjection,
    contract,
    "root",
  );
  assertString(value.thread_id, contract, "thread_id");
  assertString(value.timezone, contract, "timezone");
  assertUsageWindow(value.today, "today");
  assertUsageWindow(value.week, "week");
  assertRecord(value.context, contract, "context");
  assertWireFields(
    value.context,
    ["used_tokens", "window_tokens", "model_id", "measured_at"],
    contract,
    "context",
  );
  if (value.context.used_tokens !== null) {
    assertInteger(value.context.used_tokens, contract, "context.used_tokens");
  }
  if (value.context.window_tokens !== null) {
    assertInteger(value.context.window_tokens, contract, "context.window_tokens", 1_000);
  }
  if (
    value.context.used_tokens !== null
    && value.context.window_tokens !== null
    && value.context.used_tokens > value.context.window_tokens
  ) {
    // This is not malformed: compaction may happen after the final provider
    // usage fact. Keep it renderable instead of making the UI invent a cap.
  }
  assertNullableString(value.context.model_id, contract, "context.model_id");
  assertNullableString(value.context.measured_at, contract, "context.measured_at");
  assertString(value.calculated_at, contract, "calculated_at");
  return value as unknown as ConversationUsageProjection;
}

/**
 * Projection/Item payloads are historical facts and may predate the loaded
 * WebUI contract.  Invalid artifacts are deliberately not rendered; callers
 * that own a transport boundary should use validateArtifactProjection and
 * surface the RuntimeContractError instead.
 */
export function tryValidateArtifactProjection(value: unknown): ArtifactProjection | null {
  try {
    return validateArtifactProjection(value);
  } catch (error) {
    if (error instanceof RuntimeContractError) return null;
    throw error;
  }
}

export function validateArtifactListResponse(value: unknown): ArtifactListResponse {
  const contract = "ArtifactListResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, ["items", "count"], contract, "root");
  if (!Array.isArray(value.items)) reject(contract, "items", "an array");
  value.items.forEach((item, index) => assertArtifact(item, `items[${index}]`));
  assertInteger(value.count, contract, "count");
  if (value.count !== value.items.length) {
    reject(contract, "count", "the exact items array length");
  }
  return { items: value.items, count: value.count };
}

function assertEvent(value: unknown): asserts value is EventEnvelope {
  const contract = "EventEnvelope";
  assertRecord(value, contract, "root");
  assertWireFields(value, EVENT_FIELDS.EventEnvelope, contract, "root");
  if (value.schema_version !== GENERATED_RUNTIME_CONTRACT.versions.eventEnvelope) {
    reject(
      contract,
      "schema_version",
      `literal ${GENERATED_RUNTIME_CONTRACT.versions.eventEnvelope}`,
    );
  }
  assertString(value.event_id, contract, "event_id");
  assertInteger(value.seq, contract, "seq", 1);
  assertString(value.thread_id, contract, "thread_id");
  for (const field of [
    "turn_id",
    "item_id",
    "job_id",
    "tool_call_id",
    "client_message_id",
    "causation_id",
    "correlation_id",
    "trace_id",
    "config_snapshot_id",
    "capability_snapshot_id",
    "permission_snapshot_id",
    "extension_snapshot_id",
  ] as const) {
    assertNullableString(value[field], contract, field);
  }
  assertString(value.event_type, contract, "event_type");
  assertString(value.created_at, contract, "created_at");
  assertRecord(value.payload, contract, "payload");
  if (value.event_type === "turn.accepted") {
    assertString(
      value.payload.agent_model_id,
      contract,
      "payload.agent_model_id",
    );
    assertNullableString(
      value.payload.image_model_id,
      contract,
      "payload.image_model_id",
    );
    if ("model" in value.payload) {
      reject(contract, "payload.model", "absent in the v1 protocol");
    }
  }
}

export function validateEventEnvelope(value: unknown): EventEnvelope {
  assertEvent(value);
  return value;
}

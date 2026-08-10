import type {
  KnowledgeDocument,
  KnowledgeGraph,
  KnowledgeImportResponse,
  KnowledgeNode,
  KnowledgeTree,
  MemoryContentDocument,
  MemoryContentItem,
  MemoryContentPage,
  MemoryMutationResponse,
  MemoryResetProjection,
  MemorySnapshot,
  MigrationQuarantineProjection,
  OutputLocationCatalog,
  OutputMaterialization,
  OutputPreference,
  SystemHealthSample,
  SystemMetricHistory,
} from "./contracts.ts";
import { GENERATED_SETTINGS_RUNTIME_CONTRACT } from "./generatedSettingsRuntimeContract.ts";

type SettingsContract =
  | "KnowledgeDocumentResponse"
  | "KnowledgeGraphResponse"
  | "KnowledgeImportResponse"
  | "KnowledgeTreeResponse"
  | "MemoryContentDocumentResponse"
  | "MemoryContentPageResponse"
  | "MemoryMutationResponse"
  | "MemorySnapshotResponse"
  | "MigrationQuarantineResponse"
  | "OutputLocationCatalogResponse"
  | "OutputMaterializationResponse"
  | "OutputPreferenceResponse"
  | "SystemHealthPublicResponse"
  | "SystemHealthTechnicalResponse"
  | "SystemMetricHistoryResponse";

const fields = GENERATED_SETTINGS_RUNTIME_CONTRACT.wireFields;
const values = GENERATED_SETTINGS_RUNTIME_CONTRACT.values;
const errorBrand = Symbol.for("ecorex.runtime-contract-error.v1");

class SettingsRuntimeContractError extends Error {
  readonly [errorBrand] = true;
  readonly contract: SettingsContract;
  readonly path: string;
  readonly expectation: string;

  constructor(contract: SettingsContract, path: string, expectation: string) {
    super("运行服务与页面不兼容，请刷新或更新 e-Mate。");
    this.name = "RuntimeContractError";
    this.contract = contract;
    this.path = path;
    this.expectation = expectation;
  }
}

function reject(contract: SettingsContract, path: string, expectation: string): never {
  throw new SettingsRuntimeContractError(contract, path, expectation);
}

function assertRecord(
  value: unknown,
  contract: SettingsContract,
  path: string,
): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject(contract, path, "an object");
  }
}

function assertFields(
  value: Record<string, unknown>,
  expectedFields: readonly string[],
  contract: SettingsContract,
  path: string,
): void {
  const expected = new Set(expectedFields);
  for (const field of expectedFields) {
    if (!Object.hasOwn(value, field)) {
      reject(contract, path === "root" ? field : `${path}.${field}`, "a backend-declared field");
    }
  }
  for (const field of Object.keys(value)) {
    if (!expected.has(field)) {
      reject(contract, path === "root" ? field : `${path}.${field}`, "no undeclared fields");
    }
  }
}

function assertString(
  value: unknown,
  contract: SettingsContract,
  path: string,
): asserts value is string {
  if (typeof value !== "string" || !value.trim()) reject(contract, path, "a non-empty string");
}

function assertBoolean(
  value: unknown,
  contract: SettingsContract,
  path: string,
): asserts value is boolean {
  if (typeof value !== "boolean") reject(contract, path, "a boolean");
}

function assertInteger(
  value: unknown,
  contract: SettingsContract,
  path: string,
  minimum = 0,
): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    reject(contract, path, `a safe integer >= ${minimum}`);
  }
}

function assertOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  contract: SettingsContract,
  path: string,
): asserts value is T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    reject(contract, path, allowed.join(" | "));
  }
}

function timestampValue(value: unknown, contract: SettingsContract, path: string): number {
  assertString(value, contract, path);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    reject(contract, path, "an RFC 3339 timestamp with an explicit timezone");
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) reject(contract, path, "a valid timestamp");
  return parsed;
}

function assertDigest(value: unknown, contract: SettingsContract, path: string): void {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    reject(contract, path, "a lowercase SHA-256 digest");
  }
}

function assertBoundedJson(
  value: unknown,
  contract: SettingsContract,
  path: string,
  depth = 0,
): void {
  if (depth > 8) reject(contract, path, "bounded JSON nesting");
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) reject(contract, path, "a finite JSON number");
    return;
  }
  if (typeof value === "string") {
    if (value.length > 4096) reject(contract, path, "a bounded JSON string");
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 128) reject(contract, path, "a bounded JSON array");
    value.forEach((item, index) => assertBoundedJson(item, contract, `${path}[${index}]`, depth + 1));
    return;
  }
  assertRecord(value, contract, path);
  const entries = Object.entries(value);
  if (entries.length > 128) reject(contract, path, "a bounded JSON object");
  for (const [key, item] of entries) {
    if (!key || key.length > 64) reject(contract, path, "bounded JSON keys");
    assertBoundedJson(item, contract, `${path}.${key}`, depth + 1);
  }
}

function assertMemoryReset(
  value: unknown,
  contract: "MemoryMutationResponse" | "MemorySnapshotResponse",
  path: string,
): asserts value is MemoryResetProjection {
  assertRecord(value, contract, path);
  assertFields(value, fields.MemoryMutationResponse.MemoryResetProjectionResponse, contract, path);
  assertString(value.reset_id, contract, `${path}.reset_id`);
  assertOneOf(value.status, values.memoryResetStatuses, contract, `${path}.status`);
  assertInteger(value.affected_records, contract, `${path}.affected_records`);
  assertInteger(value.affected_files, contract, `${path}.affected_files`);
  const createdAt = timestampValue(value.created_at, contract, `${path}.created_at`);
  const undoUntil = timestampValue(value.undo_until, contract, `${path}.undo_until`);
  const updatedAt = timestampValue(value.updated_at, contract, `${path}.updated_at`);
  assertBoolean(value.can_undo, contract, `${path}.can_undo`);
  if (undoUntil < createdAt || updatedAt < createdAt) {
    reject(contract, path, "a consistent memory reset timeline");
  }
  if (value.status !== "active" && value.can_undo) {
    reject(contract, `${path}.can_undo`, "false after an active reset ends");
  }
}

function assertKnowledgeDocument(
  value: unknown,
  contract: "KnowledgeDocumentResponse" | "KnowledgeImportResponse",
  rootFields: readonly string[],
  path: string,
): asserts value is KnowledgeDocument {
  assertRecord(value, contract, path);
  assertFields(value, rootFields, contract, path);
  assertString(value.path, contract, `${path}.path`);
  assertString(value.name, contract, `${path}.name`);
  if (typeof value.content !== "string" || value.content.length > 10 * 1024 * 1024) {
    reject(contract, `${path}.content`, "bounded UTF-8 knowledge text");
  }
  assertInteger(value.size_bytes, contract, `${path}.size_bytes`);
  if ((value.size_bytes as number) > 10 * 1024 * 1024) {
    reject(contract, `${path}.size_bytes`, "at most 10 MiB");
  }
  timestampValue(value.updated_at, contract, `${path}.updated_at`);
  if (!Array.isArray(value.links) || value.links.length > 10_000) {
    reject(contract, `${path}.links`, "a bounded link list");
  }
  const links = new Set<string>();
  value.links.forEach((link, index) => {
    assertString(link, contract, `${path}.links[${index}]`);
    if (links.has(link)) reject(contract, `${path}.links[${index}]`, "a unique link");
    links.add(link);
  });
}

function assertKnowledgeNode(
  value: unknown,
  contract: "KnowledgeTreeResponse",
  path: string,
  depth = 0,
): asserts value is KnowledgeNode {
  if (depth > 32) reject(contract, path, "a knowledge tree no deeper than 32 levels");
  assertRecord(value, contract, path);
  assertFields(value, fields.KnowledgeTreeResponse.KnowledgeNodeResponse, contract, path);
  if (typeof value.path !== "string") reject(contract, `${path}.path`, "a relative path");
  assertString(value.name, contract, `${path}.name`);
  assertOneOf(value.kind, values.knowledgeNodeKinds, contract, `${path}.kind`);
  assertInteger(value.size_bytes, contract, `${path}.size_bytes`);
  timestampValue(value.updated_at, contract, `${path}.updated_at`);
  if (!Array.isArray(value.children) || value.children.length > 10_000) {
    reject(contract, `${path}.children`, "a bounded child list");
  }
  if (value.kind === "document" && value.children.length !== 0) {
    reject(contract, `${path}.children`, "no children for a document");
  }
  value.children.forEach((child, index) => (
    assertKnowledgeNode(child, contract, `${path}.children[${index}]`, depth + 1)
  ));
}

export function validateKnowledgeTree(value: unknown): KnowledgeTree {
  const contract = "KnowledgeTreeResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.KnowledgeTreeResponse.KnowledgeTreeResponse, contract, "root");
  if (value.root !== "knowledge") reject(contract, "root", "the knowledge authority");
  if (value.query !== null && (typeof value.query !== "string" || value.query.length > 256)) {
    reject(contract, "query", "a bounded search query");
  }
  if (!Array.isArray(value.items) || value.items.length > 10_000) {
    reject(contract, "items", "a bounded knowledge tree");
  }
  value.items.forEach((item, index) => assertKnowledgeNode(item, contract, `items[${index}]`));
  return value as unknown as KnowledgeTree;
}

export function validateKnowledgeNode(value: unknown): KnowledgeNode {
  assertKnowledgeNode(value, "KnowledgeTreeResponse", "root");
  return value;
}

export function validateKnowledgeDocument(value: unknown): KnowledgeDocument {
  assertKnowledgeDocument(
    value,
    "KnowledgeDocumentResponse",
    fields.KnowledgeDocumentResponse.KnowledgeDocumentResponse,
    "root",
  );
  return value;
}

export function validateKnowledgeGraph(value: unknown): KnowledgeGraph {
  const contract = "KnowledgeGraphResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.KnowledgeGraphResponse.KnowledgeGraphResponse, contract, "root");
  if (!Array.isArray(value.nodes) || value.nodes.length > 5_000) {
    reject(contract, "nodes", "a bounded graph node list");
  }
  const nodes = new Set<string>();
  value.nodes.forEach((node, index) => {
    const path = `nodes[${index}]`;
    assertRecord(node, contract, path);
    assertFields(node, fields.KnowledgeGraphResponse.KnowledgeGraphNodeResponse, contract, path);
    assertString(node.path, contract, `${path}.path`);
    assertString(node.label, contract, `${path}.label`);
    if (nodes.has(node.path)) reject(contract, `${path}.path`, "a unique graph node");
    nodes.add(node.path);
  });
  if (!Array.isArray(value.edges) || value.edges.length > 20_000) {
    reject(contract, "edges", "a bounded graph edge list");
  }
  value.edges.forEach((edge, index) => {
    const path = `edges[${index}]`;
    assertRecord(edge, contract, path);
    assertFields(edge, fields.KnowledgeGraphResponse.KnowledgeGraphEdgeResponse, contract, path);
    assertString(edge.source, contract, `${path}.source`);
    assertString(edge.target, contract, `${path}.target`);
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) {
      reject(contract, path, "an edge between declared graph nodes");
    }
  });
  return value as unknown as KnowledgeGraph;
}

export function validateKnowledgeImport(value: unknown): KnowledgeImportResponse {
  const contract = "KnowledgeImportResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.KnowledgeImportResponse.KnowledgeImportResponse, contract, "root");
  assertInteger(value.imported_count, contract, "imported_count");
  assertInteger(value.rejected_count, contract, "rejected_count");
  assertInteger(value.total_bytes, contract, "total_bytes");
  if (
    (value.imported_count as number) > 100
    || (value.rejected_count as number) > 100
    || (value.total_bytes as number) > 200 * 1024 * 1024
  ) {
    reject(contract, "root", "the bounded import contract");
  }
  if (
    !Array.isArray(value.items)
    || value.items.length !== (value.imported_count as number) + (value.rejected_count as number)
  ) {
    reject(contract, "items", "one result per submitted document");
  }
  let imported = 0;
  let rejected = 0;
  value.items.forEach((item, index) => {
    const path = `items[${index}]`;
    assertRecord(item, contract, path);
    assertFields(item, fields.KnowledgeImportResponse.KnowledgeImportItemResponse, contract, path);
    assertString(item.original_name, contract, `${path}.original_name`);
    assertOneOf(item.status, ["imported", "renamed", "rejected"] as const, contract, `${path}.status`);
    if (item.status === "rejected") {
      if (item.name !== null || item.path !== null) reject(contract, path, "no target for a rejected file");
      assertString(item.reason, contract, `${path}.reason`);
      rejected += 1;
    } else {
      assertString(item.name, contract, `${path}.name`);
      assertString(item.path, contract, `${path}.path`);
      if (item.reason !== null) reject(contract, `${path}.reason`, "null for an imported file");
      imported += 1;
    }
  });
  if (imported !== value.imported_count || rejected !== value.rejected_count) {
    reject(contract, "items", "counts matching per-file statuses");
  }
  return value as unknown as KnowledgeImportResponse;
}

function assertMemoryContentItem(
  value: unknown,
  contract: "MemoryContentPageResponse" | "MemoryContentDocumentResponse",
  rootFields: readonly string[],
  path: string,
): asserts value is MemoryContentItem {
  assertRecord(value, contract, path);
  assertFields(value, rootFields, contract, path);
  for (const field of ["item_id", "name", "path", "source"] as const) {
    assertString(value[field], contract, `${path}.${field}`);
  }
  assertOneOf(value.kind, values.memoryContentKinds, contract, `${path}.kind`);
  assertOneOf(value.origin, values.memoryContentOrigins, contract, `${path}.origin`);
  assertInteger(value.size_bytes, contract, `${path}.size_bytes`);
  if ((value.size_bytes as number) > 10 * 1024 * 1024) {
    reject(contract, `${path}.size_bytes`, "at most 10 MiB");
  }
  if (value.updated_at !== null) timestampValue(value.updated_at, contract, `${path}.updated_at`);
}

export function validateMemoryContentPage(value: unknown): MemoryContentPage {
  const contract = "MemoryContentPageResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.MemoryContentPageResponse.MemoryContentPageResponse, contract, "root");
  assertOneOf(value.view, values.memoryContentViews, contract, "view");
  assertInteger(value.page, contract, "page", 1);
  if (value.page_size !== 10) reject(contract, "page_size", "the fixed page size 10");
  assertInteger(value.total, contract, "total");
  if (!Array.isArray(value.items) || value.items.length > 10) {
    reject(contract, "items", "at most 10 memory items");
  }
  value.items.forEach((item, index) => {
    assertMemoryContentItem(
      item,
      contract,
      fields.MemoryContentPageResponse.MemoryContentItemResponse,
      `items[${index}]`,
    );
    if ((value.view === "files") !== (item.kind === "file")) {
      reject(contract, `items[${index}].kind`, "the selected memory view");
    }
  });
  return value as unknown as MemoryContentPage;
}

export function validateMemoryContentDocument(value: unknown): MemoryContentDocument {
  const contract = "MemoryContentDocumentResponse";
  assertMemoryContentItem(
    value,
    contract,
    fields.MemoryContentDocumentResponse.MemoryContentDocumentResponse,
    "root",
  );
  const content = (value as MemoryContentItem & { content?: unknown }).content;
  if (typeof content !== "string" || content.length > 10 * 1024 * 1024) {
    reject(contract, "content", "bounded memory text");
  }
  return value as MemoryContentDocument;
}

function assertMemorySnapshot(
  value: unknown,
  contract: "MemoryMutationResponse" | "MemorySnapshotResponse",
  rootFields: readonly string[],
  path: string,
): asserts value is MemorySnapshot {
  assertRecord(value, contract, path);
  assertFields(value, rootFields, contract, path);
  for (const field of [
    "revision",
    "active_learned_records",
    "active_user_files",
    "factory_records",
    "tombstoned_records",
    "tombstoned_files",
    "resettable_count",
  ] as const) {
    assertInteger(value[field], contract, `${path}.${field}`);
  }
  const resettableCount = value.resettable_count as number;
  const activeLearnedRecords = value.active_learned_records as number;
  const activeUserFiles = value.active_user_files as number;
  if (resettableCount !== activeLearnedRecords + activeUserFiles) {
    reject(contract, `${path}.resettable_count`, "the backend-derived resettable count");
  }
  if (value.latest_reset !== null) assertMemoryReset(value.latest_reset, contract, `${path}.latest_reset`);
}

export function validateMemorySnapshot(value: unknown): MemorySnapshot {
  assertMemorySnapshot(
    value,
    "MemorySnapshotResponse",
    fields.MemorySnapshotResponse.MemorySnapshotResponse,
    "root",
  );
  return value;
}

export function validateMemoryMutationResponse(value: unknown): MemoryMutationResponse {
  const contract = "MemoryMutationResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.MemoryMutationResponse.MemoryMutationResponse, contract, "root");
  assertMemorySnapshot(
    value.memory,
    contract,
    fields.MemoryMutationResponse.MemorySnapshotResponse,
    "memory",
  );
  assertMemoryReset(value.reset, contract, "reset");
  if (value.memory.latest_reset?.reset_id !== value.reset.reset_id) {
    reject(contract, "reset.reset_id", "the authoritative latest reset identity");
  }
  return value as unknown as MemoryMutationResponse;
}

export function validateMigrationQuarantineProjection(
  value: unknown,
): MigrationQuarantineProjection {
  const contract = "MigrationQuarantineResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.MigrationQuarantineResponse.MigrationQuarantineResponse, contract, "root");
  assertOneOf(value.status, values.migrationQuarantineStatuses, contract, "status");
  assertInteger(value.entry_count, contract, "entry_count");
  assertBoolean(value.can_delete, contract, "can_delete");
  const deletedAt = value.deleted_at === null ? null : timestampValue(value.deleted_at, contract, "deleted_at");
  if (!Array.isArray(value.items) || value.items.length > 64) {
    reject(contract, "items", "an array with at most 64 aggregate categories");
  }
  const identities = new Set<string>();
  let itemCount = 0;
  value.items.forEach((item, index) => {
    const path = `items[${index}]`;
    assertRecord(item, contract, path);
    assertFields(item, fields.MigrationQuarantineResponse.MigrationQuarantineItemResponse, contract, path);
    assertOneOf(item.kind, values.migrationCredentialKinds, contract, `${path}.kind`);
    assertOneOf(item.origin, values.migrationCredentialOrigins, contract, `${path}.origin`);
    assertInteger(item.count, contract, `${path}.count`, 1);
    const identity = `${item.kind}:${item.origin}`;
    if (identities.has(identity)) reject(contract, path, "one aggregate per kind and origin");
    identities.add(identity);
    itemCount += item.count;
  });
  const absent = value.status === "absent" && value.entry_count === 0
    && !value.can_delete && deletedAt === null && value.items.length === 0;
  const available = value.status === "available" && value.entry_count > 0
    && value.can_delete && deletedAt === null && itemCount === value.entry_count;
  const deleted = value.status === "deleted" && value.entry_count > 0
    && !value.can_delete && deletedAt !== null && itemCount === value.entry_count;
  if (!absent && !available && !deleted) {
    reject(contract, "root", "a consistent quarantine lifecycle projection");
  }
  return value as unknown as MigrationQuarantineProjection;
}

export function validateOutputLocationCatalog(value: unknown): OutputLocationCatalog {
  const contract = "OutputLocationCatalogResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.OutputLocationCatalogResponse.OutputLocationCatalogResponse, contract, "root");
  if (!Array.isArray(value.items) || value.items.length !== values.outputLocationAliases.length) {
    reject(contract, "items", "the complete output location catalog");
  }
  const aliases = new Set<string>();
  value.items.forEach((item, index) => {
    const path = `items[${index}]`;
    assertRecord(item, contract, path);
    assertFields(item, fields.OutputLocationCatalogResponse.OutputLocationOptionResponse, contract, path);
    assertOneOf(item.alias, values.outputLocationAliases, contract, `${path}.alias`);
    assertBoolean(item.available, contract, `${path}.available`);
    if (aliases.has(item.alias)) reject(contract, `${path}.alias`, "a unique output location alias");
    aliases.add(item.alias);
  });
  if (aliases.size !== values.outputLocationAliases.length) {
    reject(contract, "items", "every backend-declared output location alias");
  }
  return value as unknown as OutputLocationCatalog;
}

export function validateOutputPreference(value: unknown): OutputPreference {
  const contract = "OutputPreferenceResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.OutputPreferenceResponse.OutputPreferenceResponse, contract, "root");
  assertString(value.account_id, contract, "account_id");
  assertOneOf(value.location_alias, values.outputLocationAliases, contract, "location_alias");
  assertInteger(value.revision, contract, "revision", 1);
  if (typeof value.output_policy_snapshot_id !== "string"
    || !/^outpol_[0-9a-f]{64}$/.test(value.output_policy_snapshot_id)) {
    reject(contract, "output_policy_snapshot_id", "an immutable output policy identity");
  }
  timestampValue(value.updated_at, contract, "updated_at");
  return value as unknown as OutputPreference;
}

export function validateOutputMaterialization(
  value: unknown,
  expected?: Readonly<{ artifact_id: string; revision_id: string }>,
): OutputMaterialization {
  const contract = "OutputMaterializationResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.OutputMaterializationResponse.OutputMaterializationResponse, contract, "root");
  if (typeof value.materialization_id !== "string"
    || !/^mat_[0-9a-f]{64}$/.test(value.materialization_id)) {
    reject(contract, "materialization_id", "an immutable materialization identity");
  }
  assertString(value.artifact_id, contract, "artifact_id");
  assertString(value.revision_id, contract, "revision_id");
  if (expected && (value.artifact_id !== expected.artifact_id || value.revision_id !== expected.revision_id)) {
    reject(contract, "root", "the requested artifact revision identity");
  }
  if (typeof value.output_policy_snapshot_id !== "string"
    || !/^outpol_[0-9a-f]{64}$/.test(value.output_policy_snapshot_id)) {
    reject(contract, "output_policy_snapshot_id", "an immutable output policy identity");
  }
  assertOneOf(value.location_alias, values.outputLocationAliases, contract, "location_alias");
  assertString(value.display_name, contract, "display_name");
  assertDigest(value.sha256, contract, "sha256");
  assertInteger(value.size_bytes, contract, "size_bytes");
  assertOneOf(value.status, values.outputMaterializationStatuses, contract, "status");
  assertBoolean(value.reused_existing, contract, "reused_existing");
  const createdAt = timestampValue(value.created_at, contract, "created_at");
  const completedAt = value.completed_at === null ? null : timestampValue(value.completed_at, contract, "completed_at");
  if ((value.status === "completed") !== (completedAt !== null)) {
    reject(contract, "completed_at", "a timestamp exactly when status is completed");
  }
  if (completedAt !== null && completedAt < createdAt) {
    reject(contract, "completed_at", "a timestamp after creation");
  }
  return value as unknown as OutputMaterialization;
}

function assertSystemHealth(
  value: unknown,
  technical: boolean,
  contract: "SystemHealthPublicResponse" | "SystemHealthTechnicalResponse" | "SystemMetricHistoryResponse",
  rootFields: readonly string[],
  componentFields: readonly string[],
  path: string,
): asserts value is SystemHealthSample {
  assertRecord(value, contract, path);
  assertFields(value, rootFields, contract, path);
  assertString(value.sample_id, contract, `${path}.sample_id`);
  assertOneOf(value.overall, values.systemHealthStatuses, contract, `${path}.overall`);
  assertString(value.summary, contract, `${path}.summary`);
  if (!Array.isArray(value.components) || value.components.length < 1 || value.components.length > 16) {
    reject(contract, `${path}.components`, "between 1 and 16 health components");
  }
  const identities = new Set<string>();
  let worst = -1;
  const order = new Map(values.systemHealthStatuses.map((status, index) => [status, index]));
  value.components.forEach((component, index) => {
    const componentPath = `${path}.components[${index}]`;
    assertRecord(component, contract, componentPath);
    assertFields(component, componentFields, contract, componentPath);
    assertString(component.component_id, contract, `${componentPath}.component_id`);
    assertString(component.label, contract, `${componentPath}.label`);
    assertOneOf(component.status, values.systemHealthStatuses, contract, `${componentPath}.status`);
    assertString(component.message, contract, `${componentPath}.message`);
    if (identities.has(component.component_id)) {
      reject(contract, `${componentPath}.component_id`, "a unique component identity");
    }
    identities.add(component.component_id);
    worst = Math.max(worst, order.get(component.status) ?? -1);
  });
  if (values.systemHealthStatuses[worst] !== value.overall) {
    reject(contract, `${path}.overall`, "the worst component status");
  }
  timestampValue(value.sampled_at, contract, `${path}.sampled_at`);
  if (technical) {
    assertRecord(value.metrics, contract, `${path}.metrics`);
    assertFields(value.metrics, ["runtime", "process", "storage", "services"], contract, `${path}.metrics`);
    for (const group of ["runtime", "process", "storage", "services"] as const) {
      assertRecord(value.metrics[group], contract, `${path}.metrics.${group}`);
      assertBoundedJson(value.metrics[group], contract, `${path}.metrics.${group}`);
    }
  }
}

export function validateSystemHealthSample(
  value: unknown,
  options: { technical?: boolean } = {},
): SystemHealthSample {
  if (options.technical === true) {
    assertSystemHealth(
      value,
      true,
      "SystemHealthTechnicalResponse",
      fields.SystemHealthTechnicalResponse.SystemHealthTechnicalResponse,
      fields.SystemHealthTechnicalResponse.SystemHealthComponentResponse,
      "root",
    );
  } else {
    assertSystemHealth(
      value,
      false,
      "SystemHealthPublicResponse",
      fields.SystemHealthPublicResponse.SystemHealthPublicResponse,
      fields.SystemHealthPublicResponse.SystemHealthComponentResponse,
      "root",
    );
  }
  return value;
}

export function validateSystemMetricHistory(value: unknown): SystemMetricHistory {
  const contract = "SystemMetricHistoryResponse";
  assertRecord(value, contract, "root");
  assertFields(value, fields.SystemMetricHistoryResponse.SystemMetricHistoryResponse, contract, "root");
  if (!Array.isArray(value.items) || value.items.length > 200) {
    reject(contract, "items", "an array with at most 200 system samples");
  }
  value.items.forEach((item, index) => assertSystemHealth(
    item,
    true,
    contract,
    fields.SystemMetricHistoryResponse.SystemHealthTechnicalResponse,
    fields.SystemMetricHistoryResponse.SystemHealthComponentResponse,
    `items[${index}]`,
  ));
  return value as unknown as SystemMetricHistory;
}

/**
 * Compact dispatcher IDs keep the progressively loaded boundary out of the
 * initial bundle: memory, memory mutation, migration, output locations,
 * output preference, materialization, public health, technical health,
 * metric history.
 */
export type SettingsBoundaryKind = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15;

export function validateSettingsBoundary(
  kind: SettingsBoundaryKind,
  value: unknown,
  expected?: Readonly<{ artifact_id: string; revision_id: string }>,
): unknown {
  switch (kind) {
    case 0: return validateMemorySnapshot(value);
    case 1: return validateMemoryMutationResponse(value);
    case 2: return validateMigrationQuarantineProjection(value);
    case 3: return validateOutputLocationCatalog(value);
    case 4: return validateOutputPreference(value);
    case 5: return validateOutputMaterialization(value, expected);
    case 6: return validateSystemHealthSample(value);
    case 7: return validateSystemHealthSample(value, { technical: true });
    case 8: return validateSystemMetricHistory(value);
    case 9: return validateKnowledgeTree(value);
    case 10: return validateKnowledgeDocument(value);
    case 11: return validateKnowledgeGraph(value);
    case 12: return validateKnowledgeImport(value);
    case 13: return validateMemoryContentPage(value);
    case 14: return validateMemoryContentDocument(value);
    case 15: return validateKnowledgeNode(value);
  }
}

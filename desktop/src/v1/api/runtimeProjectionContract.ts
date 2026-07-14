import type {
  InteractionProjection,
  ItemProjection,
  JobProjection,
  ReplaceTurnResponse,
  ThreadListResponse,
  ThreadProjection,
  ThreadProjectionResponse,
  TurnMutationResponse,
  TurnProjection,
} from "./contracts.ts";
import { GENERATED_RUNTIME_PROJECTION_CONTRACT } from "./generatedRuntimeProjectionContract.ts";

const fields = GENERATED_RUNTIME_PROJECTION_CONTRACT.wireFields;
const values = GENERATED_RUNTIME_PROJECTION_CONTRACT.runtime;
const errorBrand = Symbol.for("ecorex.runtime-contract-error.v1");
type ContractName = keyof typeof fields;

class ProjectionContractError extends Error {
  readonly [errorBrand] = true;
  readonly contract: ContractName;
  readonly path: string;
  readonly expectation: string;

  constructor(contract: ContractName, path: string, expectation: string) {
    super("运行服务返回的数据版本与当前页面不兼容，请刷新或更新 EcoreX。");
    this.name = "RuntimeContractError";
    this.contract = contract;
    this.path = path;
    this.expectation = expectation;
  }
}

function reject(contract: ContractName, path: string, expectation: string): never {
  throw new ProjectionContractError(contract, path, expectation);
}

function assertRecord(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    reject(contract, path, "an object");
  }
}

function assertWireFields(
  value: Record<string, unknown>,
  expectedFields: readonly string[],
  contract: ContractName,
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
      reject(contract, path === "root" ? field : `${path}.${field}`, "no undeclared wire fields");
    }
  }
}

function assertString(
  value: unknown,
  contract: ContractName,
  path: string,
  allowEmpty = false,
): asserts value is string {
  if (typeof value !== "string" || (!allowEmpty && !value.trim())) {
    reject(contract, path, allowEmpty ? "a string" : "a non-empty string");
  }
}

function assertNullableString(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is string | null {
  if (value !== null) assertString(value, contract, path);
}

function assertBoolean(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is boolean {
  if (typeof value !== "boolean") reject(contract, path, "a boolean");
}

function assertInteger(
  value: unknown,
  contract: ContractName,
  path: string,
  minimum = 0,
): asserts value is number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) {
    reject(contract, path, `an integer >= ${minimum}`);
  }
}

function assertSignedInteger(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is number {
  if (typeof value !== "number" || !Number.isInteger(value)) reject(contract, path, "an integer");
}

function assertTimestamp(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is string {
  assertString(value, contract, path);
  if (!Number.isFinite(Date.parse(value))) reject(contract, path, "an ISO timestamp");
}

function assertStringArray(
  value: unknown,
  contract: ContractName,
  path: string,
): asserts value is string[] {
  if (!Array.isArray(value)) reject(contract, path, "an array");
  value.forEach((entry, index) => assertString(entry, contract, `${path}[${index}]`));
}

function assertOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  contract: ContractName,
  path: string,
): asserts value is T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    reject(contract, path, allowed.join(" | "));
  }
}

function thread(value: unknown, path = "root"): asserts value is ThreadProjection {
  const contract = "ThreadProjection";
  assertRecord(value, contract, path);
  assertWireFields(value, fields.ThreadProjection.ThreadProjection, contract, path);
  assertString(value.thread_id, contract, `${path}.thread_id`);
  assertOneOf(value.status, values.threadStatuses, contract, `${path}.status`);
  assertNullableString(value.title, contract, `${path}.title`);
  assertRecord(value.metadata, contract, `${path}.metadata`);
  assertNullableString(value.forked_from_thread_id, contract, `${path}.forked_from_thread_id`);
  assertNullableString(value.forked_from_turn_id, contract, `${path}.forked_from_turn_id`);
  if (value.forked_from_seq !== null) {
    assertInteger(value.forked_from_seq, contract, `${path}.forked_from_seq`);
  }
  assertTimestamp(value.created_at, contract, `${path}.created_at`);
  assertTimestamp(value.updated_at, contract, `${path}.updated_at`);
}

function turn(value: unknown, path = "root"): asserts value is TurnProjection {
  const contract = "TurnProjection";
  assertRecord(value, contract, path);
  assertWireFields(value, fields.TurnProjection.TurnProjection, contract, path);
  assertString(value.turn_id, contract, `${path}.turn_id`);
  assertString(value.thread_id, contract, `${path}.thread_id`);
  assertOneOf(value.status, values.turnStatuses, contract, `${path}.status`);
  assertString(value.input, contract, `${path}.input`, true);
  assertString(value.agent_model_id, contract, `${path}.agent_model_id`);
  assertNullableString(value.image_model_id, contract, `${path}.image_model_id`);
  assertNullableString(value.client_message_id, contract, `${path}.client_message_id`);
  assertRecord(value.metadata, contract, `${path}.metadata`);
  assertNullableString(value.terminal_reason, contract, `${path}.terminal_reason`);
  assertBoolean(value.inherited, contract, `${path}.inherited`);
  assertTimestamp(value.created_at, contract, `${path}.created_at`);
  assertTimestamp(value.updated_at, contract, `${path}.updated_at`);
}

function item(value: unknown, path = "root"): asserts value is ItemProjection {
  const contract = "ItemProjection";
  assertRecord(value, contract, path);
  assertWireFields(value, fields.ItemProjection.ItemProjection, contract, path);
  assertString(value.item_id, contract, `${path}.item_id`);
  assertString(value.thread_id, contract, `${path}.thread_id`);
  assertString(value.turn_id, contract, `${path}.turn_id`);
  assertOneOf(value.kind, values.itemKinds, contract, `${path}.kind`);
  assertOneOf(value.status, values.itemStatuses, contract, `${path}.status`);
  assertRecord(value.content, contract, `${path}.content`);
  assertBoolean(value.inherited, contract, `${path}.inherited`);
  assertTimestamp(value.created_at, contract, `${path}.created_at`);
  assertTimestamp(value.updated_at, contract, `${path}.updated_at`);
}

function job(value: unknown, path = "root"): asserts value is JobProjection {
  const contract = "JobProjection";
  assertRecord(value, contract, path);
  assertWireFields(value, fields.JobProjection.JobProjection, contract, path);
  assertString(value.job_id, contract, `${path}.job_id`);
  assertString(value.kind, contract, `${path}.kind`);
  assertOneOf(value.status, values.jobStatuses, contract, `${path}.status`);
  assertSignedInteger(value.priority, contract, `${path}.priority`);
  assertInteger(value.attempt, contract, `${path}.attempt`);
  assertInteger(value.max_attempts, contract, `${path}.max_attempts`, 1);
  assertNullableString(value.thread_id, contract, `${path}.thread_id`);
  assertNullableString(value.turn_id, contract, `${path}.turn_id`);
  assertTimestamp(value.available_at, contract, `${path}.available_at`);
  if (value.deadline !== null) assertTimestamp(value.deadline, contract, `${path}.deadline`);
  assertNullableString(value.reason_code, contract, `${path}.reason_code`);
  assertTimestamp(value.created_at, contract, `${path}.created_at`);
  assertTimestamp(value.updated_at, contract, `${path}.updated_at`);
}

function interactionContract(value: unknown, path: string): void {
  const contract = "InteractionProjection";
  const nested = fields.ThreadProjectionResponse;
  assertRecord(value, contract, path);
  assertWireFields(value, nested.InteractionContract, contract, path);
  if (value.schema_version !== 1) reject(contract, `${path}.schema_version`, "literal 1");
  assertString(value.title, contract, `${path}.title`);
  if (!Array.isArray(value.fields)) reject(contract, `${path}.fields`, "an array");
  value.fields.forEach((field, index) => {
    const fieldPath = `${path}.fields[${index}]`;
    assertRecord(field, contract, fieldPath);
    assertWireFields(field, nested.InteractionFormField, contract, fieldPath);
    assertString(field.field_id, contract, `${fieldPath}.field_id`);
    assertString(field.label, contract, `${fieldPath}.label`);
    assertOneOf(field.control, values.interactionFieldControls, contract, `${fieldPath}.control`);
    assertBoolean(field.required, contract, `${fieldPath}.required`);
    assertNullableString(field.description, contract, `${fieldPath}.description`);
    assertNullableString(field.placeholder, contract, `${fieldPath}.placeholder`);
    assertInteger(field.min_length, contract, `${fieldPath}.min_length`);
    assertInteger(field.max_length, contract, `${fieldPath}.max_length`);
    if (!Array.isArray(field.options)) reject(contract, `${fieldPath}.options`, "an array");
    field.options.forEach((option, optionIndex) => {
      const optionPath = `${fieldPath}.options[${optionIndex}]`;
      assertRecord(option, contract, optionPath);
      assertWireFields(option, nested.InteractionChoice, contract, optionPath);
      assertString(option.option_id, contract, `${optionPath}.option_id`);
      assertString(option.label, contract, `${optionPath}.label`);
      assertNullableString(option.description, contract, `${optionPath}.description`);
    });
    if (field.sensitive !== false) reject(contract, `${fieldPath}.sensitive`, "literal false");
  });
  if (!Array.isArray(value.actions)) reject(contract, `${path}.actions`, "an array");
  value.actions.forEach((action, index) => {
    const actionPath = `${path}.actions[${index}]`;
    assertRecord(action, contract, actionPath);
    assertWireFields(action, nested.InteractionAction, contract, actionPath);
    assertString(action.action_id, contract, `${actionPath}.action_id`);
    assertString(action.label, contract, `${actionPath}.label`);
    assertOneOf(action.action_type, values.interactionActionTypes, contract, `${actionPath}.action_type`);
    assertOneOf(action.style, values.interactionActionStyles, contract, `${actionPath}.style`);
    assertBoolean(action.submits_form, contract, `${actionPath}.submits_form`);
  });
  if (value.connector !== null) {
    const connectorPath = `${path}.connector`;
    assertRecord(value.connector, contract, connectorPath);
    assertWireFields(value.connector, nested.InteractionConnectorContext, contract, connectorPath);
    assertString(value.connector.connector_id, contract, `${connectorPath}.connector_id`);
    assertString(value.connector.display_name, contract, `${connectorPath}.display_name`);
    assertOneOf(value.connector.state, values.connectorInteractionStates, contract, `${connectorPath}.state`);
    assertStringArray(value.connector.required_action_ids, contract, `${connectorPath}.required_action_ids`);
  }
}

function interaction(value: unknown, path = "root"): asserts value is InteractionProjection {
  const contract = "InteractionProjection";
  const nested = fields.ThreadProjectionResponse;
  assertRecord(value, contract, path);
  assertWireFields(value, fields.InteractionProjection.InteractionProjection, contract, path);
  assertString(value.interaction_id, contract, `${path}.interaction_id`);
  assertOneOf(value.kind, values.interactionKinds, contract, `${path}.kind`);
  assertOneOf(value.status, values.interactionStatuses, contract, `${path}.status`);
  assertString(value.prompt, contract, `${path}.prompt`);
  interactionContract(value.contract, `${path}.contract`);
  if (!Array.isArray(value.options)) reject(contract, `${path}.options`, "an array");
  value.options.forEach((option, index) => assertRecord(option, contract, `${path}.options[${index}]`));
  if (value.response !== null) {
    assertRecord(value.response, contract, `${path}.response`);
    assertWireFields(value.response, nested.InteractionResponse, contract, `${path}.response`);
    assertString(value.response.action_id, contract, `${path}.response.action_id`);
    assertRecord(value.response.values, contract, `${path}.response.values`);
    for (const [name, responseValue] of Object.entries(value.response.values)) {
      if (typeof responseValue !== "string" && typeof responseValue !== "boolean") {
        reject(contract, `${path}.response.values.${name}`, "a string or boolean");
      }
    }
  }
  assertNullableString(value.response_client_request_id, contract, `${path}.response_client_request_id`);
  assertString(value.thread_id, contract, `${path}.thread_id`);
  assertNullableString(value.turn_id, contract, `${path}.turn_id`);
  assertNullableString(value.job_id, contract, `${path}.job_id`);
  if (value.expires_at !== null) assertTimestamp(value.expires_at, contract, `${path}.expires_at`);
  assertTimestamp(value.created_at, contract, `${path}.created_at`);
  assertTimestamp(value.updated_at, contract, `${path}.updated_at`);
}

export function validateThreadProjection(value: unknown): ThreadProjection {
  thread(value);
  return value;
}

export function validateThreadListResponse(value: unknown): ThreadListResponse {
  const contract = "ThreadListResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, fields.ThreadListResponse.ThreadListResponse, contract, "root");
  if (!Array.isArray(value.items)) reject(contract, "items", "an array");
  value.items.forEach((entry, index) => thread(entry, `items[${index}]`));
  assertNullableString(value.next_cursor, contract, "next_cursor");
  return value as unknown as ThreadListResponse;
}

export function validateThreadProjectionResponse(value: unknown): ThreadProjectionResponse {
  const contract = "ThreadProjectionResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, fields.ThreadProjectionResponse.ThreadProjectionResponse, contract, "root");
  thread(value.thread, "thread");
  if (!Array.isArray(value.turns)) reject(contract, "turns", "an array");
  value.turns.forEach((entry, index) => turn(entry, `turns[${index}]`));
  if (!Array.isArray(value.items)) reject(contract, "items", "an array");
  value.items.forEach((entry, index) => item(entry, `items[${index}]`));
  if (!Array.isArray(value.jobs)) reject(contract, "jobs", "an array");
  value.jobs.forEach((entry, index) => job(entry, `jobs[${index}]`));
  if (!Array.isArray(value.interactions)) reject(contract, "interactions", "an array");
  value.interactions.forEach((entry, index) => interaction(entry, `interactions[${index}]`));
  assertInteger(value.watermark, contract, "watermark");
  const threadId = value.thread.thread_id;
  for (const [path, entries] of [
    ["turns", value.turns],
    ["items", value.items],
    ["interactions", value.interactions],
  ] as const) {
    entries.forEach((entry, index) => {
      if (entry.thread_id !== threadId) reject(contract, `${path}[${index}].thread_id`, "the projection thread_id");
    });
  }
  value.jobs.forEach((entry, index) => {
    if (entry.thread_id !== null && entry.thread_id !== threadId) {
      reject(contract, `jobs[${index}].thread_id`, "the projection thread_id or null");
    }
  });
  return value as unknown as ThreadProjectionResponse;
}

export function validateTurnMutationResponse(value: unknown): TurnMutationResponse {
  const contract = "TurnMutationResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, fields.TurnMutationResponse.TurnMutationResponse, contract, "root");
  turn(value.turn, "turn");
  if (value.job !== null) {
    job(value.job, "job");
    if (value.job.turn_id !== null && value.job.turn_id !== value.turn.turn_id) {
      reject(contract, "job.turn_id", "the mutated turn_id or null");
    }
  }
  assertInteger(value.watermark, contract, "watermark");
  return value as unknown as TurnMutationResponse;
}

export function validateReplaceTurnResponse(value: unknown): ReplaceTurnResponse {
  const contract = "ReplaceTurnResponse";
  assertRecord(value, contract, "root");
  assertWireFields(value, fields.ReplaceTurnResponse.ReplaceTurnResponse, contract, "root");
  turn(value.superseded_turn, "superseded_turn");
  turn(value.replacement_turn, "replacement_turn");
  job(value.job, "job");
  assertInteger(value.watermark, contract, "watermark");
  if (value.superseded_turn.thread_id !== value.replacement_turn.thread_id) {
    reject(contract, "replacement_turn.thread_id", "the superseded thread_id");
  }
  if (value.job.turn_id !== value.replacement_turn.turn_id) {
    reject(contract, "job.turn_id", "the replacement turn_id");
  }
  return value as unknown as ReplaceTurnResponse;
}

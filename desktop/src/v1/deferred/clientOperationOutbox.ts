import type {
  ClientEventPage,
  ClientOperation,
  ClientOperationDisposition,
  ClientOperationOutboxOptions,
  ClientOperationOutboxRecord,
  CreateClientOperationInput,
} from "../api/runtimeClient.ts";
import type { InputAttachmentProjection } from "../api/contracts.ts";

const OUTBOX_VERSION = 1 as const;
const OUTBOX_KEY = "ecorex:v1:client-operation-outbox";
const DEFAULT_TTL_MS = 72 * 60 * 60 * 1_000;
const DEFAULT_MAX_RECORDS = 16;
const DEFAULT_MAX_BYTES = 96 * 1_024;
const DEFAULT_MAX_INPUT_BYTES = 24 * 1_024;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/u;

interface OutboxEnvelope {
  version: 1;
  saved_at: string;
  records: ClientOperationOutboxRecord[];
}

export class ClientOperationPersistenceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClientOperationPersistenceError";
  }
}

export class ClientOperationConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClientOperationConflictError";
  }
}

function requestId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

function isInputAttachment(value: unknown): value is InputAttachmentProjection {
  if (!isRecord(value)) return false;
  return (
    typeof value.attachment_id === "string"
    && ID_PATTERN.test(value.attachment_id)
    && typeof value.revision_id === "string"
    && ID_PATTERN.test(value.revision_id)
    && typeof value.display_name === "string"
    && Boolean(value.display_name.trim())
    && typeof value.mime_type === "string"
    && Boolean(value.mime_type.trim())
    && typeof value.size_bytes === "number"
    && Number.isSafeInteger(value.size_bytes)
    && value.size_bytes >= 0
    && (value.media_kind === "image" || value.media_kind === "document" || value.media_kind === "file")
    && typeof value.sha256 === "string"
    && /^[0-9a-f]{64}$/u.test(value.sha256)
    && typeof value.created_at === "string"
    && Number.isFinite(Date.parse(value.created_at))
  );
}

function assertOperation(operation: ClientOperation): void {
  const createdAt = Date.parse(operation.created_at);
  const expiresAt = Date.parse(operation.expires_at);
  const threadId = operation.thread.kind === "existing"
    ? operation.thread.thread_id
    : operation.thread.client_request_id;
  if (
    operation.schema_version !== 1
    || !ID_PATTERN.test(operation.operation_id)
    || !ID_PATTERN.test(operation.client_message_id)
    || !ID_PATTERN.test(threadId)
    || (
      operation.thread.kind === "create"
      && operation.thread.metadata !== undefined
      && !isRecord(operation.thread.metadata)
    )
    || (operation.turn !== null && !ID_PATTERN.test(operation.turn.turn_id))
    || !Array.isArray(operation.attachments)
    || !Array.isArray(operation.explicit_tool_ids)
    || operation.explicit_tool_ids.length > 64
    || operation.explicit_tool_ids.some((reference) => !ID_PATTERN.test(reference))
    || new Set(operation.explicit_tool_ids).size !== operation.explicit_tool_ids.length
    || (!operation.input.trim() && operation.attachments.length === 0)
    || operation.attachments.length > 20
    || operation.attachments.some((attachment) => !isInputAttachment(attachment))
    || !operation.models.agentModelId.trim()
    || !Number.isSafeInteger(operation.observed_after_seq)
    || operation.observed_after_seq < 0
    || !Number.isFinite(createdAt)
    || !Number.isFinite(expiresAt)
    || expiresAt <= createdAt
    || ((operation.disposition === "steer" || operation.disposition === "replace") && !operation.turn)
    || (operation.disposition === "create" && operation.turn !== null)
  ) {
    throw new ClientOperationConflictError("Client operation contract is invalid.");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function freezeDeep<T extends object>(value: T): Readonly<T> {
  for (const child of Object.values(value)) {
    if (child !== null && typeof child === "object" && !Object.isFrozen(child)) {
      freezeDeep(child);
    }
  }
  return Object.freeze(value);
}

function fingerprintPayload(operation: Omit<ClientOperation, "fingerprint">): string {
  return JSON.stringify({
    schema_version: operation.schema_version,
    operation_id: operation.operation_id,
    client_message_id: operation.client_message_id,
    thread: operation.thread,
    turn: operation.turn,
    disposition: operation.disposition,
    models: operation.models,
    input: operation.input,
    explicit_tool_ids: operation.explicit_tool_ids,
    attachments: operation.attachments,
    observed_after_seq: operation.observed_after_seq,
  });
}

function legacyFingerprintPayload(operation: Omit<ClientOperation, "fingerprint">): string {
  return JSON.stringify({
    schema_version: operation.schema_version,
    operation_id: operation.operation_id,
    client_message_id: operation.client_message_id,
    thread: operation.thread,
    turn: operation.turn,
    disposition: operation.disposition,
    models: operation.models,
    input: operation.input,
    observed_after_seq: operation.observed_after_seq,
  });
}

function preMentionFingerprintPayload(operation: Omit<ClientOperation, "fingerprint">): string {
  return JSON.stringify({
    schema_version: operation.schema_version,
    operation_id: operation.operation_id,
    client_message_id: operation.client_message_id,
    thread: operation.thread,
    turn: operation.turn,
    disposition: operation.disposition,
    models: operation.models,
    input: operation.input,
    attachments: operation.attachments,
    observed_after_seq: operation.observed_after_seq,
  });
}

function fingerprint(value: string): string {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193) >>> 0;
    second = Math.imul(second ^ code, 0x85ebca6b) >>> 0;
    second = ((second << 13) | (second >>> 19)) >>> 0;
  }
  return `fp_${first.toString(16).padStart(8, "0")}${second.toString(16).padStart(8, "0")}`;
}

function validateFingerprint(operation: ClientOperation): void {
  assertOperation(operation);
  const { fingerprint: _stored, ...payload } = operation;
  if (operation.fingerprint !== fingerprint(fingerprintPayload(payload))) {
    throw new ClientOperationConflictError(
      "Client operation payload no longer matches its fingerprint.",
    );
  }
}

function sameOperation(left: ClientOperation, right: ClientOperation): boolean {
  const { fingerprint: _left, ...leftPayload } = left;
  const { fingerprint: _right, ...rightPayload } = right;
  return fingerprintPayload(leftPayload) === fingerprintPayload(rightPayload)
    && left.created_at === right.created_at
    && left.expires_at === right.expires_at;
}

export function createClientOperation(input: CreateClientOperationInput): ClientOperation {
  const operationId = input.operationId ?? requestId("operation");
  const now = input.now ?? new Date();
  const disposition: ClientOperationDisposition = input.activeTurn
    ? input.disposition
    : "create";
  const payload: Omit<ClientOperation, "fingerprint"> = {
    schema_version: 1,
    operation_id: operationId,
    client_message_id: input.clientMessageId ?? requestId("message"),
    thread: input.threadId
      ? { kind: "existing", thread_id: input.threadId }
      : {
          kind: "create",
          client_request_id: operationId,
          ...(input.threadMetadata && Object.keys(input.threadMetadata).length
            ? { metadata: input.threadMetadata }
            : {}),
        },
    turn: input.activeTurn
      ? { turn_id: input.activeTurn.turn_id, status: input.activeTurn.status }
      : null,
    disposition,
    models: {
      agentModelId: input.models.agentModelId,
      imageModelId: input.models.imageModelId,
    },
    input: input.input.trim(),
    explicit_tool_ids: [...new Set(input.explicitToolIds ?? [])],
    attachments: [...(input.attachments ?? [])].map((attachment) => ({ ...attachment })),
    observed_after_seq: input.observedAfterSeq,
    created_at: now.toISOString(),
    expires_at: new Date(
      now.getTime() + (input.ttlMilliseconds ?? DEFAULT_TTL_MS),
    ).toISOString(),
  };
  const operation = freezeDeep({
    ...payload,
    fingerprint: fingerprint(fingerprintPayload(payload)),
  }) as ClientOperation;
  validateFingerprint(operation);
  return operation;
}

function parseOperation(value: unknown): ClientOperation | null {
  if (!isRecord(value) || !isRecord(value.thread) || !isRecord(value.models)) return null;
  const threadMetadata = isRecord(value.thread.metadata)
    ? value.thread.metadata as import("../api/contracts.ts").JsonObject
    : undefined;
  const thread = value.thread.kind === "existing"
    ? { kind: "existing" as const, thread_id: value.thread.thread_id }
    : value.thread.kind === "create"
      ? {
          kind: "create" as const,
          client_request_id: value.thread.client_request_id,
          ...(threadMetadata && Object.keys(threadMetadata).length
            ? { metadata: threadMetadata }
            : {}),
        }
      : null;
  const turn = value.turn === null
    ? null
    : isRecord(value.turn)
      && typeof value.turn.turn_id === "string"
      && typeof value.turn.status === "string"
      ? { turn_id: value.turn.turn_id, status: value.turn.status }
      : undefined;
  const rawAttachments = value.attachments;
  const hadAttachments = Array.isArray(rawAttachments);
  const attachments = Array.isArray(rawAttachments) && rawAttachments.every(isInputAttachment)
    ? rawAttachments
    : null;
  const rawExplicitToolIds = value.explicit_tool_ids;
  const hadExplicitToolIds = Array.isArray(rawExplicitToolIds);
  const explicitToolIds = hadExplicitToolIds
    && rawExplicitToolIds.every((reference: unknown) => typeof reference === "string")
    ? rawExplicitToolIds as string[]
    : hadExplicitToolIds
      ? null
      : [];
  if (
    !thread
    || turn === undefined
    || typeof value.operation_id !== "string"
    || typeof value.client_message_id !== "string"
    || typeof value.fingerprint !== "string"
    || typeof value.input !== "string"
    || typeof value.created_at !== "string"
    || typeof value.expires_at !== "string"
    || typeof value.observed_after_seq !== "number"
    || typeof value.models.agentModelId !== "string"
    || (value.models.imageModelId !== null && typeof value.models.imageModelId !== "string")
    || !["create", "steer", "queue", "replace"].includes(String(value.disposition))
    || (hadAttachments && attachments === null)
    || explicitToolIds === null
  ) return null;
  const operation = freezeDeep({
    schema_version: value.schema_version,
    operation_id: value.operation_id,
    client_message_id: value.client_message_id,
    fingerprint: value.fingerprint,
    thread,
    turn,
    disposition: value.disposition,
    models: {
      agentModelId: value.models.agentModelId,
      imageModelId: value.models.imageModelId,
    },
    input: value.input,
    explicit_tool_ids: explicitToolIds ? [...new Set(explicitToolIds)] : [],
    attachments: attachments ? attachments.map((attachment) => ({ ...attachment })) : [],
    observed_after_seq: value.observed_after_seq,
    created_at: value.created_at,
    expires_at: value.expires_at,
  }) as ClientOperation;
  try {
    validateFingerprint(operation);
    return operation;
  } catch {
    const { fingerprint: _stored, ...legacyPayload } = operation;
    if (
      !hadExplicitToolIds
      && value.fingerprint === fingerprint(preMentionFingerprintPayload(legacyPayload))
    ) {
      return freezeDeep({
        ...operation,
        fingerprint: fingerprint(fingerprintPayload(legacyPayload)),
      }) as ClientOperation;
    }
    if (hadAttachments) return null;
    if (value.fingerprint !== fingerprint(legacyFingerprintPayload(legacyPayload))) return null;
    return freezeDeep({
      ...operation,
      fingerprint: fingerprint(fingerprintPayload(legacyPayload)),
    }) as ClientOperation;
  }
}

function browserSessionStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null {
  try {
    return typeof globalThis.sessionStorage === "undefined" ? null : globalThis.sessionStorage;
  } catch {
    return null;
  }
}

/** A bounded request-only journal: never stores auth, file bytes, paths, or responses. */
export class ClientOperationOutbox {
  private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  private readonly key: string;
  private readonly maxRecords: number;
  private readonly maxBytes: number;
  private readonly maxInputBytes: number;
  private readonly now: () => Date;

  constructor(options: ClientOperationOutboxOptions = {}) {
    this.storage = options.storage === undefined ? browserSessionStorage() : options.storage;
    this.key = options.storageKey ?? OUTBOX_KEY;
    this.maxRecords = options.maxRecords ?? DEFAULT_MAX_RECORDS;
    this.maxBytes = options.maxStoredBytes ?? DEFAULT_MAX_BYTES;
    this.maxInputBytes = options.maxInputBytes ?? DEFAULT_MAX_INPUT_BYTES;
    this.now = options.now ?? (() => new Date());
  }

  private read(): ClientOperationOutboxRecord[] {
    if (!this.storage) return [];
    let raw: string | null;
    try {
      raw = this.storage.getItem(this.key);
    } catch {
      return [];
    }
    if (!raw) return [];
    if (bytes(raw) > this.maxBytes) {
      try { this.storage.removeItem(this.key); } catch { /* reported by stage */ }
      return [];
    }
    try {
      const parsed: unknown = JSON.parse(raw);
      if (
        !isRecord(parsed)
        || parsed.version !== OUTBOX_VERSION
        || !Array.isArray(parsed.records)
        || parsed.records.length > this.maxRecords
      ) {
        this.storage.removeItem(this.key);
        return [];
      }
      const now = this.now().getTime();
      const records = parsed.records.flatMap((candidate): ClientOperationOutboxRecord[] => {
        if (!isRecord(candidate)) return [];
        const operation = parseOperation(candidate.operation);
        const resolved = candidate.resolved_thread_id;
        if (
          !operation
          || bytes(operation.input) > this.maxInputBytes
          || Date.parse(operation.expires_at) <= now
          || (resolved !== null && typeof resolved !== "string")
          || (resolved && !ID_PATTERN.test(resolved))
        ) return [];
        return [{
          operation,
          resolved_thread_id: resolved,
          updated_at: typeof candidate.updated_at === "string"
            ? candidate.updated_at
            : operation.created_at,
        }];
      });
      if (records.length !== parsed.records.length) this.write(records);
      return records;
    } catch {
      try { this.storage.removeItem(this.key); } catch { /* reported by stage */ }
      return [];
    }
  }

  private write(records: ClientOperationOutboxRecord[]): void {
    if (!this.storage) {
      throw new ClientOperationPersistenceError(
        "当前浏览器无法保存待发送消息，请开启会话存储后重试。",
      );
    }
    if (!records.length) {
      try { this.storage.removeItem(this.key); } catch {
        throw new ClientOperationPersistenceError("无法清理已确认的待发送消息。");
      }
      return;
    }
    if (records.length > this.maxRecords) {
      throw new ClientOperationPersistenceError("待发送消息过多，请先恢复网络并完成已有发送。");
    }
    const envelope: OutboxEnvelope = {
      version: OUTBOX_VERSION,
      saved_at: this.now().toISOString(),
      records,
    };
    const serialized = JSON.stringify(envelope);
    if (bytes(serialized) > this.maxBytes) {
      throw new ClientOperationPersistenceError("待发送消息占用空间过大，请缩短内容后重试。");
    }
    try { this.storage.setItem(this.key, serialized); } catch {
      throw new ClientOperationPersistenceError("无法保存待发送消息，请检查浏览器存储空间。");
    }
  }

  list(): ClientOperationOutboxRecord[] {
    return this.read();
  }

  get(operationId: string): ClientOperationOutboxRecord | null {
    return this.read().find((record) => record.operation.operation_id === operationId) ?? null;
  }

  stage(operation: ClientOperation): ClientOperationOutboxRecord {
    validateFingerprint(operation);
    if (bytes(operation.input) > this.maxInputBytes) {
      throw new ClientOperationPersistenceError("消息内容过长，无法安全加入待发送队列。");
    }
    const records = this.read();
    const existing = records.find((record) => (
      record.operation.operation_id === operation.operation_id
      || record.operation.client_message_id === operation.client_message_id
    ));
    if (existing) {
      if (
        existing.operation.fingerprint !== operation.fingerprint
        || !sameOperation(existing.operation, operation)
      ) {
        throw new ClientOperationConflictError("同一个发送身份不能用于不同的消息内容或目标。");
      }
      return existing;
    }
    const record: ClientOperationOutboxRecord = {
      operation,
      resolved_thread_id: operation.thread.kind === "existing"
        ? operation.thread.thread_id
        : null,
      updated_at: this.now().toISOString(),
    };
    this.write([...records, record]);
    return record;
  }

  resolveThread(operationId: string, threadId: string): ClientOperationOutboxRecord {
    if (!ID_PATTERN.test(threadId)) throw new ClientOperationConflictError("Thread identity is invalid.");
    const records = this.read();
    const index = records.findIndex((record) => record.operation.operation_id === operationId);
    if (index < 0) throw new ClientOperationConflictError("待发送消息已不存在。");
    const current = records[index]!;
    if (current.resolved_thread_id && current.resolved_thread_id !== threadId) {
      throw new ClientOperationConflictError("新会话发送不能绑定到两个不同会话。");
    }
    const updated: ClientOperationOutboxRecord = {
      ...current,
      resolved_thread_id: threadId,
      updated_at: this.now().toISOString(),
    };
    records[index] = updated;
    this.write(records);
    return updated;
  }

  acknowledge(clientMessageIds: Iterable<string>): string[] {
    const confirmed = new Set(clientMessageIds);
    if (!confirmed.size) return [];
    const records = this.read();
    const removed = records
      .filter((record) => confirmed.has(record.operation.client_message_id))
      .map((record) => record.operation.operation_id);
    if (removed.length) {
      this.write(records.filter((record) => !confirmed.has(record.operation.client_message_id)));
    }
    return removed;
  }
}

export function resolvedOperationThreadId(record: ClientOperationOutboxRecord): string | null {
  if (record.resolved_thread_id) return record.resolved_thread_id;
  return record.operation.thread.kind === "existing"
    ? record.operation.thread.thread_id
    : null;
}

export function operationMatchesRetry(
  record: ClientOperationOutboxRecord,
  input: string,
  currentThreadId: string | null,
  attachments: readonly InputAttachmentProjection[] = [],
  explicitToolIds: readonly string[] = [],
): boolean {
  const operation = record.operation;
  const resolvedThreadId = resolvedOperationThreadId(record);
  if (
    operation.input !== input
    || operation.attachments.length !== attachments.length
    || operation.attachments.some((attachment, index) => (
      attachment.attachment_id !== attachments[index]?.attachment_id
      || attachment.revision_id !== attachments[index]?.revision_id
    ))
    || operation.explicit_tool_ids.length !== explicitToolIds.length
    || operation.explicit_tool_ids.some((reference, index) => reference !== explicitToolIds[index])
    || (
      currentThreadId !== null
      && currentThreadId !== resolvedThreadId
    )
  ) return false;
  // Current Turn state and the Composer's current disposition are deliberately
  // irrelevant. The persisted operation already froze create/steer/queue/
  // replace plus its exact target; retry means redeliver that operation, never
  // infer a new target from state that may have advanced after the first send.
  return true;
}

export async function confirmClientOperationEvents(input: {
  operation: ClientOperation;
  eventPage: (afterSeq: number, signal?: AbortSignal) => Promise<ClientEventPage>;
  acknowledge: (clientMessageIds: string[]) => void;
  stillPending: () => boolean;
  signal?: AbortSignal;
}): Promise<boolean> {
  let afterSeq = input.operation.observed_after_seq;
  for (let pageCount = 0; pageCount < 32; pageCount += 1) {
    const page = await input.eventPage(afterSeq, input.signal);
    input.acknowledge(
      page.events.flatMap((event) => event.client_message_id ? [event.client_message_id] : []),
    );
    if (!input.stillPending()) return true;
    if (!page.has_more) return false;
    const next = page.events.at(-1)?.seq ?? afterSeq;
    if (next <= afterSeq) throw new Error("消息确认事件没有继续前进。");
    afterSeq = next;
  }
  throw new Error("待确认消息较多，请重新连接后继续。");
}

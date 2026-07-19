import type {
  ActivateUpdateResponse,
  ArtifactListResponse,
  ArtifactExternalActionProjection,
  ArtifactProjection,
  BootstrapResponse,
  ConversationUsageProjection,
  ConnectorAuthChallenge,
  ConnectorAuthKind,
  ConnectorCatalogResponse,
  ConnectorInstanceProjection,
  ConnectorLoginBeginResponse,
  ConnectorLoginCancelResponse,
  ConnectorLoginCheckResponse,
  EventEnvelope,
  ExtensionActionId,
  ExtensionCatalogSnapshot,
  ExtensionMutationResponse,
  InteractionMutationResponse,
  InputAttachmentProjection,
  InteractionResponse,
  JsonObject,
  LiveReplayResponse,
  LoginSessionResponse,
  LogoutSessionResponse,
  MemoryMutationResponse,
  MemorySnapshot,
  MigrationQuarantineProjection,
  MockReplayResponse,
  OutputLocationAlias,
  OutputLocationCatalog,
  OutputMaterialization,
  OutputPreference,
  PermissionMutationResponse,
  ProjectListResponse,
  ProjectProjection,
  ReplaceTurnResponse,
  ShareListResponse,
  ShareSnapshotProjection,
  SystemHealthSample,
  SystemMetricHistory,
  ThreadProjection,
  ThreadListResponse,
  ThreadProjectionResponse,
  TurnMutationResponse,
  RetouchAnnotation,
  RetouchJobProjection,
  RetouchViewState,
  RetouchWorkspaceProjection,
  UpdateMutationResponse,
} from "./contracts.ts";
import {
  validateBootstrapResponse,
  validateConversationUsageProjection,
  validateEventEnvelope,
  validateInputAttachmentProjection,
} from "./runtimeContract.ts";
import type {
  ArtifactJsonTransport,
  ArtifactOperationKind,
} from "./artifactRuntimeOperations.ts";

declare global {
  interface Window {
    __ECOREX_RUNTIME__?: RuntimeBridgeConfig;
  }
}

export interface RuntimeBridgeConfig {
  apiBase?: string;
  bearerToken?: string;
  csrfToken?: string;
  version?: string;
}

export interface TurnModelSelection {
  agentModelId: string;
  imageModelId: string | null;
}

export type ClientOperationDisposition = "create" | "steer" | "queue" | "replace";

export type ClientOperationThreadTarget =
  | Readonly<{
      kind: "existing";
      thread_id: string;
    }>
  | Readonly<{
      kind: "create";
      client_request_id: string;
      metadata?: JsonObject;
    }>;

export interface ClientOperationTurnTarget {
  readonly turn_id: string;
  readonly status: string;
}

/**
 * One immutable user intent. Network retries may repeat delivery, but they may
 * never mint a second identity or retarget a different active Turn.
 */
export interface ClientOperation {
  readonly schema_version: 1;
  readonly operation_id: string;
  readonly client_message_id: string;
  readonly fingerprint: string;
  readonly thread: ClientOperationThreadTarget;
  readonly turn: Readonly<ClientOperationTurnTarget> | null;
  readonly disposition: ClientOperationDisposition;
  readonly models: Readonly<TurnModelSelection>;
  readonly input: string;
  readonly attachments: readonly InputAttachmentProjection[];
  readonly observed_after_seq: number;
  readonly created_at: string;
  readonly expires_at: string;
}

export interface CreateClientOperationInput {
  input: string;
  attachments?: readonly InputAttachmentProjection[];
  threadId: string | null;
  threadMetadata?: JsonObject;
  activeTurn: Readonly<{
    turn_id: string;
    status: string;
  }> | null;
  disposition: Exclude<ClientOperationDisposition, "create">;
  models: TurnModelSelection;
  observedAfterSeq: number;
  operationId?: string;
  clientMessageId?: string;
  now?: Date;
  ttlMilliseconds?: number;
}

export interface ClientOperationOutboxRecord {
  readonly operation: ClientOperation;
  readonly resolved_thread_id: string | null;
  readonly updated_at: string;
}

export interface ClientOperationOutboxOptions {
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  storageKey?: string;
  maxRecords?: number;
  maxStoredBytes?: number;
  maxInputBytes?: number;
  now?: () => Date;
}

export interface ClientEventPage {
  events: EventEnvelope[];
  after_seq: number;
  watermark: number;
  has_more: boolean;
}

function assertOperationDisposition(
  operation: ClientOperation,
  disposition: ClientOperationDisposition,
): void {
  if (operation.disposition !== disposition) throw new TypeError("Invalid client operation.");
}

export function projectionClientMessageIds(
  projection: Pick<ThreadProjectionResponse, "turns">,
): string[] {
  return projection.turns.flatMap((turn) => (
    turn.client_message_id ? [turn.client_message_id] : []
  ));
}

export function eventClientMessageIds(events: readonly EventEnvelope[]): string[] {
  return events.flatMap((event) => event.client_message_id ? [event.client_message_id] : []);
}

export class RuntimeApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "RuntimeApiError";
    this.status = status;
    this.code = code;
  }
}

export class EventCursorResetRequired extends RuntimeApiError {
  constructor(message = "事件游标已失效，需要重新同步会话。") {
    super(message, 409, "event_cursor_reset_required");
    this.name = "EventCursorResetRequired";
  }
}

export function createClientRequestId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

function normalizeBase(value: string | undefined): string {
  const base = String(value ?? "").trim();
  const trimmed = base.endsWith("/") ? base.slice(0, -1) : base;
  return trimmed.endsWith("/api/v1") ? trimmed.slice(0, -7) : trimmed;
}

function parseError(payload: unknown, fallback: string): { message: string; code: string | null } {
  if (!isRecord(payload)) return { message: fallback, code: null };
  const detail = payload.detail;
  const topLevelError = payload.error;
  if (isRecord(topLevelError)) {
    return {
      message: typeof topLevelError.message === "string" ? topLevelError.message : fallback,
      code: typeof topLevelError.code === "string" ? topLevelError.code : null,
    };
  }
  if (typeof detail === "string" && detail.trim()) return { message: detail, code: null };
  if (isRecord(detail)) {
    return {
      message: typeof detail.message === "string" ? detail.message : fallback,
      code: typeof detail.code === "string" ? detail.code : null,
    };
  }
  return { message: fallback, code: null };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateLogoutSessionResponse(value: unknown): LogoutSessionResponse {
  if (
    !isRecord(value)
    || value.authenticated !== false
    || !Number.isSafeInteger(value.generation)
    || Number(value.generation) < 1
    || value.restart_required !== true
    || typeof value.restart_scheduled !== "boolean"
  ) {
    throw new RuntimeApiError(
      "Runtime returned an invalid logout receipt.",
      502,
      "logout_receipt_invalid",
    );
  }
  return value as unknown as LogoutSessionResponse;
}

function validateLoginSessionResponse(value: unknown): LoginSessionResponse {
  if (
    !isRecord(value)
    || value.authenticated !== true
    || typeof value.display_name !== "string"
    || !value.display_name.trim()
    || !Number.isSafeInteger(value.generation)
    || Number(value.generation) < 1
    || value.restart_required !== true
    || typeof value.restart_scheduled !== "boolean"
  ) {
    throw new RuntimeApiError(
      "Runtime returned an invalid login receipt.",
      502,
      "login_receipt_invalid",
    );
  }
  return value as unknown as LoginSessionResponse;
}

async function validateThreadProjectionBoundary(value: unknown): Promise<ThreadProjection> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateThreadProjection(value);
}

async function validateThreadListBoundary(value: unknown): Promise<ThreadListResponse> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateThreadListResponse(value);
}

async function validateProjectionBoundary(value: unknown): Promise<ThreadProjectionResponse> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateThreadProjectionResponse(value);
}

async function validateTurnMutationBoundary(value: unknown): Promise<TurnMutationResponse> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateTurnMutationResponse(value);
}

async function validateReplaceTurnBoundary(value: unknown): Promise<ReplaceTurnResponse> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateReplaceTurnResponse(value);
}

async function validateInteractionBoundary<T>(
  value: unknown,
  kind: "mutation" | "begin" | "check" | "cancel",
  expectedInteractionId: string,
): Promise<T> {
  const contract = await import("./runtimeProjectionContract.ts");
  return contract.validateInteractionBoundary(
    value,
    kind,
    expectedInteractionId,
  ) as T;
}

function validateClientEventPage(value: unknown): ClientEventPage {
  if (
    !isRecord(value)
    || !Array.isArray(value.events)
    || !Number.isSafeInteger(value.after_seq)
    || !Number.isSafeInteger(value.watermark)
    || typeof value.has_more !== "boolean"
  ) {
    throw new RuntimeApiError("Runtime returned an invalid event page.", 502, "event_page_invalid");
  }
  return {
    events: value.events.map(validateEventEnvelope),
    after_seq: value.after_seq as number,
    watermark: value.watermark as number,
    has_more: value.has_more,
  };
}

export class RuntimeClient {
  private readonly base: string;
  private readonly config: RuntimeBridgeConfig;

  constructor(config: RuntimeBridgeConfig = window.__ECOREX_RUNTIME__ ?? {}) {
    // The Runtime injects and freezes the public bridge object so page code
    // cannot replace its launch identity or bearer token.  Keep that boundary
    // immutable and retain CSRF rotation in a private mutable copy.
    this.config = { ...config };
    this.base = normalizeBase(config.apiBase);
  }

  acceptBootstrap(bootstrap: BootstrapResponse): void {
    if (bootstrap.csrf_token) this.config.csrfToken = bootstrap.csrf_token;
  }

  private headers(mutation: boolean, extra: HeadersInit = {}): Headers {
    const headers = new Headers(extra);
    headers.set("Accept", "application/json");
    if (this.config.bearerToken) {
      headers.set("Authorization", `Bearer ${this.config.bearerToken}`);
    }
    if (mutation && this.config.csrfToken) {
      headers.set("X-EcoreX-CSRF", this.config.csrfToken);
    }
    return headers;
  }

  private async json<T>(
    path: string,
    init: RequestInit = {},
    mutation = false,
    validate?: ((value: unknown) => T | Promise<T>)
      | import("./settingsRuntimeContract.ts").SettingsBoundaryKind,
    validationContext?: Readonly<{ artifact_id: string; revision_id: string }>,
  ): Promise<T> {
    const headers = this.headers(mutation, init.headers);
    if (
      init.body != null
      && !headers.has("Content-Type")
      && !(typeof FormData !== "undefined" && init.body instanceof FormData)
    ) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(`${this.base}${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : null;
    if (!response.ok) {
      const fallback = `Runtime request failed (${response.status}).`;
      const error = parseError(payload, fallback);
      if (response.status === 409 && error.code === "event_cursor_reset_required") {
        throw new EventCursorResetRequired(error.message);
      }
      throw new RuntimeApiError(error.message, response.status, error.code);
    }
    if (typeof validate === "number") {
      const contract = await import("./settingsRuntimeContract.ts");
      return contract.validateSettingsBoundary(validate, payload, validationContext) as T;
    }
    return validate ? await validate(payload) : payload as T;
  }

  private async artifactOperation<T>(
    operation: ArtifactOperationKind,
    input: Readonly<Record<string, unknown>>,
  ): Promise<T> {
    const module = await import("./artifactRuntimeOperations.ts");
    const request: ArtifactJsonTransport = <R>(path: string, init: RequestInit, mutation: boolean, validate: (value: unknown) => R | Promise<R>) => (
      this.json(path, init, mutation, validate)
    );
    return module.executeArtifactOperation(request, operation, input) as Promise<T>;
  }

  bootstrap(signal?: AbortSignal): Promise<BootstrapResponse> {
    return this.json(
      "/api/v1/bootstrap",
      { signal },
      false,
      validateBootstrapResponse,
    );
  }

  async waitForCredentialRotation(
    options: Readonly<{
      timeoutMs?: number;
      pollIntervalMs?: number;
    }> = {},
  ): Promise<boolean> {
    const timeoutMs = options.timeoutMs ?? 30_000;
    const pollIntervalMs = options.pollIntervalMs ?? 500;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const bootstrap = await this.bootstrap();
        if (bootstrap.login.authenticated) return true;
      } catch (error) {
        if (error instanceof RuntimeApiError) {
          // The old process continues to accept its bearer while its managed
          // session is restart-fenced, returning 409. A 401 from the same
          // immutable client means a new process is serving with a newly
          // generated Runtime bearer/CSRF pair, so the document must reload.
          if (error.status === 401) return true;
          if (error.status !== 409) {
            // Other transient startup responses are retried within the bound.
          }
        }
      }
      await new Promise<void>((resolve) => {
        globalThis.setTimeout(resolve, pollIntervalMs);
      });
    }
    return false;
  }

  conversationUsage(
    threadId: string,
    signal?: AbortSignal,
  ): Promise<ConversationUsageProjection> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/usage`,
      { signal },
      false,
      validateConversationUsageProjection,
    );
  }

  uploadInputAttachment(
    file: File,
    clientRequestId = createClientRequestId("input_attachment"),
    signal?: AbortSignal,
  ): Promise<InputAttachmentProjection> {
    const body = new FormData();
    body.set("file", file, file.name);
    body.set("client_request_id", clientRequestId);
    return this.json(
      "/api/v1/input-attachments",
      { method: "POST", body, signal },
      true,
      validateInputAttachmentProjection,
    );
  }

  memory(signal?: AbortSignal): Promise<MemorySnapshot> {
    return this.json(
      "/api/v1/memory",
      { signal },
      false,
      0, // memory snapshot
    );
  }

  migrationQuarantine(signal?: AbortSignal): Promise<MigrationQuarantineProjection> {
    return this.json(
      "/api/v1/migration/quarantine",
      { signal },
      false,
      2, // migration quarantine
    );
  }

  deleteMigrationQuarantine(
    clientRequestId = createClientRequestId("delete_migration_quarantine"),
  ): Promise<MigrationQuarantineProjection> {
    return this.json(
      "/api/v1/migration/quarantine/delete",
      {
        method: "POST",
        body: JSON.stringify({ confirmed: true, client_request_id: clientRequestId }),
      },
      true,
      2, // migration quarantine
    );
  }

  outputLocations(signal?: AbortSignal): Promise<OutputLocationCatalog> {
    return this.json(
      "/api/v1/output/locations",
      { signal },
      false,
      3, // output locations
    );
  }

  outputPreference(signal?: AbortSignal): Promise<OutputPreference> {
    return this.json(
      "/api/v1/output/preference",
      { signal },
      false,
      4, // output preference
    );
  }

  updateOutputPreference(
    locationAlias: OutputLocationAlias,
    expectedRevision: number,
    clientRequestId = createClientRequestId("output_preference"),
  ): Promise<OutputPreference> {
    return this.json(
      "/api/v1/output/preference",
      {
        method: "PUT",
        body: JSON.stringify({
          location_alias: locationAlias,
          expected_revision: expectedRevision,
          client_request_id: clientRequestId,
        }),
      },
      true,
      4, // output preference
    );
  }

  pickOutputLocation(
    expectedRevision: number,
    clientRequestId = createClientRequestId("pick_output_location"),
  ): Promise<OutputPreference> {
    return this.json(
      "/api/v1/output/locations/pick",
      {
        method: "POST",
        body: JSON.stringify({
          expected_revision: expectedRevision,
          client_request_id: clientRequestId,
        }),
      },
      true,
      4, // output preference
    );
  }

  materializeArtifact(
    artifact: Pick<ArtifactProjection, "artifact_id" | "revision_id">,
    clientRequestId = createClientRequestId("materialize_artifact"),
  ): Promise<OutputMaterialization> {
    return this.json(
      `/api/v1/output/artifacts/${encodeURIComponent(artifact.artifact_id)}/materialize`,
      {
        method: "POST",
        body: JSON.stringify({
          revision_id: artifact.revision_id,
          client_request_id: clientRequestId,
        }),
      },
      true,
      5, // output materialization
      artifact,
    );
  }

  systemHealth(
    options: { technical?: boolean; signal?: AbortSignal } = {},
  ): Promise<SystemHealthSample> {
    const suffix = options.technical ? "?technical=true" : "";
    return this.json(
      `/api/v1/system/health${suffix}`,
      { signal: options.signal },
      false,
      options.technical ? 7 : 6, // technical/public system health
    );
  }

  systemMetrics(
    limit = 60,
    signal?: AbortSignal,
  ): Promise<SystemMetricHistory> {
    const bounded = Math.max(1, Math.min(200, Math.trunc(limit)));
    return this.json(
      `/api/v1/system/metrics?limit=${bounded}`,
      { signal },
      false,
      8, // system metric history
    );
  }

  resetLearnedMemory(
    clientRequestId = createClientRequestId("reset_memory"),
  ): Promise<MemoryMutationResponse> {
    return this.json(
      "/api/v1/memory/reset",
      {
        method: "POST",
        body: JSON.stringify({ confirmed: true, client_request_id: clientRequestId }),
      },
      true,
      1, // memory mutation
    );
  }

  undoLearnedMemoryReset(
    resetId: string,
    clientRequestId = createClientRequestId("undo_memory_reset"),
  ): Promise<MemoryMutationResponse> {
    return this.json(
      `/api/v1/memory/resets/${encodeURIComponent(resetId)}/undo`,
      {
        method: "POST",
        body: JSON.stringify({ confirmed: true, client_request_id: clientRequestId }),
      },
      true,
      1, // memory mutation
    );
  }

  loginSession(
    identifier: string,
    password: string,
    clientRequestId = createClientRequestId("session_login"),
  ): Promise<LoginSessionResponse> {
    return this.json(
      "/api/v1/session/login",
      {
        method: "POST",
        body: JSON.stringify({
          identifier,
          password,
          client_request_id: clientRequestId,
        }),
      },
      true,
      validateLoginSessionResponse,
    );
  }

  logoutSession(
    leaseDigest: string,
    clientRequestId = createClientRequestId("session_logout"),
  ): Promise<LogoutSessionResponse> {
    return this.json(
      "/api/v1/session/logout",
      {
        method: "POST",
        body: JSON.stringify({
          lease_digest: leaseDigest,
          client_request_id: clientRequestId,
          confirmed: true,
        }),
      },
      true,
      validateLogoutSessionResponse,
    );
  }

  connectorCatalog(signal?: AbortSignal): Promise<ConnectorCatalogResponse> {
    return this.json("/api/v1/connectors", { signal });
  }

  extensionCatalog(signal?: AbortSignal): Promise<ExtensionCatalogSnapshot> {
    return this.json("/api/v1/extensions", { signal });
  }

  installLocalSkill(
    extensionId: string,
    bundleBase64: string,
    expectedRevision: number,
    clientRequestId = createClientRequestId("extension_install_local"),
  ): Promise<ExtensionMutationResponse> {
    return this.json(
      "/api/v1/extensions/local-skills",
      {
        method: "POST",
        body: JSON.stringify({
          extension_id: extensionId,
          bundle_base64: bundleBase64,
          expected_revision: expectedRevision,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  mutateExtension(
    extensionId: string,
    actionId: ExtensionActionId,
    expectedRevision: number,
    clientRequestId = createClientRequestId(`extension_${actionId}`),
  ): Promise<ExtensionMutationResponse> {
    const actionPath: Record<ExtensionActionId, string> = {
      enable: "enable",
      disable: "disable",
      health_check: "health",
      rollback: "rollback",
    };
    return this.json(
      `/api/v1/extensions/${encodeURIComponent(extensionId)}/${actionPath[actionId]}`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_revision: expectedRevision,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  beginConnectorAuth(
    connectorId: string,
    authKind: ConnectorAuthKind,
    clientRequestId = createClientRequestId("connector_auth"),
  ): Promise<ConnectorAuthChallenge> {
    return this.json(
      `/api/v1/connectors/${encodeURIComponent(connectorId)}/auth/begin`,
      {
        method: "POST",
        headers: { "X-EcoreX-Client-Request-ID": clientRequestId },
        body: JSON.stringify({ auth_kind: authKind }),
      },
      true,
    );
  }

  reauthorizeConnector(
    instanceId: string,
    authKind: ConnectorAuthKind,
    clientRequestId = createClientRequestId("connector_reauthorize"),
  ): Promise<ConnectorAuthChallenge> {
    return this.json(
      `/api/v1/connectors/instances/${encodeURIComponent(instanceId)}/reauthorize`,
      {
        method: "POST",
        headers: { "X-EcoreX-Client-Request-ID": clientRequestId },
        body: JSON.stringify({ auth_kind: authKind }),
      },
      true,
    );
  }

  refreshConnectorHealth(
    instanceId: string,
    clientRequestId = createClientRequestId("connector_health"),
  ): Promise<ConnectorInstanceProjection> {
    return this.json(
      `/api/v1/connectors/instances/${encodeURIComponent(instanceId)}/health`,
      {
        method: "POST",
        headers: { "X-EcoreX-Client-Request-ID": clientRequestId },
      },
      true,
    );
  }

  async disconnectConnector(
    instanceId: string,
    clientRequestId = createClientRequestId("connector_disconnect"),
  ): Promise<void> {
    await this.json<unknown>(
      `/api/v1/connectors/instances/${encodeURIComponent(instanceId)}`,
      {
        method: "DELETE",
        headers: { "X-EcoreX-Client-Request-ID": clientRequestId },
      },
      true,
    );
  }

  updatePermission(
    profile: "default" | "full_access",
    expectedRevision: number,
    clientRequestId = createClientRequestId("permission"),
  ): Promise<PermissionMutationResponse> {
    return this.json(
      "/api/v1/settings/permissions",
      {
        method: "PUT",
        body: JSON.stringify({
          profile,
          expected_revision: expectedRevision,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  updateStatus(signal?: AbortSignal): Promise<UpdateMutationResponse> {
    return this.json("/api/v1/update", { signal });
  }

  checkUpdate(): Promise<UpdateMutationResponse> {
    return this.json("/api/v1/update/check", { method: "POST" }, true);
  }

  activateUpdate(
    transactionId: string,
    clientRequestId: string,
  ): Promise<ActivateUpdateResponse> {
    return this.json(
      "/api/v1/update/activate",
      {
        method: "POST",
        body: JSON.stringify({
          transaction_id: transactionId,
          confirmed: true,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  createThread(operation: ClientOperation, title?: string): Promise<ThreadProjection> {
    if (operation.thread.kind !== "create") {
      throw new TypeError("Invalid client operation.");
    }
    return this.json(
      "/api/v1/threads",
      {
        method: "POST",
        body: JSON.stringify({
          title: title?.trim() || null,
          metadata: operation.thread.metadata ?? {},
          client_request_id: operation.thread.client_request_id,
        }),
      },
      true,
      validateThreadProjectionBoundary,
    );
  }

  listProjects(signal?: AbortSignal): Promise<ProjectListResponse> {
    return this.json("/api/v1/projects", { signal });
  }

  pickProject(clientRequestId: string): Promise<ProjectProjection> {
    return this.json(
      "/api/v1/projects/pick",
      {
        method: "POST",
        body: JSON.stringify({ client_request_id: clientRequestId }),
      },
      true,
    );
  }

  listThreads(
    status: "active" | "archived" | "all" = "active",
    limit = 200,
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<ThreadListResponse> {
    const query = new URLSearchParams({ status, limit: String(limit) });
    if (cursor) query.set("cursor", cursor);
    return this.json(
      `/api/v1/threads?${query.toString()}`,
      { signal },
      false,
      validateThreadListBoundary,
    );
  }

  renameThread(
    threadId: string,
    title: string,
    clientRequestId = createClientRequestId("rename_thread"),
  ): Promise<ThreadProjection> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}`,
      {
        method: "PUT",
        body: JSON.stringify({ title, client_request_id: clientRequestId }),
      },
      true,
      validateThreadProjectionBoundary,
    );
  }

  setThreadArchived(
    threadId: string,
    archived: boolean,
    clientRequestId = createClientRequestId(archived ? "archive_thread" : "restore_thread"),
  ): Promise<ThreadProjection> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/${archived ? "archive" : "restore"}`,
      {
        method: "POST",
        body: JSON.stringify({ client_request_id: clientRequestId }),
      },
      true,
      validateThreadProjectionBoundary,
    );
  }

  setThreadPinned(
    threadId: string,
    pinned: boolean,
    clientRequestId = createClientRequestId(pinned ? "pin_thread" : "unpin_thread"),
  ): Promise<ThreadProjection> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/pin`,
      {
        method: "PUT",
        body: JSON.stringify({ pinned, client_request_id: clientRequestId }),
      },
      true,
      validateThreadProjectionBoundary,
    );
  }

  projection(threadId: string, signal?: AbortSignal): Promise<ThreadProjectionResponse> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/projection`,
      { signal },
      false,
      validateProjectionBoundary,
    );
  }

  mockReplay(threadId: string, signal?: AbortSignal): Promise<MockReplayResponse> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/replay`,
      { signal },
    );
  }

  liveReplay(
    threadId: string,
    sourceTurnId: string,
    clientRequestId: string,
  ): Promise<LiveReplayResponse> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/replay/live`,
      {
        method: "POST",
        body: JSON.stringify({
          source_turn_id: sourceTurnId,
          confirmed: true,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  listShares(threadId: string, signal?: AbortSignal): Promise<ShareListResponse> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/shares`,
      { signal },
    );
  }

  createShare(
    threadId: string,
    expiresInHours = 24 * 7,
    clientRequestId = createClientRequestId("create_share"),
  ): Promise<ShareSnapshotProjection> {
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/shares`,
      {
        method: "POST",
        body: JSON.stringify({
          expires_in_hours: expiresInHours,
          client_request_id: clientRequestId,
        }),
      },
      true,
    );
  }

  share(shareId: string, signal?: AbortSignal): Promise<ShareSnapshotProjection> {
    return this.json(`/api/v1/shares/${encodeURIComponent(shareId)}`, { signal });
  }

  revokeShare(
    shareId: string,
    clientRequestId = createClientRequestId("revoke_share"),
  ): Promise<ShareSnapshotProjection> {
    return this.json(
      `/api/v1/shares/${encodeURIComponent(shareId)}/revoke`,
      {
        method: "POST",
        body: JSON.stringify({ client_request_id: clientRequestId }),
      },
      true,
    );
  }

  createTurn(threadId: string, operation: ClientOperation): Promise<TurnMutationResponse> {
    assertOperationDisposition(operation, "create");
    if (operation.thread.kind === "existing" && operation.thread.thread_id !== threadId) {
      throw new TypeError("Invalid client operation.");
    }
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/turns`,
      {
        method: "POST",
        body: JSON.stringify({
          input: operation.input,
          agent_model_id: operation.models.agentModelId,
          image_model_id: operation.models.imageModelId,
          attachment_ids: operation.attachments.map((attachment) => attachment.attachment_id),
          client_message_id: operation.client_message_id,
          metadata: {},
        }),
      },
      true,
      validateTurnMutationBoundary,
    );
  }

  steerTurn(operation: ClientOperation): Promise<TurnMutationResponse> {
    assertOperationDisposition(operation, "steer");
    if (!operation.turn) throw new TypeError("Invalid client operation.");
    return this.json(
      `/api/v1/turns/${encodeURIComponent(operation.turn.turn_id)}/steer`,
      {
        method: "POST",
        body: JSON.stringify({
          input: operation.input,
          agent_model_id: operation.models.agentModelId,
          image_model_id: operation.models.imageModelId,
          attachment_ids: operation.attachments.map((attachment) => attachment.attachment_id),
          client_message_id: operation.client_message_id,
          metadata: {},
        }),
      },
      true,
      validateTurnMutationBoundary,
    );
  }

  queueTurn(threadId: string, operation: ClientOperation): Promise<TurnMutationResponse> {
    assertOperationDisposition(operation, "queue");
    if (operation.thread.kind === "existing" && operation.thread.thread_id !== threadId) {
      throw new TypeError("Invalid client operation.");
    }
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/queue`,
      {
        method: "POST",
        body: JSON.stringify({
          input: operation.input,
          agent_model_id: operation.models.agentModelId,
          image_model_id: operation.models.imageModelId,
          attachment_ids: operation.attachments.map((attachment) => attachment.attachment_id),
          client_message_id: operation.client_message_id,
          metadata: {},
        }),
      },
      true,
      validateTurnMutationBoundary,
    );
  }

  replaceTurn(operation: ClientOperation): Promise<ReplaceTurnResponse> {
    assertOperationDisposition(operation, "replace");
    if (!operation.turn) throw new TypeError("Invalid client operation.");
    return this.json(
      `/api/v1/turns/${encodeURIComponent(operation.turn.turn_id)}/replace`,
      {
        method: "POST",
        body: JSON.stringify({
          input: operation.input,
          agent_model_id: operation.models.agentModelId,
          image_model_id: operation.models.imageModelId,
          attachment_ids: operation.attachments.map((attachment) => attachment.attachment_id),
          client_message_id: operation.client_message_id,
          metadata: {},
          reason: "replaced_by_user",
        }),
      },
      true,
      validateReplaceTurnBoundary,
    );
  }

  interruptTurn(turnId: string): Promise<TurnMutationResponse> {
    return this.json(
      `/api/v1/turns/${encodeURIComponent(turnId)}/interrupt`,
      {
        method: "POST",
        body: JSON.stringify({ reason: "interrupted_by_user" }),
      },
      true,
      validateTurnMutationBoundary,
    );
  }

  connectorLoginInteraction(
    interactionId: string,
    operation: "begin",
  ): Promise<ConnectorLoginBeginResponse>;
  connectorLoginInteraction(
    interactionId: string,
    operation: "check",
  ): Promise<ConnectorLoginCheckResponse>;
  connectorLoginInteraction(
    interactionId: string,
    operation: "cancel",
  ): Promise<ConnectorLoginCancelResponse>;
  connectorLoginInteraction(
    interactionId: string,
    operation: "begin" | "check" | "cancel",
  ): Promise<ConnectorLoginBeginResponse | ConnectorLoginCheckResponse | ConnectorLoginCancelResponse> {
    return this.json<
      ConnectorLoginBeginResponse | ConnectorLoginCheckResponse | ConnectorLoginCancelResponse
    >(
      `/api/v1/interactions/${encodeURIComponent(interactionId)}/connector-login/${operation}`,
      { method: "POST", body: JSON.stringify({}) },
      true,
      (value) => validateInteractionBoundary(value, operation, interactionId),
    );
  }

  respondInteraction(
    interactionId: string,
    response: InteractionResponse,
    clientRequestId: string,
  ): Promise<InteractionMutationResponse> {
    return this.json(
      `/api/v1/interactions/${encodeURIComponent(interactionId)}/respond`,
      {
        method: "POST",
        body: JSON.stringify({
          response,
          client_request_id: clientRequestId,
        }),
      },
      true,
      (value) => validateInteractionBoundary(value, "mutation", interactionId),
    );
  }

  listArtifacts(threadId?: string, signal?: AbortSignal): Promise<ArtifactListResponse> {
    return this.artifactOperation("list", { threadId, signal });
  }

  artifact(artifactId: string, signal?: AbortSignal): Promise<ArtifactProjection> {
    return this.artifactOperation("get", { artifactId, signal });
  }

  artifactFeedback(
    artifact: ArtifactProjection,
    signal: "thumbs_up" | "thumbs_down",
  ): Promise<NonNullable<ArtifactProjection["feedback"]>> {
    const clientRequestId = createClientRequestId("artifact_feedback");
    return this.artifactOperation("feedback", { artifact, signal, clientRequestId });
  }

  artifactExternalAction(
    artifactId: string,
    action: "open" | "reveal",
    clientRequestId = createClientRequestId(`artifact_${action}`),
  ): Promise<ArtifactExternalActionProjection> {
    return this.artifactOperation("action", { artifactId, action, clientRequestId });
  }

  requestRetouch(
    artifact: ArtifactProjection,
    annotations: RetouchAnnotation[],
    globalInstruction: string,
    models: TurnModelSelection,
  ): Promise<RetouchJobProjection> {
    return this.artifactOperation("request_retouch", {
      artifact,
      annotations,
      globalInstruction,
      agentModelId: models.agentModelId,
      imageModelId: models.imageModelId,
      clientRequestId: createClientRequestId("artifact_retouch"),
    });
  }

  openRetouchWorkspace(
    artifact: ArtifactProjection,
    clientRequestId = createClientRequestId("retouch_workspace_open"),
  ): Promise<RetouchWorkspaceProjection> {
    return this.artifactOperation("open_workspace", { artifact, clientRequestId });
  }

  getRetouchWorkspace(
    workspaceId: string,
    signal?: AbortSignal,
  ): Promise<RetouchWorkspaceProjection> {
    return this.artifactOperation("workspace", { workspaceId, signal });
  }

  saveRetouchWorkspace(
    workspace: RetouchWorkspaceProjection,
    input: {
      annotations: RetouchAnnotation[];
      referenceArtifactIds: string[];
      globalInstruction: string;
      viewState: Partial<RetouchViewState>;
    },
    clientRequestId = createClientRequestId("retouch_workspace_save"),
  ): Promise<RetouchWorkspaceProjection> {
    return this.artifactOperation("save_workspace", {
      workspace,
      annotations: input.annotations,
      referenceArtifactIds: input.referenceArtifactIds,
      globalInstruction: input.globalInstruction,
      viewState: input.viewState,
      clientRequestId,
    });
  }

  submitRetouchWorkspace(
    workspace: RetouchWorkspaceProjection,
    models: TurnModelSelection,
    clientRequestId = createClientRequestId("retouch_workspace_submit"),
  ): Promise<RetouchWorkspaceProjection> {
    return this.artifactOperation("submit_workspace", {
      workspace,
      agentModelId: models.agentModelId,
      imageModelId: models.imageModelId,
      clientRequestId,
    });
  }

  reopenRetouchWorkspace(
    workspace: RetouchWorkspaceProjection,
    clientRequestId = createClientRequestId("retouch_workspace_reopen"),
  ): Promise<RetouchWorkspaceProjection> {
    return this.artifactOperation("reopen_workspace", { workspace, clientRequestId });
  }

  async retouchWorkspaceBlob(
    workspaceId: string,
    kind: "surface" | "result" | "reference",
    referenceArtifactId?: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const suffix = kind === "reference"
      ? `/references/${encodeURIComponent(referenceArtifactId ?? "")}/preview`
      : `/${kind}`;
    const response = await fetch(
      `${this.base}/api/v1/retouch-workspaces/${encodeURIComponent(workspaceId)}${suffix}`,
      {
        headers: this.headers(false),
        credentials: "same-origin",
        cache: "no-store",
        signal,
      },
    );
    if (!response.ok) {
      const contentType = response.headers.get("content-type") ?? "";
      const payload = contentType.includes("application/json")
        ? await response.json().catch(() => null)
        : null;
      const parsed = parseError(payload, `Retouch image request failed (${response.status}).`);
      throw new RuntimeApiError(parsed.message, response.status, parsed.code);
    }
    return response.blob();
  }

  async artifactBlob(
    artifactId: string,
    kind: "preview" | "content",
    signal?: AbortSignal,
  ): Promise<Blob> {
    const response = await fetch(
      `${this.base}/api/v1/artifacts/${encodeURIComponent(artifactId)}/${kind}`,
      {
        headers: this.headers(false),
        credentials: "same-origin",
        cache: "no-store",
        signal,
      },
    );
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const parsed = parseError(payload, `Artifact request failed (${response.status}).`);
      throw new RuntimeApiError(parsed.message, response.status, parsed.code);
    }
    return response.blob();
  }

  eventPage(
    threadId: string,
    afterSeq: number,
    limit = 1_000,
    signal?: AbortSignal,
  ): Promise<ClientEventPage> {
    const query = new URLSearchParams({
      after_seq: String(afterSeq),
      limit: String(limit),
    });
    return this.json(
      `/api/v1/threads/${encodeURIComponent(threadId)}/events?${query.toString()}`,
      { signal },
      false,
      validateClientEventPage,
    );
  }

  async streamEvents(
    threadId: string,
    afterSeq: number,
    onEvent: (event: EventEnvelope) => void,
    signal: AbortSignal,
    onOpen?: () => void,
  ): Promise<void> {
    const query = new URLSearchParams({ after_seq: String(afterSeq), follow: "true" });
    const headers = this.headers(false, {
      Accept: "text/event-stream",
      "Last-Event-ID": String(afterSeq),
    });
    const response = await fetch(
      `${this.base}/api/v1/threads/${encodeURIComponent(threadId)}/events/stream?${query}`,
      {
        method: "GET",
        headers,
        credentials: "same-origin",
        cache: "no-store",
        signal,
      },
    );
    if (response.status === 409) throw new EventCursorResetRequired();
    if (!response.ok || !response.body) {
      throw new RuntimeApiError(
        `Event stream failed (${response.status}).`,
        response.status,
      );
    }
    onOpen?.();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        this.parseEventBlock(block, onEvent);
        boundary = buffer.indexOf("\n\n");
      }
      if (done) return;
    }
  }

  private parseEventBlock(
    block: string,
    onEvent: (event: EventEnvelope) => void,
  ): void {
    if (!block || block.startsWith(":")) return;
    let eventType = "message";
    let eventId: string | null = null;
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      if (line.startsWith("id:")) eventId = line.slice(3).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length || eventType === "watermark" || eventType === "keepalive") return;
    const parsed = validateEventEnvelope(JSON.parse(data.join("\n")));
    if (eventType !== "message" && eventType !== parsed.event_type) {
      throw new RuntimeApiError(
        "事件流类型与事实信封不一致。",
        502,
        "event_stream_type_mismatch",
      );
    }
    if (eventId !== String(parsed.seq)) {
      throw new RuntimeApiError(
        "事件流序号与事实信封不一致。",
        502,
        "event_stream_id_mismatch",
      );
    }
    onEvent(parsed);
  }
}

export type RuntimeSession = {
  session_id?: string;
  id?: string;
  title?: string;
  created_at?: string | number;
  last_active?: string | number;
  updatedAt?: string | number;
  msg_count?: number;
  title_locked?: boolean;
  titleLocked?: boolean;
  projectId?: string;
  projectName?: string;
  projectPath?: string;
  memoryPath?: string;
  dreamsPath?: string;
  scope?: "project" | "general" | string;
  project?: ProjectSessionBinding | null;
};

export type RuntimeActiveRequest = {
  request_id?: string;
  session_id?: string;
  cancelled?: boolean;
  status?: string;
  phase?: string;
  state?: "running" | "cancelling" | string;
  source?: "cancel_registry" | string;
  run_type?: string;
  terminal_reason?: string;
  error_code?: string;
  error_message?: string;
  recoverable?: boolean;
  retryable?: boolean;
  retry_after_ms?: number;
  retry_mode?: string;
  retry_disabled_reason?: string;
  actions?: {
    open?: boolean;
    recover?: boolean;
    retry?: boolean;
    stop?: boolean;
    diagnostics?: boolean;
  };
  created_at?: number;
  cancelled_at?: number | null;
  updated_at?: number;
  terminal_at?: number;
  age_seconds?: number;
  terminal_age_seconds?: number | null;
  cancel_age_seconds?: number | null;
  stream_available?: boolean;
  metadata?: Record<string, unknown>;
};

export type RuntimeSessionLock = {
  sessionHash?: string;
  lockPath?: {
    present?: boolean;
    pathHash?: string;
    redacted?: boolean;
  };
  removeError?: boolean;
  deadOwner?: boolean;
  redacted?: boolean;
  path?: string;
  session_id?: string;
  pid?: number | string;
  host?: string;
  created_at?: number;
  age_seconds?: number;
  alive?: boolean | null;
  dead_owner?: boolean;
  stale?: boolean;
  removed?: boolean;
  removable?: boolean;
};

export type RuntimeTool = {
  name?: string;
  description?: string;
};

export type RuntimeSkill = {
  name?: string;
  display_name?: string;
  description?: string;
  source?: string;
  source_group?: string;
  sourceGroup?: string;
  source_label?: string;
  sourceLabel?: string;
  purpose_group?: string;
  purposeGroup?: string;
  purpose_label?: string;
  purposeLabel?: string;
  path?: string;
  enabled?: boolean;
  default_enabled?: boolean;
  defaultEnabled?: boolean;
  toggleable?: boolean;
  locked?: boolean;
  lock_reason?: string;
  lockReason?: string;
  category?: string;
  user_invocable?: boolean;
  disable_model_invocation?: boolean;
  mentionable?: boolean;
  mention_category?: string;
  mention_hidden_reason?: string;
  primary_env?: string;
};

export type ChatModelOption = {
  provider: string;
  providerLabel?: string;
  model: string;
  label?: string;
  hint?: string;
  configured?: boolean;
  current?: boolean;
  contextPolicy?: ChatContextPolicy;
  modelAliasFamily?: string;
  effectiveTransportProvider?: string;
  isOfficialGeminiProvider?: boolean;
  officialGeminiApiUsed?: boolean;
};

export type ChatContextPolicy = {
  contextWindowTokens?: number;
  maxOutputTokens?: number;
  autoCompactTokenLimit?: number;
  hardContextTokenLimit?: number;
  source?: string;
  note?: string;
  tokenizer?: string;
  tokenizerStatus?: string;
  tokenizerNote?: string;
};

export type ChatContextContinuity = {
  agentBridgePreserved?: boolean;
  existingAgentRoutesReset?: number;
  artifactHistoryRefs?: string;
  strategy?: string;
};

export type ChatModelsPayload = {
  status?: string;
  currentProvider: string;
  currentModel: string;
  currentContextPolicy?: ChatContextPolicy;
  options: ChatModelOption[];
};

export type SetChatModelResult = {
  [key: string]: unknown;
  status?: string;
  provider?: string;
  model?: string;
  image_model?: string;
  context_policy?: unknown;
  contextPolicy?: ChatContextPolicy;
  context_continuity?: unknown;
  contextContinuity?: ChatContextContinuity;
  applied?: Record<string, unknown>;
  noop?: boolean;
  message?: string;
};

export type RuntimeChannelAuth = {
  mode?: string;
  channelAuthorization?: string;
  channelAuthSupported?: boolean;
  authEndpoint?: string;
  authEndpointMethods?: string[];
  statusProbe?: string;
  channelConfigState?: string;
  requiredFields?: string[];
  presentFields?: string[];
  missingFields?: string[];
  agentAuthSupported?: boolean;
  agentAuthorizationAction?: Record<string, unknown> | null;
};

export type RuntimeChannelAgentSurface = {
  tool?: string;
  declaredDiscoverable?: boolean;
  schemaVisible?: boolean | null;
  discoverable?: boolean;
  toolSchemaCallable?: boolean;
  callable?: boolean;
  readiness?: string;
  callableReason?: string;
  requiresStatusProbe?: boolean;
  permissionGated?: boolean;
  policy?: string;
  installAbility?: string;
  installPack?: string;
  statusAction?: Record<string, unknown> | null;
  authorizationAction?: Record<string, unknown> | null;
  status?: string;
};

export type RuntimeExtension = {
  id: string;
  type: "builtin_skill" | "user_skill" | "connector" | "mcp_server" | "capability_pack" | "plugin" | string;
  displayName?: string;
  description?: string;
  origin?: string;
  source?: string;
  source_group?: string;
  sourceGroup?: string;
  source_label?: string;
  sourceLabel?: string;
  purpose_group?: string;
  purposeGroup?: string;
  purpose_label?: string;
  purposeLabel?: string;
  sourceUrl?: string;
  sourcePath?: string;
  version?: string;
  enabled?: boolean;
  default_enabled?: boolean;
  defaultEnabled?: boolean;
  toggleable?: boolean;
  locked?: boolean;
  lock_reason?: string;
  lockReason?: string;
  installed?: boolean;
  policy?: string;
  permissions?: string[];
  requires?: unknown;
  provides?: string[];
  configRefs?: unknown;
  status?: string;
  lastError?: string;
  category?: string;
  primary_env?: string;
  user_invocable?: boolean;
  disable_model_invocation?: boolean;
  mentionable?: boolean;
  mention_category?: string;
  mention_hidden_reason?: string;
  active?: boolean;
  configured?: boolean;
  running?: boolean;
  configState?: string;
  auth?: RuntimeChannelAuth;
  agentSurface?: RuntimeChannelAgentSurface;
};

export type RuntimeChannel = {
  name?: string;
  aliases?: string[];
  label?: {
    zh?: string;
    en?: string;
  };
  description?: string;
  active?: boolean;
  configured?: boolean;
  running?: boolean;
  status?: string;
  configState?: string;
  auth?: RuntimeChannelAuth;
  agentSurface?: RuntimeChannelAgentSurface;
};

export type ExternalConnectionField = {
  key: string;
  label?: string;
  type?: "text" | "secret" | "number" | "bool" | string;
  value?: unknown;
  default?: unknown;
  sensitive?: boolean;
  masked?: boolean;
};

export type ExternalConnectionAction = {
  id: string;
  label?: string;
  enabled?: boolean;
};

export type ExternalConnectionLogo = {
  type?: "brand" | string;
  key?: string;
  fallbackText?: string;
  icon?: string;
  color?: string;
};

export type ExternalConnectionAdapterContract = {
  version?: string;
  platform?: string;
  readiness?: Record<string, unknown>;
  lifecycle?: Record<string, unknown>;
  ingress?: Record<string, unknown>;
  egress?: Record<string, unknown>;
  projection?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ExternalConnection = {
  id: string;
  platform?: string;
  name?: string;
  label?: { zh?: string; en?: string };
  displayName?: string;
  description?: string;
  logo?: ExternalConnectionLogo;
  status?: string;
  configured?: boolean;
  enabled?: boolean;
  connected?: boolean;
  running?: boolean;
  callable?: boolean;
  lastError?: string;
  dependencyMissing?: boolean;
  dependencyStatus?: Record<string, unknown>;
  configState?: unknown;
  auth?: RuntimeChannelAuth;
  agentSurface?: RuntimeChannelAgentSurface;
  adapterContract?: ExternalConnectionAdapterContract;
  fields?: ExternalConnectionField[];
  homeChannel?: { id?: string; idHash?: string; configured?: boolean; name?: string; [key: string]: unknown };
  configSchema?: { fields?: ExternalConnectionField[]; [key: string]: unknown };
  actions?: ExternalConnectionAction[];
  source?: string;
};

export type ExternalConnectionAdapterTest = {
  testMode?: string;
  remoteConnectivityProbed?: boolean;
  [key: string]: unknown;
};

export type ExternalConnectionTestResult = {
  configured?: boolean;
  connected?: boolean;
  callable?: boolean;
  lastError?: string;
  mode?: string;
  remoteConnectivityProbed?: boolean;
  [key: string]: unknown;
};

export type ExternalConnectionActionResponse = {
  status?: string;
  message?: string;
  connection?: ExternalConnection;
  adapter?: ExternalConnectionAdapterTest;
  test?: ExternalConnectionTestResult;
  channel_type?: string;
  starting?: boolean;
  operation_id?: string;
  capability_refresh_required?: boolean;
  homeChannelConfigured?: boolean;
  applied?: string[];
  unchanged?: boolean;
  [key: string]: unknown;
};

export type ExternalConnectionsPayload = {
  status?: string;
  connections?: ExternalConnection[];
  summary?: Record<string, number>;
  updatedAt?: number;
  message?: string;
};

export type RuntimeSchedulerDeliveryTarget = {
  status?: string;
  channelType?: string;
  source?: string;
  reason?: string;
  receiverHash?: string;
  homeChannelRequired?: boolean;
  homeChannelConfigured?: boolean;
};

export type RuntimeSchedulerTaskAction = {
  type?: string;
  channelType?: string;
  receiverNameHash?: string;
  receiverHash?: string;
  isGroup?: boolean;
  contentPreview?: string;
  contentHash?: string;
  contentLength?: number;
  contentBytes?: number;
  taskDescriptionPreview?: string;
  taskDescriptionHash?: string;
  taskDescriptionLength?: number;
  taskDescriptionBytes?: number;
  toolName?: string;
  toolParams?: Record<string, unknown>;
  skillName?: string;
  skillArgs?: Record<string, unknown>;
  resultPrefixPreview?: string;
  resultPrefixHash?: string;
  resultPrefixLength?: number;
  resultPrefixBytes?: number;
  deliveryTarget?: RuntimeSchedulerDeliveryTarget;
};

export type RuntimeSchedulerTask = {
  id: string;
  name?: string;
  enabled?: boolean;
  state?: string;
  schedule?: Record<string, unknown>;
  scheduleDescription?: string;
  action?: RuntimeSchedulerTaskAction;
  createdAt?: string;
  updatedAt?: string;
  nextRunAt?: string;
  lastRunAt?: string;
  lastError?: string;
  lastErrorAt?: string;
};

export type RuntimeSchedulerProjection = {
  enabled?: boolean;
  initialized?: boolean;
  running?: boolean;
  threadAlive?: boolean;
  serviceStatus?: string;
  blockingReason?: string;
  taskStore?: {
    path?: string;
    exists?: boolean;
  };
  tasks?: RuntimeSchedulerTask[];
  taskCount?: number;
  counts?: {
    total?: number;
    enabled?: number;
    disabled?: number;
    error?: number;
  };
  loadError?: string;
  canStart?: boolean;
  canModify?: boolean;
  modifyBlockingReason?: string;
  pollIntervalSeconds?: number;
};

export type RuntimeToolCall = {
  id?: string;
  name?: string;
  tool?: string;
  arguments?: unknown;
  input?: unknown;
  result?: unknown;
  qualityEvidence?: QualityEvidence;
  status?: string;
  is_error?: boolean;
  execution_time?: number;
  deadline_seconds?: number;
  max_seconds?: number;
  extension_count?: number;
  lastHeartbeatAt?: number;
  function?: {
    name?: string;
    arguments?: unknown;
  };
};

export type RuntimeReleaseNotes = {
  version: string;
  revision?: string;
  title?: string;
  summary?: string;
  highlights?: string[];
  fixes?: string[];
  howTo?: string[];
  updatePolicy?: {
    windows?: string;
    macos?: string;
    webui?: string;
  };
};

export type RuntimeUpdateState = {
  stateAvailable?: boolean;
  source?: string;
  product?: string;
  version?: string;
  mode?: "manual" | "background" | string;
  status?: "installed" | "deferred" | "failed" | "rollback" | "started" | "ready" | string;
  reason?: string;
  url?: string;
  browserAction?: "defer-to-existing-tab-soft-refresh" | "open-default-browser" | "none" | string;
  activationPolicy?: string;
  healthCheck?: {
    endpoint?: string;
    status?: "pass" | "pending" | "failed" | string;
    passed?: boolean;
  };
  generatedAt?: string;
  refreshRequired?: boolean;
  redacted?: boolean;
};

export type RuntimeStep = {
  type?: string;
  content?: string;
  text?: string;
  thinking?: string;
  name?: string;
  tool?: string;
  arguments?: unknown;
  input?: unknown;
  result?: unknown;
  qualityEvidence?: QualityEvidence;
  status?: string;
  is_error?: boolean;
  execution_time?: number;
  deadline_seconds?: number;
  max_seconds?: number;
  extension_count?: number;
  lastHeartbeatAt?: number;
  has_tool_calls?: boolean;
  file_name?: string;
  file_type?: string;
  url?: string;
  path?: string;
};

export type RuntimeMessage = {
  role?: "user" | "assistant";
  content?: string;
  pending?: boolean;
  created_at?: number;
  seq?: number;
  _seq?: number;
  user_seq?: number;
  reasoning?: string;
  steps?: RuntimeStep[];
  tool_calls?: RuntimeToolCall[];
  artifacts?: AgentArtifact[];
  kind?: string;
  request_id?: string;
  turn_id?: string;
  bot_seq?: number;
  extras?: {
    request_id?: string;
    turn_id?: string;
    user_seq?: number;
    bot_seq?: number;
    audio?: {
      url?: string;
      kind?: string;
    };
    attachments?: unknown;
    artifacts?: unknown;
    [key: string]: unknown;
  };
};

export type RuntimeHistoryResult = {
  messages: RuntimeMessage[];
  contextStartSeq: number;
  projectContext?: ProjectSessionBinding | null;
  total?: number;
  page?: number;
  pageSize?: number;
  hasMore?: boolean;
};

export type RuntimeProjectionEvent = {
  event_id?: number;
  request_id?: string;
  session_id?: string;
  turn_id?: string;
  event_seq?: number;
  event_type?: string;
  source?: string;
  created_at?: number;
  payload?: Record<string, unknown>;
};

export type RuntimeRequestProjection = {
  request_id?: string;
  session_id?: string;
  turn_id?: string;
  state?: string;
  terminal_reason?: string;
  terminal_message?: string;
  first_event_id?: number;
  latest_event_id?: number;
  event_count?: number;
  messages?: RuntimeMessage[];
  events?: RuntimeProjectionEvent[];
};

export type RuntimeSessionProjection = {
  session_id?: string;
  after_event_id?: number;
  latest_event_id?: number;
  requests?: RuntimeRequestProjection[];
  events?: RuntimeProjectionEvent[];
};

export type RuntimeRequestProjectionResult = {
  mode: "request";
  latestEventId: number;
  projection: RuntimeRequestProjection;
};

export type RuntimeSessionProjectionResult = {
  mode: "session";
  latestEventId: number;
  projection: RuntimeSessionProjection;
};

export type RuntimeProjectionResult = RuntimeRequestProjectionResult | RuntimeSessionProjectionResult;

export type RuntimeProjectionInput =
  | {
      mode: "request";
      requestId: string;
      sessionId?: string;
      limit?: number;
    }
  | {
      mode: "session";
      sessionId: string;
      afterEventId?: number;
      limit?: number;
    };

export type FileAttachment = {
  file_path: string;
  file_name: string;
  file_type: "image" | "video" | "audio" | "file" | "directory";
  previewDataUrl?: string;
  preview_url?: string;
};

export type LocalPathStat = {
  status?: string;
  message?: string;
  path: string;
  exists: boolean;
  isFile?: boolean;
  isDirectory?: boolean;
  mimeType?: string;
  sizeBytes?: number;
};

export type LocalJsonResult = {
  status?: string;
  message?: string;
  path?: string;
  data?: unknown;
};

export type AgentArtifactKind = "file" | "image" | "video" | "audio" | "directory" | "url" | "diff";
export type AgentArtifactIntent = "deliverable" | "changed-file" | "preview";
export type AgentArtifactOperation = "created" | "modified" | "exported" | "downloaded" | "deployed";
export type AgentArtifactStatus = "pending" | "ready" | "failed" | "superseded";
export type OpenPathAction = "open" | "reveal" | "openWith";

export type QualityEvidenceStatus = "pass" | "fail" | "warn" | "pending" | "skipped" | "unknown";
export type QualityEvidenceCheck = {
  id?: string;
  status?: QualityEvidenceStatus | string;
  detail?: string;
};
export type QualityEvidence = {
  schemaVersion?: string;
  kind?: "presentation" | "spreadsheet" | "document" | "pdf" | "image" | string;
  sourceRef?: string;
  qualityGates?: string[];
  checks?: QualityEvidenceCheck[];
  missingQualityGates?: string[];
  status?: QualityEvidenceStatus | string;
  renderedArtifacts?: Array<Record<string, unknown>>;
  redacted?: boolean;
  qualityEvidenceSanitized?: boolean;
  omittedQualityEvidenceFieldCount?: number;
  presentationAnalysis?: Record<string, unknown>;
  spreadsheetAnalysis?: Record<string, unknown>;
  documentAnalysis?: Record<string, unknown>;
  pdfAnalysis?: Record<string, unknown>;
  pdfDiffAnalysis?: Record<string, unknown>;
  imageAnalysis?: Record<string, unknown>;
  [key: string]: unknown;
};

export type AgentArtifact = {
  id: string;
  requestId?: string;
  kind: AgentArtifactKind;
  intent: AgentArtifactIntent;
  operation: AgentArtifactOperation;
  status: AgentArtifactStatus;
  title: string;
  path?: string;
  relativePath?: string;
  url?: string;
  mimeType?: string;
  sizeBytes?: number;
  previewUrl?: string;
  thumbnailUrl?: string;
  statusPath?: string;
  qualityEvidence?: QualityEvidence;
  stats?: {
    addedLines?: number;
    removedLines?: number;
    bytesWritten?: number;
  };
  source?: {
    toolCallId?: string;
    toolName?: string;
    activityId?: string;
    createdAt?: number;
  };
};

export type RuntimeSnapshot = {
  status: "ready" | "offline" | "error";
  message: string;
  version?: string;
  releaseNotes?: RuntimeReleaseNotes;
  updateState?: RuntimeUpdateState;
  currentProvider?: string;
  currentModel?: string;
  sessions: RuntimeSession[];
  activeRequests?: RuntimeActiveRequest[];
  recentTerminalRequests?: RuntimeActiveRequest[];
  runStatusCounts?: Record<string, number>;
  activeRequestsStatus?: string;
  staleLocks?: RuntimeSessionLock[];
  totalSessions: number;
  toolsCount: number;
  skillsCount: number;
  modelsCount: number;
  tools?: RuntimeTool[];
  skills?: RuntimeSkill[];
  extensions?: RuntimeExtension[];
  extensionsCount?: number;
  extensionSummary?: Record<string, number>;
  modelCapabilities?: Record<string, unknown>;
  scheduler?: RuntimeSchedulerProjection;
};

export type RuntimeSnapshotOptions = {
  includeSessionIds?: string[];
};

const PINNED_SESSIONS_STORAGE_KEY = "ecorex-pinned-sessions";
const LAST_ACTIVE_SESSION_STORAGE_KEY = "ecorex-last-active-session-id";

function safeReadJsonObject(key: string): Record<string, unknown> {
  if (typeof window === "undefined" || !window.localStorage) return {};
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function runtimeSnapshotSessionIncludes(options: RuntimeSnapshotOptions = {}) {
  const includeIds = new Set<string>();
  const pinnedIds: string[] = [];
  (options.includeSessionIds || []).forEach((value) => {
    const sessionId = String(value || "").trim();
    if (sessionId) includeIds.add(sessionId);
  });
  Object.entries(safeReadJsonObject(PINNED_SESSIONS_STORAGE_KEY)).forEach(([sessionId, pinned]) => {
    if (!pinned) return;
    const normalized = String(sessionId || "").trim();
    if (!normalized) return;
    includeIds.add(normalized);
    pinnedIds.push(normalized);
  });
  if (typeof window !== "undefined" && window.localStorage) {
    const activeSessionId = String(window.localStorage.getItem(LAST_ACTIVE_SESSION_STORAGE_KEY) || "").trim();
    if (activeSessionId) includeIds.add(activeSessionId);
  }
  return {
    includeIds: Array.from(includeIds).slice(0, 200),
    pinnedIds: Array.from(new Set(pinnedIds)).slice(0, 200)
  };
}

export type RuntimeUiStateSync = {
  schemaVersion?: number;
  projects?: ProjectFolder[];
  sessionProjects?: Record<string, string>;
  sessionProjectBindings?: Record<string, ProjectSessionBinding>;
  sessionTitles?: Record<string, string>;
  sessionUiState?: Record<string, unknown>;
  pinnedSessions?: Record<string, boolean>;
  pinnedSessionTimes?: Record<string, number>;
  pinnedProjects?: Record<string, boolean>;
  activeProjectId?: string | null;
  activeSessionId?: string;
  lastActiveSessionId?: string;
  updatedAt?: number;
  savedAt?: string;
  replaceProjectState?: boolean;
  projectStateMode?: "replace" | "merge";
  [key: string]: unknown;
};

export type DiagnosticsBundle = {
  [key: string]: unknown;
  status?: string;
  type?: string;
  generatedAt?: string;
  version?: string;
  runtime?: Record<string, unknown>;
  current?: {
    session_id?: string;
    request_id?: string;
  };
  activeRequests?: unknown[];
  staleLocks?: unknown[];
  logs?: {
    path?: Record<string, unknown>;
    exists?: boolean;
    recentEvents?: Array<Record<string, unknown>>;
    note?: string;
  };
  privacy?: {
    includesPromptText?: boolean;
    includesFileContents?: boolean;
    includesArtifactContents?: boolean;
  };
  message?: string;
};

export type UsageQuota = {
  allowed?: boolean;
  reason?: string;
  dailyUsed?: number;
  weeklyUsed?: number;
  dailyLimit?: number;
  weeklyLimit?: number;
  [key: string]: unknown;
};

export type EnterpriseQuotaCheckResult = {
  ok: boolean;
  quota?: UsageQuota;
};

export type CapabilityState =
  | "installed"
  | "not-installed"
  | "checking"
  | "installing"
  | "busy"
  | "failed"
  | "unknown";

export type CapabilityPack = {
  id: string;
  name: string;
  summary: string;
  installMode: "user-or-admin" | "admin-recommended";
  defaultEnabled?: boolean;
  readOnly?: boolean;
  configureOnly?: boolean;
  discoveryOnly?: boolean;
  sourceUrl?: string;
  mirrorUrls?: string[];
  installHint?: string;
  allowedCommands?: string[];
  estimatedSizeMb?: number;
  state: CapabilityState;
  message: string;
  installed: boolean;
  logPath?: string;
  missingModules?: string[];
  updatedAt?: string;
  policyMode?: "ask" | "preinstall" | "disabled";
  installAllowed?: boolean;
  disabledReason?: string;
  policyStatus?: string;
  policyUpdatedAt?: string;
  policySource?: string;
};

export type ProjectFolder = {
  id: string;
  name: string;
  path: string;
  pinned?: boolean;
  memoryPath?: string;
  dreamsPath?: string;
  updatedAt: string;
};

export type ProjectSessionBinding = {
  projectId: string;
  projectName: string;
  projectPath: string;
  memoryPath?: string;
  dreamsPath?: string;
  createdAt?: string;
  lastUsedAt?: string;
  source?: "project-new-session" | "project-session-send" | "runtime" | string;
};

export type MemoryFile = {
  filename?: string;
  name?: string;
  category?: string;
  updated_at?: string;
  updatedAt?: string;
  size?: number;
  preview?: string;
};

export type PermissionMode = "full-access" | "smart-ask" | "always-ask" | "read-only" | "custom";

export type PermissionState = {
  mode: PermissionMode;
  grantsCount: number;
  auditPath: string;
  updatedAt?: string;
};

export type EnterpriseSession = Awaited<ReturnType<NonNullable<typeof window.ecorexDesktop>["getEnterpriseSession"]>>;

export type ChatSendResult = {
  status?: string;
  message?: string;
  code?: string;
  error_type?: string;
  state?: string;
  recoverable?: boolean;
  retryable?: boolean;
  retry_after_ms?: number;
  reason?: string;
  session_id?: string;
  active_request_ids?: string[];
  request_id?: string;
  stream?: boolean;
  inline_reply?: string;
  usage?: TokenUsage;
  same_session?: {
    policy?: string;
    queue?: string;
    decision?: "accepted" | "replacement_accepted" | "accepted_after_recovery" | "accepted_after_finalize_wait" | "retryable_conflict" | string;
    active_request_ids?: string[];
    replaced_request_ids?: string[];
    cancelled_requests?: number;
    cancelled_subagents?: number;
    retry_after_ms?: number;
    reason?: string;
  };
};

export type RetryPrepareResult = {
  status?: string;
  message?: string;
  request_id?: string;
  session_id?: string;
  retryable?: boolean;
  recoverable?: boolean;
  exactReplay?: boolean;
  exact_replay?: boolean;
  retry_after_ms?: number;
  retry_mode?: string;
  prompt?: string;
  visible_message?: string;
  attachments?: FileAttachment[];
  source_user_seq?: number | null;
  reason?: string;
};

export type ToolPermissionDecision = "allow_once" | "always_allow" | "deny";

export type TokenUsage = {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  model?: string;
  provider?: string;
};

export type StreamItem = {
  type?: string;
  protocol_version?: string;
  event_type?: string;
  state?: string;
  terminal?: boolean;
  terminal_reason?: string;
  error_code?: string;
  error_type?: string;
  error_taxonomy?: string;
  retryable?: boolean;
  recoverable?: boolean;
  retry_after_ms?: number;
  retry_mode?: string;
  retry_exhausted?: boolean;
  retry_suppressed?: boolean;
  retry_suppressed_reason?: string;
  retry_attempt?: number;
  max_retries?: number;
  status_code?: number | string;
  requested_last_event_id?: number;
  retained_from_event_id?: number;
  next_event_id?: number;
  content?: string;
  text?: string;
  delta?: string;
  message?: string;
  title?: string;
  tool?: string;
  name?: string;
  arguments?: unknown;
  input?: unknown;
  result?: unknown;
  qualityEvidence?: QualityEvidence;
  status?: string;
  execution_time?: number;
  elapsed_seconds?: number;
  timeout_seconds?: number;
  previous_deadline_seconds?: number;
  deadline_seconds?: number;
  max_seconds?: number;
  extension_count?: number;
  has_tool_calls?: boolean;
  permission_request_id?: string;
  tool_call_id?: string;
  summary?: string;
  mode?: string;
  created_at?: string;
  request_id?: string;
  timestamp?: number;
  file_name?: string;
  file_type?: string;
  url?: string;
  path?: string;
  artifact?: AgentArtifact;
  artifacts?: AgentArtifact[];
  action?: string;
  user_seq?: number;
  bot_seq?: number;
  usage?: TokenUsage;
};

type ApiSuccess = Record<string, unknown> & {
  status?: string;
  message?: string;
};

async function apiJson<T extends ApiSuccess>(path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", body?: unknown): Promise<T> {
  if (!window.ecorexDesktop?.apiJson) {
    throw new Error("EcoreX desktop bridge is not available");
  }
  const result = await window.ecorexDesktop.apiJson({ path, method, body });
  if (!result || typeof result !== "object") {
    throw new Error("Invalid sidecar response");
  }
  const payload = result as ApiSuccess;
  if (payload.status === "error") {
    throw new Error(payload.message || "EcoreX local runtime is unavailable");
  }
  return result as T;
}

function countArray(value: unknown) {
  return Array.isArray(value) ? value.length : 0;
}

function toolNameFromBuiltinExtension(extension: RuntimeExtension) {
  const id = String(extension.id || "").trim();
  if (id.startsWith("tool:")) return id.slice("tool:".length).trim();
  if (extension.type === "builtin_tool") {
    const providedTool = (extension.provides || [])
      .map((item) => String(item || "").trim())
      .find((item) => item && item !== "tool");
    if (providedTool) return providedTool;
  }
  return "";
}

function extensionDeclaresReadyTool(extension: RuntimeExtension) {
  const status = String(extension.status || "").trim().toLowerCase();
  return extension.type === "builtin_tool"
    && extension.enabled !== false
    && extension.installed !== false
    && !["disabled", "error", "missing", "not_loaded"].includes(status);
}

function mergeBuiltinExtensionTools(tools: RuntimeTool[], extensions: RuntimeExtension[]) {
  const byName = new Map<string, RuntimeTool>();
  for (const tool of tools) {
    const name = String(tool.name || "").trim();
    if (name) byName.set(name, tool);
  }
  for (const extension of extensions) {
    if (!extensionDeclaresReadyTool(extension)) continue;
    const name = toolNameFromBuiltinExtension(extension);
    if (!name || byName.has(name)) continue;
    byName.set(name, {
      name,
      description: extension.description || extension.displayName || ""
    });
  }
  return Array.from(byName.values());
}

type RuntimeCapabilitySnapshot = {
  tools: RuntimeTool[];
  skills: RuntimeSkill[];
  extensions: RuntimeExtension[];
  extensionsCount: number;
  extensionSummary: Record<string, number>;
  modelsCount: number;
  currentProvider: string;
  currentModel: string;
  modelCapabilities: Record<string, unknown>;
};

let runtimeCapabilityCache: (RuntimeCapabilitySnapshot & { fetchedAt: number }) | null = null;
let runtimeCapabilityPromise: Promise<RuntimeCapabilitySnapshot> | null = null;
const RUNTIME_CAPABILITY_CACHE_MS = 45_000;

function invalidateRuntimeCapabilities() {
  runtimeCapabilityCache = null;
  runtimeCapabilityPromise = null;
}

async function loadRuntimeCapabilities(): Promise<RuntimeCapabilitySnapshot> {
  const now = Date.now();
  if (runtimeCapabilityCache && now - runtimeCapabilityCache.fetchedAt < RUNTIME_CAPABILITY_CACHE_MS) {
    return runtimeCapabilityCache;
  }
  if (runtimeCapabilityPromise) {
    return runtimeCapabilityPromise;
  }
  runtimeCapabilityPromise = Promise.all([
    apiJson<{ tools?: RuntimeTool[] }>("/api/tools").catch(() => ({ tools: [] })),
    apiJson<{ skills?: RuntimeSkill[] }>("/api/skills").catch(() => ({ skills: [] })),
    apiJson<{ providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] }>("/api/models").catch(() => ({ providers: [], capabilities: {} })),
    apiJson<{ extensions?: RuntimeExtension[]; count?: number; summary?: Record<string, number> }>("/api/extensions").catch(() => ({ extensions: [], count: 0, summary: {} })),
    apiJson<{ channels?: RuntimeChannel[] }>("/api/channels").catch(() => ({ channels: [] }))
  ]).then(([tools, skills, models, extensions, channels]) => {
    const apiRuntimeTools = Array.isArray(tools.tools) ? tools.tools : [];
    const runtimeSkills = Array.isArray(skills.skills) ? skills.skills : [];
    const extensionMap = new Map<string, RuntimeExtension>();
    for (const extension of Array.isArray(extensions.extensions) ? extensions.extensions : []) {
      if (extension && extension.id) {
        extensionMap.set(extension.id, extension);
      }
    }
    for (const channel of Array.isArray(channels.channels) ? channels.channels : []) {
      const name = typeof channel.name === "string" ? channel.name.trim() : "";
      if (!name) continue;
      const id = `channel:${name}`;
      if (!extensionMap.has(id)) {
        extensionMap.set(id, {
          id,
          type: "connector",
          displayName: channel.label?.zh || channel.label?.en || name,
          description: channel.description || "",
          origin: "channel",
          enabled: Boolean(channel.active),
          installed: true,
          policy: "runtime-config",
          provides: ["channel"],
          status: channel.status || (channel.active ? "active" : channel.configured ? "configured" : "available")
        });
      }
    }
    const runtimeExtensions = Array.from(extensionMap.values());
    const runtimeTools = mergeBuiltinExtensionTools(apiRuntimeTools, runtimeExtensions);
    const extensionSummary: Record<string, number> = {};
    for (const extension of runtimeExtensions) {
      const type = extension.type || "unknown";
      extensionSummary[type] = (extensionSummary[type] || 0) + 1;
    }
    const capabilityCount = Array.isArray(models.capabilities)
      ? models.capabilities.length
      : models.capabilities && typeof models.capabilities === "object"
        ? Object.keys(models.capabilities).length
        : 0;
    const modelCapabilities = models.capabilities && typeof models.capabilities === "object" ? models.capabilities as Record<string, unknown> : {};
    const chatCapability = modelCapabilities.chat && typeof modelCapabilities.chat === "object"
      ? modelCapabilities.chat as Record<string, unknown>
      : {};
    const snapshot: RuntimeCapabilitySnapshot & { fetchedAt: number } = {
      tools: runtimeTools,
      skills: runtimeSkills,
      extensions: runtimeExtensions,
      extensionsCount: runtimeExtensions.length,
      extensionSummary,
      modelsCount: countArray(models.providers) || capabilityCount,
      currentProvider: pickString(chatCapability.current_provider),
      currentModel: inferCurrentModel(models),
      modelCapabilities,
      fetchedAt: Date.now()
    };
    runtimeCapabilityCache = snapshot;
    return snapshot;
  }).finally(() => {
    runtimeCapabilityPromise = null;
  });
  return runtimeCapabilityPromise;
}

export async function getEnterpriseSession() {
  return window.ecorexDesktop?.getEnterpriseSession ? window.ecorexDesktop.getEnterpriseSession() : null;
}

export async function enterpriseLogin(email: string, password: string) {
  if (!window.ecorexDesktop?.enterpriseLogin) {
    throw new Error("企业登录桥接不可用");
  }
  return window.ecorexDesktop.enterpriseLogin({ email, password });
}

export async function enterpriseLogout() {
  return window.ecorexDesktop?.enterpriseLogout?.();
}

export async function enterpriseChangePassword(input: { oldPassword: string; newPassword: string }) {
  if (!window.ecorexDesktop?.enterpriseChangePassword) {
    throw new Error("企业密码桥接不可用");
  }
  return window.ecorexDesktop.enterpriseChangePassword(input);
}

export async function checkEnterpriseQuota(estimatedTokens: number): Promise<EnterpriseQuotaCheckResult> {
  if (!window.ecorexDesktop?.checkEnterpriseQuota) {
    return { ok: true, quota: { allowed: true } };
  }
  return window.ecorexDesktop.checkEnterpriseQuota(estimatedTokens) as Promise<EnterpriseQuotaCheckResult>;
}

export async function loadRuntimeSnapshot(options: RuntimeSnapshotOptions = {}): Promise<RuntimeSnapshot> {
  const sessionIncludes = runtimeSnapshotSessionIncludes(options);
  const sessionsParams = new URLSearchParams({ page: "1", page_size: "40" });
  if (sessionIncludes.includeIds.length) {
    sessionsParams.set("include_ids", sessionIncludes.includeIds.join(","));
  }
  if (sessionIncludes.pinnedIds.length) {
    sessionsParams.set("include_pinned", "1");
    sessionsParams.set("pinned_ids", sessionIncludes.pinnedIds.join(","));
  }
  try {
    const activeRequestsPromise = apiJson<{
      status?: string;
      requests?: RuntimeActiveRequest[];
      recentTerminalRequests?: RuntimeActiveRequest[];
      recent_terminal_requests?: RuntimeActiveRequest[];
      runStatusCounts?: Record<string, number>;
      run_status_counts?: Record<string, number>;
      staleLocks?: RuntimeSessionLock[];
      stale_locks?: RuntimeSessionLock[];
    }>("/api/active-requests")
      .catch(() => ({
        status: "unavailable",
        requests: [],
        recentTerminalRequests: [],
        recent_terminal_requests: [],
        runStatusCounts: {},
        run_status_counts: {},
        staleLocks: [],
        stale_locks: []
      }));
    const schedulerPromise = apiJson<RuntimeSchedulerProjection & { status?: string; message?: string }>("/api/scheduler")
      .catch(() => ({
        enabled: false,
        initialized: false,
        running: false,
        serviceStatus: "unavailable",
        tasks: [],
        taskCount: 0,
        counts: { total: 0, enabled: 0, disabled: 0, error: 0 }
      } satisfies RuntimeSchedulerProjection));
    const [version, sessions, activeRequests, capabilities, scheduler] = await Promise.all([
      apiJson<{ version?: string; releaseNotes?: RuntimeReleaseNotes; updateState?: RuntimeUpdateState }>("/api/version"),
      apiJson<{ sessions?: RuntimeSession[]; total?: number; message?: string }>(`/api/sessions?${sessionsParams.toString()}`),
      activeRequestsPromise,
      loadRuntimeCapabilities(),
      schedulerPromise
    ]);

    const runtimeSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
    const runtimeActiveRequests = Array.isArray(activeRequests.requests) ? activeRequests.requests : [];
    const runtimeRecentTerminalRequests = Array.isArray(activeRequests.recentTerminalRequests)
      ? activeRequests.recentTerminalRequests
      : Array.isArray(activeRequests.recent_terminal_requests)
        ? activeRequests.recent_terminal_requests
        : [];
    const runStatusCounts: Record<string, number> = activeRequests.runStatusCounts && typeof activeRequests.runStatusCounts === "object"
      ? activeRequests.runStatusCounts as Record<string, number>
      : activeRequests.run_status_counts && typeof activeRequests.run_status_counts === "object"
        ? activeRequests.run_status_counts as Record<string, number>
        : {};
    const staleLocks = Array.isArray(activeRequests.staleLocks)
      ? activeRequests.staleLocks
      : Array.isArray(activeRequests.stale_locks)
        ? activeRequests.stale_locks
        : [];
    return {
      status: "ready",
      message: "已连接本地 EcoreX 运行时",
      version: version.version,
      releaseNotes: version.releaseNotes,
      updateState: version.updateState,
      sessions: runtimeSessions,
      activeRequests: runtimeActiveRequests,
      recentTerminalRequests: runtimeRecentTerminalRequests,
      runStatusCounts,
      activeRequestsStatus: activeRequests.status || "success",
      staleLocks,
      totalSessions: typeof sessions.total === "number" ? sessions.total : runtimeSessions.length,
      toolsCount: capabilities.tools.length,
      skillsCount: capabilities.skills.length,
      extensions: capabilities.extensions,
      extensionsCount: capabilities.extensionsCount,
      extensionSummary: capabilities.extensionSummary,
      modelsCount: capabilities.modelsCount,
      currentProvider: capabilities.currentProvider,
      currentModel: capabilities.currentModel,
      tools: capabilities.tools,
      skills: capabilities.skills,
      modelCapabilities: capabilities.modelCapabilities,
      scheduler
    };
  } catch (error) {
    return {
      status: "offline",
      message: error instanceof Error ? error.message : "本地运行时暂不可用",
      sessions: [],
      activeRequests: [],
      activeRequestsStatus: "unavailable",
      staleLocks: [],
      totalSessions: 0,
      toolsCount: 0,
      skillsCount: 0,
      extensions: [],
      extensionsCount: 0,
      extensionSummary: {},
      modelsCount: 0,
      tools: [],
      skills: [],
      modelCapabilities: {},
      scheduler: {
        enabled: false,
        initialized: false,
        running: false,
        serviceStatus: "unavailable",
        tasks: [],
        taskCount: 0,
        counts: { total: 0, enabled: 0, disabled: 0, error: 0 }
      }
    };
  }
}

function pickString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function pickPositiveNumber(...values: unknown[]) {
  for (const value of values) {
    const parsed = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return Math.round(parsed);
  }
  return undefined;
}

function pickNonNegativeNumber(...values: unknown[]) {
  for (const value of values) {
    const parsed = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) return Math.round(parsed);
  }
  return undefined;
}

function normalizeChatContextPolicy(value: unknown): ChatContextPolicy | undefined {
  if (!value || typeof value !== "object") return undefined;
  const data = value as Record<string, unknown>;
  const policy: ChatContextPolicy = {
    contextWindowTokens: pickPositiveNumber(data.contextWindowTokens, data.context_window_tokens),
    maxOutputTokens: pickPositiveNumber(data.maxOutputTokens, data.max_output_tokens),
    autoCompactTokenLimit: pickPositiveNumber(data.autoCompactTokenLimit, data.auto_compact_token_limit),
    hardContextTokenLimit: pickPositiveNumber(data.hardContextTokenLimit, data.hard_context_token_limit),
    source: pickString(data.source),
    note: pickString(data.note),
    tokenizer: pickString(data.tokenizer),
    tokenizerStatus: pickString(data.tokenizerStatus) || pickString(data.tokenizer_status),
    tokenizerNote: pickString(data.tokenizerNote) || pickString(data.tokenizer_note)
  };
  return Object.values(policy).some((item) => item !== undefined && item !== "") ? policy : undefined;
}

function normalizeChatContextContinuity(value: unknown): ChatContextContinuity | undefined {
  if (!value || typeof value !== "object") return undefined;
  const data = value as Record<string, unknown>;
  const continuity: ChatContextContinuity = {
    agentBridgePreserved: typeof data.agentBridgePreserved === "boolean"
      ? data.agentBridgePreserved
      : (typeof data.agent_bridge_preserved === "boolean" ? data.agent_bridge_preserved : undefined),
    existingAgentRoutesReset: pickNonNegativeNumber(data.existingAgentRoutesReset, data.existing_agent_routes_reset),
    artifactHistoryRefs: pickString(data.artifactHistoryRefs) || pickString(data.artifact_history_refs),
    strategy: pickString(data.strategy)
  };
  return Object.values(continuity).some((item) => item !== undefined && item !== "") ? continuity : undefined;
}

function inferCurrentModel(models: { providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] }) {
  if (models.capabilities && !Array.isArray(models.capabilities) && typeof models.capabilities === "object") {
    const capabilities = models.capabilities as Record<string, unknown>;
    const chat = capabilities.chat;
    if (chat && typeof chat === "object") {
      const data = chat as Record<string, unknown>;
      const current = pickString(data.current_model) || pickString(data.model) || pickString(data.default_model);
      if (current) return current;
    }
  }
  const providers = Array.isArray(models.providers) ? models.providers : [];
  for (const provider of providers) {
    if (provider && typeof provider === "object") {
      const data = provider as Record<string, unknown>;
      const direct = pickString(data.current_model) || pickString(data.model) || pickString(data.default_model);
      if (direct) return direct;
      const nested = data.models;
      if (Array.isArray(nested) && nested.length > 0) {
        const first = nested[0];
        if (typeof first === "string") return first;
        if (first && typeof first === "object") {
          const modelData = first as Record<string, unknown>;
          const model = pickString(modelData.current_model) || pickString(modelData.model) || pickString(modelData.name) || pickString(modelData.id);
          if (model) return model;
        }
      }
    }
  }
  return "";
}

function normalizeChatModelOption(value: unknown): ChatModelOption | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  const provider = pickString(data.provider) || pickString(data.provider_id);
  const model = pickString(data.model) || pickString(data.value);
  if (!provider || !model) return null;
  return {
    provider,
    providerLabel: pickString(data.providerLabel) || pickString(data.provider_label),
    model,
    label: pickString(data.label) || model,
    hint: pickString(data.hint),
    configured: data.configured !== false,
    current: data.current === true,
    contextPolicy: normalizeChatContextPolicy(data.contextPolicy || data.context_policy),
    modelAliasFamily: pickString(data.modelAliasFamily) || pickString(data.model_alias_family),
    effectiveTransportProvider: pickString(data.effectiveTransportProvider) || pickString(data.effective_transport_provider),
    isOfficialGeminiProvider: data.isOfficialGeminiProvider === true || data.is_official_gemini_provider === true,
    officialGeminiApiUsed: data.officialGeminiApiUsed === true || data.official_gemini_api_used === true
  };
}

function enterpriseProviderLabel(provider: string) {
  const labels: Record<string, string> = {
    openai: "OpenAI",
    deepseek: "DeepSeek",
    gemini: "Google Gemini",
    doubao: "豆包",
    ark: "火山方舟",
    zhipu: "智谱",
    qianfan: "百度千帆",
    moonshot: "Moonshot",
    claudeAPI: "Claude",
    claude: "Claude"
  };
  return labels[provider] || provider;
}

function normalizeEnterpriseModelConfigOptions(payload: unknown): ChatModelsPayload | null {
  if (!payload || typeof payload !== "object") return null;
  const data = payload as Record<string, unknown>;
  if (data.configured === false) return null;
  const currentProvider = pickString(data.provider);
  const currentModel = pickString(data.model);
  const options: ChatModelOption[] = [];
  const seen = new Set<string>();
  function add(provider: string, model: string, name?: string) {
    if (!provider || !model) return;
    const key = `${provider}:${model}`;
    if (seen.has(key)) return;
    seen.add(key);
    options.push({
      provider,
      providerLabel: enterpriseProviderLabel(provider),
      model,
      label: name || model,
      configured: true,
      current: provider === currentProvider && model === currentModel
    });
  }
  const credentials = Array.isArray(data.modelCredentials) ? data.modelCredentials : [];
  for (const item of credentials) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (row.enabled === false) continue;
    add(pickString(row.provider), pickString(row.model), pickString(row.name));
  }
  add(currentProvider, currentModel, pickString(data.name));
  if (!options.length || !currentModel) return null;
  return {
    status: "success",
    currentProvider,
    currentModel,
    options
  };
}

async function loadEnterpriseChatModelOptionsFallback(): Promise<ChatModelsPayload | null> {
  if (!window.ecorexDesktop?.getEnterpriseModelConfig) return null;
  try {
    const payload = await window.ecorexDesktop.getEnterpriseModelConfig();
    return normalizeEnterpriseModelConfigOptions(payload);
  } catch {
    return null;
  }
}

export async function loadChatModelOptions(): Promise<ChatModelsPayload> {
  let payload: { status?: string; providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] };
  try {
    payload = await apiJson<{ status?: string; providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] }>("/api/models");
  } catch (error) {
    const fallback = await loadEnterpriseChatModelOptionsFallback();
    if (fallback) return fallback;
    throw error;
  }
  const capabilities = payload.capabilities && !Array.isArray(payload.capabilities) && typeof payload.capabilities === "object"
    ? payload.capabilities as Record<string, unknown>
    : {};
  const chat = capabilities.chat && typeof capabilities.chat === "object"
    ? capabilities.chat as Record<string, unknown>
    : {};
  const options = Array.isArray(chat.model_options)
    ? chat.model_options.map(normalizeChatModelOption).filter(Boolean) as ChatModelOption[]
    : [];
  return {
    status: payload.status,
    currentProvider: pickString(chat.current_provider),
    currentModel: pickString(chat.current_model) || inferCurrentModel(payload),
    currentContextPolicy: normalizeChatContextPolicy(chat.context_policy || chat.contextPolicy),
    options
  };
}

export async function setChatModel(provider: string, model: string): Promise<SetChatModelResult> {
  const result = await apiJson<SetChatModelResult>("/api/models", "POST", {
    action: "set_capability",
    capability: "chat",
    provider_id: provider,
    model
  });
  invalidateRuntimeCapabilities();
  result.contextPolicy = normalizeChatContextPolicy(result.context_policy || result.contextPolicy);
  result.contextContinuity = normalizeChatContextContinuity(result.context_continuity || result.contextContinuity);
  return result;
}

export async function sendChatMessage(input: {
  sessionId: string;
  message: string;
  visibleMessage?: string;
  hiddenContext?: string;
  projectContext?: ProjectSessionBinding | null;
  attachments?: FileAttachment[];
  lang?: string;
  internalAction?: boolean;
  clientAttemptId?: string;
  interruptsRequestId?: string;
  retryOfRequestId?: string;
}): Promise<ChatSendResult> {
  if (!window.ecorexDesktop?.apiJson) {
    return {
      status: "error",
      message: "本地运行时桥接不可用"
    };
  }

  const result = await window.ecorexDesktop.apiJson({
    path: "/message",
    method: "POST",
    body: {
      session_id: input.sessionId,
      message: input.message,
      visible_message: input.visibleMessage ?? input.message,
      hidden_context: input.hiddenContext || "",
      project_context_meta: input.projectContext || null,
      internal_action: Boolean(input.internalAction),
      stream: true,
      timestamp: new Date().toISOString(),
      attachments: input.attachments || [],
      lang: input.lang || "zh",
      client_attempt_id: input.clientAttemptId || "",
      interrupts_request_id: input.interruptsRequestId || "",
      retry_of_request_id: input.retryOfRequestId || ""
    }
  });

  return (result || {}) as ChatSendResult;
}

export async function prepareRequestRetry(input: { requestId: string; sessionId?: string }) {
  const requestId = String(input.requestId || "").trim();
  if (!requestId) {
    return { status: "error", message: "missing request_id", retryable: false, recoverable: false } as RetryPrepareResult;
  }
  return apiJson<RetryPrepareResult>(
    `/api/requests/${encodeURIComponent(requestId)}/retry-prepare`,
    "POST",
    { session_id: input.sessionId || "" }
  );
}

export async function cancelChatRequest(input: { requestId?: string; sessionId?: string }) {
  return apiJson<{ status?: string; cancelled?: number }>("/cancel", "POST", {
    request_id: input.requestId,
    session_id: input.sessionId,
    lang: "zh"
  });
}

export async function cancelSubagentTask(taskId: string) {
  return apiJson<{ status?: string; cancelled?: number; task?: unknown }>(
    `/api/subagents/${encodeURIComponent(taskId)}/cancel`,
    "POST"
  );
}

export async function decideToolPermission(input: {
  requestId: string;
  decision: ToolPermissionDecision;
  remember?: boolean;
}) {
  return apiJson<{ status?: string; allowed?: boolean; message?: string }>("/api/tool-permissions", "POST", {
    request_id: input.requestId,
    decision: input.decision,
    remember: input.remember
  });
}

export async function loadSessionHistoryWithMeta(sessionId: string): Promise<RuntimeHistoryResult> {
  if (!sessionId) {
    return { messages: [], contextStartSeq: 0 };
  }
  const result = await apiJson<{
    messages?: RuntimeMessage[];
    context_start_seq?: number;
    project_context?: ProjectSessionBinding | null;
    total?: number;
    page?: number;
    page_size?: number;
    has_more?: boolean;
  }>(
    `/api/history?session_id=${encodeURIComponent(sessionId)}&page=1&page_size=50`
  );
  return {
    messages: Array.isArray(result.messages) ? result.messages : [],
    contextStartSeq: typeof result.context_start_seq === "number" ? result.context_start_seq : 0,
    projectContext: result.project_context || null,
    total: result.total,
    page: result.page,
    pageSize: result.page_size,
    hasMore: result.has_more
  };
}

export async function loadSessionHistory(sessionId: string): Promise<RuntimeMessage[]> {
  return (await loadSessionHistoryWithMeta(sessionId)).messages;
}

export async function loadRuntimeProjection(input: RuntimeProjectionInput): Promise<RuntimeProjectionResult> {
  const params = new URLSearchParams();
  if (input.mode === "request") {
    params.set("request_id", input.requestId);
    if (input.sessionId) params.set("session_id", input.sessionId);
    if (typeof input.limit === "number") params.set("limit", String(Math.max(1, input.limit)));
  } else {
    params.set("session_id", input.sessionId);
    if (typeof input.afterEventId === "number") params.set("after_event_id", String(Math.max(0, input.afterEventId)));
    if (typeof input.limit === "number") params.set("limit", String(Math.max(1, input.limit)));
  }
  const result = await apiJson<{
    mode?: "request" | "session";
    latest_event_id?: number;
    projection?: RuntimeRequestProjection | RuntimeSessionProjection;
  }>(`/api/runtime-projection?${params.toString()}`);
  const latestEventId = typeof result.latest_event_id === "number" ? result.latest_event_id : 0;
  if (input.mode === "session") {
    return {
      mode: "session",
      latestEventId,
      projection: (result.projection || {}) as RuntimeSessionProjection
    };
  }
  return {
    mode: "request",
    latestEventId,
    projection: (result.projection || {}) as RuntimeRequestProjection
  };
}

export async function generateSessionTitle(input: { sessionId: string; userMessage: string; assistantReply?: string }) {
  if (!input.sessionId || !input.userMessage) {
    return "";
  }
  const result = await apiJson<{ status?: string; title?: string }>(
    `/api/sessions/${encodeURIComponent(input.sessionId)}/generate_title`,
    "POST",
    { user_message: input.userMessage, assistant_reply: input.assistantReply || "" }
  );
  return result.title || "";
}

export async function renameRuntimeSession(input: { sessionId: string; title: string }) {
  const title = input.title.trim();
  if (!input.sessionId || !title) {
    throw new Error("会话名称不能为空");
  }
  return apiJson<{ status?: string; message?: string }>(`/api/sessions/${encodeURIComponent(input.sessionId)}`, "PUT", { title });
}

export async function deleteRuntimeSession(sessionId: string) {
  if (!sessionId) {
    throw new Error("会话不存在");
  }
  return apiJson<{ status?: string; message?: string }>(`/api/sessions/${encodeURIComponent(sessionId)}`, "DELETE", {});
}

export async function deleteMessagePair(input: { sessionId: string; userSeq: number }) {
  return apiJson<{ status?: string; deleted?: number }>("/api/messages/delete", "POST", {
    session_id: input.sessionId,
    user_seq: input.userSeq,
    delete_user: true,
    cascade: false
  });
}

export async function clearRuntimeContext(sessionId: string) {
  if (!sessionId) {
    throw new Error("session_id required");
  }
  const result = await apiJson<{ status?: string; context_start_seq?: number; message?: string }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/clear_context`,
    "POST",
    {}
  );
  if (result.status && result.status !== "success") {
    throw new Error(result.message || "clear context failed");
  }
  return typeof result.context_start_seq === "number" ? result.context_start_seq : 0;
}

export async function chooseLocalFiles(webPort = 9899): Promise<FileAttachment[]> {
  if (!window.ecorexDesktop?.chooseFiles) {
    return [];
  }
  const files = await window.ecorexDesktop.chooseFiles();
  return files.map((file) => ({
    ...file,
    previewDataUrl: file.file_type === "image" ? filePreviewUrl(file.file_path, webPort) : undefined
  }));
}

export async function chooseProjectFolder(): Promise<ProjectFolder | null> {
  if (!window.ecorexDesktop?.chooseProjectFolder) {
    return null;
  }
  return window.ecorexDesktop.chooseProjectFolder();
}

export async function registerProjectFolderPath(projectPath: string): Promise<ProjectFolder | null> {
  const trimmedPath = String(projectPath || "").trim();
  if (!trimmedPath) return null;
  const result = await apiJson<{ status?: string; project?: ProjectFolder }>("/api/project-folder", "POST", { path: trimmedPath });
  return result.project || null;
}

export async function savePastedFile(file: File): Promise<FileAttachment | null> {
  if (!window.ecorexDesktop?.savePastedFile) {
    return null;
  }
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  const dataBase64 = window.btoa(binary);
  const attachment: FileAttachment = await window.ecorexDesktop.savePastedFile({
    fileName: file.name || `paste-${Date.now()}`,
    mimeType: file.type,
    dataBase64
  });
  if (file.type.startsWith("image/")) {
    attachment.previewDataUrl = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => resolve("");
      reader.readAsDataURL(file);
    });
  }
  return attachment;
}

export async function openLocalPath(filePath: string, action: OpenPathAction = "open") {
  const trimmedPath = String(filePath || "").trim();
  if (!trimmedPath) {
    return "path is required";
  }
  if (window.ecorexDesktop?.openPath) {
    return window.ecorexDesktop.openPath(trimmedPath, action);
  }
  return openRuntimePath(trimmedPath, action);
}

export async function openRuntimePath(filePath: string, action: OpenPathAction = "open") {
  const result = await apiJson<{ status?: string; message?: string }>("/api/open-path", "POST", { path: filePath, action });
  return result.message || "";
}

export async function statLocalPath(filePath: string): Promise<LocalPathStat> {
  const trimmedPath = String(filePath || "").trim();
  if (!trimmedPath) {
    return { path: "", exists: false, status: "error", message: "path is required" };
  }
  if (window.ecorexDesktop?.statPath) {
    return window.ecorexDesktop.statPath(trimmedPath);
  }
  return apiJson<LocalPathStat>("/api/file-stat", "POST", { path: trimmedPath });
}

export async function readLocalJson(filePath: string): Promise<LocalJsonResult> {
  const trimmedPath = String(filePath || "").trim();
  if (!trimmedPath) {
    return { status: "error", path: "", message: "path is required" };
  }
  return apiJson<LocalJsonResult>("/api/file-json", "POST", { path: trimmedPath });
}

export async function loadPermissionState(): Promise<PermissionState | null> {
  const normalize = async (state: PermissionState | null): Promise<PermissionState | null> => {
    if (!state) return null;
    if (state.mode === "full-access" || state.mode === "smart-ask") return state;
    try {
      const migrated = await updatePermissionMode("smart-ask");
      return migrated || { ...state, mode: "smart-ask" };
    } catch {
      return { ...state, mode: "smart-ask" };
    }
  };
  if (window.ecorexDesktop?.getPermissionState) {
    return normalize(await window.ecorexDesktop.getPermissionState());
  }
  return normalize(await apiJson<PermissionState & { status?: string }>("/api/tool-permissions"));
}

export async function saveRuntimeUiState(state: unknown) {
  return apiJson<{ status?: string; message?: string }>("/api/ui-state", "POST", state);
}

export async function loadRuntimeUiState(): Promise<RuntimeUiStateSync | null> {
  const result = await apiJson<{ status?: string; state?: RuntimeUiStateSync }>("/api/ui-state", "GET");
  return result.state || null;
}

export async function requestAgentInstallRequest(input: {
  packId: string;
  packName?: string;
  sessionId?: string;
}) {
  return apiJson<{
    status?: string;
    message?: string;
    prompt?: string;
    packId?: string;
    packName?: string;
    sessionId?: string;
    discoveryOnly?: boolean;
    sourceUrl?: string;
    mirrorUrls?: string[];
    installHint?: string;
  }>("/api/agent-install-request", "POST", input);
}

export async function updatePermissionMode(mode: PermissionMode): Promise<PermissionState | null> {
  if (window.ecorexDesktop?.setPermissionMode) {
    return window.ecorexDesktop.setPermissionMode(mode);
  }
  return apiJson<PermissionState & { status?: string }>("/api/tool-permissions", "POST", { action: "set_mode", mode });
}

export async function resetPermissionGrants(): Promise<PermissionState | null> {
  if (window.ecorexDesktop?.resetPermissionGrants) {
    return window.ecorexDesktop.resetPermissionGrants();
  }
  return apiJson<PermissionState & { status?: string }>("/api/tool-permissions", "POST", { action: "reset_grants" });
}

export async function listCapabilityPacks(): Promise<CapabilityPack[]> {
  let bridgePacks: CapabilityPack[] = [];
  try {
    const payload = await apiJson<Record<string, unknown>>("/api/capabilities");
    const runtimePacks = capabilityPacksFromRuntime(payload);
    if (runtimePacks.length) {
      return runtimePacks;
    }
  } catch {
    // Fall back to the Electron bridge while the runtime is still starting.
  }
  if (window.ecorexDesktop?.listCapabilityPacks) {
    try {
      bridgePacks = await window.ecorexDesktop.listCapabilityPacks();
    } catch {
      bridgePacks = [];
    }
  }
  if (bridgePacks.length) {
    return bridgePacks;
  }
  return bridgePacks;
}

function capabilityPacksFromRuntime(payload: Record<string, unknown>): CapabilityPack[] {
  const abilitiesPayload = payload.abilities;
  const resultPayload = payload.result as Record<string, unknown> | undefined;
  const resultAbilitiesPayload = resultPayload?.abilities;
  const rawAbilities = Array.isArray(abilitiesPayload)
    ? abilitiesPayload
    : abilitiesPayload && typeof abilitiesPayload === "object" && Array.isArray((abilitiesPayload as Record<string, unknown>).abilities)
      ? ((abilitiesPayload as Record<string, unknown>).abilities as unknown[])
      : Array.isArray(resultAbilitiesPayload)
        ? resultAbilitiesPayload
        : resultAbilitiesPayload && typeof resultAbilitiesPayload === "object" && Array.isArray((resultAbilitiesPayload as Record<string, unknown>).abilities)
          ? ((resultAbilitiesPayload as Record<string, unknown>).abilities as unknown[])
          : [];
  return rawAbilities
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
    .filter((item) => Boolean(item.agentCanInstall || item.packId || item.kind === "capability-pack"))
    .map((item) => {
      const state = item.capabilityState && typeof item.capabilityState === "object"
        ? item.capabilityState as Record<string, unknown>
        : {};
      const id = String(item.packId || item.id || "");
      const installed = Boolean(state.installed);
      const rawState = String(state.state || (installed ? "installed" : "not-installed"));
      const capabilityState = isCapabilityState(rawState) ? rawState : "unknown";
      const mirrorUrls = Array.isArray(item.mirrorUrls)
        ? item.mirrorUrls.filter((url): url is string => typeof url === "string")
        : Array.isArray(state.mirrorUrls)
          ? state.mirrorUrls.filter((url): url is string => typeof url === "string")
          : undefined;
      const allowedCommands = Array.isArray(item.allowedCommands)
        ? item.allowedCommands.filter((command): command is string => typeof command === "string")
        : Array.isArray(state.allowedCommands)
          ? state.allowedCommands.filter((command): command is string => typeof command === "string")
          : undefined;
      return {
        id,
        name: String(item.label || id),
        summary: String(item.notes || item.defaultPolicy || ""),
        installMode: "user-or-admin",
        defaultEnabled: item.defaultEnabled === true || state.defaultEnabled === true,
        readOnly: item.readOnly === true || state.readOnly === true,
        configureOnly: item.configureOnly === true || state.configureOnly === true,
        discoveryOnly: item.discoveryOnly === true || state.discoveryOnly === true,
        sourceUrl: typeof item.sourceUrl === "string"
          ? item.sourceUrl
          : typeof state.sourceUrl === "string"
            ? state.sourceUrl
            : undefined,
        mirrorUrls,
        installHint: typeof item.installHint === "string"
          ? item.installHint
          : typeof state.installHint === "string"
            ? state.installHint
            : undefined,
        allowedCommands,
        state: capabilityState,
        message: String(state.message || (installed ? "能力包已安装" : "点击安装后由当前会话 agent 处理")),
        installed,
        logPath: typeof state.logPath === "string" ? state.logPath : undefined,
        updatedAt: typeof state.updatedAt === "string" ? state.updatedAt : undefined,
        policyMode: item.policyMode === "disabled" || item.policyMode === "preinstall" || item.policyMode === "ask"
          ? item.policyMode
          : "ask",
        installAllowed: item.installAllowed !== false,
        disabledReason: typeof item.disabledReason === "string" ? item.disabledReason : undefined,
        policyStatus: typeof item.policyStatus === "string" ? item.policyStatus : undefined,
        policyUpdatedAt: typeof item.policyUpdatedAt === "string" ? item.policyUpdatedAt : undefined,
        policySource: typeof item.policySource === "string" ? item.policySource : undefined
      } satisfies CapabilityPack;
    })
    .filter((pack) => Boolean(pack.id));
}

function isCapabilityState(value: string): value is CapabilityState {
  return ["installed", "not-installed", "checking", "installing", "busy", "failed", "unknown"].includes(value);
}

export async function setSkillEnabled(name: string, enabled: boolean) {
  invalidateRuntimeCapabilities();
  const result = await apiJson<{ status?: string }>("/api/skills", "POST", {
    action: enabled ? "open" : "close",
    name
  });
  invalidateRuntimeCapabilities();
  return result;
}

export async function loadSchedulerProjection(): Promise<RuntimeSchedulerProjection> {
  const result = await apiJson<RuntimeSchedulerProjection & { status?: string }>("/api/scheduler");
  return result;
}

export async function updateScheduler(input: Record<string, unknown>): Promise<RuntimeSchedulerProjection & { status?: string; message?: string }> {
  return apiJson<RuntimeSchedulerProjection & { status?: string; message?: string }>("/api/scheduler", "POST", input);
}

export async function loadExternalConnections(): Promise<ExternalConnectionsPayload> {
  return apiJson<ExternalConnectionsPayload>("/api/external-connections");
}

export async function updateExternalConnection(platform: string, input: Record<string, unknown>): Promise<ExternalConnectionActionResponse> {
  const id = String(platform || "").trim();
  if (!id) {
    throw new Error("external connection platform is required");
  }
  invalidateRuntimeCapabilities();
  const result = await apiJson<ExternalConnectionActionResponse>(
    `/api/external-connections/${encodeURIComponent(id)}/actions`,
    "POST",
    input
  );
  invalidateRuntimeCapabilities();
  return result;
}

export async function enableDefaultSkills(skills: RuntimeSkill[]) {
  const disabledBuiltIns = skills.filter((skill) => {
    if (!skill.name || skill.enabled !== false) return false;
    return skill.source_group === "builtin" || skill.sourceGroup === "builtin" || skill.source === "builtin";
  });
  await Promise.all(disabledBuiltIns.map((skill) => setSkillEnabled(skill.name || "", true).catch(() => undefined)));
  return disabledBuiltIns.length;
}

export async function loadMemoryFiles(category = "memory"): Promise<MemoryFile[]> {
  try {
    const result = await apiJson<{ files?: MemoryFile[]; items?: MemoryFile[]; list?: MemoryFile[] }>(
      `/api/memory?page=1&page_size=12&category=${encodeURIComponent(category)}`
    );
    if (Array.isArray(result.files)) return result.files;
    if (Array.isArray(result.items)) return result.items;
    if (Array.isArray(result.list)) return result.list;
  } catch {
    // Memory listing is best-effort for the desktop settings panel.
  }
  return [];
}

export async function reportDesktopEvent(event: {
  type: "usage" | "error" | "warn" | "info";
  source?: string;
  message?: string;
  category?: string;
  label?: string;
  amount?: number;
  sessionId?: string;
  tool?: string;
  detail?: Record<string, unknown>;
}) {
  if (!window.ecorexDesktop?.reportTelemetry) {
    return;
  }
  try {
    await window.ecorexDesktop.reportTelemetry(event);
  } catch {
    // Telemetry is best-effort and must not affect the user task.
  }
}

export async function exportDiagnosticsBundle(input: { sessionId?: string; requestId?: string } = {}) {
  const params = new URLSearchParams();
  if (input.sessionId) params.set("session_id", input.sessionId);
  if (input.requestId) params.set("request_id", input.requestId);
  const suffix = params.toString();
  return apiJson<DiagnosticsBundle>(`/api/diagnostics/bundle${suffix ? `?${suffix}` : ""}`);
}

export function filePreviewUrl(filePath: string, webPort: number) {
  if (/^https?:\/\//i.test(filePath)) return filePath;
  if (/^\/(?:uploads|static|app)(?:\/|$)|^\/api\/file(?:[/?#]|$)/.test(filePath)) {
    return `http://127.0.0.1:${webPort}${filePath}`;
  }
  return `http://127.0.0.1:${webPort}/api/file?path=${encodeURIComponent(filePath)}`;
}

const streamLastEventIds = new Map<string, string>();
const streamCursorCleanupTimers = new Map<string, number>();

function rememberStreamCursor(requestId: string, eventId: string) {
  if (!requestId || !eventId) return;
  streamLastEventIds.set(requestId, eventId);
  const cleanup = streamCursorCleanupTimers.get(requestId);
  if (cleanup) window.clearTimeout(cleanup);
}

function scheduleStreamCursorCleanup(requestId: string, delayMs = 120_000) {
  if (!requestId) return;
  const cleanup = streamCursorCleanupTimers.get(requestId);
  if (cleanup) window.clearTimeout(cleanup);
  streamCursorCleanupTimers.set(requestId, window.setTimeout(() => {
    streamLastEventIds.delete(requestId);
    streamCursorCleanupTimers.delete(requestId);
  }, delayMs));
}

export function hasMessageStreamCursor(requestId: string) {
  return Boolean(requestId && streamLastEventIds.has(requestId));
}

function isTerminalVoiceStreamItem(item: StreamItem) {
  if (item.type !== "voice_attach") return false;
  const record = item as StreamItem & { terminal?: unknown; final?: unknown; done?: unknown };
  return record.terminal === true
    || record.final === true
    || record.done === true
    || item.status === "done"
    || item.status === "completed";
}

export function openMessageStream(input: {
  requestId: string;
  sessionId?: string;
  webPort: number;
  onItem: (item: StreamItem) => void;
  onError: () => void;
}) {
  const params = new URLSearchParams({ request_id: input.requestId });
  if (input.sessionId) params.set("session_id", input.sessionId);
  const lastEventId = streamLastEventIds.get(input.requestId);
  if (lastEventId) params.set("last_event_id", lastEventId);
  // Electron injects X-EcoreX-Runtime-Token for loopback EventSource
  // requests. Keeping the runtime token out of the URL avoids leaking it via
  // request logs, devtools, or copied stream URLs.
  const url = `http://127.0.0.1:${input.webPort}/stream?${params.toString()}`;
  const events = new EventSource(url);
  let lastEventAt = Date.now();
  let firstTransientErrorAt = 0;
  let terminal = false;
  const STREAM_TRANSIENT_ERROR_GRACE_MS = 75_000;
  events.onopen = () => {
    lastEventAt = Date.now();
    firstTransientErrorAt = 0;
  };
  events.onmessage = (event) => {
    try {
      lastEventAt = Date.now();
      firstTransientErrorAt = 0;
      rememberStreamCursor(input.requestId, event.lastEventId);
      const item = JSON.parse(event.data) as StreamItem;
      if (item.type === "done" || item.type === "error" || item.type === "cancelled" || item.type === "interrupted" || item.type === "replay_gap" || isTerminalVoiceStreamItem(item)) {
        terminal = true;
        scheduleStreamCursorCleanup(input.requestId);
      }
      input.onItem(item);
    } catch {
      input.onItem({ type: "error", message: "无法解析运行时返回" });
    }
  };
  events.onerror = () => {
    const now = Date.now();
    if (!terminal) {
      firstTransientErrorAt = firstTransientErrorAt || now;
      if (
        events.readyState !== EventSource.CLOSED
        && now - firstTransientErrorAt < STREAM_TRANSIENT_ERROR_GRACE_MS
      ) {
        return;
      }
    }
    events.close();
    input.onError();
  };
  return () => events.close();
}

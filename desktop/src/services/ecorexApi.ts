export type RuntimeSession = {
  session_id?: string;
  id?: string;
  title?: string;
  created_at?: string | number;
  last_active?: string | number;
  updatedAt?: string | number;
  msg_count?: number;
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
  created_at?: number;
  cancelled_at?: number | null;
  updated_at?: number;
  terminal_at?: number;
  age_seconds?: number;
  cancel_age_seconds?: number | null;
  stream_available?: boolean;
  metadata?: Record<string, unknown>;
};

export type RuntimeSessionLock = {
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
  path?: string;
  enabled?: boolean;
  category?: string;
  user_invocable?: boolean;
  disable_model_invocation?: boolean;
  mentionable?: boolean;
  mention_category?: string;
  mention_hidden_reason?: string;
  primary_env?: string;
};

export type RuntimeExtension = {
  id: string;
  type: "builtin_skill" | "user_skill" | "connector" | "mcp_server" | "capability_pack" | "plugin" | string;
  displayName?: string;
  description?: string;
  origin?: string;
  sourceUrl?: string;
  sourcePath?: string;
  version?: string;
  enabled?: boolean;
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
};

export type RuntimeToolCall = {
  id?: string;
  name?: string;
  tool?: string;
  arguments?: unknown;
  input?: unknown;
  result?: unknown;
  status?: string;
  is_error?: boolean;
  execution_time?: number;
  function?: {
    name?: string;
    arguments?: unknown;
  };
};

export type RuntimeReleaseNotes = {
  version: string;
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
  status?: string;
  is_error?: boolean;
  execution_time?: number;
  has_tool_calls?: boolean;
  file_name?: string;
  file_type?: string;
  url?: string;
  path?: string;
};

export type RuntimeMessage = {
  role?: "user" | "assistant";
  content?: string;
  created_at?: number;
  seq?: number;
  _seq?: number;
  user_seq?: number;
  reasoning?: string;
  steps?: RuntimeStep[];
  tool_calls?: RuntimeToolCall[];
  kind?: string;
  request_id?: string;
  extras?: {
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
  total?: number;
  page?: number;
  pageSize?: number;
  hasMore?: boolean;
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
  currentModel?: string;
  sessions: RuntimeSession[];
  activeRequests?: RuntimeActiveRequest[];
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

export type DesktopUpdateStatus = {
  state: "idle" | "checking" | "available" | "not-available" | "downloading" | "downloaded" | "blocked" | "installing" | "error";
  platform: string;
  currentVersion: string;
  version?: string;
  message: string;
  downloadUrl?: string;
  releaseDate?: string;
  progress?: number;
  activeRequests?: number;
  checkedAt?: string;
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
  discoveryOnly?: boolean;
  sourceUrl?: string;
  mirrorUrls?: string[];
  installHint?: string;
  estimatedSizeMb?: number;
  state: CapabilityState;
  message: string;
  installed: boolean;
  logPath?: string;
  missingModules?: string[];
  updatedAt?: string;
  policyMode?: "ask" | "preinstall" | "disabled";
  policyStatus?: string;
  policyUpdatedAt?: string;
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
  request_id?: string;
  stream?: boolean;
  inline_reply?: string;
  usage?: TokenUsage;
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
  recoverable?: boolean;
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
  status?: string;
  execution_time?: number;
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

function delay(ms: number) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
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

export async function loadRuntimeSnapshot(): Promise<RuntimeSnapshot> {
  try {
    const activeRequestsPromise = apiJson<{
      status?: string;
      requests?: RuntimeActiveRequest[];
      staleLocks?: RuntimeSessionLock[];
      stale_locks?: RuntimeSessionLock[];
    }>("/api/active-requests")
      .catch(() => ({ status: "unavailable", requests: [], staleLocks: [], stale_locks: [] }));
    const extensionsPromise = apiJson<{
      extensions?: RuntimeExtension[];
      count?: number;
      summary?: Record<string, number>;
    }>("/api/extensions").catch(() => ({ extensions: [], count: 0, summary: {} }));
    const skillsPromise = apiJson<{ skills?: RuntimeSkill[] }>("/api/skills").catch(() => ({ skills: [] }));
    const [version, sessions, tools, skills, models, activeRequests, extensions] = await Promise.all([
      apiJson<{ version?: string; releaseNotes?: RuntimeReleaseNotes }>("/api/version"),
      apiJson<{ sessions?: RuntimeSession[]; total?: number; message?: string }>("/api/sessions?page=1&page_size=40"),
      apiJson<{ tools?: RuntimeTool[] }>("/api/tools"),
      skillsPromise,
      apiJson<{ providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] }>("/api/models"),
      activeRequestsPromise,
      extensionsPromise
    ]);

    const runtimeSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
    const runtimeTools = Array.isArray(tools.tools) ? tools.tools : [];
    const runtimeSkills = Array.isArray(skills.skills) ? skills.skills : [];
    const runtimeExtensions = Array.isArray(extensions.extensions) ? extensions.extensions : [];
    const runtimeActiveRequests = Array.isArray(activeRequests.requests) ? activeRequests.requests : [];
    const staleLocks = Array.isArray(activeRequests.staleLocks)
      ? activeRequests.staleLocks
      : Array.isArray(activeRequests.stale_locks)
        ? activeRequests.stale_locks
        : [];
    const capabilityCount = Array.isArray(models.capabilities)
      ? models.capabilities.length
      : models.capabilities && typeof models.capabilities === "object"
        ? Object.keys(models.capabilities).length
        : 0;
    return {
      status: "ready",
      message: "已连接本地 EcoreX 运行时",
      version: version.version,
      releaseNotes: version.releaseNotes,
      sessions: runtimeSessions,
      activeRequests: runtimeActiveRequests,
      activeRequestsStatus: activeRequests.status || "success",
      staleLocks,
      totalSessions: typeof sessions.total === "number" ? sessions.total : runtimeSessions.length,
      toolsCount: runtimeTools.length,
      skillsCount: runtimeSkills.length,
      extensions: runtimeExtensions,
      extensionsCount: typeof extensions.count === "number" ? extensions.count : runtimeExtensions.length,
      extensionSummary: extensions.summary || {},
      modelsCount: countArray(models.providers) || capabilityCount,
      currentModel: inferCurrentModel(models),
      tools: runtimeTools,
      skills: runtimeSkills,
      modelCapabilities: models.capabilities && typeof models.capabilities === "object" ? models.capabilities as Record<string, unknown> : {}
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
      modelCapabilities: {}
    };
  }
}

function pickString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
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

export async function sendChatMessage(input: {
  sessionId: string;
  message: string;
  visibleMessage?: string;
  hiddenContext?: string;
  attachments?: FileAttachment[];
  lang?: string;
  internalAction?: boolean;
}): Promise<ChatSendResult> {
  if (!window.ecorexDesktop?.apiJson) {
    return {
      status: "error",
      message: "本地运行时桥接不可用"
    };
  }

  if (window.ecorexDesktop.refreshEnterprisePolicy) {
    try {
      const refresh = await window.ecorexDesktop.refreshEnterprisePolicy();
      if (refresh.restarted) {
        await delay(1200);
      }
    } catch {
      // Enterprise model policy refresh is best-effort; sidecar errors are handled below.
    }
  }

  const result = await window.ecorexDesktop.apiJson({
    path: "/message",
    method: "POST",
    body: {
      session_id: input.sessionId,
      message: input.message,
      visible_message: input.visibleMessage ?? input.message,
      hidden_context: input.hiddenContext || "",
      internal_action: Boolean(input.internalAction),
      stream: true,
      timestamp: new Date().toISOString(),
      attachments: input.attachments || [],
      lang: input.lang || "zh"
    }
  });

  return (result || {}) as ChatSendResult;
}

export async function cancelChatRequest(input: { requestId?: string; sessionId?: string }) {
  return apiJson<{ status?: string; cancelled?: number }>("/cancel", "POST", {
    request_id: input.requestId,
    session_id: input.sessionId,
    lang: "zh"
  });
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
    total: result.total,
    page: result.page,
    pageSize: result.page_size,
    hasMore: result.has_more
  };
}

export async function loadSessionHistory(sessionId: string): Promise<RuntimeMessage[]> {
  return (await loadSessionHistoryWithMeta(sessionId)).messages;
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
  if (window.ecorexDesktop?.getPermissionState) {
    return window.ecorexDesktop.getPermissionState();
  }
  return apiJson<PermissionState & { status?: string }>("/api/tool-permissions");
}

export async function checkForUpdates(): Promise<DesktopUpdateStatus | null> {
  if (!window.ecorexDesktop?.checkForUpdates) return null;
  return window.ecorexDesktop.checkForUpdates() as Promise<DesktopUpdateStatus>;
}

export async function getUpdateStatus(): Promise<DesktopUpdateStatus | null> {
  if (!window.ecorexDesktop?.getUpdateStatus) return null;
  return window.ecorexDesktop.getUpdateStatus() as Promise<DesktopUpdateStatus>;
}

export async function installDownloadedUpdate(): Promise<DesktopUpdateStatus | null> {
  if (!window.ecorexDesktop?.installDownloadedUpdate) return null;
  return window.ecorexDesktop.installDownloadedUpdate() as Promise<DesktopUpdateStatus>;
}

export async function openDownloadPage() {
  return window.ecorexDesktop?.openDownloadPage?.();
}

export async function saveRuntimeUiState(state: unknown) {
  return apiJson<{ status?: string; message?: string }>("/api/ui-state", "POST", state);
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
  const rawAbilities = Array.isArray(payload.abilities)
    ? payload.abilities
    : Array.isArray((payload.result as Record<string, unknown> | undefined)?.abilities)
      ? ((payload.result as Record<string, unknown>).abilities as unknown[])
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
      return {
        id,
        name: String(item.label || id),
        summary: String(item.notes || item.defaultPolicy || ""),
        installMode: "user-or-admin",
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
        state: capabilityState,
        message: String(state.message || (installed ? "能力包已安装" : "点击安装后由当前会话 agent 处理")),
        installed,
        logPath: typeof state.logPath === "string" ? state.logPath : undefined,
        updatedAt: typeof state.updatedAt === "string" ? state.updatedAt : undefined,
        policyMode: "ask"
      } satisfies CapabilityPack;
    })
    .filter((pack) => Boolean(pack.id));
}

function isCapabilityState(value: string): value is CapabilityState {
  return ["installed", "not-installed", "checking", "installing", "busy", "failed", "unknown"].includes(value);
}

export async function setSkillEnabled(name: string, enabled: boolean) {
  return apiJson<{ status?: string }>("/api/skills", "POST", {
    action: enabled ? "open" : "close",
    name
  });
}

export async function enableDefaultSkills(skills: RuntimeSkill[]) {
  const disabledBuiltIns = skills.filter((skill) => {
    if (!skill.name || skill.enabled !== false) return false;
    return skill.source === "builtin" || skill.source === "custom";
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

export function openMessageStream(input: {
  requestId: string;
  webPort: number;
  onItem: (item: StreamItem) => void;
  onError: () => void;
}) {
  const params = new URLSearchParams({ request_id: input.requestId });
  const lastEventId = streamLastEventIds.get(input.requestId);
  if (lastEventId) params.set("last_event_id", lastEventId);
  // Electron injects X-EcoreX-Runtime-Token for loopback EventSource
  // requests. Keeping the runtime token out of the URL avoids leaking it via
  // request logs, devtools, or copied stream URLs.
  const url = `http://127.0.0.1:${input.webPort}/stream?${params.toString()}`;
  const events = new EventSource(url);
  events.onmessage = (event) => {
    try {
      rememberStreamCursor(input.requestId, event.lastEventId);
      const item = JSON.parse(event.data) as StreamItem;
      if (item.type === "done" || item.type === "error" || item.type === "cancelled" || item.type === "voice_attach") {
        scheduleStreamCursorCleanup(input.requestId);
      }
      input.onItem(item);
    } catch {
      input.onItem({ type: "error", message: "无法解析运行时返回" });
    }
  };
  events.onerror = () => {
    events.close();
    input.onError();
  };
  return () => events.close();
}

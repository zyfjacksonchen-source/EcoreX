export type RuntimeSession = {
  session_id?: string;
  id?: string;
  title?: string;
  last_active?: string | number;
  updatedAt?: string | number;
  msg_count?: number;
};

export type RuntimeActiveRequest = {
  request_id?: string;
  session_id?: string;
  cancelled?: boolean;
  state?: "running" | "cancelling" | string;
  created_at?: number;
  age_seconds?: number;
  stream_available?: boolean;
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
  path?: string;
};

export type RuntimeMessage = {
  role?: "user" | "assistant";
  content?: string;
  created_at?: number;
  seq?: number;
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
    [key: string]: unknown;
  };
};

export type FileAttachment = {
  file_path: string;
  file_name: string;
  file_type: "image" | "video" | "audio" | "file" | "directory";
  previewDataUrl?: string;
};

export type RuntimeSnapshot = {
  status: "ready" | "offline" | "error";
  message: string;
  version?: string;
  releaseNotes?: RuntimeReleaseNotes;
  currentModel?: string;
  sessions: RuntimeSession[];
  activeRequests?: RuntimeActiveRequest[];
  totalSessions: number;
  toolsCount: number;
  skillsCount: number;
  modelsCount: number;
  tools?: RuntimeTool[];
  skills?: RuntimeSkill[];
  modelCapabilities?: Record<string, unknown>;
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
  content?: string;
  text?: string;
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
    const activeRequestsPromise = apiJson<{ requests?: RuntimeActiveRequest[] }>("/api/active-requests")
      .catch(() => ({ requests: [] }));
    const [version, sessions, tools, skills, models, activeRequests] = await Promise.all([
      apiJson<{ version?: string; releaseNotes?: RuntimeReleaseNotes }>("/api/version"),
      apiJson<{ sessions?: RuntimeSession[]; total?: number; message?: string }>("/api/sessions?page=1&page_size=40"),
      apiJson<{ tools?: RuntimeTool[] }>("/api/tools"),
      apiJson<{ skills?: RuntimeSkill[] }>("/api/skills"),
      apiJson<{ providers?: unknown[]; capabilities?: Record<string, unknown> | unknown[] }>("/api/models"),
      activeRequestsPromise
    ]);

    const runtimeSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
    const runtimeTools = Array.isArray(tools.tools) ? tools.tools : [];
    const runtimeSkills = Array.isArray(skills.skills) ? skills.skills : [];
    const runtimeActiveRequests = Array.isArray(activeRequests.requests) ? activeRequests.requests : [];
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
      totalSessions: typeof sessions.total === "number" ? sessions.total : runtimeSessions.length,
      toolsCount: runtimeTools.length,
      skillsCount: runtimeSkills.length,
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
      totalSessions: 0,
      toolsCount: 0,
      skillsCount: 0,
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
  attachments?: FileAttachment[];
  lang?: string;
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

export async function loadSessionHistory(sessionId: string): Promise<RuntimeMessage[]> {
  if (!sessionId) {
    return [];
  }
  const result = await apiJson<{ messages?: RuntimeMessage[] }>(
    `/api/history?session_id=${encodeURIComponent(sessionId)}&page=1&page_size=50`
  );
  return Array.isArray(result.messages) ? result.messages : [];
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

export async function openLocalPath(filePath: string) {
  if (!window.ecorexDesktop?.openPath) {
    return "desktop bridge unavailable";
  }
  return window.ecorexDesktop.openPath(filePath);
}

export async function loadPermissionState(): Promise<PermissionState | null> {
  if (window.ecorexDesktop?.getPermissionState) {
    return window.ecorexDesktop.getPermissionState();
  }
  return apiJson<PermissionState & { status?: string }>("/api/tool-permissions");
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
  if (!window.ecorexDesktop?.listCapabilityPacks) {
    return [];
  }
  return window.ecorexDesktop.listCapabilityPacks();
}

export async function installCapabilityPack(packId: string): Promise<CapabilityPack | null> {
  if (!window.ecorexDesktop?.installCapabilityPack) {
    return null;
  }
  const result = await window.ecorexDesktop.installCapabilityPack(packId);
  await reportDesktopEvent({
    type: result.installed ? "info" : "error",
    source: "Capability",
    message: result.message,
    category: "capability-install",
    label: result.id,
    detail: {
      state: result.state,
      installed: result.installed,
      logPath: result.logPath,
      missingModules: result.missingModules
    }
  });
  return result;
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

export function filePreviewUrl(filePath: string, webPort: number) {
  return `http://127.0.0.1:${webPort}/api/file?path=${encodeURIComponent(filePath)}`;
}

export function openMessageStream(input: {
  requestId: string;
  webPort: number;
  onItem: (item: StreamItem) => void;
  onError: () => void;
}) {
  const url = `http://127.0.0.1:${input.webPort}/stream?request_id=${encodeURIComponent(input.requestId)}`;
  const events = new EventSource(url);
  events.onmessage = (event) => {
    try {
      input.onItem(JSON.parse(event.data) as StreamItem);
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

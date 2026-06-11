export type RuntimeSession = {
  session_id?: string;
  id?: string;
  title?: string;
  last_active?: string;
  msg_count?: number;
};

export type RuntimeMessage = {
  role?: "user" | "assistant";
  content?: string;
  created_at?: number;
  seq?: number;
  user_seq?: number;
  tool_calls?: Array<{ name?: string; result?: string }>;
};

export type FileAttachment = {
  file_path: string;
  file_name: string;
  file_type: "image" | "video" | "file" | "directory";
  previewDataUrl?: string;
};

export type RuntimeSnapshot = {
  status: "ready" | "offline" | "error";
  message: string;
  version?: string;
  sessions: RuntimeSession[];
  totalSessions: number;
  toolsCount: number;
  skillsCount: number;
  modelsCount: number;
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

export type PermissionMode = "smart-ask" | "always-ask" | "read-only" | "custom";

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
};

export type StreamItem = {
  type?: string;
  content?: string;
  message?: string;
  tool?: string;
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

export async function checkEnterpriseQuota(estimatedTokens: number) {
  if (!window.ecorexDesktop?.checkEnterpriseQuota) {
    return { ok: true, quota: { allowed: true } };
  }
  return window.ecorexDesktop.checkEnterpriseQuota(estimatedTokens);
}

export async function loadRuntimeSnapshot(): Promise<RuntimeSnapshot> {
  try {
    const [version, sessions, tools, skills, models] = await Promise.all([
      apiJson<{ version?: string }>("/api/version"),
      apiJson<{ sessions?: RuntimeSession[]; total?: number; message?: string }>("/api/sessions?page=1&page_size=40"),
      apiJson<{ tools?: unknown[] }>("/api/tools"),
      apiJson<{ skills?: unknown[] }>("/api/skills"),
      apiJson<{ providers?: unknown[]; capabilities?: unknown[] }>("/api/models")
    ]);

    const runtimeSessions = Array.isArray(sessions.sessions) ? sessions.sessions : [];
    return {
      status: "ready",
      message: "已连接本地 EcoreX 运行时",
      version: version.version,
      sessions: runtimeSessions,
      totalSessions: typeof sessions.total === "number" ? sessions.total : runtimeSessions.length,
      toolsCount: countArray(tools.tools),
      skillsCount: countArray(skills.skills),
      modelsCount: countArray(models.providers) || countArray(models.capabilities)
    };
  } catch (error) {
    return {
      status: "offline",
      message: error instanceof Error ? error.message : "本地运行时暂不可用",
      sessions: [],
      totalSessions: 0,
      toolsCount: 0,
      skillsCount: 0,
      modelsCount: 0
    };
  }
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

export async function deleteMessagePair(input: { sessionId: string; userSeq: number }) {
  return apiJson<{ status?: string; deleted?: number }>("/api/messages/delete", "POST", {
    session_id: input.sessionId,
    user_seq: input.userSeq,
    delete_user: true,
    cascade: false
  });
}

export async function chooseLocalFiles(): Promise<FileAttachment[]> {
  if (!window.ecorexDesktop?.chooseFiles) {
    return [];
  }
  const files = await window.ecorexDesktop.chooseFiles();
  return files.map((file) => ({
    ...file,
    previewDataUrl: file.file_type === "image" ? filePreviewUrl(file.file_path, 9899) : undefined
  }));
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
  if (!window.ecorexDesktop?.getPermissionState) {
    return null;
  }
  return window.ecorexDesktop.getPermissionState();
}

export async function updatePermissionMode(mode: PermissionMode): Promise<PermissionState | null> {
  if (!window.ecorexDesktop?.setPermissionMode) {
    return null;
  }
  return window.ecorexDesktop.setPermissionMode(mode);
}

export async function resetPermissionGrants(): Promise<PermissionState | null> {
  if (!window.ecorexDesktop?.resetPermissionGrants) {
    return null;
  }
  return window.ecorexDesktop.resetPermissionGrants();
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

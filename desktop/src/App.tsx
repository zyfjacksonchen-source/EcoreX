import {
  Activity,
  AlertTriangle,
  ArrowDownToLine,
  AtSign,
  Bell,
  Bot,
  BookOpen,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Database,
  FileText,
  FolderInput,
  FolderPlus,
  FolderOpen,
  FolderX,
  Globe2,
  HardDrive,
  Image as ImageIcon,
  KeyRound,
  LogOut,
  Moon,
  MoreHorizontal,
  Paperclip,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Search,
  SendHorizontal,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  SquareTerminal,
  SunMedium,
  Trash2,
  Upload,
  UserRound,
  WandSparkles,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, CSSProperties, DragEvent, MouseEvent, ReactNode } from "react";
import { MessageContent, type AgentStepDisclosure, type LocalFilePayload, type ToolCallDisclosure } from "./components/MessageContent";
import {
  cancelChatRequest,
  cancelSubagentTask,
  checkForUpdates,
  clearRuntimeContext,
  enterpriseChangePassword,
  checkEnterpriseQuota,
  chooseProjectFolder,
  chooseLocalFiles,
  decideToolPermission,
  enableDefaultSkills,
  enterpriseLogin,
  enterpriseLogout,
  exportDiagnosticsBundle,
  filePreviewUrl,
  generateSessionTitle,
  getEnterpriseSession,
  hasMessageStreamCursor,
  installDownloadedUpdate,
  listCapabilityPacks,
  loadPermissionState,
  loadMemoryFiles,
  loadRuntimeSnapshot,
  loadSessionHistoryWithMeta,
  openLocalPath,
  openDownloadPage,
  openMessageStream,
  prepareRequestRetry,
  readLocalJson,
  reportDesktopEvent,
  requestAgentInstallRequest,
  resetPermissionGrants,
  deleteRuntimeSession,
  renameRuntimeSession,
  registerProjectFolderPath,
  savePastedFile,
  saveRuntimeUiState,
  sendChatMessage,
  setSkillEnabled,
  statLocalPath,
  updatePermissionMode,
  type CapabilityPack,
  type ChatSendResult,
  type DesktopUpdateStatus,
  type AgentArtifact,
  type EnterpriseQuotaCheckResult,
  type EnterpriseSession,
  type FileAttachment,
  type LocalJsonResult,
  type LocalPathStat,
  type MemoryFile,
  type PermissionMode,
  type PermissionState,
  type ProjectFolder,
  type RuntimeActiveRequest,
  type RuntimeSessionLock,
  type RuntimeExtension,
  type RuntimeMessage,
  type RuntimeSkill,
  type RuntimeStep,
  type RuntimeToolCall,
  type RuntimeTool,
  type RuntimeSnapshot,
  type StreamItem,
  type TokenUsage,
  type UsageQuota,
  type OpenPathAction
} from "./services/ecorexApi";
import { CHAT_SCROLL_THRESHOLD_PX, getChatScrollState, scrollElementToBottom } from "./utils/chatUx";
import { redactInternalPromptText } from "./utils/redaction";

type ThemeMode = "light" | "dark";
type SidecarStatus = {
  state: "starting" | "running" | "stopped" | "failed" | "skipped";
  phase?: "idle" | "spawning" | "probing" | "ready" | "degraded" | "restarting" | "failed" | "stopped" | "skipped";
  message: string;
  pid?: number;
  webPort: number;
  diagnostics?: {
    bootId: string;
    restartAttempts: number;
    consecutiveHealthFailures: number;
    startupInFlight: boolean;
    lastProbeOkAt?: string;
    lastProbeErrorAt?: string;
    recentEvents?: Array<{ ts: string; state: string; phase: string; message: string; reason?: string }>;
  };
};
type StreamRequestPhase =
  | "connecting"
  | "streaming"
  | "stalled"
  | "flushing"
  | "text_done_tail_open"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";
type StreamRequestState = {
  sessionId: string;
  requestId: string;
  phase: StreamRequestPhase;
  updatedAt: number;
  terminalAt?: number;
  lastEventAt?: number;
};
type SessionRow = {
  id: string;
  title: string;
  detail: string;
  activityAt?: string | number;
  createdAt?: string | number;
  updatedAt: string | number;
  status: "active" | "waiting" | "cancelling" | "ready" | "failed";
  requestId?: string;
  streamAvailable?: boolean;
  cancelling?: boolean;
  pinned?: boolean;
  projectId?: string;
  projectName?: string;
};
type ChatItem = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  attachments?: FileAttachment[];
  pending?: boolean;
  paused?: boolean;
  reasoning?: string;
  steps?: AgentStepDisclosure[];
  toolCalls?: ToolCallDisclosure[];
  artifacts?: AgentArtifact[];
  cancelled?: boolean;
  requestId?: string;
  phaseStartedAt?: number;
  userSeq?: number;
  botSeq?: number;
  contextExcluded?: boolean;
  sendAttempt?: {
    id: string;
    state: "stopping-previous" | "sending" | "accepted" | "restore-available";
    interruptsRequestId?: string;
  };
  recovery?: {
    kind: "stalled" | "failed" | "interrupted" | "replay_gap" | "retryable_conflict";
    requestId?: string;
    message: string;
    retryable?: boolean;
    recoverable?: boolean;
  };
};
type ApprovalState =
  | {
      type: "capability";
      title: string;
      message: string;
      pack: CapabilityPack;
      resume: () => void;
    }
  | {
      type: "open-file";
      title: string;
      message: string;
      file: FileAttachment;
    }
  | {
      type: "quota" | "permission" | "info" | "error";
      title: string;
      message: string;
      actions?: Array<{ label: string; primary?: boolean; onClick: () => void }>;
    };
type SettingsSection = "account" | "projects" | "abilities" | "permissions" | "memory" | "diagnostics";
type SessionProjectMap = Record<string, string>;
type StringBoolMap = Record<string, boolean>;
type StringMap = Record<string, string>;
type SessionUiState = {
  title: string;
  projectId: string | null;
  messages: ChatItem[];
  composerText: string;
  attachments: FileAttachment[];
  contextStartSeq?: number;
  lastActivityAt?: string | number;
};
type ProjectContextMenu = {
  projectId: string;
  x: number;
  y: number;
} | null;
type ChatFileContextMenu = {
  file: FileAttachment;
  x: number;
  y: number;
  canAdd: boolean;
  disabledReason?: string;
} | null;
type SidebarCollapseState = {
  projectsSection: boolean;
  generalSessions: boolean;
  projectGroups: StringBoolMap;
};
type InstallNotice = {
  packId: string;
  packName: string;
  message: string;
  dismissed?: boolean;
} | null;

const brandIconUrl = new URL("../build/icon.png", import.meta.url).href;
document.documentElement.dataset.platform = window.ecorexDesktop?.platform || "web";

function isRuntimePreviewPath(value?: string) {
  const source = String(value || "").trim();
  return /^https?:\/\//i.test(source) || /^(?:\/(?:uploads|static|app)(?:\/|$)|\/api\/file(?:[/?#]|$))/.test(source);
}

function nativePathFromFileUrl(value?: string) {
  const source = String(value || "").trim();
  if (!/^file:\/\//i.test(source)) return "";
  try {
    const parsed = new URL(source);
    if (parsed.hostname) {
      return `\\\\${parsed.hostname}${decodeURIComponent(parsed.pathname).replace(/\//g, "\\")}`;
    }
    const decoded = decodeURIComponent(parsed.pathname || "");
    return decoded.replace(/^\/([a-zA-Z]:[\\/])/, "$1").replace(/\//g, "\\");
  } catch {
    return source.replace(/^file:\/\/+/i, "").replace(/^\/([a-zA-Z]:[\\/])/, "$1");
  }
}

function normalizeLocalSource(value?: string) {
  const source = String(value || "").trim();
  return nativePathFromFileUrl(source) || source;
}

function isLocalAbsolutePath(value?: string) {
  const source = normalizeLocalSource(value);
  const platform = window.ecorexDesktop?.platform || "";
  return /^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("\\\\") || (platform !== "win32" && /^\//.test(source) && !isRuntimePreviewPath(source));
}

function isOpenPathNotFoundMessage(value?: string) {
  return /path not found|not found|找不到|不存在/i.test(String(value || ""));
}

function isOpenPathDeniedMessage(value?: string) {
  return /denied|blocked|forbidden|permission|refusing to launch|not allowed|拒绝|阻止|权限|危险/i.test(String(value || ""));
}

function isOpenPathBridgeFailure(value?: string) {
  return /desktop bridge is not available|local runtime is unavailable|failed to fetch|networkerror|econnrefused|sidecar|runtime/i.test(String(value || ""));
}

function joinLocalPath(base: string, child: string) {
  const root = base.trim();
  const rel = child.trim().replace(/^[\\/]+/g, "");
  if (!root || !rel) return child;
  const slash = root.includes("\\") && !root.includes("/") ? "\\" : "/";
  return root.replace(/[\\/]+$/g, "") + slash + rel;
}

function isImageAttachment(file: FileAttachment) {
  return file.file_type === "image" || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.file_name || file.file_path || "");
}

function normalizeAttachmentDedupeKey(file: FileAttachment) {
  const raw = normalizeLocalSource(file.file_path || file.preview_url || file.file_name || "");
  const compact = raw.replace(/[\\/]+$/g, "").replace(/\\/g, "/");
  if (/^[a-zA-Z]:\//.test(compact) || compact.startsWith("//")) return compact.toLowerCase();
  return compact;
}

function isDurableLocalAttachment(file: FileAttachment) {
  const path = normalizeLocalSource(file.file_path || "");
  if (!path || /^data:/i.test(path) || /^https?:\/\//i.test(path) || isRuntimePreviewPath(path)) return false;
  return true;
}

const initialRuntime: RuntimeSnapshot = {
  status: "offline",
  message: "正在连接本地运行时",
  sessions: [],
  totalSessions: 0,
  toolsCount: 0,
  skillsCount: 0,
  extensionsCount: 0,
  extensionSummary: {},
  modelsCount: 0
};
const initialSidecar: SidecarStatus = {
  state: "starting",
  message: "正在启动本地运行时",
  webPort: 9899
};
const initialUpdateStatus: DesktopUpdateStatus = {
  state: "idle",
  platform: window.ecorexDesktop?.platform || "web",
  currentVersion: "",
  message: "尚未检查更新"
};

function friendlyUpdateErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || "");
  if (/app-update\.ya?ml/i.test(message) && /ENOENT|no such file/i.test(message)) {
    return "当前测试包未配置自动更新通道";
  }
  if (/latest\.ya?ml/i.test(message) && /404|not found|Cannot find channel/i.test(message)) {
    return "当前自动更新通道暂未发布，请打开下载页获取最新版本";
  }
  if (/HttpError|at ElectronHttpExecutor|builder-util-runtime|app\.asar/i.test(message)) {
    return "更新检查失败，请打开下载页获取最新版本";
  }
  return message || "更新检查失败";
}

const PROJECTS_STORAGE_KEY = "ecorex-projects";
const SESSION_PROJECTS_STORAGE_KEY = "ecorex-session-projects";
const SESSION_TITLES_STORAGE_KEY = "ecorex-session-titles";
const LOCKED_SESSION_TITLES_STORAGE_KEY = "ecorex-locked-session-titles";
const PINNED_SESSIONS_STORAGE_KEY = "ecorex-pinned-sessions";
const PINNED_PROJECTS_STORAGE_KEY = "ecorex-pinned-projects";
const SESSION_UI_STORAGE_KEY = "ecorex-session-ui-state";
const LAST_ACTIVE_SESSION_STORAGE_KEY = "ecorex-last-active-session-id";
const CAPABILITY_ENABLED_STORAGE_KEY = "ecorex-capability-enabled";
const SKILL_DEFAULTS_STORAGE_KEY = "ecorex-skill-defaults-v1";
const RELEASE_NOTES_SEEN_STORAGE_KEY = "ecorex-release-notes-seen-version";
const SIDEBAR_COLLAPSE_STORAGE_KEY = "ecorex-sidebar-collapse-state-v1";
const RUN_CENTER_DEV_GATE_STORAGE_KEY = "ecorex-dev-run-center";
const CONTEXT_THRESHOLD_TOKENS = 258_000;
const EFFECTIVE_MODEL_FALLBACK = "gpt-5.5";
const EFFECTIVE_MODEL_ALIAS_PREFIXES = ["deepseek-"];
const COMPOSER_PERMISSION_MENU_MODES: PermissionMode[] = ["always-ask", "smart-ask", "full-access", "read-only", "custom"];
const SETTINGS_PERMISSION_MODES: PermissionMode[] = ["full-access", "smart-ask", "always-ask", "read-only", "custom"];

const coreAbilityNames = new Set([
  "bash",
  "read",
  "write",
  "edit",
  "ls",
  "find",
  "vision",
  "web_search",
  "web_fetch",
  "browser",
  "ecorex_cli",
  "memory_search",
  "memory_get"
]);

const skillAbilityNames = new Set(["find", "image-generation", "knowledge-wiki", "skill-creator"]);

function readStorage<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeStorage<T>(key: string, value: T) {
  window.localStorage.setItem(key, JSON.stringify(value));
}

function initialSidebarCollapseState(): SidebarCollapseState {
  const saved = readStorage<Partial<SidebarCollapseState>>(SIDEBAR_COLLAPSE_STORAGE_KEY, {});
  return {
    projectsSection: Boolean(saved.projectsSection),
    generalSessions: Boolean(saved.generalSessions),
    projectGroups: saved.projectGroups && typeof saved.projectGroups === "object" ? saved.projectGroups : {}
  };
}

function pruneSessionUiState(state: Record<string, SessionUiState>) {
  const entries = Object.entries(state);
  const liveEntries = entries.filter(([, value]) => hasLivePersistedMessages(value.messages || []));
  const retained = new Map<string, SessionUiState>();
  for (const [sessionId, value] of entries.slice(-24)) retained.set(sessionId, value);
  for (const [sessionId, value] of liveEntries) retained.set(sessionId, value);
  return Object.fromEntries(
    [...retained.entries()].map(([sessionId, value]) => [
      sessionId,
      {
        ...value,
        messages: value.messages.slice(-60),
        attachments: value.attachments.slice(0, 12)
      }
    ])
  );
}

function hasLivePersistedMessages(messages: ChatItem[]) {
  return messages.some((message) => (
    message.role === "assistant"
    && message.pending
    && Boolean(message.requestId)
    && !message.paused
    && !message.cancelled
  ));
}

function hasLiveSessionUiState(state: Record<string, SessionUiState>) {
  return Object.values(state).some((value) => hasLivePersistedMessages(value.messages || []));
}

function pickBootSession(state: Record<string, SessionUiState>) {
  const entries = Object.entries(state);
  const savedId = window.localStorage.getItem(LAST_ACTIVE_SESSION_STORAGE_KEY) || "";
  if (savedId && state[savedId]) {
    return { id: savedId, state: state[savedId] };
  }
  const liveEntry = [...entries].reverse().find(([, value]) => hasLivePersistedMessages(value.messages || []));
  if (liveEntry) return { id: liveEntry[0], state: liveEntry[1] };
  const latestEntry = entries[entries.length - 1];
  return latestEntry ? { id: latestEntry[0], state: latestEntry[1] } : null;
}

function initialTheme(): ThemeMode {
  const saved = window.localStorage.getItem("ecorex-theme");
  if (saved === "dark" || saved === "light") return saved;
  return "dark";
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.dataset.theme = theme;
  void window.ecorexDesktop?.setWindowTheme?.(theme).catch(() => undefined);
}

function displayModelName(value?: string) {
  const model = (value || "").trim();
  if (!model || /^ecorex$/i.test(model) || /^openai$/i.test(model)) return EFFECTIVE_MODEL_FALLBACK;
  const normalized = model.toLowerCase();
  if (EFFECTIVE_MODEL_ALIAS_PREFIXES.some((prefix) => normalized.startsWith(prefix))) return EFFECTIVE_MODEL_FALLBACK;
  return model;
}

function isRuntimeRequestUiActive(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  if (!request.cancelled) return true;
  const ageSeconds = Number(request.cancel_age_seconds ?? request.age_seconds ?? 0);
  return !Number.isFinite(ageSeconds) || ageSeconds < 30;
}

function isSubagentRuntimeRequest(request?: RuntimeActiveRequest | null) {
  const requestId = String(request?.request_id || "");
  const sessionId = String(request?.session_id || "");
  return (
    request?.run_type === "subagent"
    || requestId.startsWith("subagent-")
    || sessionId.startsWith("subagent-")
  );
}

function isSchedulerRuntimeRequest(request?: RuntimeActiveRequest | null) {
  const requestId = String(request?.request_id || "");
  const sessionId = String(request?.session_id || "");
  return (
    request?.run_type === "scheduler"
    || requestId.startsWith("scheduler_")
    || sessionId.startsWith("scheduler_")
  );
}

function isPrimaryChatActiveRequest(request?: RuntimeActiveRequest | null) {
  return (
    !isSubagentRuntimeRequest(request)
    && !isSchedulerRuntimeRequest(request)
    && isRuntimeRequestUiActive(request)
  );
}

function isAbnormalTerminalRequest(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  const raw = String(request.state || request.status || request.phase || "").toLowerCase();
  const terminalReason = String(request.terminal_reason || "").trim().toLowerCase();
  const completed = /(complete|success|done|finish)/.test(raw) || /(complete|success|done|finish)/.test(terminalReason);
  if (completed && !request.cancelled) return false;
  if (request.cancelled || request.error_message || request.error_code) return true;
  if (terminalReason && !/(complete|success|done|finish)/.test(terminalReason)) return true;
  return (
    raw.includes("cancel")
    || raw.includes("fail")
    || raw.includes("error")
    || raw.includes("interrupt")
    || raw.includes("stale")
    || raw.includes("dead")
    || raw.includes("lock")
  );
}

function isPrimaryChatTerminalRequest(request?: RuntimeActiveRequest | null) {
  return Boolean(
    request?.request_id
    && !isSubagentRuntimeRequest(request)
    && !isSchedulerRuntimeRequest(request)
    && isAbnormalTerminalRequest(request)
  );
}

function runCenterState(request?: RuntimeActiveRequest | null) {
  const raw = String(request?.state || request?.status || request?.phase || "").toLowerCase();
  if (raw === "cancelled" && request?.terminal_at != null) return "cancelled";
  if (request?.cancelled || raw.includes("cancell")) return "cancelling";
  if (raw.includes("complete") || raw === "done") return "completed";
  if (raw.includes("fail") || raw.includes("error") || raw.includes("interrupt")) return "failed";
  if (raw.includes("queue") || raw.includes("pending")) return "queued";
  if (raw.includes("final")) return "finalizing";
  return raw || "running";
}

function runCenterStateLabel(request?: RuntimeActiveRequest | null) {
  const state = runCenterState(request);
  if (state === "cancelling") return "Stopping";
  if (state === "failed") return "Failed";
  if (state === "cancelled") return "Stopped";
  if (state === "queued") return "Queued";
  if (state === "finalizing") return "Finalizing";
  return "Running";
}

function runCenterStateClass(request?: RuntimeActiveRequest | null) {
  const state = runCenterState(request);
  if (state === "cancelling") return "is-cancelling";
  if (state === "cancelled") return "is-cancelling";
  if (state === "failed") return "is-failed";
  if (state === "queued") return "is-queued";
  if (state === "finalizing") return "is-finalizing";
  return "is-running";
}

function isRunCenterFailedRequest(request?: RuntimeActiveRequest | null) {
  return runCenterState(request) === "failed";
}

function isRunCenterVisibleRequest(request?: RuntimeActiveRequest | null) {
  if (!request?.request_id) return false;
  return runCenterState(request) !== "completed";
}

function isRunCenterSubagentRequest(request?: RuntimeActiveRequest | null) {
  return isSubagentRuntimeRequest(request);
}

function isRunCenterSchedulerRequest(request?: RuntimeActiveRequest | null) {
  return isSchedulerRuntimeRequest(request);
}

function getRunCenterSubagentTaskId(request?: RuntimeActiveRequest | null) {
  const metadataTaskId = request?.metadata?.task_id;
  if (typeof metadataTaskId === "string" && metadataTaskId.trim()) {
    return metadataTaskId.trim();
  }
  for (const value of [request?.request_id, request?.session_id]) {
    const text = String(value || "");
    if (text.startsWith("subagent-") && text.length > "subagent-".length) {
      return text.slice("subagent-".length);
    }
  }
  return "";
}

function shortRequestId(value?: string) {
  const text = String(value || "").trim();
  if (!text) return "unknown";
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text;
}

function formatRunAge(seconds?: number | null) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function isRetryableConcurrencyResult(result?: ChatSendResult | null) {
  return Boolean(
    result
    && result.status === "error"
    && (
      result.code === "REQUEST_CONFLICT_RETRYABLE"
      || result.error_type === "concurrency_conflict"
      || result.state === "retryable_conflict"
    )
    && (result.retryable || result.recoverable)
  );
}

function chatSendErrorMessage(result: ChatSendResult) {
  if (isRetryableConcurrencyResult(result)) {
    return result.message || "The previous run is still stopping. Please retry shortly.";
  }
  return result.message || "发送失败";
}

function BrandMark() {
  const [failed, setFailed] = useState(false);
  return (
    <div className="brand-mark" aria-hidden="true">
      {failed ? <Sparkles aria-hidden="true" /> : <img src={brandIconUrl} alt="" onError={() => setFailed(true)} />}
    </div>
  );
}

function versionLabel(version?: string) {
  const value = String(version || "").trim();
  if (!value) return "";
  return value.startsWith("v") ? value : `v${value}`;
}

function WindowBrand(props: { version?: string } = {}) {
  const label = versionLabel(props.version);
  return (
    <div className="window-brand" aria-hidden="true">
      <img src={brandIconUrl} alt="" />
      <span>EcoreX</span>
      {label && <small>{label}</small>}
    </div>
  );
}

function formatTime(value?: string | number) {
  if (!value) return "刚刚";
  const normalized = typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function timeMs(value?: string | number) {
  if (!value) return 0;
  if (typeof value === "string" && /^(运行中|正在停止|刚刚|本地)$/.test(value)) return 0;
  const normalized = typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value;
  const parsed = new Date(normalized).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function latestMessageMs(messages: ChatItem[] | undefined) {
  return (messages || []).reduce((latest, message) => Math.max(latest, timeMs(message.createdAt)), 0);
}

function latestTimeValue(...values: Array<string | number | undefined>) {
  let bestValue: string | number | undefined;
  let bestMs = 0;
  for (const value of values) {
    const ms = timeMs(value);
    if (ms > bestMs) {
      bestMs = ms;
      bestValue = value;
    }
  }
  return bestValue || values.find((value) => Boolean(value)) || "";
}

function sessionActivityMs(row: SessionRow) {
  const activity = timeMs(row.activityAt);
  if (activity) return activity;
  const updated = timeMs(row.updatedAt);
  if (updated) return updated;
  return timeMs(row.createdAt);
}

function shortTitle(text: string) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 22) : "新对话";
}

function mapSessions(
  snapshot: RuntimeSnapshot,
  activeSessionId: string,
  localTitle: string,
  sessionProjects: SessionProjectMap,
  sessionTitles: StringMap,
  pinnedSessions: StringBoolMap,
  projects: ProjectFolder[],
  sessionUiState: Record<string, SessionUiState>,
  locallyCompletedRequestIds: StringBoolMap = {}
): SessionRow[] {
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const activeRequestBySession = new Map<string, RuntimeActiveRequest>(
    (snapshot.activeRequests || [])
      .filter((request) => request.session_id && request.request_id)
      .filter(isPrimaryChatActiveRequest)
      .filter((request) => !locallyCompletedRequestIds[String(request.request_id || "")])
      .map((request) => [String(request.session_id), request])
  );
  const rows: SessionRow[] = snapshot.sessions.map((session, index) => {
    const id = session.session_id || session.id || `runtime-${index}`;
    const projectId = sessionProjects[id] || null;
    const project = projectId ? projectById.get(projectId) : undefined;
    const cached = sessionUiState[id];
    const cachedActivity = latestTimeValue(cached?.lastActivityAt, latestMessageMs(cached?.messages));
    const activeRequest = activeRequestBySession.get(id);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activityAt = latestTimeValue(cachedActivity, session.last_active, session.updatedAt, session.created_at);
    return {
      id,
      title: sessionTitles[id] || session.title || session.session_id || "未命名会话",
      detail: project ? project.name : "",
      activityAt,
      createdAt: session.created_at || cachedActivity || activityAt,
      updatedAt: activeRequestId && !activityAt ? (isCancelling ? "正在停止" : "运行中") : activityAt || "",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : id === activeSessionId ? "active" : "ready",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[id]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    } satisfies SessionRow;
  });
  const rowIds = new Set(rows.map((row) => row.id));
  for (const [sessionId, cached] of Object.entries(sessionUiState)) {
    if (rowIds.has(sessionId)) continue;
    const hasContent = Boolean(cached.composerText || cached.attachments.length || cached.messages.length);
    if (!hasContent) continue;
    const projectId = sessionProjects[sessionId] || null;
    const project = projectId ? projectById.get(projectId) : undefined;
    const live = (cached.messages || []).some((message) => (
      isLiveAssistantMessage(message)
      && !(message.requestId && locallyCompletedRequestIds[message.requestId])
    ));
    const activeRequest = activeRequestBySession.get(sessionId);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activityAt = cached.lastActivityAt || latestMessageMs(cached.messages);
    rows.push({
      id: sessionId,
      activityAt,
      createdAt: activityAt,
      title: sessionTitles[sessionId] || cached.title || "未命名会话",
      detail: project ? project.name : "",
      updatedAt: activeRequestId ? (isCancelling ? "正在停止" : "运行中") : live ? "运行中" : "本地",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : live ? "waiting" : sessionId === activeSessionId ? "active" : "ready",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[sessionId]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
    rowIds.add(sessionId);
  }
  for (const [sessionId, activeRequest] of activeRequestBySession.entries()) {
    if (rowIds.has(sessionId)) continue;
    const projectId = sessionProjects[sessionId] || null;
    const project = projectId ? projectById.get(projectId) : undefined;
    const requestId = activeRequest.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest.cancelled);
    const activityAt = activeRequest.created_at || Date.now();
    rows.push({
      id: sessionId,
      activityAt,
      createdAt: activityAt,
      title: sessionTitles[sessionId] || sessionUiState[sessionId]?.title || sessionId,
      detail: project ? project.name : "",
      updatedAt: isCancelling ? "正在停止" : "运行中",
      status: isCancelling ? "cancelling" : "waiting",
      requestId,
      streamAvailable: activeRequest.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[sessionId]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
    rowIds.add(sessionId);
  }
  if (!rows.some((row) => row.id === activeSessionId)) {
    const projectId = sessionProjects[activeSessionId] || null;
    const project = projectId ? projectById.get(projectId) : undefined;
    const activeRequest = activeRequestBySession.get(activeSessionId);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    const activityAt = sessionUiState[activeSessionId]?.lastActivityAt || latestMessageMs(sessionUiState[activeSessionId]?.messages) || Date.now();
    rows.unshift({
      id: activeSessionId,
      activityAt,
      createdAt: activityAt,
      title: sessionTitles[activeSessionId] || localTitle || "新对话",
      detail: project ? project.name : "",
      updatedAt: activeRequestId ? (isCancelling ? "正在停止" : "运行中") : "刚刚",
      status: activeRequestId ? (isCancelling ? "cancelling" : "waiting") : "active",
      requestId: activeRequestId,
      streamAvailable: activeRequest?.stream_available !== false,
      cancelling: isCancelling,
      pinned: Boolean(pinnedSessions[activeSessionId]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
  }
  return rows.sort((a, b) => {
    const pinnedDiff = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
    if (pinnedDiff) return pinnedDiff;
    const activityDiff = sessionActivityMs(b) - sessionActivityMs(a);
    if (activityDiff) return activityDiff;
    const createdDiff = timeMs(b.createdAt) - timeMs(a.createdAt);
    if (createdDiff) return createdDiff;
    return b.id.localeCompare(a.id);
  });
}

function estimateTextTokens(text: string) {
  let latin = 0;
  let wide = 0;
  let symbols = 0;
  for (const char of text || "") {
    if (/\s/.test(char)) continue;
    if (/[\u3400-\u9fff\uf900-\ufaff]/.test(char)) {
      wide += 1;
    } else if (/[\x00-\x7f]/.test(char)) {
      latin += 1;
    } else {
      symbols += 1;
    }
  }
  return Math.ceil(latin / 4) + Math.ceil(wide * 1.25) + Math.ceil(symbols / 2);
}

function estimateStructuredTokens(value: unknown) {
  if (value === undefined || value === null || value === "") return 0;
  if (typeof value === "string") return estimateTextTokens(value);
  try {
    return estimateTextTokens(JSON.stringify(value));
  } catch {
    return estimateTextTokens(String(value));
  }
}

function estimateFileTokens(files: FileAttachment[]) {
  return files.reduce((total, file) => {
    const nameTokens = estimateTextTokens(file.file_name || file.file_path || "");
    const mediaCost = file.file_type === "image" ? 420 : file.file_type === "video" ? 900 : file.file_type === "directory" ? 220 : 160;
    return total + nameTokens + mediaCost;
  }, 0);
}

function estimateTokenCount(text: string, files: FileAttachment[]) {
  return Math.max(0, estimateTextTokens(text) + estimateFileTokens(files));
}

function estimateTokens(text: string, files: FileAttachment[]) {
  return Math.max(1, estimateTokenCount(text, files));
}

function usageTotal(usage?: TokenUsage | null) {
  if (!usage) return 0;
  const total = Number(usage.totalTokens || 0);
  if (Number.isFinite(total) && total > 0) return total;
  const input = Number(usage.inputTokens || 0);
  const output = Number(usage.outputTokens || 0);
  return Math.max(0, (Number.isFinite(input) ? input : 0) + (Number.isFinite(output) ? output : 0));
}

function compactTokenCount(value: number) {
  const safe = Math.max(0, Math.round(Number.isFinite(value) ? value : 0));
  if (safe >= 1_000_000) {
    const amount = safe / 1_000_000;
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}m`;
  }
  if (safe >= 1_000) {
    const amount = safe / 1_000;
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}k`;
  }
  return String(safe);
}

function quotaNumber(quota: UsageQuota | null | undefined, key: keyof UsageQuota) {
  const value = Number(quota?.[key]);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function isQuotaLimitFailure(quota: UsageQuota | null | undefined) {
  if (!quota || quota.allowed !== false) return false;
  if (quota.overDaily === true || quota.overWeekly === true) return true;
  const reason = String(quota.reason || "").toLowerCase();
  if (!reason) return false;
  if (/device does not match|invalid user token|user token expired|missing user token|尚未登录|未登录|登录|session|device/.test(reason)) return false;
  return /quota|daily|weekly|limit|额度|上限|已用完|超过|本次请求/.test(reason);
}

function isEnterpriseAuthFailure(quota: UsageQuota | null | undefined) {
  if (!quota || quota.allowed !== false) return false;
  const reason = String(quota.reason || "").toLowerCase();
  return /device does not match|invalid user token|user token expired|missing user token|尚未登录|未登录|登录|session|token|device/.test(reason);
}

function percentOf(used: number, limit: number) {
  if (!limit) return 0;
  return Math.min(100, Math.max(0, (used / limit) * 100));
}

function meterTitle(label: string, used: number, limit?: number) {
  const usedDetail = `${compactTokenCount(used)} tokens`;
  if (!limit) return `${label}：${usedDetail}，暂无上限数据`;
  const limitDetail = `${compactTokenCount(limit)} tokens`;
  return `${label}：${usedDetail} / ${limitDetail}，${Math.round(percentOf(used, limit))}%`;
}

function estimateContextTokens(messages: ChatItem[], draft: string, files: FileAttachment[]) {
  const history = messages.reduce((total, message) => {
    if (message.contextExcluded) return total;
    const messageTokens = estimateTokenCount(message.content || "", message.attachments || []);
    const stepTokens = (message.steps || []).reduce((stepTotal, step) => {
      if (step.type === "thinking" || step.type === "content" || step.type === "phase") {
        return stepTotal + estimateStructuredTokens(step.content);
      }
      if (step.type === "tool") {
        return stepTotal
          + estimateStructuredTokens(step.name)
          + estimateStructuredTokens(step.arguments)
          + estimateStructuredTokens(step.result)
          + 80;
      }
      return stepTotal + estimateStructuredTokens(step.fileName || step.url) + 120;
    }, 0);
    const legacyToolTokens = (message.toolCalls || []).reduce((toolTotal, tool) => (
      toolTotal
      + estimateStructuredTokens(tool.name)
      + estimateStructuredTokens(tool.arguments)
      + estimateStructuredTokens(tool.result)
      + 80
    ), 0);
    return total + messageTokens + stepTokens + legacyToolTokens;
  }, 0);
  return history + estimateTokenCount(draft, files);
}

function detectNeededPack(text: string, files: FileAttachment[], packs: CapabilityPack[]) {
  const lower = text.toLowerCase();
  const hasOffice = files.some((file) => /\.(pdf|docx?|xlsx?|pptx?)$/i.test(file.file_name));
  const hasBrowser = /网页|浏览器|playwright|打开网站|搜索网页|爬取|browser/.test(lower);
  const hasVoice = /语音|录音|转写|tts|stt|voice/.test(lower);
  const hasIm = /slack|discord|telegram|wechat|微信|钉钉|dingtalk/.test(lower);
  const hasLark = /飞书|lark|feishu/.test(lower);
  const targetId = hasOffice
    ? "office-pdf"
    : hasBrowser
      ? "browser-automation"
      : hasVoice
        ? "voice"
        : hasIm
          ? "im-channels"
          : hasLark
            ? "feishu-lark"
            : "";
  const pack = packs.find((item) => item.id === targetId);
  return pack && !pack.installed ? pack : null;
}

function fileIcon(file: FileAttachment) {
  if (file.file_type === "image") return <Upload aria-hidden="true" />;
  return <FileText aria-hidden="true" />;
}

function ThinkingIndicator({ label = "思考中", compact = false }: { label?: string; compact?: boolean }) {
  return (
    <span className={`thinking-indicator${compact ? " is-compact" : ""}`} title={label}>
      <span className="thinking-ring" aria-hidden="true" />
      {!compact && <span>{label}</span>}
    </span>
  );
}

function permissionModeLabel(mode?: PermissionMode) {
  return mode === "full-access"
    ? "完全访问"
    : mode === "smart-ask"
    ? "智能确认"
    : mode === "always-ask"
      ? "每次询问"
      : mode === "read-only"
        ? "只读优先"
        : mode === "custom"
          ? "自定义"
          : "未设置";
}

function composerPermissionTitle(mode?: PermissionMode) {
  return mode === "always-ask"
    ? "请求批准"
    : mode === "smart-ask"
      ? "替我批准"
      : mode === "full-access"
        ? "完全访问权限"
        : mode === "read-only"
          ? "只读优先"
          : mode === "custom"
            ? "自定义 (config.toml)"
            : "权限";
}

function composerPermissionDetail(mode?: PermissionMode) {
  return mode === "always-ask"
    ? "编辑外部文件和使用互联网时始终询问"
    : mode === "smart-ask"
      ? "仅对检测到的风险操作请求批准"
      : mode === "full-access"
        ? "可不受限制地访问互联网和电脑上的任何文件"
        : mode === "read-only"
          ? "读取和查看优先，阻止高风险写入和执行"
          : mode === "custom"
            ? "使用 config.toml 中定义的权限"
            : "";
}

function composerPermissionIcon(mode?: PermissionMode) {
  if (mode === "full-access") return <KeyRound aria-hidden="true" />;
  if (mode === "custom") return <Settings aria-hidden="true" />;
  if (mode === "always-ask") return <ShieldCheck aria-hidden="true" />;
  if (mode === "smart-ask") return <CheckCircle2 aria-hidden="true" />;
  return <ShieldCheck aria-hidden="true" />;
}

function pausedMessageContent(content: string) {
  return content || "";
}

function interruptedMessageContent(content: string) {
  return content || "任务已中断，输入新消息后可重试";
}

type AgentFinishReason = "done" | "paused" | "cancelled" | "error";

function finishAgentSteps(steps: AgentStepDisclosure[] | undefined, reason: AgentFinishReason = "done") {
  return (steps || []).map((step) => {
    if (step.type === "thinking" && step.running) {
      return { ...step, running: false };
    }
    if (step.type === "tool" && step.running) {
      const status = step.status === "running"
        ? reason === "done"
          ? "done"
          : reason
        : step.status;
      return { ...step, running: false, status };
    }
    return step;
  });
}

function pausePendingMessage(item: ChatItem, interrupted = false): ChatItem {
  return {
    ...item,
    content: interrupted ? interruptedMessageContent(item.content) : pausedMessageContent(item.content),
    pending: false,
    paused: true,
    cancelled: false,
    steps: finishAgentSteps(item.steps, "paused"),
    toolCalls: item.toolCalls?.map((tool) => ({ ...tool, running: false }))
  };
}

function finishInactivePendingMessage(item: ChatItem): ChatItem {
  return {
    ...item,
    pending: false,
    paused: false,
    cancelled: false,
    steps: finishAgentSteps(item.steps, "done"),
    toolCalls: item.toolCalls?.map((tool) => ({ ...tool, running: false }))
  };
}

function normalizePausedMessages(
  items: ChatItem[],
  options?: {
    sessionId?: string;
    activeRequestIds?: Set<string>;
    staleSessionIds?: Set<string>;
    nowMs?: number;
    inactiveRequestGraceMs?: number;
  }
) {
  let changed = false;
  const staleSession = Boolean(options?.sessionId && options.staleSessionIds?.has(options.sessionId));
  const activeRequestIds = options?.activeRequestIds;
  const nowMs = options?.nowMs || Date.now();
  const inactiveGraceMs = options?.inactiveRequestGraceMs ?? 0;
  const next = items.map((item) => {
    if (!item.pending) return item;
    const createdAtMs = item.createdAt ? new Date(item.createdAt).getTime() : 0;
    const inGrace = Boolean(createdAtMs && Number.isFinite(createdAtMs) && nowMs - createdAtMs < inactiveGraceMs);
    if (!item.requestId || staleSession) {
      if (!staleSession && !item.requestId && inGrace) return item;
      changed = true;
      return pausePendingMessage(item, Boolean(staleSession));
    }
    if (activeRequestIds && !activeRequestIds.has(item.requestId)) {
      if (!inGrace) {
        changed = true;
        return finishInactivePendingMessage(item);
      }
    }
    return item;
  });
  return changed ? next : items;
}

function messageSequenceKey(message: ChatItem) {
  if (message.role === "user" && typeof message.userSeq === "number") return `user:${message.userSeq}`;
  if (message.role === "assistant" && typeof message.botSeq === "number") return `assistant:${message.botSeq}`;
  return "";
}

function messageRequestKey(message: ChatItem) {
  return message.role === "assistant" && message.requestId ? `request:${message.requestId}` : "";
}

function messageContentKey(message: ChatItem) {
  const content = redactInternalPromptText(message.content || "").trim();
  if (!content) return "";
  return `${message.role}:${content}`;
}

function artifactMergeKey(artifact: AgentArtifact) {
  return String(artifact.path || artifact.relativePath || artifact.url || artifact.title || artifact.id || "")
    .replace(/\\/g, "/")
    .toLowerCase();
}

function artifactStatusPriority(status?: AgentArtifact["status"]) {
  if (status === "ready" || status === "failed") return 3;
  if (status === "superseded") return 2;
  if (status === "pending") return 1;
  return 0;
}

function mergeAgentArtifactRecord(existing: AgentArtifact, incoming: AgentArtifact): AgentArtifact {
  const existingPriority = artifactStatusPriority(existing.status);
  const incomingPriority = artifactStatusPriority(incoming.status);
  const merged = { ...existing, ...incoming };
  merged.status = incomingPriority >= existingPriority ? incoming.status : existing.status;
  merged.statusPath = incoming.statusPath || existing.statusPath;
  merged.previewUrl = incoming.previewUrl || existing.previewUrl;
  merged.thumbnailUrl = incoming.thumbnailUrl || existing.thumbnailUrl;
  merged.mimeType = incoming.mimeType || existing.mimeType;
  merged.path = incoming.path || existing.path;
  merged.relativePath = incoming.relativePath || existing.relativePath;
  merged.url = incoming.url || existing.url;
  merged.sizeBytes = typeof incoming.sizeBytes === "number" ? incoming.sizeBytes : existing.sizeBytes;
  return merged;
}

function mergeLocalTailArtifacts(historyMessage: ChatItem, localMessage?: ChatItem) {
  return mergeHistoryAndLocalRequestMessage(historyMessage, localMessage);
}

function mergeArtifactsIntoMessage(message: ChatItem, artifacts: AgentArtifact[]) {
  if (!artifacts.length) return message;
  const nextArtifacts = [...(message.artifacts || [])];
  const seen = new Set(nextArtifacts.map(artifactMergeKey).filter(Boolean));
  let changed = false;
  for (const artifact of artifacts) {
    const key = artifactMergeKey(artifact);
    const index = key ? nextArtifacts.findIndex((entry) => artifactMergeKey(entry) === key) : -1;
    if (index >= 0) {
      const merged = mergeAgentArtifactRecord(nextArtifacts[index], artifact);
      if (JSON.stringify(merged) !== JSON.stringify(nextArtifacts[index])) {
        nextArtifacts[index] = merged;
        changed = true;
      }
      continue;
    }
    nextArtifacts.push(artifact);
    if (key) seen.add(key);
    changed = true;
  }
  return changed ? { ...message, artifacts: nextArtifacts } : message;
}

function messageHasTerminalPayload(message: ChatItem) {
  return Boolean(redactInternalPromptText(message.content || "").trim())
    || Boolean(message.steps?.length)
    || Boolean(message.toolCalls?.length)
    || Boolean(message.artifacts?.length);
}

function isTerminalAssistantMessage(message?: ChatItem) {
  return Boolean(message
    && message.role === "assistant"
    && !message.pending
    && !message.paused
    && !message.cancelled
    && messageHasTerminalPayload(message));
}

function isSameAssistantTurn(left: ChatItem, right: ChatItem) {
  if (left.role !== "assistant" || right.role !== "assistant") return false;
  if (left.requestId && right.requestId && left.requestId === right.requestId) return true;
  if (typeof left.botSeq === "number" && typeof right.botSeq === "number" && left.botSeq === right.botSeq) return true;
  if (typeof left.userSeq === "number" && typeof right.userSeq === "number" && left.userSeq === right.userSeq) return true;
  return false;
}

function mergeHistoryAndLocalRequestMessage(historyMessage: ChatItem, localMessage?: ChatItem) {
  if (!localMessage) return historyMessage;
  const historyWithLocalArtifacts = mergeArtifactsIntoMessage(historyMessage, localMessage.artifacts || []);
  if (historyMessage.role !== "assistant" || localMessage.role !== "assistant") {
    return historyWithLocalArtifacts;
  }
  if (!isSameAssistantTurn(historyMessage, localMessage)) {
    return historyWithLocalArtifacts;
  }
  const historyText = redactInternalPromptText(historyMessage.content || "").trim();
  const localText = redactInternalPromptText(localMessage.content || "").trim();
  if (!isTerminalAssistantMessage(localMessage) || !localText) {
    return historyWithLocalArtifacts;
  }
  const historyHasSameText = Boolean(historyText) && historyText === localText;
  const historyIsClearlyStronger = historyText.length > localText.length + 64;
  if (historyHasSameText || historyIsClearlyStronger) {
    return historyWithLocalArtifacts;
  }
  const mergedLocal = mergeArtifactsIntoMessage(localMessage, historyMessage.artifacts || []);
  return {
    ...mergedLocal,
    id: historyMessage.id || mergedLocal.id,
    createdAt: historyMessage.createdAt || mergedLocal.createdAt,
    requestId: mergedLocal.requestId || historyMessage.requestId,
    userSeq: typeof historyMessage.userSeq === "number" ? historyMessage.userSeq : mergedLocal.userSeq,
    botSeq: typeof historyMessage.botSeq === "number" ? historyMessage.botSeq : mergedLocal.botSeq,
    steps: mergedLocal.steps?.length ? mergedLocal.steps : historyMessage.steps,
    toolCalls: mergedLocal.toolCalls?.length ? mergedLocal.toolCalls : historyMessage.toolCalls
  };
}

function mergeHistoryWithLocalMessages(history: ChatItem[], local: ChatItem[]) {
  if (!history.length || !local.length) return history.length ? history : local;
  const historyHasFinalAssistant = history.some((message) => (
    message.role === "assistant"
    && !message.pending
    && messageHasTerminalPayload(message)
  ));
  const sequenceKeys = new Set(history.map(messageSequenceKey).filter(Boolean));
  const requestKeys = new Set(history.map(messageRequestKey).filter(Boolean));
  const contentKeys = new Set(history.map(messageContentKey).filter(Boolean));
  const localByRequestKey = new Map(local.map((message) => [messageRequestKey(message), message]).filter(([key]) => Boolean(key)) as [string, ChatItem][]);
  const localBySequenceKey = new Map(local
    .filter((message) => isTerminalAssistantMessage(message))
    .map((message) => [messageSequenceKey(message), message])
    .filter(([key]) => Boolean(key)) as [string, ChatItem][]);
  const preserved: ChatItem[] = [];
  let skipPendingAssistantAfterMatchedUser = false;
  const mergedHistory = history.map((message) => mergeLocalTailArtifacts(
    message,
    localByRequestKey.get(messageRequestKey(message)) || localBySequenceKey.get(messageSequenceKey(message))
  ));

  for (const message of local) {
    if (historyHasFinalAssistant && message.role === "assistant" && message.pending && message.id.startsWith("a-resume-")) {
      continue;
    }
    const sequenceKey = messageSequenceKey(message);
    if (sequenceKey && sequenceKeys.has(sequenceKey)) {
      skipPendingAssistantAfterMatchedUser = message.role === "user";
      continue;
    }
    const requestKey = messageRequestKey(message);
    if (requestKey && requestKeys.has(requestKey)) {
      skipPendingAssistantAfterMatchedUser = false;
      continue;
    }
    const contentKey = messageContentKey(message);
    if (contentKey && contentKeys.has(contentKey)) {
      skipPendingAssistantAfterMatchedUser = message.role === "user";
      continue;
    }

    if (message.role === "assistant" && message.pending && skipPendingAssistantAfterMatchedUser) {
      skipPendingAssistantAfterMatchedUser = false;
      continue;
    }

    const localOnly = message.pending
      || message.paused
      || message.id.startsWith("u-")
      || message.id.startsWith("a-")
      || (!sequenceKey && !requestKey);
    if (!localOnly) continue;

    preserved.push(message);
    skipPendingAssistantAfterMatchedUser = false;
    if (sequenceKey) sequenceKeys.add(sequenceKey);
    if (requestKey) requestKeys.add(requestKey);
    if (contentKey) contentKeys.add(contentKey);
  }

  return preserved.length ? [...mergedHistory, ...preserved] : mergedHistory;
}

function plainTextForMessage(message: ChatItem) {
  const parts: string[] = [];
  const seen = new Set<string>();
  const addPart = (value?: string) => {
    const text = redactInternalPromptText(value || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    parts.push(text);
  };
  addPart(message.content);
  for (const step of message.steps || []) {
    if (step.type === "content" && step.content && !step.intermediate) {
      addPart(step.content);
    } else if (step.type === "media" && (step.url || step.filePath)) {
      const source = step.filePath || step.url || "";
      addPart(step.fileName ? `${step.fileName}: ${source}` : source);
    }
  }
  for (const file of message.attachments || []) {
    addPart(`${file.file_name}: ${file.file_path}`);
  }
  return parts.join("\n\n").trim();
}

function isLiveAssistantMessage(message: ChatItem) {
  return message.role === "assistant" && message.pending === true && !message.paused && !message.cancelled;
}

function isSilentPausedAssistantMessage(message: ChatItem) {
  return message.role === "assistant"
    && Boolean(message.paused)
    && !message.pending
    && !message.cancelled
    && !message.content.trim()
    && !(message.reasoning || "").trim()
    && !(message.steps || []).length
    && !(message.toolCalls || []).length;
}

function projectContextPrompt(project: ProjectFolder) {
  return [
    "【EcoreX 项目上下文】",
    "默认沟通风格：专业、严谨、克制，称呼用户为“同学”。",
    "对外身份始终是 EcoreX。",
    `项目名称：${project.name}`,
    `项目文件夹：${project.path}`,
    `项目记忆：${project.memoryPath || `${project.path}/.ecorex/project-memory.md`}`,
    `项目梦境蒸馏：${project.dreamsPath || `${project.path}/.ecorex/dreams`}`,
    "请优先围绕该项目文件夹读取、分析、写入文件。需要总结或沉淀时，只写入项目记忆，不写入全局 MEMORY.md。"
  ].join("\n");
}

function normalizeToolCall(tool: RuntimeToolCall | ToolCallDisclosure): ToolCallDisclosure {
  const fn = "function" in tool ? tool.function : undefined;
  return {
    name: tool.name || ("tool" in tool ? tool.tool : undefined) || fn?.name,
    arguments: tool.arguments ?? ("input" in tool ? tool.input : undefined) ?? fn?.arguments,
    result: typeof tool.result === "string" ? redactInternalPromptText(tool.result) : tool.result,
    status: tool.status,
    is_error: tool.is_error,
    execution_time: tool.execution_time
  };
}

function normalizeStep(step: RuntimeStep): AgentStepDisclosure {
  const type = String(step.type || "").toLowerCase();
  const content = redactInternalPromptText(step.content || step.text || step.thinking || "");
  if (type === "thinking" || type === "reasoning") {
    return { type: "thinking", content };
  }
  if (type === "tool" || type === "tool_start" || type === "tool_end") {
    return {
      type: "tool",
      name: step.name || step.tool,
      arguments: step.arguments ?? step.input,
      result: typeof step.result === "string" ? redactInternalPromptText(step.result) : step.result,
      status: step.status,
      is_error: step.is_error,
      execution_time: step.execution_time,
      running: type === "tool_start" || step.status === "running"
    };
  }
  if (type === "phase") {
    return { type: "phase", content };
  }
  if (type === "image" || type === "video" || type === "audio" || type === "file" || step.file_type) {
    const fileType = step.file_type === "image" || step.file_type === "video" || step.file_type === "audio"
      ? step.file_type
      : type === "image" || type === "video" || type === "audio"
        ? type
        : "file";
    return {
      type: "media",
      fileType,
      url: step.url || content || step.path,
      filePath: step.path,
      previewUrl: step.url,
      fileName: step.file_name
    };
  }
  return { type: "content", content };
}

function runtimeExtrasMediaSteps(item: RuntimeMessage): AgentStepDisclosure[] {
  const audio = item.extras?.audio;
  const audioUrl = typeof audio?.url === "string" ? audio.url : "";
  if (!audioUrl) return [];
  return [{
    type: "media",
    fileType: "audio",
    url: audioUrl,
    fileName: typeof audio?.kind === "string" ? audio.kind : undefined
  }];
}

function runtimeExtrasAttachments(item: RuntimeMessage): FileAttachment[] | undefined {
  const raw = item.extras?.attachments;
  if (!Array.isArray(raw)) return undefined;
  const attachments = raw
    .filter((entry): entry is Record<string, unknown> => Boolean(entry && typeof entry === "object"))
    .map((entry): FileAttachment | null => {
      const filePath = String(entry.file_path || entry.path || "").trim();
      if (!filePath) return null;
      const fileName = String(entry.file_name || entry.name || filePath.split(/[\\/]/).filter(Boolean).pop() || filePath);
      const rawType = String(entry.file_type || entry.type || "file");
      const fileType: FileAttachment["file_type"] = rawType === "image" || rawType === "video" || rawType === "audio" || rawType === "directory"
        ? rawType
        : "file";
      const attachment: FileAttachment = {
        file_path: filePath,
        file_name: fileName,
        file_type: fileType
      };
      if (typeof entry.preview_url === "string" && entry.preview_url) {
        attachment.preview_url = entry.preview_url;
      }
      return attachment;
    })
    .filter((entry): entry is FileAttachment => Boolean(entry));
  return attachments.length ? attachments : undefined;
}

function inferArtifactKind(rawKind: string, mimeType: string, source: string, hasLocalPath: boolean): AgentArtifact["kind"] {
  if (rawKind === "image" || rawKind === "video" || rawKind === "audio" || rawKind === "directory" || rawKind === "url" || rawKind === "diff") {
    return rawKind;
  }
  const lowerMime = mimeType.toLowerCase();
  const lowerSource = source.toLowerCase().split(/[?#]/)[0];
  if (lowerMime.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|svg)$/.test(lowerSource)) return "image";
  if (lowerMime.startsWith("video/") || /\.(mp4|webm|mov|m4v|mkv|avi)$/.test(lowerSource)) return "video";
  if (lowerMime.startsWith("audio/") || /\.(mp3|wav|ogg|m4a|aac|flac)$/.test(lowerSource)) return "audio";
  if (rawKind === "file" || hasLocalPath) return "file";
  return "url";
}

function normalizeArtifactEntry(entry: unknown, index: number, requestId?: string): AgentArtifact | null {
  if (!entry || typeof entry !== "object") return null;
  const raw = entry as Record<string, unknown>;
  const path = String(raw.path || raw.file_path || raw.filePath || "").trim();
  const url = String(raw.url || "").trim();
  const relativePath = String(raw.relativePath || raw.relative_path || "").trim();
  const mimeType = String(raw.mimeType || raw.mime_type || "").trim();
  const titleSource = String(raw.title || raw.file_name || raw.name || path || relativePath || url || "").trim();
  if (!path && !url && !relativePath && !titleSource) return null;
  const rawKind = String(raw.kind || raw.file_type || raw.type || "").toLowerCase();
  const kind = inferArtifactKind(rawKind, mimeType, path || relativePath || url || titleSource, Boolean(path || relativePath));
  const rawIntent = String(raw.intent || "").toLowerCase();
  const intent: AgentArtifact["intent"] = rawIntent === "changed-file" || rawIntent === "preview" ? rawIntent : "deliverable";
  const rawOperation = String(raw.operation || "").toLowerCase();
  const operation: AgentArtifact["operation"] = rawOperation === "modified" || rawOperation === "created" || rawOperation === "downloaded" || rawOperation === "deployed"
    ? rawOperation
    : "exported";
  const rawStatus = String(raw.status || "").toLowerCase();
  const status: AgentArtifact["status"] = rawStatus === "pending" || rawStatus === "failed" || rawStatus === "superseded" ? rawStatus : "ready";
  const id = String(raw.id || `${requestId || "artifact"}-${index}-${path || relativePath || url || titleSource}`).trim();
  const source = raw.source && typeof raw.source === "object" ? raw.source as Record<string, unknown> : {};
  const stats = raw.stats && typeof raw.stats === "object" ? raw.stats as Record<string, unknown> : {};
  return {
    id,
    requestId: String(raw.requestId || raw.request_id || requestId || "").trim() || undefined,
    kind,
    intent,
    operation,
    status,
    title: titleSource || "未命名产物",
    path: path || undefined,
    relativePath: relativePath || undefined,
    url: url || undefined,
    mimeType: mimeType || undefined,
    sizeBytes: typeof raw.sizeBytes === "number" ? raw.sizeBytes : typeof raw.size_bytes === "number" ? raw.size_bytes : undefined,
    previewUrl: String(raw.previewUrl || raw.preview_url || "").trim() || undefined,
    thumbnailUrl: String(raw.thumbnailUrl || raw.thumbnail_url || "").trim() || undefined,
    statusPath: String(raw.statusPath || raw.status_path || "").trim() || undefined,
    stats: Object.keys(stats).length ? {
      addedLines: typeof stats.addedLines === "number" ? stats.addedLines : typeof stats.added_lines === "number" ? stats.added_lines : undefined,
      removedLines: typeof stats.removedLines === "number" ? stats.removedLines : typeof stats.removed_lines === "number" ? stats.removed_lines : undefined,
      bytesWritten: typeof stats.bytesWritten === "number" ? stats.bytesWritten : typeof stats.bytes_written === "number" ? stats.bytes_written : undefined
    } : undefined,
    source: {
      toolCallId: String(source.toolCallId || source.tool_call_id || "").trim() || undefined,
      toolName: String(source.toolName || source.tool_name || "").trim() || undefined,
      activityId: String(source.activityId || source.activity_id || "").trim() || undefined,
      createdAt: typeof source.createdAt === "number" ? source.createdAt : typeof source.created_at === "number" ? source.created_at : undefined
    }
  };
}

function runtimeExtrasArtifacts(item: RuntimeMessage): AgentArtifact[] | undefined {
  const raw = item.extras?.artifacts;
  if (!Array.isArray(raw)) return undefined;
  const artifacts = raw
    .map((entry, index) => normalizeArtifactEntry(entry, index, item.request_id))
    .filter((entry): entry is AgentArtifact => Boolean(entry));
  return artifacts.length ? artifacts : undefined;
}

function mapRuntimeMessage(item: RuntimeMessage, sessionId: string, index: number, contextStartSeq = 0, turnSeq?: number): ChatItem {
  const steps = [
    ...(item.steps?.map(normalizeStep) || []),
    ...runtimeExtrasMediaSteps(item)
  ];
  const displaySeq = typeof item._seq === "number" ? item._seq : typeof item.seq === "number" ? item.seq : undefined;
  const contextSeq = item.role === "user" ? displaySeq : (typeof displaySeq === "number" ? displaySeq : turnSeq);
  return {
    id: `${sessionId}-${index}`,
    role: item.role === "user" ? "user" : "assistant",
    content: redactInternalPromptText(item.content || ""),
    createdAt: item.created_at ? new Date(item.created_at * 1000).toISOString() : new Date().toISOString(),
    reasoning: item.reasoning ? redactInternalPromptText(item.reasoning) : undefined,
    steps: steps.length ? steps : undefined,
    attachments: item.role === "user" ? runtimeExtrasAttachments(item) : undefined,
    artifacts: item.role === "assistant" ? runtimeExtrasArtifacts(item) : undefined,
    toolCalls: item.tool_calls?.map(normalizeToolCall),
    requestId: item.request_id,
    userSeq: item.user_seq ?? item._seq ?? item.seq,
    botSeq: item.role === "assistant" ? item.seq : undefined,
    contextExcluded: contextStartSeq > 0 && typeof contextSeq === "number" && contextSeq < contextStartSeq
  };
}

function mapRuntimeHistory(messages: RuntimeMessage[], sessionId: string, contextStartSeq = 0): ChatItem[] {
  let currentTurnSeq: number | undefined;
  return messages.map((item, index) => {
    const seq = typeof item._seq === "number" ? item._seq : typeof item.seq === "number" ? item.seq : undefined;
    if (item.role === "user" && typeof seq === "number") currentTurnSeq = seq;
    return mapRuntimeMessage(item, sessionId, index, contextStartSeq, currentTurnSeq);
  });
}

function toolEnabled(tools: RuntimeTool[] | undefined, name: string) {
  return Boolean((tools || []).some((tool) => tool.name === name));
}

function skillEnabled(skills: RuntimeSkill[] | undefined, name: string) {
  const skill = (skills || []).find((item) => item.name === name);
  return Boolean(skill && skill.enabled !== false);
}

type SkillMentionCategory = "creative" | "document" | "automation" | "developer" | "general" | "background";

type SkillDisplayRow = {
  key: string;
  name: string;
  displayName: string;
  display_name?: string;
  description?: string;
  source?: string;
  path?: string;
  enabled: boolean;
  installed: boolean;
  status?: string;
  origin?: string;
  policy?: string;
  toggleable: boolean;
  mentionable: boolean;
  category: SkillMentionCategory;
  categoryLabel: string;
  mentionHiddenReason?: string;
};

const SKILL_CATEGORY_LABELS: Record<SkillMentionCategory, string> = {
  creative: "创作",
  document: "文档",
  automation: "自动化",
  developer: "开发",
  general: "通用",
  background: "后台 / CLI"
};

type RawSkillDisplayRow = Omit<SkillDisplayRow, "mentionable" | "category" | "categoryLabel" | "mentionHiddenReason"> & {
  rawCategory?: string;
  rawMentionCategory?: string;
  primaryEnv?: string;
  userInvocable?: boolean;
  disableModelInvocation?: boolean;
  explicitMentionable?: boolean;
  explicitMentionHiddenReason?: string;
};

const SKILL_CATEGORY_ORDER: SkillMentionCategory[] = ["creative", "document", "automation", "developer", "general", "background"];

function normalizeSkillText(value: unknown) {
  return String(value || "").trim().toLowerCase();
}

function mapExplicitSkillCategory(value?: string): SkillMentionCategory | "" {
  const category = normalizeSkillText(value).replace(/[_\s]+/g, "-");
  if (!category) return "";
  if (["creative", "creation", "content", "media", "design"].includes(category)) return "creative";
  if (["document", "documents", "doc", "pdf", "office", "spreadsheet", "slides"].includes(category)) return "document";
  if (["automation", "browser", "computer-use", "workflow"].includes(category)) return "automation";
  if (["developer", "dev", "coding", "github", "figma", "macos"].includes(category)) return "developer";
  if (["background", "cli", "system", "internal", "tooling", "connector"].includes(category)) return "background";
  if (category === "skill" || category === "general") return "general";
  return "";
}

function isBackgroundCliSkill(row: RawSkillDisplayRow) {
  const name = normalizeSkillText(row.name || row.displayName);
  const pathText = normalizeSkillText(row.path);
  const primaryEnv = normalizeSkillText(row.primaryEnv);
  const description = normalizeSkillText(row.description);
  const sourceText = normalizeSkillText(`${row.source || ""} ${row.origin || ""}`);
  if (/^(?:lark|feishu)(?:[-_:]|$)/.test(name)) return true;
  if (/(?:^|[\\/])(?:lark|feishu)-[^\\/]+[\\/]skill\.md$/.test(pathText)) return true;
  if (/^(?:lark|feishu)_/.test(primaryEnv)) return true;
  if (description.includes("lark-cli") || sourceText.includes("lark-cli")) return true;
  if ((description.includes("飞书") || sourceText.includes("飞书")) && (description.includes("cli") || sourceText.includes("cli"))) return true;
  return false;
}

function isTestFixtureSkill(row: RawSkillDisplayRow) {
  const name = normalizeSkillText(row.name || row.displayName);
  const pathText = normalizeSkillText(row.path);
  return /^good-skill(?:-|$)/.test(name) || pathText.includes("skill-format-check");
}

function skillMentionHiddenReason(value?: string) {
  const reason = normalizeSkillText(value);
  if (!reason) return "";
  if (reason.includes("lark") || reason.includes("feishu")) return "由飞书/Lark CLI 自动触发";
  if (reason.includes("test")) return "测试样例";
  if (reason.includes("background") || reason.includes("disable") || reason.includes("model")) return "后台触发";
  return value || "";
}

function inferSkillCategory(row: RawSkillDisplayRow): SkillMentionCategory {
  const mentionCategory = mapExplicitSkillCategory(row.rawMentionCategory);
  if (mentionCategory) return mentionCategory;
  const explicit = mapExplicitSkillCategory(row.rawCategory);
  if (explicit) return explicit;
  const text = [
    row.name,
    row.displayName,
    row.description,
    row.source,
    row.origin,
    row.path
  ].map(normalizeSkillText).join(" ");
  if (/(xiaohongshu|image|design|figma|hallmark|remotion|presentation|video|creative|生成|设计)/.test(text)) return "creative";
  if (/(document|documents|pdf|spreadsheet|slides|docx|pptx|xlsx|office|文档|表格|幻灯片)/.test(text)) return "document";
  if (/(browser|chrome|computer-use|automation|workflow|calendar|attendance|自动化|浏览器)/.test(text)) return "automation";
  if (/(github|build-macos|openai|plugin|skill|codex|cli|developer|swift|xcode|开发)/.test(text)) return "developer";
  return "general";
}

function finalizeSkillDisplayRow(row: RawSkillDisplayRow): SkillDisplayRow {
  const category = inferSkillCategory(row);
  let mentionable = category !== "background";
  let mentionHiddenReason = "";
  const explicitHiddenReason = skillMentionHiddenReason(row.explicitMentionHiddenReason);

  if (row.explicitMentionable === false || row.userInvocable === false || row.disableModelInvocation) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "后台触发";
  }
  if (isBackgroundCliSkill(row)) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "由飞书/Lark CLI 自动触发";
  }
  if (isTestFixtureSkill(row)) {
    mentionable = false;
    mentionHiddenReason = explicitHiddenReason || "测试样例";
  }

  const finalCategory: SkillMentionCategory = mentionable ? category : "background";
  return {
    ...row,
    mentionable,
    category: finalCategory,
    categoryLabel: SKILL_CATEGORY_LABELS[finalCategory],
    mentionHiddenReason: mentionable ? undefined : mentionHiddenReason || "后台触发"
  };
}

function skillNameFromExtension(extension: RuntimeExtension) {
  const id = String(extension.id || "");
  return id.startsWith("skill:") ? id.slice("skill:".length) : "";
}

function extensionSkillEnabled(snapshot: RuntimeSnapshot, name: string) {
  const extension = (snapshot.extensions || []).find((item) => skillNameFromExtension(item) === name);
  if (extension) return extension.enabled !== false && extension.installed !== false;
  return skillEnabled(snapshot.skills, name);
}

function buildSkillDisplayRows(snapshot: RuntimeSnapshot): SkillDisplayRow[] {
  const legacyByName = new Map((snapshot.skills || []).map((skill) => [skill.name || skill.display_name || "", skill]));
  const rows: SkillDisplayRow[] = [];
  const seen = new Set<string>();
  for (const extension of snapshot.extensions || []) {
    if (extension.type !== "builtin_skill" && extension.type !== "user_skill") continue;
    const name = skillNameFromExtension(extension) || extension.displayName || extension.id;
    if (!name) continue;
    const legacy = legacyByName.get(name);
    seen.add(name);
    rows.push(finalizeSkillDisplayRow({
      key: extension.id || `skill:${name}`,
      name,
      displayName: extension.displayName || legacy?.display_name || name,
      display_name: extension.displayName || legacy?.display_name || name,
      description: extension.description || legacy?.description,
      source: extension.origin || legacy?.source,
      path: extension.sourcePath || legacy?.path,
      enabled: extension.enabled !== false && (legacy?.enabled ?? true) !== false,
      installed: extension.installed !== false,
      status: extension.status,
      origin: extension.origin,
      policy: extension.policy,
      toggleable: Boolean(legacy?.name),
      rawCategory: legacy?.category || extension.category,
      rawMentionCategory: legacy?.mention_category || extension.mention_category,
      primaryEnv: legacy?.primary_env || extension.primary_env,
      userInvocable: legacy?.user_invocable ?? extension.user_invocable,
      disableModelInvocation: legacy?.disable_model_invocation ?? extension.disable_model_invocation,
      explicitMentionable: legacy?.mentionable ?? extension.mentionable,
      explicitMentionHiddenReason: legacy?.mention_hidden_reason || extension.mention_hidden_reason
    }));
  }
  for (const skill of snapshot.skills || []) {
    const name = skill.name || skill.display_name || "";
    if (!name || seen.has(name)) continue;
    rows.push(finalizeSkillDisplayRow({
      key: `legacy:${name}`,
      name,
      displayName: skill.display_name || name,
      display_name: skill.display_name || name,
      description: skill.description,
      source: skill.source,
      path: skill.path,
      enabled: skill.enabled !== false,
      installed: true,
      status: skill.enabled === false ? "disabled" : "ready",
      toggleable: Boolean(skill.name),
      rawCategory: skill.category,
      rawMentionCategory: skill.mention_category,
      primaryEnv: skill.primary_env,
      userInvocable: skill.user_invocable,
      disableModelInvocation: skill.disable_model_invocation,
      explicitMentionable: skill.mentionable,
      explicitMentionHiddenReason: skill.mention_hidden_reason
    }));
  }
  return rows.sort((a, b) => {
    const categoryDiff = SKILL_CATEGORY_ORDER.indexOf(a.category) - SKILL_CATEGORY_ORDER.indexOf(b.category);
    if (categoryDiff) return categoryDiff;
    const originDiff = String(a.origin || a.source || "").localeCompare(String(b.origin || b.source || ""));
    if (originDiff) return originDiff;
    return a.displayName.localeCompare(b.displayName);
  });
}

function memoryFileName(file: MemoryFile) {
  return file.filename || file.name || "未命名记忆";
}

function memoryFileTime(file: MemoryFile) {
  return file.updated_at || file.updatedAt || "";
}

function AuthGate(props: { onLogin: (session: EnterpriseSession) => void; version?: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      props.onLogin(await enterpriseLogin(email, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <WindowBrand version={props.version} />
      <section className="auth-panel">
        <BrandMark />
        <h1>EcoreX</h1>
        <p>亦芯广告 AI Agent</p>
        <form onSubmit={submit}>
          <label>
            登录邮箱
            <input value={email} onChange={(event) => setEmail(event.target.value)} type="email" autoComplete="email" required />
          </label>
          <label>
            密码
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required />
          </label>
          {error && <div className="inline-error">{error}</div>}
          <button type="submit" disabled={busy}>{busy ? "正在登录" : "登录并进入"}</button>
        </form>
      </section>
    </main>
  );
}

export function App() {
  const bootSession = useMemo(() => pickBootSession(readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {})), []);
  const bootSessionProjects = useMemo(() => readStorage<SessionProjectMap>(SESSION_PROJECTS_STORAGE_KEY, {}), []);
  const [theme, setTheme] = useState<ThemeMode>(initialTheme);
  const [session, setSession] = useState<EnterpriseSession | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [runtimeSnapshot, setRuntimeSnapshot] = useState(initialRuntime);
  const [sidecarStatus, setSidecarStatus] = useState(initialSidecar);
  const [updateStatus, setUpdateStatus] = useState<DesktopUpdateStatus>(initialUpdateStatus);
  const [quotaSnapshot, setQuotaSnapshot] = useState<UsageQuota | null>(null);
  const [activeSessionId, setActiveSessionId] = useState(bootSession?.id || `ecorex-${Date.now()}`);
  const [activeSessionTitle, setActiveSessionTitle] = useState(bootSession?.state.title || "新对话");
  const [searchQuery, setSearchQuery] = useState("");
  const [messages, setMessages] = useState<ChatItem[]>(() => normalizePausedMessages(bootSession?.state.messages || []));
  const [composerText, setComposerText] = useState(bootSession?.state.composerText || "");
  const [attachments, setAttachments] = useState<FileAttachment[]>(bootSession?.state.attachments || []);
  const [composerDragActive, setComposerDragActive] = useState(false);
  const [, setActiveRequestId] = useState("");
  const [approval, setApproval] = useState<ApprovalState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [runCenterOpen, setRunCenterOpen] = useState(false);
  const [releaseNotesOpen, setReleaseNotesOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [previewFile, setPreviewFile] = useState<FileAttachment | null>(null);
  const [previewZoom, setPreviewZoom] = useState(1);
  const [packs, setPacks] = useState<CapabilityPack[]>([]);
  const [permissionState, setPermissionState] = useState<PermissionState | null>(null);
  const [projects, setProjects] = useState<ProjectFolder[]>(() => readStorage<ProjectFolder[]>(PROJECTS_STORAGE_KEY, []));
  const [sessionProjects, setSessionProjects] = useState<SessionProjectMap>(() => bootSessionProjects);
  const [sessionTitles, setSessionTitles] = useState<StringMap>(() => readStorage<StringMap>(SESSION_TITLES_STORAGE_KEY, {}));
  const [lockedSessionTitles, setLockedSessionTitles] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(LOCKED_SESSION_TITLES_STORAGE_KEY, {}));
  const [pinnedSessions, setPinnedSessions] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_SESSIONS_STORAGE_KEY, {}));
  const [pinnedProjects, setPinnedProjects] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_PROJECTS_STORAGE_KEY, {}));
  const [unreadSessionIds, setUnreadSessionIds] = useState<StringBoolMap>({});
  const [sessionUiState, setSessionUiState] = useState<Record<string, SessionUiState>>(() => readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {}));
  const [enabledCapabilityPacks, setEnabledCapabilityPacks] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(CAPABILITY_ENABLED_STORAGE_KEY, {}));
  const [sessionRequestIds, setSessionRequestIds] = useState<StringMap>({});
  const [locallyCompletedRequestIds, setLocallyCompletedRequestIds] = useState<StringBoolMap>({});
  const [activeProjectId, setActiveProjectId] = useState<string | null>(bootSession?.id ? bootSessionProjects[bootSession.id] || null : null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("account");
  const [projectMenu, setProjectMenu] = useState<ProjectContextMenu>(null);
  const [chatFileMenu, setChatFileMenu] = useState<ChatFileContextMenu>(null);
  const [sidebarCollapse, setSidebarCollapse] = useState<SidebarCollapseState>(() => initialSidebarCollapseState());
  const [installingPackIds, setInstallingPackIds] = useState<StringBoolMap>({});
  const [installNotice, setInstallNotice] = useState<InstallNotice>(null);
  const [memoryFiles, setMemoryFiles] = useState<MemoryFile[]>([]);
  const [dreamFiles, setDreamFiles] = useState<MemoryFile[]>([]);
  const [toast, setToast] = useState("");
  const [copiedMessageId, setCopiedMessageId] = useState("");
  const [passwordDraft, setPasswordDraft] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const composerDragDepth = useRef(0);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const streamCleanup = useRef<null | (() => void)>(null);
  const streamCleanups = useRef<Record<string, () => void>>({});
  const streamCleanupRequestIds = useRef<StringMap>({});
  const streamDeltaBuffers = useRef<Record<string, { sessionId: string; assistantId: string; requestId: string; text: string; timer: number | null }>>({});
  const sessionRequestIdsRef = useRef<StringMap>({});
  const latestSendAttemptRef = useRef<Record<string, string>>({});
  const installWatchers = useRef<Record<string, number>>({});
  const queuedInstallRef = useRef<Array<{ pack: CapabilityPack; onInstalled?: () => void; sessionId: string }>>([]);
  const activeSessionIdRef = useRef(activeSessionId);
  const messagesRef = useRef(messages);
  const sessionSwitchSeq = useRef(0);
  const autoScrollRef = useRef(true);
  const appBootMs = useRef(Date.now());
  const completedRequestIds = useRef<StringBoolMap>({});
  const completedRequestCleanupTimers = useRef<Record<string, number>>({});
  const locallyCompletedRequestIdsRef = useRef<StringBoolMap>({});
  const lockedSessionTitlesRef = useRef<StringBoolMap>(lockedSessionTitles);
  const handledSnapshotTerminalRequestsRef = useRef<StringBoolMap>({});
  const postDoneStreamCloseTimers = useRef<Record<string, number>>({});
  const postDoneTailArtifactsRef = useRef<Record<string, AgentArtifact[]>>({});
  const streamRetryCounts = useRef<Record<string, number>>({});
  const streamRequestStates = useRef<Record<string, StreamRequestState>>({});
  const streamStallTimers = useRef<Record<string, number>>({});
  const sendGenerationRef = useRef<Record<string, number>>({});
  const preflightAbortRef = useRef<Record<string, AbortController>>({});
  const phaseTimersRef = useRef<Record<string, number[]>>({});
  const historyRecoveryTimersRef = useRef<Record<string, number[]>>({});
  const preloadDone = useRef(false);
  const bootHistoryRefreshDone = useRef(false);
  const releaseNotesDismissedVersion = useRef("");
  const uiStateLocalSyncTimer = useRef<number | null>(null);
  const pendingUiStateStorage = useRef<Record<string, SessionUiState> | null>(null);
  const uiStateSyncTimer = useRef<number | null>(null);

  function beginSessionPreflight(sessionId: string) {
    const generation = (sendGenerationRef.current[sessionId] || 0) + 1;
    sendGenerationRef.current = { ...sendGenerationRef.current, [sessionId]: generation };
    preflightAbortRef.current[sessionId]?.abort();
    const controller = new AbortController();
    preflightAbortRef.current = { ...preflightAbortRef.current, [sessionId]: controller };
    return { generation, controller };
  }

  function isSessionPreflightCurrent(sessionId: string, generation: number, controller: AbortController) {
    return sendGenerationRef.current[sessionId] === generation && !controller.signal.aborted;
  }

  function clearSessionPreflight(sessionId: string, controller: AbortController) {
    if (preflightAbortRef.current[sessionId] !== controller) return;
    const next = { ...preflightAbortRef.current };
    delete next[sessionId];
    preflightAbortRef.current = next;
  }

  function abortSessionPreflight(sessionId: string) {
    sendGenerationRef.current = { ...sendGenerationRef.current, [sessionId]: (sendGenerationRef.current[sessionId] || 0) + 1 };
    preflightAbortRef.current[sessionId]?.abort();
    const next = { ...preflightAbortRef.current };
    delete next[sessionId];
    preflightAbortRef.current = next;
  }

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem("ecorex-theme", theme);
  }, [theme]);

  useEffect(() => {
    writeStorage(PROJECTS_STORAGE_KEY, projects);
  }, [projects]);

  useEffect(() => {
    writeStorage(SESSION_PROJECTS_STORAGE_KEY, sessionProjects);
  }, [sessionProjects]);

  useEffect(() => {
    writeStorage(SESSION_TITLES_STORAGE_KEY, sessionTitles);
  }, [sessionTitles]);

  useEffect(() => {
    writeStorage(LOCKED_SESSION_TITLES_STORAGE_KEY, lockedSessionTitles);
    lockedSessionTitlesRef.current = lockedSessionTitles;
  }, [lockedSessionTitles]);

  useEffect(() => {
    writeStorage(PINNED_SESSIONS_STORAGE_KEY, pinnedSessions);
  }, [pinnedSessions]);

  useEffect(() => {
    writeStorage(PINNED_PROJECTS_STORAGE_KEY, pinnedProjects);
  }, [pinnedProjects]);

  useEffect(() => {
    writeStorage(CAPABILITY_ENABLED_STORAGE_KEY, enabledCapabilityPacks);
  }, [enabledCapabilityPacks]);

  useEffect(() => {
    writeStorage(SIDEBAR_COLLAPSE_STORAGE_KEY, sidebarCollapse);
  }, [sidebarCollapse]);

  useEffect(() => {
    const projectSyncedState = Object.fromEntries(
      Object.entries(sessionUiState).map(([sessionId, state]) => [
        sessionId,
        { ...state, projectId: sessionProjects[sessionId] || null }
      ])
    );
    const pruned = pruneSessionUiState(projectSyncedState);
    pendingUiStateStorage.current = pruned;
    if (uiStateLocalSyncTimer.current) {
      window.clearTimeout(uiStateLocalSyncTimer.current);
    }
    const hasLiveState = hasLiveSessionUiState(pruned);
    uiStateLocalSyncTimer.current = window.setTimeout(() => {
      const pending = pendingUiStateStorage.current;
      if (pending) {
        writeStorage(SESSION_UI_STORAGE_KEY, pending);
        pendingUiStateStorage.current = null;
      }
      uiStateLocalSyncTimer.current = null;
    }, hasLiveState ? 1800 : 120);
    if (sidecarStatus.state !== "running") return;
    if (uiStateSyncTimer.current) {
      window.clearTimeout(uiStateSyncTimer.current);
    }
    const runtimeActiveProjectId = sessionProjects[activeSessionIdRef.current] || null;
    uiStateSyncTimer.current = window.setTimeout(() => {
      void saveRuntimeUiState({
        version: 1,
        lastActiveSessionId: activeSessionIdRef.current,
        activeProjectId: runtimeActiveProjectId,
        sessionUiState: pruned,
        savedAt: new Date().toISOString()
      }).catch(() => undefined);
      uiStateSyncTimer.current = null;
    }, hasLiveState ? 2500 : 350);
  }, [sessionUiState, sessionProjects, sidecarStatus.state]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
    window.localStorage.setItem(LAST_ACTIVE_SESSION_STORAGE_KEY, activeSessionId);
  }, [activeSessionId]);

  useEffect(() => {
    locallyCompletedRequestIdsRef.current = locallyCompletedRequestIds;
  }, [locallyCompletedRequestIds]);

  useEffect(() => {
    const projectId = sessionProjects[activeSessionId] || null;
    setActiveProjectId((current) => current === projectId ? current : projectId);
  }, [activeSessionId, sessionProjects]);

  useEffect(() => {
    sessionRequestIdsRef.current = sessionRequestIds;
  }, [sessionRequestIds]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    const notes = runtimeSnapshot.releaseNotes;
    if (runtimeSnapshot.status !== "ready" || !notes?.version) return;
    if (releaseNotesDismissedVersion.current === notes.version) return;
    try {
      if (window.localStorage.getItem(RELEASE_NOTES_SEEN_STORAGE_KEY) === notes.version) return;
    } catch {
      // Showing the notes is still useful when storage is unavailable.
    }
    setReleaseNotesOpen(true);
  }, [runtimeSnapshot.status, runtimeSnapshot.releaseNotes?.version]);

  useEffect(() => {
    const projectId = sessionProjects[activeSessionId] || null;
    setSessionUiState((current) => ({
      ...current,
      [activeSessionId]: {
        ...(current[activeSessionId] || {}),
        title: activeSessionTitle,
        projectId,
        messages,
        composerText,
        attachments,
        contextStartSeq: current[activeSessionId]?.contextStartSeq,
        lastActivityAt: latestMessageMs(messages) || current[activeSessionId]?.lastActivityAt || Date.now()
      }
    }));
  }, [activeSessionId, activeSessionTitle, sessionProjects, messages, composerText, attachments]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(syncComposerHeight);
    return () => window.cancelAnimationFrame(frame);
  }, [composerText, activeSessionId]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (autoScrollRef.current) {
        scrollToLatest(false);
      } else {
        updateJumpLatestState();
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, activeSessionId]);

  useEffect(() => {
    if (sidecarStatus.state !== "running") return;
    const liveMessage = messages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (!liveMessage?.requestId) return;
    attachMessageStream(activeSessionId, liveMessage.id, liveMessage.requestId);
  }, [sidecarStatus.state, sidecarStatus.webPort, activeSessionId, messages]);

  useEffect(() => {
    if (bootHistoryRefreshDone.current) return;
    if (sidecarStatus.state !== "running") return;
    if (!bootSession?.id) return;
    const liveMessage = messages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (liveMessage?.requestId) return;
    bootHistoryRefreshDone.current = true;
    void refreshSessionFromHistory(bootSession.id);
  }, [sidecarStatus.state, bootSession?.id, messages]);

  useEffect(() => {
    getEnterpriseSession()
      .then((existing) => setSession(existing))
      .catch(() => setSession(null))
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    const unsubscribe = window.ecorexDesktop?.onSidecarStatus?.((status) => setSidecarStatus(status));
    const unsubscribeUpdate = window.ecorexDesktop?.onUpdateStatus?.((status) => setUpdateStatus(status));
    window.ecorexDesktop?.getSidecarStatus?.().then((status) => setSidecarStatus(status)).catch(() => undefined);
    window.ecorexDesktop?.getUpdateStatus?.().then((status) => setUpdateStatus(status)).catch(() => undefined);
    return () => {
      streamCleanup.current?.();
      Object.values(streamCleanups.current).forEach((cleanup) => cleanup());
      streamCleanup.current = null;
      streamCleanups.current = {};
      streamCleanupRequestIds.current = {};
      Object.values(streamDeltaBuffers.current).forEach((buffer) => {
        if (buffer.timer !== null) window.clearTimeout(buffer.timer);
      });
      streamDeltaBuffers.current = {};
      Object.values(postDoneStreamCloseTimers.current).forEach((timer) => window.clearTimeout(timer));
      postDoneStreamCloseTimers.current = {};
      Object.values(completedRequestCleanupTimers.current).forEach((timer) => window.clearTimeout(timer));
      completedRequestCleanupTimers.current = {};
      Object.values(historyRecoveryTimersRef.current).forEach((timers) => {
        timers.forEach((timer) => window.clearTimeout(timer));
      });
      historyRecoveryTimersRef.current = {};
      Object.values(installWatchers.current).forEach((timer) => window.clearInterval(timer));
      installWatchers.current = {};
      if (uiStateLocalSyncTimer.current) {
        window.clearTimeout(uiStateLocalSyncTimer.current);
        uiStateLocalSyncTimer.current = null;
      }
      if (pendingUiStateStorage.current) {
        writeStorage(SESSION_UI_STORAGE_KEY, pendingUiStateStorage.current);
        pendingUiStateStorage.current = null;
      }
      if (uiStateSyncTimer.current) {
        window.clearTimeout(uiStateSyncTimer.current);
        uiStateSyncTimer.current = null;
      }
      unsubscribe?.();
      unsubscribeUpdate?.();
    };
  }, []);

  useEffect(() => {
    if (preloadDone.current) return;
    if (sidecarStatus.state !== "running") return;
    preloadDone.current = true;
    void (async () => {
      const nextPacks = await listCapabilityPacks().catch(() => []);
      setPacks(nextPacks);
      const snapshot = await loadRuntimeSnapshot().catch(() => null);
      if (snapshot) {
        let nextSnapshot = snapshot;
        if (!window.localStorage.getItem(SKILL_DEFAULTS_STORAGE_KEY)) {
          const changed = await enableDefaultSkills(snapshot.skills || []).catch(() => 0);
          window.localStorage.setItem(SKILL_DEFAULTS_STORAGE_KEY, "1");
          if (changed) {
            nextSnapshot = await loadRuntimeSnapshot().catch(() => snapshot);
          }
        }
        setRuntimeSnapshot(nextSnapshot);
      }
    })();
  }, [sidecarStatus.state]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    async function refresh() {
      const [snapshot, nextPacks, nextPermissions, nextMemoryFiles, nextDreamFiles, quota] = await Promise.all([
        loadRuntimeSnapshot(),
        listCapabilityPacks(),
        loadPermissionState(),
        loadMemoryFiles("memory"),
        loadMemoryFiles("dream"),
        checkEnterpriseQuota(0).catch(() => null)
      ]);
      if (!cancelled) {
        setRuntimeSnapshot(snapshot);
        setPacks(nextPacks);
        setPermissionState(nextPermissions);
        setMemoryFiles(nextMemoryFiles);
        setDreamFiles(nextDreamFiles);
        if (quota?.quota) setQuotaSnapshot(quota.quota);
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [session]);

  useEffect(() => {
    if (!packs.length) return;
    const terminal = new Set(
      packs
        .filter((pack) => pack.installed || pack.state === "failed")
        .map((pack) => pack.id)
    );
    if (!terminal.size) return;
    setInstallingPackIds((current) => {
      let changed = false;
      const next = { ...current };
      for (const id of terminal) {
        if (next[id]) {
          delete next[id];
          changed = true;
        }
      }
      return changed ? next : current;
    });
    setInstallNotice((current) => {
      if (!current?.packId || current.dismissed || !terminal.has(current.packId)) return current;
      return null;
    });
  }, [packs]);

  useEffect(() => {
    if (!session || !window.ecorexDesktop?.checkForUpdates) return;
    const timer = window.setTimeout(() => {
      void handleCheckForUpdates();
    }, 7000);
    return () => window.clearTimeout(timer);
  }, [session]);

  useEffect(() => {
    if (!chatFileMenu) return undefined;
    const close = () => setChatFileMenu(null);
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [chatFileMenu]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    if (runtimeSnapshot.activeRequestsStatus === "unavailable") return;
    const activeRequestIds = new Set(
      (runtimeSnapshot.activeRequests || [])
        .filter(isPrimaryChatActiveRequest)
        .map((request) => request.request_id ? String(request.request_id) : "")
        .filter((requestId) => !locallyCompletedRequestIds[requestId])
        .filter(Boolean)
    );
    const staleSessionIds = new Set(
      (runtimeSnapshot.staleLocks || [])
        .filter((lock) => lock.removed || lock.dead_owner || lock.stale)
        .map((lock) => lock.session_id ? String(lock.session_id) : "")
        .filter(Boolean)
    );
    const nowMs = Date.now();
    const bootMs = appBootMs.current;
    const nextState: Record<string, SessionUiState> = {};
    const settledSessionIds = new Set<string>();
    let changed = false;
    for (const [sessionId, state] of Object.entries(sessionUiState)) {
      const normalized = normalizePausedMessages(state.messages || [], {
        sessionId,
        activeRequestIds,
        staleSessionIds,
        nowMs,
        inactiveRequestGraceMs: (state.messages || []).some((message) => (
          message.pending
          && message.requestId
          && message.createdAt
          && new Date(message.createdAt).getTime() < bootMs
        )) ? 2_000 : 45_000
      });
      if (normalized !== state.messages) {
        changed = true;
        settledSessionIds.add(sessionId);
        nextState[sessionId] = { ...state, messages: normalized };
      } else {
        nextState[sessionId] = state;
      }
    }
    if (!changed) return;
    setSessionUiState(nextState);
    const activeId = activeSessionIdRef.current;
    if (activeId && settledSessionIds.has(activeId)) {
      const activeState = nextState[activeId];
      if (activeState) setMessages(activeState.messages);
    }
    settledSessionIds.forEach((sessionId) => {
      finishSessionRequest(sessionId);
      void refreshSessionFromHistory(sessionId);
    });
  }, [runtimeSnapshot, sessionUiState, locallyCompletedRequestIds]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    (runtimeSnapshot.recentTerminalRequests || [])
      .filter(isPrimaryChatTerminalRequest)
      .forEach((request) => settleTerminalSnapshotRequest(request));
  }, [runtimeSnapshot.status, runtimeSnapshot.recentTerminalRequests, sessionUiState]);

  useEffect(() => {
    if (runtimeSnapshot.status !== "ready") return;
    const lockedFromRuntime: StringBoolMap = {};
    (runtimeSnapshot.sessions || []).forEach((session, index) => {
      const id = session.session_id || session.id || `runtime-${index}`;
      if (!id) return;
      if (session.title_locked || session.titleLocked) lockedFromRuntime[id] = true;
    });
    if (Object.keys(lockedFromRuntime).length === 0) return;
    setLockedSessionTitles((current) => {
      const changed = Object.keys(lockedFromRuntime).some((sessionId) => !current[sessionId]);
      return changed ? { ...current, ...lockedFromRuntime } : current;
    });
  }, [runtimeSnapshot.status, runtimeSnapshot.sessions]);

  const allSessions = useMemo(() => (
    mapSessions(runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, sessionUiState, locallyCompletedRequestIds)
  ), [runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, sessionUiState, locallyCompletedRequestIds]);
  const visibleSessions = useMemo(() => {
    const needle = searchQuery.trim().toLowerCase();
    return needle ? allSessions.filter((row) => `${row.title} ${row.detail}`.toLowerCase().includes(needle)) : allSessions;
  }, [allSessions, searchQuery]);
  const runCenterRequests = useMemo(() => {
    const seen = new Set<string>();
    return [
      ...(runtimeSnapshot.activeRequests || []),
      ...(runtimeSnapshot.recentTerminalRequests || [])
    ].filter((request) => {
      if (!isRunCenterVisibleRequest(request)) return false;
      const key = String(request.request_id || `${request.session_id || ""}-${request.run_type || request.source || ""}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [runtimeSnapshot.activeRequests, runtimeSnapshot.recentTerminalRequests]);
  const runCenterStaleLocks = useMemo(() => (
    (runtimeSnapshot.staleLocks || [])
      .filter((lock) => lock.removed || lock.dead_owner || lock.stale)
  ), [runtimeSnapshot.staleLocks]);
  const runCenterStats = useMemo(() => {
    const cancelling = runCenterRequests.filter((request) => ["cancelling", "cancelled"].includes(runCenterState(request))).length;
    const failed = runCenterRequests.filter(isRunCenterFailedRequest).length;
    return {
      running: runCenterRequests.length - cancelling - failed,
      cancelling,
      failed,
      stale: runCenterStaleLocks.length
    };
  }, [runCenterRequests, runCenterStaleLocks]);
  const runCenterNavCount = runCenterRequests.length + runCenterStaleLocks.length;
  const runCenterDevVisible = useMemo(() => {
    try {
      const params = new URLSearchParams(window.location.search || "");
      return window.localStorage.getItem(RUN_CENTER_DEV_GATE_STORAGE_KEY) === "1" || params.get("runCenter") === "1";
    } catch {
      return false;
    }
  }, []);

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) || null,
    [projects, activeProjectId]
  );

  const projectPathForSession = (sessionId: string) => {
    const projectId = sessionProjects[sessionId] || null;
    if (!projectId) return "";
    return projects.find((project) => project.id === projectId)?.path || "";
  };

  const resolveArtifactPathForSession = (sessionId: string, filePath: string) => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || isRuntimePreviewPath(raw) || isLocalAbsolutePath(raw) || /^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw;
    const projectPath = projectPathForSession(sessionId);
    return projectPath ? joinLocalPath(projectPath, raw) : raw;
  };

  const resolveArtifactPath = (filePath: string) => {
    return resolveArtifactPathForSession(activeSessionIdRef.current, filePath);
  };
  const statArtifactPath = async (filePath: string, sessionId = activeSessionIdRef.current): Promise<LocalPathStat> => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || isRuntimePreviewPath(raw) || /^https?:\/\//i.test(raw)) {
      return { path: raw, exists: Boolean(raw), status: raw ? "remote" : "error" };
    }
    const resolvedPath = resolveArtifactPathForSession(sessionId, raw);
    return statLocalPath(resolvedPath);
  };
  const readArtifactStatusJson = async (filePath: string, sessionId = activeSessionIdRef.current): Promise<LocalJsonResult> => {
    const raw = normalizeLocalSource(filePath);
    if (!raw || /^https?:\/\//i.test(raw)) {
      return { path: raw, status: "error", message: raw ? "remote status JSON is not supported" : "path is required" };
    }
    const resolvedPath = resolveArtifactPathForSession(sessionId, raw);
    return readLocalJson(resolvedPath);
  };
  const attachmentPreviewUrl = (file: FileAttachment) => {
    if (file.previewDataUrl) return file.previewDataUrl;
    if (file.preview_url) return filePreviewUrl(file.preview_url, sidecarStatus.webPort);
    if (!isImageAttachment(file)) return "";
    return filePreviewUrl(file.file_path, sidecarStatus.webPort);
  };
  const sortedProjects = useMemo(
    () => [...projects].sort((a, b) => Number(Boolean(pinnedProjects[b.id] || b.pinned)) - Number(Boolean(pinnedProjects[a.id] || a.pinned))),
    [projects, pinnedProjects]
  );
  const projectSessions = visibleSessions.filter((row) => Boolean(row.projectId));
  const generalSessions = visibleSessions.filter((row) => !row.projectId);
  const projectSessionGroups = useMemo(
    () => sortedProjects.map((project) => ({
      project,
      sessions: projectSessions.filter((row) => row.projectId === project.id)
    })),
    [sortedProjects, projectSessions]
  );
  const selectOrCreateProjectSession = (project: ProjectFolder) => {
    const existing = allSessions.find((row) => row.projectId === project.id);
    if (existing) {
      void selectSession(existing);
      return;
    }
    startNewSession(project);
  };
  const currentModelName = displayModelName(runtimeSnapshot.currentModel);
  const appVersion = runtimeSnapshot.version || runtimeSnapshot.releaseNotes?.version || updateStatus.currentVersion;
  const skillDisplayRows = useMemo(() => buildSkillDisplayRows(runtimeSnapshot), [runtimeSnapshot]);
  const mentionableSkillRows = useMemo(() => skillDisplayRows.filter((skill) => skill.mentionable), [skillDisplayRows]);
  const backgroundSkillRows = useMemo(() => skillDisplayRows.filter((skill) => !skill.mentionable), [skillDisplayRows]);
  const skillMentionCandidates = useMemo(
    () => mentionableSkillRows.filter((skill) => skill.enabled && skill.installed),
    [mentionableSkillRows]
  );
  const mentionMatch = /@([\w\u4e00-\u9fa5-]*)$/.exec(composerText);
  const skillMentionNeedle = mentionMatch ? mentionMatch[1].toLowerCase() : "";
  const skillMatchesNeedle = (skill: SkillDisplayRow) => {
    const haystack = [
      skill.displayName,
      skill.name,
      skill.description,
      skill.source,
      skill.path,
      skill.categoryLabel
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(skillMentionNeedle);
  };
  const skillMentions = mentionMatch
    ? skillMentionCandidates.filter(skillMatchesNeedle)
    : [];
  const hiddenSkillMentions = mentionMatch && skillMentionNeedle
    ? backgroundSkillRows.filter(skillMatchesNeedle)
    : [];
  const skillMentionGroups = SKILL_CATEGORY_ORDER
    .map((category) => ({
      category,
      label: SKILL_CATEGORY_LABELS[category],
      items: skillMentions.filter((skill) => skill.category === category)
    }))
    .filter((group) => group.items.length > 0);
  const skillMentionNoResults = Boolean(mentionMatch && mentionMatch[1] && !skillMentions.length);
  const activeSessionRequestId = sessionRequestIds[activeSessionId] || "";
  const dailyUsed = quotaNumber(quotaSnapshot, "dailyUsed");
  const weeklyUsed = quotaNumber(quotaSnapshot, "weeklyUsed");
  const dailyLimit = quotaNumber(quotaSnapshot, "dailyLimit");
  const weeklyLimit = quotaNumber(quotaSnapshot, "weeklyLimit");
  const contextUsed = estimateContextTokens(messages, composerText, attachments);
  const contextPercent = percentOf(contextUsed, CONTEXT_THRESHOLD_TOKENS);
  const tokenMeters = [
    {
      key: "daily",
      label: "今日",
      percent: percentOf(dailyUsed, dailyLimit),
      title: meterTitle("今日 token 用量", dailyUsed, dailyLimit)
    },
    {
      key: "weekly",
      label: "本周",
      percent: percentOf(weeklyUsed, weeklyLimit),
      title: meterTitle("本周 token 用量", weeklyUsed, weeklyLimit)
    }
  ];
  const contextMeter = {
    key: "context",
    label: "上下文",
    percent: contextPercent,
    title: meterTitle("当前会话上下文估算", contextUsed, CONTEXT_THRESHOLD_TOKENS)
  };

  useEffect(() => {
    const queue = queuedInstallRef.current;
    if (!queue.length) return;
    const nextIndex = queue.findIndex((item) => {
      if (sessionRequestIds[item.sessionId]) return false;
      const sessionMessages = item.sessionId === activeSessionId
        ? messages
        : sessionUiState[item.sessionId]?.messages || [];
      return !sessionMessages.some(isUiLiveAssistantMessage);
    });
    if (nextIndex < 0) return;
    const [queued] = queue.splice(nextIndex, 1);
    window.setTimeout(() => void handleInstallPack(queued.pack, queued.onInstalled, queued.sessionId), 0);
  }, [activeSessionId, activeSessionRequestId, messages, sessionRequestIds, sessionUiState]);

  useEffect(() => {
    setPreviewZoom(1);
  }, [previewFile?.file_path]);

  useEffect(() => {
    if (!composerDragActive) return;
    const reset = () => clearComposerDragState();
    window.addEventListener("dragend", reset);
    window.addEventListener("blur", reset);
    return () => {
      window.removeEventListener("dragend", reset);
      window.removeEventListener("blur", reset);
    };
  }, [composerDragActive]);

  useEffect(() => {
    const preventFileDropNavigation = (event: globalThis.DragEvent) => {
      const types = Array.from(event.dataTransfer?.types || []);
      if (!types.includes("Files")) return;
      event.preventDefault();
      if (event.type === "drop") {
        clearComposerDragState();
      }
    };
    window.addEventListener("dragover", preventFileDropNavigation);
    window.addEventListener("drop", preventFileDropNavigation);
    return () => {
      window.removeEventListener("dragover", preventFileDropNavigation);
      window.removeEventListener("drop", preventFileDropNavigation);
    };
  }, []);

  function capabilityPackEnabled(packId: string) {
    return enabledCapabilityPacks[packId] !== false;
  }

  function toggleCapabilityPack(pack: CapabilityPack, enabled: boolean) {
    setEnabledCapabilityPacks((current) => ({ ...current, [pack.id]: enabled }));
    setToast(enabled ? `${pack.name} 已启用` : `${pack.name} 已关闭`);
  }

  async function toggleRuntimeSkill(skill: Pick<SkillDisplayRow, "name" | "displayName">, enabled: boolean) {
    const name = skill.name || "";
    if (!name) return;
    try {
      await setSkillEnabled(name, enabled);
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast(enabled ? `${skill.displayName || name} 已启用` : `${skill.displayName || name} 已关闭`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Skill 开关失败");
    }
  }

  function insertSkillMention(skill: Pick<SkillDisplayRow, "name" | "displayName">) {
    const label = skill.displayName || skill.name || "";
    if (!label) return;
    setComposerText((current) => current.replace(/@([\w\u4e00-\u9fa5-]*)$/, `@${label} `));
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  function syncComposerHeight() {
    const textarea = composerRef.current;
    if (!textarea) return;
    const maxHeight = Number.parseFloat(window.getComputedStyle(textarea).maxHeight) || 168;
    textarea.style.height = "auto";
    const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
  }

  function updateJumpLatestState() {
    const list = messageListRef.current;
    if (!list) return;
    const state = getChatScrollState(list, CHAT_SCROLL_THRESHOLD_PX);
    autoScrollRef.current = state.autoScrollEnabled;
    setShowJumpLatest(state.showJumpLatest);
  }

  function scrollToLatest(forceAuto = true) {
    const list = messageListRef.current;
    if (!list) return;
    if (forceAuto) autoScrollRef.current = true;
    if (forceAuto) {
      const targetTop = Math.max(0, list.scrollHeight - list.clientHeight);
      list.scrollTo({ top: targetTop, behavior: "smooth" });
    } else {
      scrollElementToBottom(list, "auto");
    }
    setShowJumpLatest(false);
  }

  function focusComposerSoon() {
    const focus = () => {
      const textarea = composerRef.current;
      if (!textarea) return;
      textarea.focus({ preventScroll: true });
      const cursor = textarea.value.length;
      try {
        textarea.setSelectionRange(cursor, cursor);
      } catch {
        // IME/composition can briefly reject selection updates; focus is still useful.
      }
      syncComposerHeight();
    };
    focus();
    window.requestAnimationFrame(focus);
    window.requestAnimationFrame(() => window.requestAnimationFrame(focus));
    [40, 120, 300, 700].forEach((delay) => window.setTimeout(focus, delay));
  }

  function insertComposerNewline(textarea: HTMLTextAreaElement) {
    const start = textarea.selectionStart ?? composerText.length;
    const end = textarea.selectionEnd ?? start;
    const value = textarea.value;
    const next = `${value.slice(0, start)}\n${value.slice(end)}`;
    const nextCursor = start + 1;
    setComposerText(next);
    window.requestAnimationFrame(() => {
      const current = composerRef.current;
      if (!current) return;
      current.focus({ preventScroll: true });
      current.setSelectionRange(nextCursor, nextCursor);
      syncComposerHeight();
    });
  }

  function resumeRuntimeRequest(sessionId: string, requestId?: string, streamAvailable = true) {
    if (!requestId) return;
    const cachedMessages = sessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const existing = cachedMessages.find((message) => message.role === "assistant" && message.requestId === requestId);
    if (completedRequestIds.current[requestId] || locallyCompletedRequestIdsRef.current[requestId] || isTerminalAssistantMessage(existing)) {
      markRequestLocallyCompleted(requestId);
      clearSessionRequestState(sessionId, requestId);
      return;
    }
    const assistantId = existing?.id || `a-resume-${requestId}`;
    updateSessionMessages(sessionId, (current) => {
      const hasExisting = current.some((message) => message.id === assistantId || message.requestId === requestId);
      if (hasExisting) {
        return current.map((message) => (
          message.id === assistantId || message.requestId === requestId
            ? {
                ...message,
                id: message.id || assistantId,
                requestId,
                pending: true,
                paused: false,
                cancelled: false
              }
            : message
        ));
      }
      return [
        ...current,
        {
          id: assistantId,
          role: "assistant",
          content: "",
          pending: true,
          requestId,
          createdAt: new Date().toISOString(),
          steps: [{ type: "phase", content: "正在连接后台任务" }]
        }
      ];
    });
    sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [sessionId]: requestId };
    setSessionRequestIds((current) => ({ ...current, [sessionId]: requestId }));
    streamRetryCounts.current[sessionId] = 0;
    if (streamAvailable) {
      window.setTimeout(() => attachMessageStream(sessionId, assistantId, requestId), 0);
      scheduleHistoryRecovery(sessionId, requestId);
    } else {
      void refreshSessionFromHistory(sessionId).then((restored) => {
        if (!restored) {
          window.setTimeout(() => void refreshSessionFromHistory(sessionId), 3000);
        }
      });
    }
  }

  function restoreCachedSession(sessionId: string, activeRequestId?: string, streamAvailable = true) {
    const cached = sessionUiState[sessionId];
    if (!cached) return false;
    const projectId = sessionProjects[sessionId] || null;
    const nextMessages = normalizePausedMessages(cached.messages, {
      sessionId,
      activeRequestIds: activeRequestId ? new Set([activeRequestId]) : new Set(),
      inactiveRequestGraceMs: 45_000
    });
    setMessages(nextMessages);
    setComposerText(cached.composerText);
    setAttachments(cached.attachments);
    setActiveSessionTitle(sessionTitles[sessionId] || cached.title || "新对话");
    setActiveProjectId(projectId);
    setSessionUiState((current) => ({
      ...current,
      [sessionId]: {
        ...cached,
        projectId,
        messages: nextMessages
      }
    }));
    const liveMessage = nextMessages.find((message) => isLiveAssistantMessage(message) && message.requestId);
    if (liveMessage?.requestId && streamAvailable && (!activeRequestId || liveMessage.requestId === activeRequestId)) {
      setSessionRequestIds((current) => ({ ...current, [sessionId]: liveMessage.requestId || "" }));
      window.setTimeout(() => attachMessageStream(sessionId, liveMessage.id, liveMessage.requestId || ""), 0);
    } else if (activeRequestId) {
      resumeRuntimeRequest(sessionId, activeRequestId, streamAvailable);
    } else {
      void refreshSessionFromHistory(sessionId);
    }
    return true;
  }

  async function selectSession(row: SessionRow) {
    const switchSeq = ++sessionSwitchSeq.current;
    const nextProjectId = sessionProjects[row.id] || null;
    autoScrollRef.current = true;
    setShowJumpLatest(false);
    activeSessionIdRef.current = row.id;
    setActiveSessionId(row.id);
    setActiveSessionTitle(row.title);
    setActiveProjectId(nextProjectId);
    setPreviewFile(null);
    setApproval(null);
    clearSessionUnread(row.id);
    if (restoreCachedSession(row.id, row.requestId, row.streamAvailable !== false)) {
      focusComposerSoon();
      return;
    }
    updateSessionMessages(row.id, () => []);
    setSessionUiState((current) => ({
      ...current,
      [row.id]: {
        title: row.title,
        projectId: nextProjectId,
        messages: [],
        composerText: "",
        attachments: []
      }
    }));
    setMessages([]);
    setComposerText("");
    setAttachments([]);
    focusComposerSoon();
    try {
      const history = await loadSessionHistoryWithMeta(row.id);
      if (sessionSwitchSeq.current !== switchSeq || activeSessionIdRef.current !== row.id) {
        return;
      }
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, row.id, history.contextStartSeq));
      setSessionUiState((current) => ({
        ...current,
        [row.id]: {
          ...(current[row.id] || {
            title: row.title,
            composerText: "",
            attachments: []
          }),
          projectId: nextProjectId,
          messages: mapped,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(mapped) || current[row.id]?.lastActivityAt || row.activityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === row.id) {
        setMessages(mapped);
        focusComposerSoon();
      }
      if (row.requestId) {
        resumeRuntimeRequest(row.id, row.requestId, row.streamAvailable !== false);
      }
    } catch (error) {
      setToast(error instanceof Error ? error.message : "加载会话失败");
    }
  }

  function startNewSession(project?: ProjectFolder | null) {
    sessionSwitchSeq.current += 1;
    const id = project ? `ecorex-project-${project.id}-${Date.now()}` : `ecorex-${Date.now()}`;
    const title = project ? `${project.name} · 新会话` : "新对话";
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
    setActiveProjectId(project?.id || null);
    setActiveSessionTitle(title);
    setSessionTitles((current) => ({ ...current, [id]: title }));
    if (project) {
      setSessionProjects((current) => ({ ...current, [id]: project.id }));
    }
    setMessages([]);
    setAttachments([]);
    setComposerText("");
    setApproval(null);
    setSessionUiState((current) => ({
      ...current,
      [id]: {
        title,
        projectId: project?.id || null,
        messages: [],
        composerText: "",
        attachments: []
      }
    }));
    focusComposerSoon();
  }

  async function renameSession(row: SessionRow) {
    const nextTitle = window.prompt("重命名会话", row.title)?.trim();
    if (!nextTitle) return;
    if (nextTitle === row.title) {
      lockedSessionTitlesRef.current = { ...lockedSessionTitlesRef.current, [row.id]: true };
      setLockedSessionTitles((current) => ({ ...current, [row.id]: true }));
      setPinnedSessions((current) => ({ ...current, [row.id]: true }));
      setToast("会话标题已锁定");
      return;
    }
    try {
      setSessionTitles((current) => ({ ...current, [row.id]: nextTitle }));
      lockedSessionTitlesRef.current = { ...lockedSessionTitlesRef.current, [row.id]: true };
      setLockedSessionTitles((current) => ({ ...current, [row.id]: true }));
      setPinnedSessions((current) => ({ ...current, [row.id]: true }));
      const result = await renameRuntimeSession({ sessionId: row.id, title: nextTitle });
      if (result.status === "error") {
        throw new Error(result.message || "重命名失败");
      }
      if (row.id === activeSessionId) {
        setActiveSessionTitle(nextTitle);
      }
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast("会话已重命名");
    } catch (error) {
      if (row.id === activeSessionId) {
        setActiveSessionTitle(nextTitle);
      }
      setToast(error instanceof Error ? `仅更新本地标题：${error.message}` : "仅更新本地标题");
    }
  }

  async function removeSession(row: SessionRow) {
    if (!window.confirm(`删除会话「${row.title}」？该操作会清除这条会话记录。`)) return;
    try {
      const result = await deleteRuntimeSession(row.id);
      if (result.status === "error") {
        throw new Error(result.message || "删除失败");
      }
      setSessionProjects((current) => {
        const next = { ...current };
        delete next[row.id];
        return next;
      });
      setLockedSessionTitles((current) => {
        const next = { ...current };
        delete next[row.id];
        lockedSessionTitlesRef.current = next;
        return next;
      });
      setPinnedSessions((current) => {
        const next = { ...current };
        delete next[row.id];
        return next;
      });
      if (row.id === activeSessionId) {
        startNewSession(null);
      }
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast("会话已删除");
    } catch (error) {
      setApproval({ type: "error", title: "会话删除失败", message: error instanceof Error ? error.message : "请稍后重试。" });
    }
  }

  async function addProject() {
    try {
      const project = await chooseProjectFolder();
      if (!project) return;
      let nextProject = project;
      setProjects((current) => {
        const existing = current.find((item) => item.path === project.path);
        if (existing) {
          nextProject = { ...existing, updatedAt: project.updatedAt };
          return current.map((item) => item.path === project.path ? nextProject : item);
        }
        return [project, ...current];
      });
      window.setTimeout(() => startNewSession(nextProject), 0);
      setToast("项目已添加");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "添加项目失败");
    }
  }

  function togglePinSession(row: SessionRow) {
    setPinnedSessions((current) => ({ ...current, [row.id]: !current[row.id] }));
  }

  function togglePinProject(project: ProjectFolder) {
    const nextPinned = !Boolean(pinnedProjects[project.id] || project.pinned);
    setPinnedProjects((current) => ({ ...current, [project.id]: nextPinned }));
    setProjects((current) => current.map((item) => item.id === project.id ? { ...item, pinned: nextPinned } : item));
  }

  function renameProject(project: ProjectFolder) {
    const nextName = window.prompt("重命名项目", project.name)?.trim();
    if (!nextName || nextName === project.name) return;
    setProjects((current) => current.map((item) => item.id === project.id ? { ...item, name: nextName, updatedAt: new Date().toISOString() } : item));
    setProjectMenu(null);
  }

  function deleteProject(project: ProjectFolder) {
    if (!window.confirm(`删除项目「${project.name}」？项目文件夹不会被删除，已有项目会话会变成通用会话。`)) return;
    setProjects((current) => current.filter((item) => item.id !== project.id));
    setPinnedProjects((current) => {
      const next = { ...current };
      delete next[project.id];
      return next;
    });
    setSessionProjects((current) => {
      const next = { ...current };
      Object.entries(next).forEach(([sessionId, projectId]) => {
        if (projectId === project.id) delete next[sessionId];
      });
      return next;
    });
    if (activeProjectId === project.id) {
      setActiveProjectId(null);
    }
    setProjectMenu(null);
  }

  function openProjectInExplorer(project: ProjectFolder) {
    void registerProjectFolderPath(project.path).catch(() => null).then(() => openLocalPath(project.path));
    setProjectMenu(null);
  }

  function showProjectMenu(event: MouseEvent, project: ProjectFolder) {
    event.preventDefault();
    setProjectMenu({ projectId: project.id, x: event.clientX, y: event.clientY });
  }

  function showChatFileMenu(event: MouseEvent, file: FileAttachment | LocalFilePayload) {
    event.preventDefault();
    event.stopPropagation();
    const normalizedFile: FileAttachment = {
      file_path: normalizeLocalSource(file.file_path),
      file_name: file.file_name || normalizeLocalSource(file.file_path).split(/[\\/]/).filter(Boolean).pop() || "file",
      file_type: file.file_type || (isImageAttachment(file as FileAttachment) ? "image" : "file"),
      previewDataUrl: file.previewDataUrl,
      preview_url: file.preview_url
    };
    const durable = isDurableLocalAttachment(normalizedFile);
    setProjectMenu(null);
    setChatFileMenu({
      file: normalizedFile,
      x: event.clientX,
      y: event.clientY,
      canAdd: durable,
      disabledReason: durable ? "" : "Only verified local files can be added to the current chat"
    });
  }

  function addFileToCurrentChat(file: FileAttachment) {
    if (!isDurableLocalAttachment(file)) {
      setToast("Only verified local files can be added to the current chat");
      setChatFileMenu(null);
      return;
    }
    const normalizedPath = normalizeLocalSource(file.file_path);
    const normalizedFile: FileAttachment = {
      ...file,
      file_path: normalizedPath,
      file_name: file.file_name || normalizedPath.split(/[\\/]/).filter(Boolean).pop() || "file",
      file_type: file.file_type || (isImageAttachment(file) ? "image" : "file")
    };
    const key = normalizeAttachmentDedupeKey(normalizedFile);
    setAttachments((current) => {
      if (current.some((item) => normalizeAttachmentDedupeKey(item) === key)) return current;
      return [...current, normalizedFile];
    });
    setChatFileMenu(null);
    focusComposerSoon();
    setToast("已添加到当前聊天");
  }

  async function chooseFiles() {
    try {
      const files = await chooseLocalFiles(sidecarStatus.webPort);
      setAttachments((current) => {
        const seen = new Set(current.map(normalizeAttachmentDedupeKey));
        const next = [...current];
        files.forEach((file) => {
          const key = normalizeAttachmentDedupeKey(file);
          if (!seen.has(key)) {
            seen.add(key);
            next.push(file);
          }
        });
        return next;
      });
      composerRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setToast(error instanceof Error ? error.message : "选择文件失败");
    }
  }

  async function attachBrowserFiles(files: File[], source: "paste" | "drop") {
    if (!files.length) return;
    try {
      const results = await Promise.allSettled(files.map((file) => savePastedFile(file)));
      const nextFiles = results
        .filter((result): result is PromiseFulfilledResult<FileAttachment | null> => result.status === "fulfilled")
        .map((result) => result.value)
        .filter(Boolean) as FileAttachment[];
      if (!nextFiles.length) {
        setToast(source === "drop" ? "未能添加拖拽的文件" : "未能添加粘贴的文件");
        return;
      }
      setAttachments((current) => {
        const seen = new Set(current.map(normalizeAttachmentDedupeKey));
        const next = [...current];
        nextFiles.forEach((file) => {
          const key = normalizeAttachmentDedupeKey(file);
          if (!seen.has(key)) {
            seen.add(key);
            next.push(file);
          }
        });
        return next;
      });
      const failedCount = results.filter((result) => result.status === "rejected").length;
      setToast(failedCount ? `已添加 ${nextFiles.length} 个文件，${failedCount} 个失败` : source === "drop" ? `已添加 ${nextFiles.length} 个文件` : "已添加粘贴的文件");
      composerRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setToast(error instanceof Error ? error.message : source === "drop" ? "拖拽附件失败" : "粘贴附件失败");
    }
  }

  async function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files || []);
    if (!files.length) return;
    event.preventDefault();
    await attachBrowserFiles(files, "paste");
  }

  function dragEventHasFiles(event: DragEvent<HTMLElement>) {
    return Array.from(event.dataTransfer.types || []).includes("Files");
  }

  function handleComposerDragEnter(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    composerDragDepth.current += 1;
    event.dataTransfer.dropEffect = "copy";
    setComposerDragActive(true);
  }

  function handleComposerDragOver(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setComposerDragActive(true);
  }

  function handleComposerDragLeave(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    composerDragDepth.current = Math.max(0, composerDragDepth.current - 1);
    if (composerDragDepth.current === 0) {
      setComposerDragActive(false);
    }
  }

  function handleComposerDrop(event: DragEvent<HTMLFormElement>) {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    composerDragDepth.current = 0;
    setComposerDragActive(false);
    const files = Array.from(event.dataTransfer.files || []);
    void attachBrowserFiles(files, "drop");
  }

  function clearComposerDragState() {
    composerDragDepth.current = 0;
    setComposerDragActive(false);
  }

  function updateSessionMessages(sessionId: string, updater: (messages: ChatItem[]) => ChatItem[]) {
    setSessionUiState((current) => {
      const existing = current[sessionId] || {
        title: sessionTitles[sessionId] || activeSessionTitle,
        projectId: sessionProjects[sessionId] || null,
        messages: sessionId === activeSessionIdRef.current ? messages : [],
        composerText: "",
        attachments: []
      };
      const nextMessages = updater(existing.messages);
      const activityAt = latestMessageMs(nextMessages) || existing.lastActivityAt || Date.now();
      return {
        ...current,
        [sessionId]: {
          ...existing,
          projectId: sessionProjects[sessionId] || null,
          messages: nextMessages,
          lastActivityAt: activityAt
        }
      };
    });
    if (activeSessionIdRef.current === sessionId) {
      setMessages(updater);
    }
  }

  function clearSessionUnread(sessionId: string) {
    setUnreadSessionIds((current) => {
      if (!current[sessionId]) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }

  function markSessionOutputReady(sessionId: string) {
    if (activeSessionIdRef.current === sessionId) return;
    setUnreadSessionIds((current) => current[sessionId] ? current : { ...current, [sessionId]: true });
  }

  function updateAssistantMessage(sessionId: string, assistantId: string, updater: (message: ChatItem) => ChatItem) {
    updateSessionMessages(sessionId, (current) => current.map((message) => message.id === assistantId ? updater(message) : message));
  }

  function updateAssistantMessageForRequest(sessionId: string, assistantId: string, requestId: string, updater: (message: ChatItem) => ChatItem) {
    updateSessionMessages(sessionId, (current) => {
      let updated = false;
      const next = current.map((message) => {
        if (message.id === assistantId || (requestId && message.role === "assistant" && message.requestId === requestId)) {
          updated = true;
          return updater({ ...message, requestId: requestId || message.requestId });
        }
        return message;
      });
      if (updated) return next;
      const fallback: ChatItem = {
        id: assistantId || `a-resume-${requestId || Date.now()}`,
        role: "assistant",
        content: "",
        pending: true,
        requestId,
        createdAt: new Date().toISOString(),
        steps: [{ type: "phase", content: "正在连接响应" }]
      };
      return [...current, updater(fallback)];
    });
  }

  async function refreshSessionFromHistory(sessionId: string) {
    try {
      const history = await loadSessionHistoryWithMeta(sessionId);
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, sessionId, history.contextStartSeq))
        .map(mergeBufferedPostDoneArtifacts);
      const hasFinalAssistant = mapped.some((message) => (
        message.role === "assistant"
        && !message.pending
        && messageHasTerminalPayload(message)
      ));
      if (!hasFinalAssistant) return false;
      const localMessages = sessionId === activeSessionIdRef.current
        ? messagesRef.current
        : sessionUiState[sessionId]?.messages || [];
      const merged = mergeHistoryWithLocalMessages(mapped, localMessages);
      updateSessionMessages(sessionId, () => merged);
      setSessionUiState((current) => ({
        ...current,
        [sessionId]: {
          ...(current[sessionId] || {
            title: sessionTitles[sessionId] || activeSessionTitle,
            projectId: sessionProjects[sessionId] || null,
            composerText: "",
            attachments: []
          }),
          messages: merged,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(merged) || current[sessionId]?.lastActivityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === sessionId) {
        setMessages(merged);
      } else {
        markSessionOutputReady(sessionId);
      }
      return true;
    } catch (error) {
      return false;
    }
  }

  async function refreshSessionFromHistoryForRequest(sessionId: string, requestId: string) {
    if (!requestId) return false;
    try {
      const history = await loadSessionHistoryWithMeta(sessionId);
      const mapped = normalizePausedMessages(mapRuntimeHistory(history.messages, sessionId, history.contextStartSeq))
        .map(mergeBufferedPostDoneArtifacts);
      const scopedFinal = mapped.some((message) => (
        message.role === "assistant"
        && message.requestId === requestId
        && !message.pending
        && messageHasTerminalPayload(message)
      ));
      const localMessages = sessionId === activeSessionIdRef.current
        ? messagesRef.current
        : sessionUiState[sessionId]?.messages || [];
      const merged = mergeHistoryWithLocalMessages(mapped, localMessages);
      updateSessionMessages(sessionId, () => merged);
      setSessionUiState((current) => ({
        ...current,
        [sessionId]: {
          ...(current[sessionId] || {
            title: sessionTitles[sessionId] || activeSessionTitle,
            projectId: sessionProjects[sessionId] || null,
            composerText: "",
            attachments: []
          }),
          messages: merged,
          contextStartSeq: history.contextStartSeq,
          lastActivityAt: latestMessageMs(merged) || current[sessionId]?.lastActivityAt || Date.now()
        }
      }));
      if (activeSessionIdRef.current === sessionId) {
        setMessages(merged);
      } else {
        markSessionOutputReady(sessionId);
      }
      return scopedFinal;
    } catch {
      return false;
    }
  }

  function historyRecoveryKey(sessionId: string, requestId: string) {
    return `${sessionId}::${requestId}`;
  }

  function clearHistoryRecovery(sessionId: string, requestId?: string) {
    const prefix = requestId ? historyRecoveryKey(sessionId, requestId) : `${sessionId}::`;
    Object.keys(historyRecoveryTimersRef.current).forEach((key) => {
      if (requestId ? key === prefix : key.startsWith(prefix)) {
        historyRecoveryTimersRef.current[key].forEach((timer) => window.clearTimeout(timer));
        delete historyRecoveryTimersRef.current[key];
      }
    });
  }

  function scheduleHistoryRecovery(sessionId: string, requestId: string, delays = [1200, 3500, 8000]) {
    if (!requestId) return;
    const key = historyRecoveryKey(sessionId, requestId);
    clearHistoryRecovery(sessionId, requestId);
    historyRecoveryTimersRef.current[key] = delays.map((delay) => window.setTimeout(() => {
      const currentRequestId = sessionRequestIdsRef.current[sessionId];
      if (currentRequestId && currentRequestId !== requestId && !completedRequestIds.current[requestId]) {
        clearHistoryRecovery(sessionId, requestId);
        return;
      }
      void refreshSessionFromHistory(sessionId);
    }, delay));
  }

  function recoverStaleRequestFromHistory(sessionId: string, assistantId: string, requestId: string) {
    void refreshSessionFromHistory(sessionId).then((restored) => {
      if (restored) return;
      updateAssistantMessage(sessionId, assistantId, (message) => ({
        ...finishRunningSteps(message),
        requestId: undefined,
        content: redactInternalPromptText(message.content || "任务状态已同步。如未完成，请重新发送。"),
        pending: false,
        paused: false,
        cancelled: false
      }));
      markSessionOutputReady(sessionId);
    });
  }

  function handleReplayGapStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const requested = typeof item.requested_last_event_id === "number" ? item.requested_last_event_id : undefined;
    const retained = typeof item.retained_from_event_id === "number" ? item.retained_from_event_id : undefined;
    const detail = [requested !== undefined ? `requested=${requested}` : "", retained !== undefined ? `retainedFrom=${retained}` : ""]
      .filter(Boolean)
      .join(", ");
    const message = redactInternalPromptText(
      item.message
      || item.content
      || `Response stream history expired${detail ? ` (${detail})` : ""}. Refreshed saved conversation; retry if the final answer is missing.`
    );
    markStreamTerminal(sessionId, requestId, "failed");
    finishSessionRequest(sessionId, requestId);
    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: false,
        recovery: {
          kind: "replay_gap",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "stream_replay_gap",
      message,
      sessionId,
      detail: {
        requestId,
        requestedLastEventId: requested ?? null,
        retainedFromEventId: retained ?? null,
        nextEventId: item.next_event_id ?? null
      }
    });
  }

  function handleInterruptedStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const message = redactInternalPromptText(
      item.message
      || item.content
      || "Runtime sidecar restarted before this run reached a terminal state. Refreshed saved conversation; retry if the final answer is missing."
    );
    markStreamTerminal(sessionId, requestId, "interrupted");
    finishSessionRequest(sessionId, requestId);
    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: false,
        recovery: {
          kind: "interrupted",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "stream_interrupted",
      message,
      sessionId,
      detail: {
        requestId,
        terminalReason: item.terminal_reason || null,
        errorCode: item.error_code || null
      }
    });
  }

  function settleTerminalSnapshotRequest(request: RuntimeActiveRequest) {
    const sessionId = String(request.session_id || "").trim();
    const requestId = String(request.request_id || "").trim();
    if (!sessionId || !requestId) return;
    if (!isAbnormalTerminalRequest(request)) return;
    const handledKey = `${sessionId}::${requestId}`;
    if (handledSnapshotTerminalRequestsRef.current[handledKey]) return;

    const sourceMessages = activeSessionIdRef.current === sessionId
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const assistant = sourceMessages.find((message) => (
      message.role === "assistant"
      && message.requestId === requestId
      && message.pending
    ));
    if (!assistant && sessionRequestIdsRef.current[sessionId] !== requestId) return;

    handledSnapshotTerminalRequestsRef.current = {
      ...handledSnapshotTerminalRequestsRef.current,
      [handledKey]: true
    };
    const state = runCenterState(request);
    const phase = state === "cancelled" || state === "cancelling" ? "cancelled" : "interrupted";
    const message = redactInternalPromptText(
      request.error_message
      || request.terminal_reason
      || "Runtime session lock owner disappeared before the run reached a terminal state."
    );
    markStreamTerminal(sessionId, requestId, phase);
    finishSessionRequest(sessionId, requestId);
    if (!assistant) return;

    void refreshSessionFromHistoryForRequest(sessionId, requestId).then((restored) => {
      if (restored) return;
      updateAssistantMessageForRequest(sessionId, assistant.id, requestId, (entry) => ({
        ...finishRunningSteps(entry, phase === "cancelled" ? "cancelled" : "error"),
        content: message,
        pending: false,
        paused: false,
        cancelled: phase === "cancelled",
        recovery: phase === "cancelled" ? undefined : {
          kind: "interrupted",
          requestId,
          message,
          recoverable: true,
          retryable: true
        }
      }));
      markSessionOutputReady(sessionId);
    });
    void reportDesktopEvent({
      type: "warn",
      source: "Desktop",
      category: "runtime",
      label: "snapshot_terminal_request",
      message,
      sessionId,
      detail: {
        requestId,
        state: request.state || request.status || request.phase || null,
        terminalReason: request.terminal_reason || null,
        errorCode: request.error_code || null
      }
    });
  }

  function reportStreamErrorTelemetry(sessionId: string, requestId: string, message: string, staleRequest: boolean) {
    if (staleRequest) {
      void reportDesktopEvent({
        type: "warn",
        source: "Desktop",
        category: "runtime",
        label: "stale_stream_request",
        message,
        sessionId,
        detail: {
          requestId,
          recovery: "history",
          suppressedErrorLog: true
        }
      });
      return;
    }
    void reportDesktopEvent({
      type: "error",
      source: "Desktop",
      message,
      sessionId,
      detail: { requestId }
    });
  }

  function finishRunningSteps(message: ChatItem, reason: AgentFinishReason = "done"): ChatItem {
    return { ...message, steps: finishAgentSteps(message.steps, reason), toolCalls: message.toolCalls?.map((tool) => ({ ...tool, running: false })) };
  }

  function clearTransientSendSteps(message: ChatItem): ChatItem {
    return {
      ...message,
      steps: (message.steps || []).filter((step) => (
        step.type !== "phase"
        || !isTransientPhaseContent(step.content)
      ))
    };
  }

  function isTransientPhaseContent(content?: string) {
    const value = String(content || "").trim();
    if (!value) return true;
    return [
      "正在发送",
      "正在连接响应",
      "正在连接后台任务",
      "已收到，正在准备响应",
      "正在检查额度",
      "正在建立响应通道",
      "正在组织上下文",
      "正在连接模型响应",
      "正在恢复响应通道"
    ].some((prefix) => value === prefix || value.startsWith(`${prefix} `) || value.startsWith(`${prefix} ·`))
      || value.startsWith("等待本机工具授权");
  }

  function replaceCurrentPhase(message: ChatItem, rawContent: string): ChatItem {
    const content = redactInternalPromptText(rawContent || "").trim();
    if (!content) return message;
    const steps = (message.steps || []).filter((step) => (
      step.type !== "phase" || !isTransientPhaseContent(step.content)
    ));
    const last = steps[steps.length - 1];
    if (last?.type === "phase") {
      if ((last.content || "") === content) {
        return { ...message, pending: true, paused: false };
      }
      steps[steps.length - 1] = { ...last, content };
    } else {
      steps.push({ type: "phase", content });
    }
    return {
      ...message,
      pending: true,
      paused: false,
      phaseStartedAt: message.phaseStartedAt || Date.now(),
      steps
    };
  }

  function clearAssistantPhaseTimers(assistantId?: string) {
    if (!assistantId) return;
    const timers = phaseTimersRef.current[assistantId] || [];
    timers.forEach((timer) => window.clearTimeout(timer));
    delete phaseTimersRef.current[assistantId];
  }

  function clearAllPhaseTimers() {
    Object.keys(phaseTimersRef.current).forEach((assistantId) => clearAssistantPhaseTimers(assistantId));
  }

  function queuePreflightPhase(sessionId: string, assistantId: string, generation: number, delayMs: number, content: string) {
    const timer = window.setTimeout(() => {
      if (sendGenerationRef.current[sessionId] !== generation) return;
      updateAssistantMessage(sessionId, assistantId, (message) => {
        if (!message.pending || message.requestId || message.cancelled) return message;
        return replaceCurrentPhase(message, content);
      });
    }, delayMs);
    phaseTimersRef.current[assistantId] = [...(phaseTimersRef.current[assistantId] || []), timer];
  }

  function markSessionRequestsPaused(sessionId: string) {
    updateSessionMessages(sessionId, (current) => current.map((message) => message.pending ? {
      ...finishRunningSteps(message, "paused"),
      content: pausedMessageContent(message.content),
      pending: false,
      paused: true,
      cancelled: false
    } : message));
  }

  function appendReasoningStep(message: ChatItem, chunk: string): ChatItem {
    chunk = redactInternalPromptText(chunk);
    if (!chunk) return message;
    const steps = [...(message.steps || [])];
    const last = steps[steps.length - 1];
    if (last?.type === "thinking" && last.running) {
      steps[steps.length - 1] = { ...last, content: `${last.content || ""}${chunk}` };
    } else {
      steps.push({ type: "thinking", content: chunk, running: true, startedAt: Date.now() });
    }
    return { ...message, pending: true, steps };
  }

  function flushIntermediateContent(message: ChatItem): ChatItem {
    const content = redactInternalPromptText(message.content).trim();
    if (!content) return message;
    return {
      ...message,
      content: "",
      steps: [...(message.steps || []), { type: "content", content, intermediate: true }]
    };
  }

  function appendToolStart(message: ChatItem, item: StreamItem): ChatItem {
    const next = flushIntermediateContent(finishRunningSteps(message));
    const toolName = item.tool || item.name || "tool";
    const toolId = item.tool_call_id || `${toolName}-${Date.now()}`;
    const steps = [...(next.steps || [])];
    const toolIndex = steps.findIndex((step) => step.type === "tool" && ((toolId && step.id === toolId) || (!toolId && step.name === toolName)));
    const runningTool: Extract<AgentStepDisclosure, { type: "tool" }> = {
      type: "tool",
      id: toolId,
      name: toolName,
      arguments: item.arguments ?? item.input,
      status: "running",
      running: true
    };
    if (toolIndex >= 0) {
      steps[toolIndex] = runningTool;
    } else {
      steps.push(runningTool);
    }
    return {
      ...next,
      pending: true,
      steps
    };
  }

  function appendToolEnd(message: ChatItem, item: StreamItem): ChatItem {
    const steps = [...(message.steps || [])];
    const toolName = item.tool || item.name;
    const toolId = item.tool_call_id || "";
    let targetIndex = -1;
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const step = steps[index];
      if (step.type === "tool" && step.running && ((toolId && step.id === toolId) || (!toolId && (!toolName || step.name === toolName)))) {
        targetIndex = index;
        break;
      }
    }
    if (targetIndex < 0 && toolName) {
      for (let index = steps.length - 1; index >= 0; index -= 1) {
        const step = steps[index];
        if (step.type === "tool" && step.name === toolName) {
          targetIndex = index;
          break;
        }
      }
    }
    const completedTool: Extract<AgentStepDisclosure, { type: "tool" }> = {
      type: "tool",
      id: toolId || undefined,
      name: toolName,
      arguments: item.arguments ?? item.input,
      result: typeof (item.result ?? item.content ?? item.message) === "string"
        ? redactInternalPromptText(item.result ?? item.content ?? item.message)
        : item.result ?? item.content ?? item.message,
      status: item.status || "done",
      execution_time: item.execution_time,
      is_error: item.status === "error" || item.status === "failed",
      running: false
    };
    if (targetIndex >= 0) {
      const previous = steps[targetIndex];
      if (previous.type === "tool") {
        steps[targetIndex] = {
          ...previous,
          ...completedTool,
          arguments: completedTool.arguments ?? previous.arguments
        };
      }
    } else {
      steps.push(completedTool);
    }
    return { ...message, pending: true, steps };
  }

  function appendMediaStep(message: ChatItem, item: StreamItem, pending = true): ChatItem {
    const next = finishRunningSteps(message);
    const type = item.type === "image" || item.file_type === "image"
      ? "image"
      : item.type === "video" || item.file_type === "video"
        ? "video"
        : item.type === "audio" || item.type === "voice_attach" || item.file_type === "audio"
          ? "audio"
          : "file";
    return {
      ...next,
      pending,
      paused: false,
      steps: [
        ...(next.steps || []),
        {
          type: "media",
          fileType: type,
          url: item.path || item.url || redactInternalPromptText(item.content || ""),
          fileName: item.file_name || item.name
        }
      ]
    };
  }

  function artifactDedupeKey(artifact: AgentArtifact) {
    return (artifact.path || artifact.relativePath || artifact.url || artifact.id).replace(/\\/g, "/").toLowerCase();
  }

  function streamItemArtifacts(item: StreamItem, requestId?: string) {
    const sourceRequestId = item.request_id || requestId;
    const incoming = item.artifact
      ? normalizeArtifactEntry(item.artifact, 0, sourceRequestId)
      : Array.isArray(item.artifacts)
        ? item.artifacts.map((entry, index) => normalizeArtifactEntry(entry, index, sourceRequestId)).filter((entry): entry is AgentArtifact => Boolean(entry))
        : [];
    return Array.isArray(incoming) ? incoming : incoming ? [incoming] : [];
  }

  function appendArtifact(message: ChatItem, item: StreamItem, pending = true): ChatItem {
    const artifacts = streamItemArtifacts(item, item.request_id || message.requestId);
    if (!artifacts.length) return message;
    const nextArtifacts = [...(message.artifacts || [])];
    for (const artifact of artifacts) {
      const key = artifactDedupeKey(artifact);
      const index = nextArtifacts.findIndex((entry) => entry.id === artifact.id || artifactDedupeKey(entry) === key);
      if (index >= 0) {
        nextArtifacts[index] = mergeAgentArtifactRecord(nextArtifacts[index], artifact);
      } else {
        nextArtifacts.push(artifact);
      }
    }
    return { ...message, pending, paused: false, artifacts: nextArtifacts };
  }

  function rememberPostDoneTailArtifacts(requestId: string, item: StreamItem) {
    const artifacts = streamItemArtifacts(item, requestId);
    if (!requestId || !artifacts.length) return;
    const currentMessage: ChatItem = {
      id: `postdone-buffer-${requestId}`,
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      artifacts: postDoneTailArtifactsRef.current[requestId] || []
    };
    postDoneTailArtifactsRef.current = {
      ...postDoneTailArtifactsRef.current,
      [requestId]: mergeArtifactsIntoMessage(currentMessage, artifacts).artifacts || artifacts
    };
  }

  function mergeBufferedPostDoneArtifacts(message: ChatItem) {
    const requestId = message.requestId || "";
    return requestId ? mergeArtifactsIntoMessage(message, postDoneTailArtifactsRef.current[requestId] || []) : message;
  }

  function sessionHasAssistantRequest(sessionId: string, requestId: string) {
    if (!requestId) return false;
    const source = activeSessionIdRef.current === sessionId
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    return source.some((message) => message.role === "assistant" && message.requestId === requestId);
  }

  function isCurrentSessionRequest(sessionId: string, requestId?: string) {
    if (!requestId) return true;
    const currentRequestId = sessionRequestIdsRef.current[sessionId];
    if (currentRequestId) return currentRequestId === requestId;
    return sessionHasAssistantRequest(sessionId, requestId);
  }

  function isUiLiveAssistantMessage(message: ChatItem) {
    return isLiveAssistantMessage(message)
      && !(message.requestId && locallyCompletedRequestIdsRef.current[message.requestId]);
  }

  function isPostDoneTailItem(item: StreamItem) {
    return item.type === "voice_attach"
      || item.type === "artifact"
      || item.type === "file"
      || item.type === "image"
      || item.type === "video"
      || item.type === "audio"
      || Boolean(item.artifact || item.artifacts);
  }

  function streamItemText(item: StreamItem) {
    return String(item.content ?? item.text ?? item.delta ?? "");
  }

  function streamRequestKey(sessionId: string, requestId: string) {
    return `${sessionId}::${requestId}`;
  }

  function isTerminalStreamPhase(phase?: StreamRequestPhase) {
    return phase === "completed" || phase === "failed" || phase === "cancelled" || phase === "interrupted";
  }

  function getStreamRequestState(sessionId: string, requestId: string) {
    return streamRequestStates.current[streamRequestKey(sessionId, requestId)];
  }

  function setStreamRequestPhase(sessionId: string, requestId: string, phase: StreamRequestPhase) {
    if (!sessionId || !requestId) return;
    const key = streamRequestKey(sessionId, requestId);
    const current = streamRequestStates.current[key];
    if (isTerminalStreamPhase(current?.phase) && !isTerminalStreamPhase(phase)) return;
    streamRequestStates.current[key] = {
      sessionId,
      requestId,
      phase,
      updatedAt: Date.now(),
      terminalAt: isTerminalStreamPhase(phase) ? current?.terminalAt || Date.now() : current?.terminalAt,
      lastEventAt: current?.lastEventAt
    };
  }

  function clearStreamStallTimer(sessionId: string, requestId: string) {
    const key = streamRequestKey(sessionId, requestId);
    const timer = streamStallTimers.current[key];
    if (timer) {
      window.clearTimeout(timer);
      delete streamStallTimers.current[key];
    }
  }

  function scheduleStreamStallTimer(sessionId: string, assistantId: string, requestId: string, delayMs: number) {
    clearStreamStallTimer(sessionId, requestId);
    const key = streamRequestKey(sessionId, requestId);
    streamStallTimers.current[key] = window.setTimeout(() => {
      delete streamStallTimers.current[key];
      const state = getStreamRequestState(sessionId, requestId);
      if (isTerminalStreamPhase(state?.phase) || completedRequestIds.current[requestId]) return;
      setStreamRequestPhase(sessionId, requestId, "stalled");
      updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
        message.pending ? {
          ...replaceCurrentPhase(message, "Response stalled; reconnecting"),
          recovery: {
            kind: "stalled",
            requestId,
            message: "Response stalled; reconnecting. You can reconnect or recover from saved history.",
            recoverable: true,
            retryable: false
          }
        } : message
      ));
      scheduleStreamReconnect(sessionId, assistantId, requestId);
    }, delayMs);
  }

  function beginStreamRequest(sessionId: string, assistantId: string, requestId: string) {
    setStreamRequestPhase(sessionId, requestId, "connecting");
    scheduleStreamStallTimer(sessionId, assistantId, requestId, 20_000);
  }

  function observeStreamItem(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    const key = streamRequestKey(sessionId, requestId);
    const current = streamRequestStates.current[key];
    if (isTerminalStreamPhase(current?.phase)) return;
    streamRequestStates.current[key] = {
      sessionId,
      requestId,
      phase: item.type === "message_update" || item.type === "delta" ? "streaming" : current?.phase || "streaming",
      updatedAt: Date.now(),
      lastEventAt: Date.now()
    };
    scheduleStreamStallTimer(sessionId, assistantId, requestId, 90_000);
  }

  function markStreamTerminal(sessionId: string, requestId: string, phase: "completed" | "failed" | "cancelled" | "interrupted") {
    setStreamRequestPhase(sessionId, requestId, phase);
    clearStreamStallTimer(sessionId, requestId);
  }

  function flushStreamBoundary(sessionId: string, assistantId: string, requestId: string, item: StreamItem) {
    observeStreamItem(sessionId, assistantId, requestId, item);
    const isDeltaItem = item.type === "message_update" || item.type === "delta";
    if (!isDeltaItem) {
      setStreamRequestPhase(sessionId, requestId, "flushing");
      flushStreamDeltaBuffers(sessionId, requestId);
    }
  }

  function streamItemExplicitText(item: StreamItem, keys: Array<keyof StreamItem | "final_text">) {
    const record = item as StreamItem & { final_text?: unknown };
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(record, key)) {
        return String(record[key] ?? "");
      }
    }
    return null;
  }

  function doneItemContent(item: StreamItem, currentContent: string) {
    return redactInternalPromptText(streamItemExplicitText(item, ["final_text", "content", "text", "message"]) ?? currentContent);
  }

  function isReplayGapStreamItem(item: StreamItem) {
    return item.type === "replay_gap" || item.event_type === "stream.replay_gap";
  }

  function isInterruptedStreamItem(item: StreamItem) {
    return item.type === "interrupted" || item.event_type === "run.interrupted" || item.state === "interrupted";
  }

  function shouldAcceptStreamItem(sessionId: string, requestId: string, item: StreamItem) {
    const itemRecord = item as StreamItem & { requestId?: string };
    const itemRequestId = item.request_id || itemRecord.requestId;
    if (itemRequestId && itemRequestId !== requestId) return false;
    if (completedRequestIds.current[requestId]) {
      const state = getStreamRequestState(sessionId, requestId);
      const activeTailStream = streamCleanupRequestIds.current[sessionId] === requestId;
      return isPostDoneTailItem(item)
        && (Boolean(itemRequestId) || activeTailStream || state?.phase === "text_done_tail_open");
    }
    return isCurrentSessionRequest(sessionId, requestId);
  }

  function isTerminalVoiceAttach(item: StreamItem) {
    if (item.type !== "voice_attach") return false;
    const record = item as StreamItem & { terminal?: unknown; final?: unknown; done?: unknown };
    return record.terminal === true
      || record.final === true
      || record.done === true
      || item.status === "done"
      || item.status === "completed";
  }

  function streamDeltaKey(sessionId: string, assistantId: string, requestId: string) {
    return `${sessionId}::${assistantId}::${requestId}`;
  }

  function flushBufferedDelta(key: string) {
    const buffer = streamDeltaBuffers.current[key];
    if (!buffer) return;
    if (buffer.timer !== null) {
      window.clearTimeout(buffer.timer);
      buffer.timer = null;
    }
    const text = buffer.text;
    buffer.text = "";
    if (!text) return;
    updateAssistantMessageForRequest(buffer.sessionId, buffer.assistantId, buffer.requestId, (message) => ({
      ...finishRunningSteps(message),
      content: `${message.content}${text}`,
      pending: true,
      paused: false
    }));
  }

  function enqueueAssistantDelta(sessionId: string, assistantId: string, requestId: string, rawContent: string) {
    const deltaContent = redactInternalPromptText(rawContent);
    if (!deltaContent) return;
    const key = streamDeltaKey(sessionId, assistantId, requestId);
    const buffer = streamDeltaBuffers.current[key] || {
      sessionId,
      assistantId,
      requestId,
      text: "",
      timer: null
    };
    buffer.text += deltaContent;
    streamDeltaBuffers.current[key] = buffer;
    if (buffer.timer !== null) return;
    const currentLength = activeSessionIdRef.current === sessionId
      ? messagesRef.current.find((message) => message.id === assistantId)?.content.length || 0
      : 0;
    const flushDelay = currentLength >= 100000 ? 180 : currentLength >= 30000 ? 90 : 34;
    buffer.timer = window.setTimeout(() => {
      const current = streamDeltaBuffers.current[key];
      if (current) current.timer = null;
      flushBufferedDelta(key);
    }, flushDelay);
  }

  function flushStreamDeltaBuffers(sessionId: string, requestId?: string) {
    Object.keys(streamDeltaBuffers.current).forEach((key) => {
      const buffer = streamDeltaBuffers.current[key];
      if (!buffer || buffer.sessionId !== sessionId) return;
      if (requestId && buffer.requestId !== requestId) return;
      flushBufferedDelta(key);
    });
  }

  function clearStreamDeltaBuffers(sessionId: string, requestId?: string) {
    Object.keys(streamDeltaBuffers.current).forEach((key) => {
      const buffer = streamDeltaBuffers.current[key];
      if (!buffer || buffer.sessionId !== sessionId) return;
      if (requestId && buffer.requestId !== requestId) return;
      if (buffer.timer !== null) window.clearTimeout(buffer.timer);
      delete streamDeltaBuffers.current[key];
    });
  }

  function closeSessionStream(sessionId: string, requestId?: string) {
    const cleanup = streamCleanups.current[sessionId];
    if (!cleanup) return;
    const cleanupRequestId = streamCleanupRequestIds.current[sessionId];
    if (requestId && cleanupRequestId && cleanupRequestId !== requestId) return;
    if (requestId) {
      const postDoneKey = `${sessionId}::${requestId}`;
      if (postDoneStreamCloseTimers.current[postDoneKey]) {
        window.clearTimeout(postDoneStreamCloseTimers.current[postDoneKey]);
        delete postDoneStreamCloseTimers.current[postDoneKey];
      }
      clearStreamStallTimer(sessionId, requestId);
    }
    flushStreamDeltaBuffers(sessionId, requestId);
    cleanup();
    delete streamCleanups.current[sessionId];
    delete streamCleanupRequestIds.current[sessionId];
    if (streamCleanup.current === cleanup) {
      streamCleanup.current = null;
    }
    clearStreamDeltaBuffers(sessionId, requestId);
  }

  function markRequestLocallyCompleted(requestId: string, ttlMs = 30 * 60_000) {
    if (!requestId) return;
    completedRequestIds.current[requestId] = true;
    locallyCompletedRequestIdsRef.current = {
      ...locallyCompletedRequestIdsRef.current,
      [requestId]: true
    };
    setLocallyCompletedRequestIds((current) => current[requestId] ? current : { ...current, [requestId]: true });
    if (completedRequestCleanupTimers.current[requestId]) {
      window.clearTimeout(completedRequestCleanupTimers.current[requestId]);
    }
    completedRequestCleanupTimers.current[requestId] = window.setTimeout(() => {
      delete completedRequestIds.current[requestId];
      delete postDoneTailArtifactsRef.current[requestId];
      Object.keys(streamRequestStates.current).forEach((key) => {
        if (key.endsWith(`::${requestId}`)) delete streamRequestStates.current[key];
      });
      Object.keys(streamStallTimers.current).forEach((key) => {
        if (!key.endsWith(`::${requestId}`)) return;
        window.clearTimeout(streamStallTimers.current[key]);
        delete streamStallTimers.current[key];
      });
      const nextCompleted = { ...locallyCompletedRequestIdsRef.current };
      delete nextCompleted[requestId];
      locallyCompletedRequestIdsRef.current = nextCompleted;
      setLocallyCompletedRequestIds(nextCompleted);
      delete completedRequestCleanupTimers.current[requestId];
    }, ttlMs);
  }

  function schedulePostDoneStreamClose(sessionId: string, requestId: string, delayMs = 60_000) {
    if (!sessionId || !requestId) return;
    const key = `${sessionId}::${requestId}`;
    if (postDoneStreamCloseTimers.current[key]) {
      window.clearTimeout(postDoneStreamCloseTimers.current[key]);
    }
    postDoneStreamCloseTimers.current[key] = window.setTimeout(() => {
      setStreamRequestPhase(sessionId, requestId, "completed");
      closeSessionStream(sessionId, requestId);
      delete postDoneStreamCloseTimers.current[key];
    }, delayMs);
  }

  function clearSessionRequestState(sessionId: string, requestId?: string) {
    const shouldClear = !requestId || sessionRequestIdsRef.current[sessionId] === requestId;
    setActiveRequestId((current) => (!requestId || current === requestId ? "" : current));
    if (shouldClear) {
      const nextRef = { ...sessionRequestIdsRef.current };
      delete nextRef[sessionId];
      sessionRequestIdsRef.current = nextRef;
    }
    setSessionRequestIds((current) => {
      if (requestId && current[sessionId] !== requestId) return current;
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
    if (shouldClear) {
      delete streamRetryCounts.current[sessionId];
    }
  }

  function finishSessionRequest(sessionId: string, requestId?: string) {
    flushStreamDeltaBuffers(sessionId, requestId);
    clearHistoryRecovery(sessionId, requestId);
    if (requestId) clearStreamStallTimer(sessionId, requestId);
    clearSessionRequestState(sessionId, requestId);
    closeSessionStream(sessionId, requestId);
    clearStreamDeltaBuffers(sessionId, requestId);
  }

  function scheduleStreamReconnect(sessionId: string, assistantId: string, requestId: string) {
    if (completedRequestIds.current[requestId]) return;
    if (!isCurrentSessionRequest(sessionId, requestId)) return;
    const attempts = streamRetryCounts.current[sessionId] || 0;
    updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
      message.pending ? replaceCurrentPhase(message, attempts ? `正在恢复响应通道 · 第 ${attempts + 1} 次` : "正在恢复响应通道") : message
    ));
    if (attempts >= 5) {
      void (async () => {
        const snapshot = await loadRuntimeSnapshot().catch(() => null);
        const active = (snapshot?.activeRequests || []).find((request) => (
          String(request.session_id || "") === sessionId
          && String(request.request_id || "") === requestId
          && isPrimaryChatActiveRequest(request)
        ));
        if (snapshot) setRuntimeSnapshot(snapshot);
        if (active?.cancelled) {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...message,
            requestId,
            pending: true,
            paused: false,
            cancelled: false
          }));
          window.setTimeout(() => scheduleStreamReconnect(sessionId, assistantId, requestId), 5000);
          return;
        }
        if (active && !active.cancelled) {
          const restored = active.stream_available === false
            ? await refreshSessionFromHistory(sessionId)
            : false;
          if (restored) return;
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...message,
            requestId,
            pending: true,
            paused: false,
            cancelled: false
          }));
          streamRetryCounts.current[sessionId] = active.stream_available === false ? 5 : 0;
          if (active.stream_available !== false) {
            window.setTimeout(() => attachMessageStream(sessionId, assistantId, requestId), 3000);
          } else {
            window.setTimeout(() => scheduleStreamReconnect(sessionId, assistantId, requestId), 5000);
          }
          return;
        }
        const restored = await refreshSessionFromHistory(sessionId);
        if (restored) {
          clearSessionRequestState(sessionId, requestId);
          return;
        }
        updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
          ...finishRunningSteps(message),
          requestId,
          pending: false,
          paused: false,
          cancelled: false
        }));
        markSessionOutputReady(sessionId);
        finishSessionRequest(sessionId, requestId);
      })();
      return;
    }
    streamRetryCounts.current[sessionId] = attempts + 1;
    window.setTimeout(() => {
      if (!isCurrentSessionRequest(sessionId, requestId)) return;
      attachMessageStream(sessionId, assistantId, requestId);
    }, Math.min(1500 * (attempts + 1), 8000));
  }

  function attachMessageStream(sessionId: string, assistantId: string, requestId: string) {
    if (!requestId) return;
    const cachedMessages = sessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[sessionId]?.messages || [];
    const existingMessage = cachedMessages.find((message) => (
      message.id === assistantId || (message.role === "assistant" && message.requestId === requestId)
    ));
    if (completedRequestIds.current[requestId] || locallyCompletedRequestIdsRef.current[requestId] || isTerminalAssistantMessage(existingMessage)) {
      markRequestLocallyCompleted(requestId);
      clearSessionRequestState(sessionId, requestId);
      markStreamTerminal(sessionId, requestId, "completed");
      return;
    }
    const existingRequestId = streamCleanupRequestIds.current[sessionId];
    if (existingRequestId === requestId && streamCleanups.current[sessionId]) return;
    if (existingRequestId && existingRequestId !== requestId) {
      closeSessionStream(sessionId, existingRequestId);
    }
    const hasCursor = hasMessageStreamCursor(requestId);
    if (!hasCursor) {
      updateAssistantMessage(sessionId, assistantId, (message) => (
        message.pending && message.requestId === requestId && message.content
          ? { ...message, steps: message.steps?.length ? message.steps : [{ type: "phase", content: "正在恢复响应通道" }] }
          : message
      ));
    }
    setActiveRequestId(requestId);
    sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [sessionId]: requestId };
    setSessionRequestIds((current) => ({ ...current, [sessionId]: requestId }));
    scheduleHistoryRecovery(sessionId, requestId);
    beginStreamRequest(sessionId, assistantId, requestId);
    const cleanup = openMessageStream({
      requestId,
      webPort: sidecarStatus.webPort,
      onItem: (item) => {
        if (item.request_id && item.request_id !== requestId) return;
        if (!shouldAcceptStreamItem(sessionId, requestId, item)) return;
        flushStreamBoundary(sessionId, assistantId, requestId, item);
        if (isReplayGapStreamItem(item)) {
          handleReplayGapStreamItem(sessionId, assistantId, requestId, item);
          return;
        }
        if (isInterruptedStreamItem(item)) {
          handleInterruptedStreamItem(sessionId, assistantId, requestId, item);
          return;
        }
        if (item.type === "cancelled") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...finishRunningSteps(message, "cancelled"),
            content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
            pending: false,
            cancelled: true
          }));
          markStreamTerminal(sessionId, requestId, "cancelled");
          markSessionOutputReady(sessionId);
          finishSessionRequest(sessionId, requestId);
          return;
        }
        if (item.type === "done") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
            const nextMessage = item.artifact || item.artifacts
              ? appendArtifact(finishRunningSteps(message), item)
              : finishRunningSteps(message);
              return {
                ...clearTransientSendSteps(nextMessage),
                content: doneItemContent(item, message.content),
                pending: false,
                requestId,
                userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
              };
          });
          markRequestLocallyCompleted(requestId);
          setStreamRequestPhase(sessionId, requestId, "text_done_tail_open");
          markSessionOutputReady(sessionId);
          clearHistoryRecovery(sessionId, requestId);
          clearSessionRequestState(sessionId, requestId);
          schedulePostDoneStreamClose(sessionId, requestId);
          window.setTimeout(() => {
            void refreshSessionFromHistory(sessionId);
          }, 300);
          return;
        }
        if (item.type === "error") {
          const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
          const staleRequest = /invalid request_id/i.test(message);
          markStreamTerminal(sessionId, requestId, "failed");
          finishSessionRequest(sessionId, requestId);
          if (staleRequest) {
            recoverStaleRequestFromHistory(sessionId, assistantId, requestId);
          } else {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (entry) => ({
              ...finishRunningSteps(entry, "error"),
              content: message,
              pending: false,
              paused: false
            }));
            markSessionOutputReady(sessionId);
          }
          reportStreamErrorTelemetry(sessionId, requestId, message, staleRequest);
          return;
        }
        if (item.type === "reasoning" || item.type === "thinking") {
          const chunk = item.content || item.text || "";
          if (chunk) {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendReasoningStep(message, chunk));
          }
          return;
        }
        if (item.type === "message_end") {
          if (item.has_tool_calls) {
            updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => flushIntermediateContent(finishRunningSteps(message)));
          }
          return;
        }
        if (item.type === "tool_permission_request") {
          const permissionRequestId = item.permission_request_id || "";
          if (!permissionRequestId) return;
          setApproval({
            type: "permission",
            title: item.title || "本机工具执行前确认",
            message: item.message || `EcoreX 将执行 ${item.tool || "tool"}，请确认是否允许。`,
            actions: [
              {
                label: "允许本次",
                primary: true,
                onClick: () => void answerToolPermission(permissionRequestId, "allow_once")
              },
              {
                label: "始终允许",
                onClick: () => void answerToolPermission(permissionRequestId, "always_allow")
              },
              {
                label: "拒绝",
                onClick: () => void answerToolPermission(permissionRequestId, "deny")
              }
            ]
          });
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => ({
            ...replaceCurrentPhase(message, `等待本机工具授权：${item.tool || "tool"}`),
            pending: true,
            paused: false
          }));
          return;
        }
        if (item.type === "tool_start") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolStart(message, item));
          return;
        }
        if (item.type === "tool_end") {
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => appendToolEnd(message, item));
          return;
        }
        if (item.type === "artifact") {
          const postDoneTail = Boolean(completedRequestIds.current[requestId]);
          if (postDoneTail) rememberPostDoneTailArtifacts(requestId, item);
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => (
            postDoneTail
              ? clearTransientSendSteps(appendArtifact(finishRunningSteps(message), item, false))
              : appendArtifact(message, item)
          ));
          if (postDoneTail) markSessionOutputReady(sessionId);
          return;
        }
        if (item.type === "image" || item.type === "video" || item.type === "audio" || item.type === "file" || item.type === "voice_attach") {
          const postDoneTail = Boolean(completedRequestIds.current[requestId]);
          const terminalVoiceAttach = isTerminalVoiceAttach(item);
          updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => {
            const next = appendMediaStep(message, item, !postDoneTail);
            return postDoneTail || terminalVoiceAttach
              ? { ...clearTransientSendSteps(finishRunningSteps(next)), pending: false, paused: false }
              : next;
          });
          if (postDoneTail || terminalVoiceAttach) {
            markSessionOutputReady(sessionId);
            if (!postDoneTail && terminalVoiceAttach) {
              markRequestLocallyCompleted(requestId);
              setStreamRequestPhase(sessionId, requestId, "text_done_tail_open");
              clearHistoryRecovery(sessionId, requestId);
              clearSessionRequestState(sessionId, requestId);
              schedulePostDoneStreamClose(sessionId, requestId);
            }
          }
          return;
        }
            if (item.type === "phase" && (item.content || item.message)) {
              const phaseContent = redactInternalPromptText(item.content || item.message || "");
              if (!phaseContent) return;
              updateAssistantMessageForRequest(sessionId, assistantId, requestId, (message) => replaceCurrentPhase(message, phaseContent));
              return;
            }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(sessionId, assistantId, requestId, streamItemText(item));
            }
      },
      onError: () => {
        if (completedRequestIds.current[requestId]) {
          markStreamTerminal(sessionId, requestId, "completed");
          closeSessionStream(sessionId, requestId);
          return;
          }
          if (!isCurrentSessionRequest(sessionId, requestId)) return;
          setStreamRequestPhase(sessionId, requestId, "stalled");
          closeSessionStream(sessionId, requestId);
          scheduleHistoryRecovery(sessionId, requestId, [800, 2400, 5000]);
          scheduleStreamReconnect(sessionId, assistantId, requestId);
        }
      });
    streamCleanup.current = cleanup;
    streamCleanups.current[sessionId] = cleanup;
    streamCleanupRequestIds.current[sessionId] = requestId;
  }

  function isCompactCommand(text: string) {
    return /^\/(?:compact|context\s+clear)$/i.test(text.trim());
  }

  async function runCompactCommand(text: string) {
    const requestSessionId = activeSessionId;
    const createdAt = new Date().toISOString();
    const userMessage: ChatItem = {
      id: `u-compact-${Date.now()}`,
      role: "user",
      content: text || "/compact",
      createdAt
    };
    const assistantId = `a-compact-${Date.now()}`;
    setComposerText("");
    setAttachments([]);
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      userMessage,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt,
        steps: [{ type: "phase", content: "正在压缩上下文" }]
      }
    ]);
    try {
      const contextStartSeq = await clearRuntimeContext(requestSessionId);
      updateSessionMessages(requestSessionId, (current) => current.map((message) => {
        if (message.id === userMessage.id) return message;
        if (message.id === assistantId) {
          return {
            ...finishRunningSteps(message),
            content: "已压缩上下文。",
            pending: false,
            paused: false
          };
        }
        return { ...message, contextExcluded: true };
      }));
      setSessionUiState((current) => ({
        ...current,
        [requestSessionId]: {
          ...(current[requestSessionId] || {
            title: activeSessionTitle,
            projectId: sessionProjects[requestSessionId] || null,
            messages: [],
            composerText: "",
            attachments: []
          }),
          contextStartSeq,
          lastActivityAt: Date.now()
        }
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : "压缩上下文失败";
      updateAssistantMessage(requestSessionId, assistantId, (entry) => ({
        ...finishRunningSteps(entry, "error"),
        content: message,
        pending: false,
        paused: false
      }));
    }
  }

  async function sendNow(skipCapabilityCheck = false) {
    const text = composerText.trim();
    if (!text && !attachments.length) return;
    const previousRequestId = activeSessionRequestId;
    const previousSessionId = activeSessionId;

    if (isCompactCommand(text) && !attachments.length) {
      await runCompactCommand(text);
      return;
    }

    const enabledPacks = packs.filter((pack) => capabilityPackEnabled(pack.id));
    const neededPack = skipCapabilityCheck ? null : detectNeededPack(text, attachments, enabledPacks);

    const projectAttachment: FileAttachment | null = activeProject
      ? {
          file_path: activeProject.path,
          file_name: activeProject.name,
          file_type: "directory"
        }
      : null;
    const outboundAttachments = projectAttachment
      ? [projectAttachment, ...attachments.filter((file) => file.file_path !== projectAttachment.file_path)]
      : attachments;
    const displayText = text || "请处理这些附件";
    let hiddenContext = activeProject ? projectContextPrompt(activeProject) : "";

    let estimatedTokens = estimateTokens(`${hiddenContext}\n\n${displayText}`.trim(), outboundAttachments);
    let streamTextChars = 0;
    let streamToolChars = 0;
    let streamSawDelta = false;
    const observeStreamUsage = (item: StreamItem) => {
      const textPart = String(item.content || item.text || item.message || "");
      if (item.type === "delta" || item.type === "message_update" || item.type === "reasoning" || item.type === "thinking" || item.type === "phase") {
        streamTextChars += textPart.length;
        if (item.type === "delta" || item.type === "message_update") {
          streamSawDelta = true;
        }
      }
      if (item.type === "done" && !streamSawDelta) {
        streamTextChars += textPart.length;
      }
      if (item.type === "tool_start" || item.type === "tool_end") {
        streamToolChars += 80;
        try {
          streamToolChars += JSON.stringify(item.arguments ?? item.input ?? item.result ?? item.content ?? "").length;
        } catch {
          streamToolChars += String(item.result ?? item.content ?? "").length;
        }
      }
      if (item.type === "file" || item.type === "image" || item.type === "video" || item.type === "voice_attach") {
        streamToolChars += 120;
      }
    };
    const liveEstimatedTokens = () => estimatedTokens + Math.ceil(streamTextChars / 2) + Math.ceil(streamToolChars / 3);
    const userMessage: ChatItem = {
      id: `u-${Date.now()}`,
      role: "user",
      content: text || "请处理这些附件",
      attachments,
      createdAt: new Date().toISOString()
    };
    const assistantId = `a-${Date.now()}`;
    const requestSessionId = activeSessionId;
    const clientAttemptId = `attempt-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    latestSendAttemptRef.current[requestSessionId] = clientAttemptId;
    const { generation: sendGeneration, controller: preflightController } = beginSessionPreflight(requestSessionId);
    const restoreUnacceptedDraft = (message: string) => {
      if (latestSendAttemptRef.current[requestSessionId] !== clientAttemptId) return;
      clearAssistantPhaseTimers(assistantId);
      clearSessionPreflight(requestSessionId, preflightController);
      updateSessionMessages(requestSessionId, (current) => current.filter((item) => item.id !== userMessage.id && item.id !== assistantId));
      setComposerText(text);
      setAttachments(attachments);
      setApproval({
        type: "info",
        title: "消息未发送",
        message,
        actions: [
          {
            label: "重试发送",
            primary: true,
            onClick: () => {
              setApproval(null);
              void sendNow(skipCapabilityCheck);
            }
          },
          {
            label: "保留草稿",
            onClick: () => setApproval(null)
          }
        ]
      });
      markSessionOutputReady(requestSessionId);
    };
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      {
        ...userMessage,
        sendAttempt: {
          id: clientAttemptId,
          state: previousRequestId ? "stopping-previous" : "sending",
          interruptsRequestId: previousRequestId || undefined
        }
      },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt: new Date().toISOString(),
        sendAttempt: {
          id: clientAttemptId,
          state: previousRequestId ? "stopping-previous" : "sending",
          interruptsRequestId: previousRequestId || undefined
        },
        steps: [{ type: "phase", content: "正在发送" }]
      }
    ]);
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 800, "已收到，正在准备响应");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 1800, "正在检查额度");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 3600, "正在建立响应通道");
    queuePreflightPhase(requestSessionId, assistantId, sendGeneration, 6500, "正在组织上下文");
    if (!lockedSessionTitlesRef.current[requestSessionId]) {
      setActiveSessionTitle((current) => {
        const nextTitle = current === "新对话" ? shortTitle(text) : current;
        setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
        return nextTitle;
      });
    }
    if (activeProject) {
      setSessionProjects((current) => ({ ...current, [requestSessionId]: activeProject.id }));
    }
    setComposerText("");
    setAttachments([]);
    setApproval(null);

    if (neededPack?.policyMode === "disabled") {
      restoreUnacceptedDraft(`${neededPack.name} is disabled by policy. Please ask an administrator to enable or preinstall it.`);
      return;
    }

    if (neededPack) {
      updateAssistantMessage(requestSessionId, assistantId, (message) => replaceCurrentPhase(
        message,
        neededPack.discoveryOnly
          ? `Preparing find-skill discovery for ${neededPack.name}`
          : `Preparing capability setup for ${neededPack.name}`
      ));
      try {
        const request = await requestAgentInstallRequest({
          packId: neededPack.id,
          packName: neededPack.name,
          sessionId: requestSessionId
        });
        if (request.status === "error" || !request.prompt) {
          throw new Error(request.message || "Failed to prepare capability setup task");
        }
        const capabilityContext = [
          "Internal capability preflight:",
          "The visible user turn has already been recorded and must remain the only user request.",
          "Do not restate this preflight as user-authored content.",
          "Run required discovery or install steps as assistant/tool progress, then continue the original user request.",
          request.prompt
        ].join("\n");
        hiddenContext = [hiddenContext, capabilityContext].filter(Boolean).join("\n\n");
        estimatedTokens = estimateTokens(`${hiddenContext}\n\n${displayText}`.trim(), outboundAttachments);
        setInstallNotice({
          packId: neededPack.id,
          packName: neededPack.name,
          message: neededPack.discoveryOnly
            ? `${neededPack.name} will be handled through find skill in this response.`
            : `${neededPack.name} setup will run inside this response.`
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to prepare capability setup task";
        restoreUnacceptedDraft(message);
        return;
      }
    }

    const quota = await checkEnterpriseQuota(estimatedTokens).catch((error) => {
      setToast(error instanceof Error ? `额度检查暂不可用，已继续发送：${error.message}` : "额度检查暂不可用，已继续发送");
      return { ok: true, quota: { allowed: true } } as EnterpriseQuotaCheckResult;
    });
    if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
      clearAssistantPhaseTimers(assistantId);
      return;
    }
    if (quota.quota) {
      setQuotaSnapshot(quota.quota);
    }
    if (quota.quota && quota.quota.allowed === false) {
      const quotaMessage = quota.quota.reason || "当前账号暂时不能继续发送。";
      const authFailure = isEnterpriseAuthFailure(quota.quota) && !isQuotaLimitFailure(quota.quota);
      restoreUnacceptedDraft(authFailure ? `${quotaMessage} Please sign in again before sending.` : quotaMessage);
      return;
      setApproval({
        type: authFailure ? "error" : "quota",
        title: authFailure ? "登录状态异常" : "额度已达到上限",
        message: authFailure ? `${quotaMessage}。请重新登录后继续。` : quotaMessage,
        actions: authFailure ? [
          {
            label: "重新登录",
            primary: true,
            onClick: () => void logout()
          },
          {
            label: "知道了",
            onClick: () => setApproval(null)
          }
        ] : undefined
      });
      updateAssistantMessage(requestSessionId, assistantId, (message) => ({
        ...finishRunningSteps(message, "error"),
        content: authFailure ? "登录状态异常，请重新登录后继续。" : quotaMessage,
        pending: false
      }));
      clearAssistantPhaseTimers(assistantId);
      clearSessionPreflight(requestSessionId, preflightController);
      markSessionOutputReady(requestSessionId);
      return;
    }

    let usageReported = false;
    const reportChatUsage = (usage: TokenUsage | undefined, source: "provider" | "estimated") => {
      if (usageReported) return;
      usageReported = true;
      const providerTotal = usageTotal(usage);
      const localEstimate = liveEstimatedTokens();
      const totalTokens = Math.max(providerTotal, localEstimate, estimatedTokens);
      const usageSource = providerTotal >= localEstimate && providerTotal > 0 ? source : "estimated";
      void reportDesktopEvent({
        type: "usage",
        source: "Desktop",
        category: "chat",
        label: "message",
        amount: totalTokens,
        sessionId: requestSessionId,
        detail: {
          inputTokens: usage?.inputTokens || estimatedTokens,
          outputTokens: usage?.outputTokens || Math.max(0, totalTokens - estimatedTokens),
          totalTokens,
          model: usage?.model || currentModelName,
          provider: usage?.provider || "",
          estimatedTokens,
          streamEstimatedTokens: localEstimate,
          providerTotalTokens: providerTotal,
          usageSource
        }
      });
      setQuotaSnapshot((current) => current ? {
        ...current,
        dailyUsed: quotaNumber(current, "dailyUsed") + totalTokens,
        weeklyUsed: quotaNumber(current, "weeklyUsed") + totalTokens
      } : current);
      void checkEnterpriseQuota(0)
        .then((next) => {
          if (next.quota) setQuotaSnapshot(next.quota);
        })
        .catch(() => undefined);
    };

    try {
      const result = await sendChatMessage({
        sessionId: requestSessionId,
        message: displayText,
        hiddenContext,
        attachments: outboundAttachments,
        clientAttemptId,
        interruptsRequestId: previousRequestId || undefined
      });
      if (latestSendAttemptRef.current[requestSessionId] !== clientAttemptId) {
        clearAssistantPhaseTimers(assistantId);
        if (result.request_id) {
          void cancelChatRequest({ requestId: result.request_id, sessionId: requestSessionId }).catch(() => undefined);
        }
        updateSessionMessages(requestSessionId, (current) => current.filter((item) => item.id !== userMessage.id && item.id !== assistantId));
        return;
      }
      if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
        clearAssistantPhaseTimers(assistantId);
        if (result.request_id) {
          void cancelChatRequest({ requestId: result.request_id, sessionId: requestSessionId }).catch(() => undefined);
        }
        return;
      }
      if (result.status === "error") {
        const message = chatSendErrorMessage(result);
        if (isRetryableConcurrencyResult(result)) {
          void reportDesktopEvent({
            type: "warn",
            source: "Desktop",
            category: "runtime",
            label: "request_conflict_retryable",
            message,
            sessionId: requestSessionId,
            detail: {
              code: result.code || "",
              errorType: result.error_type || "",
              state: result.state || "",
              retryAfterMs: result.retry_after_ms || 0,
              activeRequestIds: result.active_request_ids || []
            }
          });
        }
        restoreUnacceptedDraft(message);
        return;
      }
      updateSessionMessages(requestSessionId, (current) => current.map((item) => (
        item.id === userMessage.id || item.id === assistantId
          ? { ...item, sendAttempt: item.sendAttempt ? { ...item.sendAttempt, state: "accepted" } : undefined }
          : item
      )));
      if (previousRequestId && result.same_session?.decision === "replacement_accepted") {
        closeSessionStream(previousSessionId, previousRequestId);
        clearSessionRequestState(previousSessionId, previousRequestId);
        updateSessionMessages(previousSessionId, (current) => current.map((item) => (
          item.role === "assistant" && item.requestId === previousRequestId && item.pending
            ? {
                ...finishRunningSteps(item, "cancelled"),
                content: item.content || "Stopped because a newer message was accepted.",
                pending: false,
                cancelled: true
              }
            : item
        )));
      }
      if (result.inline_reply) {
        const inlineReply = redactInternalPromptText(result.inline_reply || "");
        streamTextChars += inlineReply.length;
        updateSessionMessages(requestSessionId, (current) => current.map((item) => item.id === assistantId ? {
          ...clearTransientSendSteps(finishRunningSteps(item)),
          content: inlineReply,
          pending: false
        } : item));
        markSessionOutputReady(requestSessionId);
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
      }
      if (result.request_id && result.stream) {
        const requestId = result.request_id;
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
        setActiveRequestId(requestId);
        sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [requestSessionId]: requestId };
        setSessionRequestIds((current) => ({ ...current, [requestSessionId]: requestId }));
        streamRetryCounts.current[requestSessionId] = 0;
        scheduleHistoryRecovery(requestSessionId, requestId);
        beginStreamRequest(requestSessionId, assistantId, requestId);
        updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
          ...replaceCurrentPhase(message, "正在连接响应"),
          requestId
        }));
        const cleanup = openMessageStream({
          requestId,
          webPort: sidecarStatus.webPort,
          onItem: (item) => {
            if (item.request_id && item.request_id !== requestId) return;
            if (!shouldAcceptStreamItem(requestSessionId, requestId, item)) return;
            observeStreamUsage(item);
            flushStreamBoundary(requestSessionId, assistantId, requestId, item);
            if (isReplayGapStreamItem(item)) {
              handleReplayGapStreamItem(requestSessionId, assistantId, requestId, item);
              return;
            }
            if (isInterruptedStreamItem(item)) {
              handleInterruptedStreamItem(requestSessionId, assistantId, requestId, item);
              return;
            }
            if (item.type === "cancelled") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
                ...finishRunningSteps(message, "cancelled"),
                content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
                pending: false,
                cancelled: true
              }));
              markStreamTerminal(requestSessionId, requestId, "cancelled");
              markSessionOutputReady(requestSessionId);
              finishSessionRequest(requestSessionId, requestId);
              return;
            }
            if (item.type === "done") {
              if (typeof item.user_seq === "number") {
                updateSessionMessages(requestSessionId, (current) => current.map((message) => (
                  message.id === userMessage.id ? { ...message, userSeq: item.user_seq } : message
                )));
              }
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => {
                const nextMessage = item.artifact || item.artifacts
                  ? appendArtifact(finishRunningSteps(message), item)
                  : finishRunningSteps(message);
                return {
                  ...clearTransientSendSteps(nextMessage),
                  content: doneItemContent(item, message.content),
                  pending: false,
                  requestId,
                  recovery: undefined,
                  userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                  botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
                };
              });
              markRequestLocallyCompleted(requestId);
              setStreamRequestPhase(requestSessionId, requestId, "text_done_tail_open");
              markSessionOutputReady(requestSessionId);
              clearHistoryRecovery(requestSessionId, requestId);
              clearSessionRequestState(requestSessionId, requestId);
              schedulePostDoneStreamClose(requestSessionId, requestId);
              window.setTimeout(() => {
                void refreshSessionFromHistory(requestSessionId);
              }, 300);
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              return;
            }
            if (item.type === "error") {
              const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
              const staleRequest = /invalid request_id/i.test(message);
              markStreamTerminal(requestSessionId, requestId, "failed");
              finishSessionRequest(requestSessionId, requestId);
              if (staleRequest) {
                recoverStaleRequestFromHistory(requestSessionId, assistantId, requestId);
              } else {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (entry) => ({
                  ...finishRunningSteps(entry, "error"),
                  content: message,
                  pending: false,
                  paused: false,
                  recovery: {
                    kind: "failed",
                    requestId,
                    message,
                    recoverable: true,
                    retryable: true
                  }
                }));
                markSessionOutputReady(requestSessionId);
              }
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              reportStreamErrorTelemetry(requestSessionId, requestId, message, staleRequest);
              return;
            }
            if (item.type === "reasoning" || item.type === "thinking") {
              const chunk = item.content || item.text || "";
              if (chunk) {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendReasoningStep(message, chunk));
              }
              return;
            }
            if (item.type === "message_end") {
              if (item.has_tool_calls) {
                updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => flushIntermediateContent(finishRunningSteps(message)));
              }
              return;
            }
            if (item.type === "tool_permission_request") {
              const permissionRequestId = item.permission_request_id || "";
              if (!permissionRequestId) return;
              setApproval({
                type: "permission",
                title: item.title || "本机工具执行前确认",
                message: item.message || `EcoreX 将执行 ${item.tool || "tool"}，请确认是否允许。`,
                actions: [
                  {
                    label: "允许本次",
                    primary: true,
                    onClick: () => void answerToolPermission(permissionRequestId, "allow_once")
                  },
                  {
                    label: "始终允许",
                    onClick: () => void answerToolPermission(permissionRequestId, "always_allow")
                  },
                  {
                    label: "拒绝",
                    onClick: () => void answerToolPermission(permissionRequestId, "deny")
                  }
                ]
              });
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({
                ...replaceCurrentPhase(message, `等待本机工具授权：${item.tool || "tool"}`),
                pending: true
              }));
              return;
            }
            if (item.type === "tool_start") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolStart(message, item));
              return;
            }
            if (item.type === "tool_end") {
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => appendToolEnd(message, item));
              return;
            }
            if (item.type === "artifact") {
              const postDoneTail = Boolean(completedRequestIds.current[requestId]);
              if (postDoneTail) rememberPostDoneTailArtifacts(requestId, item);
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => (
                postDoneTail
                  ? clearTransientSendSteps(appendArtifact(finishRunningSteps(message), item, false))
                  : appendArtifact(message, item)
              ));
              if (postDoneTail) markSessionOutputReady(requestSessionId);
              return;
            }
            if (item.type === "image" || item.type === "video" || item.type === "audio" || item.type === "file" || item.type === "voice_attach") {
              const postDoneTail = Boolean(completedRequestIds.current[requestId]);
              const terminalVoiceAttach = isTerminalVoiceAttach(item);
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => {
                const next = appendMediaStep(message, item, !postDoneTail);
                return postDoneTail || terminalVoiceAttach
                  ? { ...clearTransientSendSteps(finishRunningSteps(next)), pending: false, paused: false }
                  : next;
              });
              if (postDoneTail || terminalVoiceAttach) {
                markSessionOutputReady(requestSessionId);
                if (!postDoneTail && terminalVoiceAttach) {
                  markRequestLocallyCompleted(requestId);
                  setStreamRequestPhase(requestSessionId, requestId, "text_done_tail_open");
                  clearHistoryRecovery(requestSessionId, requestId);
                  clearSessionRequestState(requestSessionId, requestId);
                  schedulePostDoneStreamClose(requestSessionId, requestId);
                }
              }
              return;
            }
            if (item.type === "phase" && (item.content || item.message)) {
              const phaseContent = redactInternalPromptText(item.content || item.message || "");
              if (!phaseContent) return;
              updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => replaceCurrentPhase(message, phaseContent));
              return;
            }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(requestSessionId, assistantId, requestId, streamItemText(item));
            }
          },
          onError: () => {
            if (completedRequestIds.current[requestId]) {
              markStreamTerminal(requestSessionId, requestId, "completed");
              closeSessionStream(requestSessionId, requestId);
              return;
            }
            if (!isCurrentSessionRequest(requestSessionId, requestId)) return;
            setStreamRequestPhase(requestSessionId, requestId, "stalled");
            closeSessionStream(requestSessionId, requestId);
            scheduleHistoryRecovery(requestSessionId, requestId, [800, 2400, 5000]);
            scheduleStreamReconnect(requestSessionId, assistantId, requestId);
          }
        });
        streamCleanup.current = cleanup;
        streamCleanups.current[requestSessionId] = cleanup;
        streamCleanupRequestIds.current[requestSessionId] = requestId;
      } else if (!result.inline_reply) {
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
        clearAssistantPhaseTimers(assistantId);
        clearSessionPreflight(requestSessionId, preflightController);
      }
      if (!lockedSessionTitlesRef.current[requestSessionId]) {
        generateSessionTitle({ sessionId: requestSessionId, userMessage: text || activeProject?.name || "项目会话" }).then((title) => {
          if (!title || lockedSessionTitlesRef.current[requestSessionId]) return;
          setSessionTitles((current) => ({ ...current, [requestSessionId]: title }));
          setSessionUiState((current) => ({
            ...current,
            [requestSessionId]: {
              ...(current[requestSessionId] || {
                messages: [],
                composerText: "",
                attachments: []
              }),
              projectId: sessionProjects[requestSessionId] || null,
              title
            }
          }));
          if (activeSessionIdRef.current === requestSessionId) {
            setActiveSessionTitle(title);
          }
        }).catch(() => undefined);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "发送失败";
      if (!isSessionPreflightCurrent(requestSessionId, sendGeneration, preflightController)) {
        clearAssistantPhaseTimers(assistantId);
        return;
      }
      restoreUnacceptedDraft(message);
      finishSessionRequest(requestSessionId);
      void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: requestSessionId });
    }
  }

  async function stopActiveRequest() {
    abortSessionPreflight(activeSessionId);
    clearAllPhaseTimers();
    const requestId = activeSessionRequestId;
    try {
      if (requestId) {
        await cancelChatRequest({ requestId, sessionId: activeSessionId });
      }
    } catch (error) {
      console.warn("[EcoreX] Failed to cancel active request", error);
    } finally {
      setApproval(null);
      closeSessionStream(activeSessionId, requestId);
      clearSessionRequestState(activeSessionId, requestId);
      updateSessionMessages(activeSessionId, (current) => current.map((message) => message.pending ? {
        ...finishRunningSteps(message, "cancelled"),
        content: message.content || "已停止",
        pending: false,
        cancelled: true
      } : message));
    }
  }

  function handleComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
    const isMeta = event.metaKey || event.ctrlKey;
    if (event.key === "Enter" && isMeta) {
      event.preventDefault();
      insertComposerNewline(event.currentTarget);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendNow();
      return;
    }
  }

  async function syncRuntimeUiStateNow() {
    const projectId = sessionProjects[activeSessionId] || null;
    const mergedState = pruneSessionUiState({
      ...sessionUiState,
      [activeSessionId]: {
        ...(sessionUiState[activeSessionId] || {}),
        title: activeSessionTitle,
        projectId,
        messages,
        composerText,
        attachments,
        contextStartSeq: sessionUiState[activeSessionId]?.contextStartSeq,
        lastActivityAt: latestMessageMs(messages) || sessionUiState[activeSessionId]?.lastActivityAt || Date.now()
      }
    });
    writeStorage(SESSION_UI_STORAGE_KEY, mergedState);
    if (sidecarStatus.state !== "running") return;
    await saveRuntimeUiState({
      version: 1,
      lastActiveSessionId: activeSessionId,
      activeProjectId: projectId,
      sessionUiState: mergedState,
      savedAt: new Date().toISOString()
    }).catch(() => undefined);
  }

  async function handleCheckForUpdates() {
    const status = await checkForUpdates().catch((error) => ({
      ...updateStatus,
      state: /app-update\.ya?ml|latest\.ya?ml/i.test(error instanceof Error ? error.message : String(error || "")) ? "not-available" as const : "error" as const,
      message: friendlyUpdateErrorMessage(error)
    }));
    if (status) {
      setUpdateStatus(status);
      if (status.message) setToast(status.message);
    }
  }

  async function handleInstallDownloadedUpdate() {
    await syncRuntimeUiStateNow();
    const status = await installDownloadedUpdate().catch((error) => ({
      ...updateStatus,
      state: "error" as const,
      message: friendlyUpdateErrorMessage(error) || "安装更新失败"
    }));
    if (status) {
      setUpdateStatus(status);
      if (status.message) setToast(status.message);
    }
  }

  function packActionLabel(pack: CapabilityPack, installing = false) {
    if (pack.discoveryOnly) {
      return installing ? "正在用 find skill" : "用 find skill";
    }
    return installing ? "正在安装" : "安装";
  }

  function shouldOpenCapabilitySettings(pack: CapabilityPack) {
    return !pack.discoveryOnly;
  }

  function packTaskNoun(pack: CapabilityPack) {
    return pack.discoveryOnly ? "find skill 任务" : "安装任务";
  }

  function watchAgentPackInstall(pack: CapabilityPack, onInstalled?: () => void) {
    if (installWatchers.current[pack.id]) return;
    const started = Date.now();
    const timer = window.setInterval(async () => {
      const nextPacks = await listCapabilityPacks().catch(() => null);
      if (nextPacks) {
        setPacks(nextPacks);
        const nextPack = nextPacks.find((item) => item.id === pack.id);
        if (nextPack?.installed) {
          window.clearInterval(timer);
          delete installWatchers.current[pack.id];
          setInstallingPackIds((current) => {
            const next = { ...current };
            delete next[pack.id];
            return next;
          });
          setInstallNotice((current) => current?.packId === pack.id ? null : current);
          setToast(`${pack.name} 已安装`);
          void loadRuntimeSnapshot().then(setRuntimeSnapshot).catch(() => undefined);
          onInstalled?.();
          return;
        }
        if (nextPack?.state === "failed") {
          window.clearInterval(timer);
          delete installWatchers.current[pack.id];
          setInstallingPackIds((current) => {
            const next = { ...current };
            delete next[pack.id];
            return next;
          });
          setInstallNotice((current) => current?.packId === pack.id ? null : current);
          setToast(nextPack.message || `${pack.name} 安装失败，请查看当前会话诊断`);
          return;
        }
      }
      if (Date.now() - started > 10 * 60 * 1000) {
        window.clearInterval(timer);
        delete installWatchers.current[pack.id];
        setInstallingPackIds((current) => {
          const next = { ...current };
          delete next[pack.id];
          return next;
        });
        setToast(`${pack.name} 安装未确认，请查看当前会话结果`);
      }
    }, 2000);
    installWatchers.current[pack.id] = timer;
  }

  async function startAgentInstallChatTask(pack: CapabilityPack, prompt: string, sessionId: string) {
    const requestSessionId = sessionId;
    const taskLabel = pack.discoveryOnly ? `用 find skill 发现能力：${pack.name}` : `安装能力包：${pack.name}`;
    const assistantId = `a-install-${Date.now()}`;
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
        createdAt: new Date().toISOString(),
        steps: [{ type: "phase", content: taskLabel }]
      }
    ]);
    const currentTitle = sessionTitles[requestSessionId]
      || sessionUiState[requestSessionId]?.title
      || (activeSessionIdRef.current === requestSessionId ? activeSessionTitle : "新对话");
    const nextTitle = currentTitle === "新对话" ? (pack.discoveryOnly ? `find skill ${pack.name}` : `安装 ${pack.name}`) : currentTitle;
    if (!lockedSessionTitlesRef.current[requestSessionId]) {
      setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
      if (activeSessionIdRef.current === requestSessionId) {
        setActiveSessionTitle(nextTitle);
      }
    }
    const result = await sendChatMessage({
      sessionId: requestSessionId,
      message: taskLabel,
      visibleMessage: "",
      hiddenContext: prompt,
      attachments: [],
      internalAction: true
    });
    if (result.status === "error") {
      throw new Error(chatSendErrorMessage(result));
    }
    if (result.inline_reply) {
      updateAssistantMessage(requestSessionId, assistantId, (message) => ({
        ...message,
        content: redactInternalPromptText(result.inline_reply || ""),
        pending: false
      }));
    }
    if (result.request_id && result.stream) {
      const requestId = result.request_id;
      setActiveRequestId(requestId);
      sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [requestSessionId]: requestId };
      setSessionRequestIds((current) => ({ ...current, [requestSessionId]: requestId }));
      streamRetryCounts.current[requestSessionId] = 0;
      updateAssistantMessageForRequest(requestSessionId, assistantId, requestId, (message) => ({ ...message, requestId }));
      attachMessageStream(requestSessionId, assistantId, requestId);
    }
  }

  async function handleInstallPack(pack: CapabilityPack, onInstalled?: () => void, targetSessionId?: string) {
    const requestSessionId = targetSessionId || activeSessionIdRef.current;
    if (pack.policyMode === "disabled") {
      if (shouldOpenCapabilitySettings(pack)) {
        setSettingsSection("abilities");
        setSettingsOpen(true);
      }
      setToast("管理员已禁用安装，请联系管理员预置能力包");
      return;
    }
    const targetRequestId = sessionRequestIdsRef.current[requestSessionId] || sessionRequestIds[requestSessionId] || "";
    const targetMessages = requestSessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[requestSessionId]?.messages || [];
    if (targetRequestId || targetMessages.some(isUiLiveAssistantMessage)) {
      const alreadyQueued = queuedInstallRef.current.some((item) => item.sessionId === requestSessionId && item.pack.id === pack.id);
      if (!alreadyQueued) {
        queuedInstallRef.current.push({ pack, onInstalled, sessionId: requestSessionId });
      }
      if (shouldOpenCapabilitySettings(pack)) {
        setSettingsSection("abilities");
        setSettingsOpen(true);
      }
      setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
      setInstallNotice({
        packId: pack.id,
        packName: pack.name,
        message: `${pack.name} 已排队，当前任务结束后自动${pack.discoveryOnly ? "走 find skill" : "安装"}`
      });
      setToast(`${pack.name} 已排队${pack.discoveryOnly ? "走 find skill" : "安装"}`);
      return;
    }
    if (shouldOpenCapabilitySettings(pack)) {
      setSettingsSection("abilities");
      setSettingsOpen(true);
    }
    setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
    setInstallNotice({
      packId: pack.id,
      packName: pack.name,
      message: pack.discoveryOnly ? `${pack.name} 正在通过 find skill 发现安装源，请稍后` : `${pack.name} 正在安装，请稍后`
    });
    try {
      const request = await requestAgentInstallRequest({
        packId: pack.id,
        packName: pack.name,
        sessionId: requestSessionId
      });
      if (request.status === "error" || !request.prompt) {
        throw new Error(request.message || "生成安装任务失败");
      }
      await startAgentInstallChatTask(pack, request.prompt, requestSessionId);
      if (pack.discoveryOnly) {
        setInstallingPackIds((current) => {
          const next = { ...current };
          delete next[pack.id];
          return next;
        });
        setInstallNotice({
          packId: pack.id,
          packName: pack.name,
          message: `${pack.name} find skill 任务已创建`
        });
      } else {
        watchAgentPackInstall(pack, onInstalled);
      }
      setToast(`${pack.name} ${packTaskNoun(pack)}已创建`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : `${pack.name} ${packTaskNoun(pack)}创建失败`);
      setInstallingPackIds((current) => {
        const next = { ...current };
        delete next[pack.id];
        return next;
      });
      setInstallNotice((current) => current?.packId === pack.id ? null : current);
    }
  }

  function requestOpenFile(file: FileAttachment) {
    setApproval({
      type: "open-file",
      title: "打开文件前确认",
      message: `EcoreX 将在系统中打开 ${file.file_name}。`,
      file
    });
  }

  function previewOrOpenFile(file: FileAttachment) {
    if (!isImageAttachment(file)) {
      requestOpenFile(file);
      return;
    }
    setPreviewFile({
      ...file,
      file_type: "image",
      previewDataUrl: attachmentPreviewUrl(file)
    });
  }

  async function confirmOpenFile(file: FileAttachment) {
    if (activeProject?.path) {
      await registerProjectFolderPath(activeProject.path).catch(() => null);
    }
    const result = await openLocalPath(file.file_path);
    setApproval(null);
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  async function answerToolPermission(requestId: string, decision: "allow_once" | "always_allow" | "deny") {
    try {
      const result = await decideToolPermission({
        requestId,
        decision,
        remember: decision === "always_allow"
      });
      if (result.status === "error") {
        setToast(result.message || "权限确认已失效");
      }
    } catch (error) {
      setToast(error instanceof Error ? error.message : "权限确认失败");
    } finally {
      setApproval(null);
    }
  }

  async function copyMessageText(message: ChatItem) {
    const text = plainTextForMessage(message);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "true");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    setCopiedMessageId(message.id);
    window.setTimeout(() => setCopiedMessageId((current) => current === message.id ? "" : current), 1400);
  }

  function downloadJsonFile(fileName: string, payload: unknown) {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function handleExportDiagnostics() {
    try {
      const bundle = await exportDiagnosticsBundle({
        sessionId: activeSessionId,
        requestId: activeSessionRequestId || undefined
      });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJsonFile(`ecorex-diagnostics-${stamp}.json`, bundle);
      await navigator.clipboard?.writeText(JSON.stringify(bundle, null, 2)).catch(() => undefined);
      setToast("诊断包已生成并复制到剪贴板");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "诊断包生成失败");
    }
  }

  async function refreshRunCenter(showToast = true) {
    const snapshot = await loadRuntimeSnapshot();
    setRuntimeSnapshot(snapshot);
    if (showToast) setToast("Run Center refreshed");
  }

  function openRunCenterSurface() {
    if (!runCenterDevVisible) return;
    setRunCenterOpen(true);
    void refreshRunCenter(false).catch(() => undefined);
  }

  async function openRunCenterSession(request: RuntimeActiveRequest, options: { closeSurface?: boolean } = {}) {
    if (isRunCenterSubagentRequest(request)) {
      setToast("Subagent runs are visible in Run Center; export diagnostics for details");
      return;
    }
    if (isRunCenterSchedulerRequest(request)) {
      setToast("Scheduler runs are visible in Run Center; export diagnostics for details");
      return;
    }
    const sessionId = String(request.session_id || "");
    if (!sessionId) {
      setToast("Run Center item has no session id");
      return;
    }
    const existing = allSessions.find((row) => row.id === sessionId);
    const requestId = request.request_id ? String(request.request_id) : undefined;
    const state = runCenterState(request);
    const scopedRow: SessionRow = {
      ...(existing || {
        id: sessionId,
        title: sessionId,
        detail: String(request.run_type || request.source || ""),
        activityAt: request.updated_at || request.created_at,
        createdAt: request.created_at,
        updatedAt: request.updated_at || request.created_at || "running",
        status: state === "failed" ? "failed" : state === "cancelling" ? "cancelling" : "waiting"
      }),
      id: sessionId,
      requestId,
      streamAvailable: state !== "failed" && request.stream_available !== false,
      cancelling: state === "cancelling",
      status: state === "failed" ? "failed" : state === "cancelling" ? "cancelling" : existing?.status || "waiting"
    };
    await selectSession(scopedRow);
    if (options.closeSurface) {
      setRunCenterOpen(false);
    }
  }

  async function openRunCenterStaleLockSession(lock: RuntimeSessionLock, options: { closeSurface?: boolean } = {}) {
    if (!lock.session_id) return;
    await selectSession({
      id: String(lock.session_id),
      title: String(lock.session_id),
      detail: "stale lock",
      activityAt: Date.now(),
      updatedAt: Date.now(),
      status: "failed"
    });
    if (options.closeSurface) {
      setRunCenterOpen(false);
    }
  }

  function runCenterRetryPolicy(request: RuntimeActiveRequest) {
    const sessionId = String(request.session_id || "");
    const state = runCenterState(request);
    const retryAfterMs = Number(request.retry_after_ms || 0);
    if (request.actions && request.actions.retry === false) {
      return {
        enabled: false,
        title: request.retry_disabled_reason || "Retry is unavailable for this run"
      };
    }
    if (request.actions?.retry === true) {
      return {
        enabled: true,
        title: retryAfterMs > 0
          ? `Prepare a retry after ${Math.ceil(retryAfterMs / 1000)}s`
          : "Open the session and prepare a retry prompt"
      };
    }
    if (isRunCenterSubagentRequest(request)) {
      return {
        enabled: false,
        title: "Subagent runs are stop/diagnostics-only until subagent replay is available"
      };
    }
    if (isRunCenterSchedulerRequest(request)) {
      return {
        enabled: false,
        title: "Scheduler runs are stop/diagnostics-only until scheduler replay is available"
      };
    }
    if (!sessionId) {
      return {
        enabled: false,
        title: "Retry requires a chat session id"
      };
    }
    if (state !== "failed") {
      return {
        enabled: false,
        title: state === "cancelling" ? "Retry is available after stopping finishes" : "Retry is available for failed chat runs"
      };
    }
    if (request.retryable === false && request.recoverable === false) {
      return {
        enabled: false,
        title: "This failed run is marked non-retryable"
      };
    }
    return {
      enabled: true,
      title: retryAfterMs > 0
        ? `Prepare a retry after ${Math.ceil(retryAfterMs / 1000)}s`
        : "Open the session and prepare a retry prompt"
    };
  }

  async function retryRunCenterRequest(request: RuntimeActiveRequest) {
    const policy = runCenterRetryPolicy(request);
    if (!policy.enabled) {
      setToast(policy.title);
      return;
    }
    await openRunCenterSession(request);
    const requestId = String(request.request_id || "");
    const prepared = await prepareRetryDraft(requestId, String(request.session_id || ""));
    if (prepared) setToast("Run Center retry prepared; review and send.");
    setRunCenterOpen(false);
  }

  async function prepareRetryDraft(requestId: string, sessionId = activeSessionIdRef.current) {
    if (!requestId) {
      setToast("Retry requires a request id");
      return false;
    }
    try {
      const result = await prepareRequestRetry({ requestId, sessionId });
      if (result.recoverable) {
        void refreshSessionFromHistory(sessionId);
      }
      if (result.status === "error" || !result.retryable || !result.prompt) {
        setToast(result.message || "This request cannot be safely retried yet");
        return false;
      }
      setComposerText(result.prompt);
      setAttachments((result.attachments || []).filter(isDurableLocalAttachment));
      focusComposerSoon();
      setToast(result.exactReplay || result.exact_replay ? "Retry draft prepared; review and send." : "Retry draft prepared from latest history; review before sending.");
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Retry prepare failed");
      return false;
    }
  }

  async function stopRunCenterRequest(request: RuntimeActiveRequest) {
    const requestId = String(request.request_id || "");
    const sessionId = String(request.session_id || "");
    if (!requestId && !sessionId) {
      setToast("Run Center item cannot be stopped");
      return;
    }
    try {
      if (isRunCenterSubagentRequest(request)) {
        const taskId = getRunCenterSubagentTaskId(request);
        if (!taskId) {
          setToast("Subagent run cannot be stopped without task id");
          return;
        }
        try {
          await cancelSubagentTask(taskId);
        } catch (subagentError) {
          const fallback = await cancelChatRequest({ requestId, sessionId });
          if (Number(fallback.cancelled || 0) <= 0) {
            throw subagentError;
          }
        }
        setRuntimeSnapshot(await loadRuntimeSnapshot());
        setToast("Subagent stop requested");
        return;
      }
      const result = await cancelChatRequest({ requestId, sessionId });
      if (Number(result.cancelled || 0) <= 0) {
        throw new Error("Run Center stop found no cancellable runtime row");
      }
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast(isRunCenterSchedulerRequest(request) ? "Scheduler stop requested" : "Stop requested");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Stop request failed");
    }
  }

  async function exportRunCenterDiagnostics(request: RuntimeActiveRequest) {
    try {
      const requestId = request.request_id ? String(request.request_id) : undefined;
      const sessionId = request.session_id ? String(request.session_id) : undefined;
      const bundle = await exportDiagnosticsBundle({ sessionId, requestId });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJsonFile(`ecorex-run-diagnostics-${stamp}.json`, bundle);
      setToast("Run diagnostics generated");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Run diagnostics failed");
    }
  }

  async function openArtifactFile(
    file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"]; open_action?: "preview" | "open" | "reveal" | "copy" | "openWith"; previewDataUrl?: string; preview_url?: string },
    sessionId = activeSessionIdRef.current
  ) {
    const rawPath = normalizeLocalSource(file.file_path);
    if (!rawPath) return;
    let action = file.open_action || "open";
    const fileType = file.file_type || "file";
    const artifactSessionId = sessionId;
    const resolvedPath = resolveArtifactPathForSession(artifactSessionId, rawPath);
    if (action === "preview") {
      if (fileType !== "image") {
        action = "open";
      } else {
        const previewPath = isRuntimePreviewPath(rawPath) ? rawPath : resolvedPath;
        setPreviewFile({
          file_path: previewPath,
          file_name: file.file_name,
          file_type: "image",
          previewDataUrl: file.previewDataUrl || (file.preview_url ? filePreviewUrl(file.preview_url, sidecarStatus.webPort) : filePreviewUrl(previewPath, sidecarStatus.webPort)),
          preview_url: file.preview_url
        });
        return;
      }
    }
    if (action === "copy") {
      await navigator.clipboard?.writeText(resolvedPath || rawPath).catch(() => undefined);
      setToast("路径已复制");
      return;
    }
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    const projectPath = projectPathForSession(artifactSessionId);
    if (projectPath) {
      await registerProjectFolderPath(projectPath).catch(() => null);
    }
    let result = "";
    const openAction: OpenPathAction = action === "reveal" ? "reveal" : action === "openWith" ? "openWith" : "open";
    for (const candidate of candidates) {
      try {
        result = await openLocalPath(candidate, openAction);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error || "");
        result = message;
        if (isOpenPathDeniedMessage(message)) {
          break;
        }
        if (isLocalAbsolutePath(candidate) && isOpenPathBridgeFailure(message)) {
          result = await openLocalPath(candidate, openAction);
        }
      }
      if (isOpenPathDeniedMessage(result) || !isOpenPathNotFoundMessage(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  async function legacyOpenArtifactFile(file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"] }, sessionId = activeSessionIdRef.current) {
    const rawPath = normalizeLocalSource(file.file_path);
    if (!rawPath) return;
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const artifactSessionId = sessionId;
    const resolvedPath = resolveArtifactPathForSession(artifactSessionId, rawPath);
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    const projectPath = projectPathForSession(artifactSessionId);
    if (projectPath) {
      await registerProjectFolderPath(projectPath).catch(() => null);
    }
    let result = "";
    for (const candidate of candidates) {
      try {
        result = await openLocalPath(candidate);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error || "");
        result = message;
        if (isOpenPathDeniedMessage(message)) {
          break;
        }
        if (isLocalAbsolutePath(candidate) && isOpenPathBridgeFailure(message)) {
          result = await openLocalPath(candidate);
        }
      }
      if (isOpenPathDeniedMessage(result) || !isOpenPathNotFoundMessage(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  const handleOpenMessageLocalFile = useCallback((file: LocalFilePayload) => {
    void openArtifactFile(file, activeSessionId);
  }, [activeSessionId, activeProject?.path, sidecarStatus.webPort, sessionProjects, projects]);

  const messageLocalFilePreviewUrl = useCallback((filePath: string) => (
    filePreviewUrl(resolveArtifactPathForSession(activeSessionId, filePath), sidecarStatus.webPort)
  ), [activeSessionId, sidecarStatus.webPort, sessionProjects, projects]);

  const messageLocalFileStat = useCallback((filePath: string) => (
    statArtifactPath(filePath, activeSessionId)
  ), [activeSessionId, sessionProjects, projects]);

  const messageLocalJson = useCallback((filePath: string) => (
    readArtifactStatusJson(filePath, activeSessionId)
  ), [activeSessionId, sessionProjects, projects]);

  async function logout() {
    await enterpriseLogout();
    setSession(null);
    setQuotaSnapshot(null);
    setMessages([]);
    setApproval(null);
  }

  async function submitPasswordChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (passwordDraft.newPassword.length < 8) {
      setApproval({ type: "error", title: "密码太短", message: "新密码至少需要 8 个字符。" });
      return;
    }
    if (passwordDraft.newPassword !== passwordDraft.confirmPassword) {
      setApproval({ type: "error", title: "两次密码不一致", message: "请重新输入并确认新密码。" });
      return;
    }
    setPasswordBusy(true);
    try {
      const nextSession = await enterpriseChangePassword({
        oldPassword: passwordDraft.oldPassword,
        newPassword: passwordDraft.newPassword
      });
      setSession(nextSession);
      setPasswordDraft({ oldPassword: "", newPassword: "", confirmPassword: "" });
      setToast("密码已更新");
    } catch (error) {
      setApproval({ type: "error", title: "密码修改失败", message: error instanceof Error ? error.message : "请稍后重试。" });
    } finally {
      setPasswordBusy(false);
    }
  }

  function closeReleaseNotes() {
    const seenVersion = runtimeSnapshot.releaseNotes?.version || runtimeSnapshot.version;
    if (seenVersion) {
      releaseNotesDismissedVersion.current = seenVersion;
      try {
        window.localStorage.setItem(RELEASE_NOTES_SEEN_STORAGE_KEY, seenVersion);
      } catch {
        // Ignore storage failures; closing should still work.
      }
    }
    setReleaseNotesOpen(false);
  }

  const browserPack = packs.find((pack) => pack.id === "browser-automation");
  const abilityRows = [
    {
      id: "web_search",
      name: "联网搜索",
      detail: toolEnabled(runtimeSnapshot.tools, "web_search") ? "搜索工具已加载" : "已启用入口，等待搜索服务凭据",
      enabled: toolEnabled(runtimeSnapshot.tools, "web_search"),
      icon: <Globe2 aria-hidden="true" />
    },
    {
      id: "bash",
      name: "Bash / Shell",
      detail: toolEnabled(runtimeSnapshot.tools, "bash") ? "可在项目上下文中执行命令" : "运行时尚未返回 shell 工具",
      enabled: toolEnabled(runtimeSnapshot.tools, "bash"),
      icon: <SquareTerminal aria-hidden="true" />
    },
    {
      id: "files",
      name: "本地文件读写",
      detail: ["read", "write", "edit", "ls"].every((name) => toolEnabled(runtimeSnapshot.tools, name))
        ? "读取、写入、编辑、目录浏览已就绪"
        : "基础文件工具未全部加载",
      enabled: ["read", "write", "edit", "ls"].every((name) => toolEnabled(runtimeSnapshot.tools, name)),
      icon: <HardDrive aria-hidden="true" />
    },
    {
      id: "vision",
      name: "OCR / 图像理解",
      detail: toolEnabled(runtimeSnapshot.tools, "vision") ? "图像理解工具已开启，模型凭据由企业策略决定" : "等待视觉模型或工具加载",
      enabled: toolEnabled(runtimeSnapshot.tools, "vision"),
      icon: <ImageIcon aria-hidden="true" />
    },
    {
      id: "image-generation",
      name: "Image Gen",
      detail: extensionSkillEnabled(runtimeSnapshot, "image-generation") ? "图像生成 Skill 已开启" : "等待开启图像生成 Skill",
      enabled: extensionSkillEnabled(runtimeSnapshot, "image-generation"),
      icon: <WandSparkles aria-hidden="true" />
    },
    {
      id: "browser",
      name: "Playwright 浏览器",
      detail: toolEnabled(runtimeSnapshot.tools, "browser")
        ? "浏览器工具已加载"
        : browserPack?.installed
          ? "能力包已安装，等待运行时刷新"
          : "首次使用会安装 Playwright 与 Chromium",
      enabled: toolEnabled(runtimeSnapshot.tools, "browser") || Boolean(browserPack?.installed),
      icon: <Globe2 aria-hidden="true" />,
      pack: browserPack
    },
    {
      id: "memory",
      name: "项目记忆",
      detail: activeProject ? `写入 ${activeProject.name} 的 .ecorex/project-memory.md` : "通用会话保留原项目内置记忆入口",
      enabled: true,
      icon: <Brain aria-hidden="true" />
    }
  ];
  const activeProjectMemoryPath = activeProject?.memoryPath || (activeProject ? `${activeProject.path}\\.ecorex\\project-memory.md` : "");
  const hasPendingAssistantMessage = messages.some(isUiLiveAssistantMessage);
  const visibleMessages = messages.filter((message) => !isSilentPausedAssistantMessage(message));
  const composerHasPayload = Boolean(composerText.trim() || attachments.length);
  const sessionRowNeedsReveal = (row: SessionRow) => {
    const cachedMessages = sessionUiState[row.id]?.messages || [];
    const isRunning = row.status === "waiting" || row.status === "cancelling" || Boolean(row.requestId) || Boolean(sessionRequestIds[row.id]) || cachedMessages.some(isUiLiveAssistantMessage);
    return row.id === activeSessionId || isRunning || Boolean(unreadSessionIds[row.id]) || Boolean(searchQuery.trim());
  };
  const projectsForceRevealed = projectSessionGroups.some(({ project, sessions }) => (
    project.id === activeProjectId || sessions.some(sessionRowNeedsReveal)
  ));
  const projectsSectionCollapsed = sidebarCollapse.projectsSection && !projectsForceRevealed && !searchQuery.trim();
  const generalForceRevealed = generalSessions.some(sessionRowNeedsReveal);
  const generalSessionsCollapsed = sidebarCollapse.generalSessions && !generalForceRevealed && !searchQuery.trim();
  const currentComposerPermissionMode: PermissionMode = permissionState?.mode || "smart-ask";
  const releaseNotes = runtimeSnapshot.releaseNotes;
  const updateVisible = ["available", "downloading", "downloaded", "blocked", "error"].includes(updateStatus.state);
  const updatePrimaryLabel = updateStatus.state === "downloaded"
    ? "重启安装"
    : updateStatus.platform === "win32" && updateStatus.state === "downloading"
      ? "后台下载中"
      : "打开下载页";
  const settingsNav: Array<{ id: SettingsSection; label: string; icon: ReactNode }> = [
    { id: "account", label: "账号", icon: <UserRound aria-hidden="true" /> },
    { id: "projects", label: "项目", icon: <FolderOpen aria-hidden="true" /> },
    { id: "abilities", label: "能力", icon: <Sparkles aria-hidden="true" /> },
    { id: "permissions", label: "权限", icon: <ShieldCheck aria-hidden="true" /> },
    { id: "memory", label: "记忆", icon: <Brain aria-hidden="true" /> },
    { id: "diagnostics", label: "诊断", icon: <Database aria-hidden="true" /> }
  ];

  function renderRunCenterPanel(surface: "settings" | "primary" = "settings") {
    return (
      <div className={`run-center-panel is-${surface}`} aria-label="Run Center" data-run-center-surface={surface}>
        <div className="run-center-head">
          <div>
            <strong>Run Center</strong>
            <span>{runCenterRequests.length} active/recent / {runCenterStaleLocks.length} stale</span>
          </div>
          <button type="button" onClick={() => void refreshRunCenter()} title="Refresh Run Center">
            <RefreshCw aria-hidden="true" />
            Refresh
          </button>
        </div>
        <div className="run-center-stats" aria-label="Run state summary">
          <span><Activity aria-hidden="true" />{runCenterStats.running} running</span>
          <span className="is-cancelling"><Square aria-hidden="true" />{runCenterStats.cancelling} stopping</span>
          <span className="is-failed"><AlertTriangle aria-hidden="true" />{runCenterStats.failed} failed</span>
          <span className="is-stale"><HardDrive aria-hidden="true" />{runCenterStats.stale} stale</span>
        </div>
        <div className="run-center-list">
          {runCenterRequests.map((request) => {
            const requestId = String(request.request_id || "");
            const sessionId = String(request.session_id || "");
            const isSubagent = isRunCenterSubagentRequest(request);
            const isScheduler = isRunCenterSchedulerRequest(request);
            const diagnosticsOnly = isSubagent || isScheduler;
            const subagentTaskId = isSubagent ? getRunCenterSubagentTaskId(request) : "";
            const age = formatRunAge(request.cancelled ? request.cancel_age_seconds ?? request.age_seconds : request.age_seconds);
            const diagnosticsOnlyTitle = isScheduler ? "Scheduler runs are diagnostics-only here" : "Subagent runs are diagnostics-only here";
            const retryPolicy = runCenterRetryPolicy(request);
            const openAllowed = request.actions?.open ?? !diagnosticsOnly;
            const stopAllowed = request.actions?.stop ?? !(runCenterState(request) === "failed" || (isSubagent && !subagentTaskId));
            return (
              <article className={`run-center-row ${runCenterStateClass(request)}`} key={requestId || `${sessionId}-${request.source || "request"}`}>
                <div className="run-center-row-main">
                  <span className="run-center-state">{runCenterStateLabel(request)}</span>
                  <strong>{sessionId || request.run_type || request.source || "runtime run"}</strong>
                  <small>{shortRequestId(requestId)}{request.phase ? ` · ${request.phase}` : ""}{age ? ` · ${age}` : ""}</small>
                </div>
                <div className="run-center-actions">
                  <button type="button" onClick={() => void openRunCenterSession(request, { closeSurface: surface === "primary" })} disabled={!openAllowed} title={!openAllowed ? diagnosticsOnlyTitle : "Open or recover session"}>
                    <FolderOpen aria-hidden="true" />
                    Open
                  </button>
                  <button type="button" onClick={() => void retryRunCenterRequest(request)} disabled={!retryPolicy.enabled} title={retryPolicy.title}>
                    <RefreshCw aria-hidden="true" />
                    Retry
                  </button>
                  <button type="button" onClick={() => void exportRunCenterDiagnostics(request)} title="Export diagnostics for this run">
                    <ArrowDownToLine aria-hidden="true" />
                    Diagnostics
                  </button>
                  <button
                    type="button"
                    onClick={() => void stopRunCenterRequest(request)}
                    disabled={!stopAllowed}
                    title={isScheduler ? "Stop scheduler run" : isSubagent ? (subagentTaskId ? "Stop subagent run" : "Subagent task id unavailable") : "Stop run"}
                  >
                    <Square aria-hidden="true" />
                    Stop
                  </button>
                </div>
              </article>
            );
          })}
          {runCenterStaleLocks.map((lock, index) => (
            <article className="run-center-row is-stale" key={`${lock.path || lock.session_id || "lock"}-${index}`}>
              <div className="run-center-row-main">
                <span className="run-center-state">Stale</span>
                <strong>{lock.session_id || "session lock"}</strong>
                <small>{lock.removed ? "removed" : lock.dead_owner ? "dead owner" : "stale"}{formatRunAge(lock.age_seconds) ? ` · ${formatRunAge(lock.age_seconds)}` : ""}</small>
              </div>
              {lock.session_id && (
                <div className="run-center-actions">
                  <button type="button" onClick={() => void openRunCenterStaleLockSession(lock, { closeSurface: surface === "primary" })} title="Open session">
                    <FolderOpen aria-hidden="true" />
                    Open
                  </button>
                </div>
              )}
            </article>
          ))}
          {!runCenterRequests.length && !runCenterStaleLocks.length && (
            <div className="session-empty">No active or stale runs</div>
          )}
        </div>
      </div>
    );
  }

  const renderSessionRow = (row: SessionRow) => {
    const cachedMessages = sessionUiState[row.id]?.messages || [];
    const isRunning = row.status === "waiting" || row.status === "cancelling" || Boolean(row.requestId) || Boolean(sessionRequestIds[row.id]) || cachedMessages.some(isUiLiveAssistantMessage) || (row.id === activeSessionId && hasPendingAssistantMessage);
    const isActive = row.id === activeSessionId;
    const hasUnread = Boolean(unreadSessionIds[row.id]) && !isActive && !isRunning;
    const waitingReply = isActive && Boolean(approval);
    const rowTitle = [row.title, row.detail, formatTime(row.updatedAt)].filter(Boolean).join("\n");
    return (
      <article className={`session-row is-${isRunning ? "waiting" : row.status}${isActive ? " is-active" : ""}${waitingReply ? " is-awaiting-reply" : ""}${hasUnread ? " is-unread" : ""}`} key={row.id}>
        <button
          className="session-main"
          type="button"
          onClick={() => void selectSession(row)}
          title={rowTitle}
          data-tooltip={rowTitle}
          aria-current={isActive ? "page" : undefined}
        >
          {isRunning ? <ThinkingIndicator compact /> : hasUnread ? <span className="session-unread-dot" aria-hidden="true" /> : row.projectId ? <FolderOpen aria-hidden="true" /> : <Bot aria-hidden="true" />}
          <span className="session-line"><strong>{row.title}</strong>{row.detail ? <small>{row.detail}</small> : null}</span>
          <em>{waitingReply ? <span className="session-waiting-reply">等待回复</span> : formatTime(row.updatedAt)}</em>
        </button>
        <div className="session-actions">
          <button type="button" onClick={() => togglePinSession(row)} title={row.pinned ? "取消置顶" : "置顶会话"} aria-label={row.pinned ? "取消置顶" : "置顶会话"}>
            {row.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
          </button>
            <button type="button" onClick={() => void renameSession(row)} title="重命名会话" aria-label="重命名会话"><Pencil aria-hidden="true" /></button>
            <button type="button" onClick={() => void removeSession(row)} title="删除会话" aria-label="删除会话"><Trash2 aria-hidden="true" /></button>
        </div>
      </article>
    );
  };

  function renderRecoveryActions(message: ChatItem, sessionId: string) {
    const recovery = message.recovery;
    const requestId = recovery?.requestId || message.requestId || "";
    if (!recovery && !message.sendAttempt) return null;
    if (message.sendAttempt && message.sendAttempt.state !== "accepted") {
      return <div className="message-recovery-actions"><span>Sending while stopping the previous response</span></div>;
    }
    if (!recovery) return null;
    return (
      <div className="message-recovery-actions">
        <span>{recovery.message}</span>
        {requestId && message.pending && (
          <button type="button" onClick={() => attachMessageStream(sessionId, message.id, requestId)}>
            <RefreshCw aria-hidden="true" />Reconnect
          </button>
        )}
        {recovery.recoverable && (
          <button type="button" onClick={() => void refreshSessionFromHistory(sessionId)}>
            <BookOpen aria-hidden="true" />Recover
          </button>
        )}
        {requestId && message.pending && (
          <button type="button" onClick={() => void cancelChatRequest({ requestId, sessionId }).then(() => stopActiveRequest()).catch(() => undefined)}>
            <Square aria-hidden="true" />Stop
          </button>
        )}
        {requestId && recovery.retryable && (
          <button type="button" onClick={() => void prepareRetryDraft(requestId, sessionId)}>
            <RefreshCw aria-hidden="true" />Retry draft
          </button>
        )}
        <button type="button" onClick={() => void exportDiagnosticsBundle({ sessionId, requestId: requestId || undefined }).then((bundle) => {
          const stamp = new Date().toISOString().replace(/[:.]/g, "-");
          downloadJsonFile(`ecorex-recovery-diagnostics-${stamp}.json`, bundle);
        }).catch((error) => setToast(error instanceof Error ? error.message : "Diagnostics export failed"))}>
          <FileText aria-hidden="true" />Diagnostics
        </button>
      </div>
    );
  }

  if (!authChecked) {
    return <main className="auth-shell"><WindowBrand version={appVersion} /><section className="auth-panel"><p>正在检查登录状态</p></section></main>;
  }

  if (!session) {
    return <AuthGate onLogin={(next) => {
      if (!next) return;
      setSession(next);
      setQuotaSnapshot((next.quota || null) as UsageQuota | null);
    }} version={appVersion} />;
  }

  return (
    <main className="app-shell">
      <WindowBrand version={appVersion} />
      <aside className="session-sidebar">
        <div className="sidebar-actions">
          <button onClick={() => startNewSession(null)} title="创建不绑定项目的通用会话" data-tooltip="创建不绑定项目的通用会话" data-tooltip-position="bottom-left"><Plus aria-hidden="true" />新对话</button>
          <label className="search-box" title="搜索会话标题和摘要">
            <Search aria-hidden="true" />
            <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="搜索会话" />
          </label>
        </div>

        <section className="project-panel" aria-label="项目">
          <div className="sidebar-section-title">
            <button className="sidebar-collapse-button" type="button" onClick={() => setSidebarCollapse((current) => ({ ...current, projectsSection: !current.projectsSection }))} aria-expanded={!projectsSectionCollapsed} title={projectsSectionCollapsed ? "展开项目" : "折叠项目"}>
              {projectsSectionCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
              <span>项目</span>
            </button>
            <button className="icon-button" type="button" onClick={() => void addProject()} title="添加项目文件夹">
              <FolderPlus aria-hidden="true" />
            </button>
          </div>
          {projectsSectionCollapsed ? null : projectSessionGroups.length === 0 ? (
            <button className="project-empty" type="button" onClick={() => void addProject()} title="选择一个本地文件夹作为项目">
              <FolderOpen aria-hidden="true" />
              <span>添加项目文件夹</span>
            </button>
          ) : (
            <div className="project-list">
              {projectSessionGroups.slice(0, 8).map(({ project, sessions }) => {
                const forceRevealGroup = project.id === activeProjectId || sessions.some(sessionRowNeedsReveal);
                const groupCollapsed = Boolean(sidebarCollapse.projectGroups[project.id]) && !forceRevealGroup && !searchQuery.trim();
                return (
                <article className={`project-group ${project.id === activeProjectId ? "is-active" : ""}${groupCollapsed ? " is-collapsed" : ""}`} key={project.id}>
                  <div
                    className="project-row"
                    onContextMenu={(event) => showProjectMenu(event, project)}
                  >
                    <button
                      type="button"
                      onClick={() => selectOrCreateProjectSession(project)}
                      title={`${project.name}\n${project.path}\n项目记忆：${project.memoryPath || ".ecorex/project-memory.md"}`}
                    >
                      <FolderOpen aria-hidden="true" />
                      <span>{project.name}</span>
                    </button>
                    <button className="project-collapse-button" type="button" title={groupCollapsed ? "展开项目会话" : "折叠项目会话"} aria-expanded={!groupCollapsed} onClick={() => setSidebarCollapse((current) => ({ ...current, projectGroups: { ...current.projectGroups, [project.id]: !current.projectGroups[project.id] } }))}>
                      {groupCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
                    </button>
                    <button className="project-new-session-button" type="button" title={`为 ${project.name} 创建新会话`} aria-label={`为 ${project.name} 创建新会话`} onClick={() => startNewSession(project)}>
                      <Plus aria-hidden="true" />
                    </button>
                    <button className="project-menu-button" type="button" title="项目操作" aria-label="项目操作" onClick={(event) => showProjectMenu(event, project)}>
                      <MoreHorizontal aria-hidden="true" />
                    </button>
                  </div>
                  {!groupCollapsed && <div className="project-session-list" aria-label={`${project.name} 的会话`}>
                    {sessions.length ? (
                      sessions.map(renderSessionRow)
                    ) : (
                      <button className="project-session-empty" type="button" onClick={() => startNewSession(project)} title={`为 ${project.name} 创建项目会话`}>
                        新建项目会话
                      </button>
                    )}
                  </div>}
                </article>
                );
              })}
            </div>
          )}
        </section>

        <div className={`session-list${generalSessionsCollapsed ? " is-collapsed" : ""}`} aria-label="会话列表">
          <div className="sidebar-section-title">
            <button className="sidebar-collapse-button" type="button" onClick={() => setSidebarCollapse((current) => ({ ...current, generalSessions: !current.generalSessions }))} aria-expanded={!generalSessionsCollapsed} title={generalSessionsCollapsed ? "展开通用会话" : "折叠通用会话"}>
              {generalSessionsCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
              <span>通用会话</span>
            </button>
            <small>{generalSessions.length}</small>
          </div>
          {generalSessionsCollapsed ? null : generalSessions.length ? generalSessions.map(renderSessionRow) : <div className="session-empty">暂无通用会话</div>}
        </div>

        <div className="sidebar-footer">
          {runCenterDevVisible && (
            <button className={`run-center-nav-button${runCenterOpen ? " is-active" : ""}`} onClick={() => openRunCenterSurface()} title="Open Run Center" aria-label="Open Run Center">
              <Activity aria-hidden="true" />
              <span>Run Center</span>
              <em>{runCenterNavCount > 99 ? "99+" : runCenterNavCount}</em>
            </button>
          )}
          <button onClick={() => { setSettingsSection("account"); setSettingsOpen(true); }} title="设置、能力包、权限和诊断"><Settings aria-hidden="true" />设置</button>
          <button onClick={() => { setSettingsSection("account"); setSettingsOpen(true); }} title={`${session.user.name} / ${session.user.email}`}><UserRound aria-hidden="true" />{session.user.name}</button>
        </div>
      </aside>

      {projectMenu && (() => {
        const project = projects.find((item) => item.id === projectMenu.projectId);
        if (!project) return null;
        const isPinned = Boolean(pinnedProjects[project.id] || project.pinned);
        return (
          <div className="context-menu" style={{ left: projectMenu.x, top: projectMenu.y }} onMouseLeave={() => setProjectMenu(null)}>
            <button type="button" onClick={() => openProjectInExplorer(project)}><FolderInput aria-hidden="true" />在资源管理器打开</button>
            <button type="button" onClick={() => { togglePinProject(project); setProjectMenu(null); }}>
              {isPinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}{isPinned ? "取消置顶项目" : "置顶项目"}
            </button>
            <button type="button" onClick={() => renameProject(project)}><Pencil aria-hidden="true" />重命名项目</button>
            <button type="button" onClick={() => deleteProject(project)}><FolderX aria-hidden="true" />删除项目</button>
          </div>
        );
      })()}

      {chatFileMenu && (
        <div className="context-menu chat-file-context-menu" style={{ left: chatFileMenu.x, top: chatFileMenu.y }} onMouseLeave={() => setChatFileMenu(null)}>
          <button type="button" disabled={!chatFileMenu.canAdd} title={chatFileMenu.disabledReason || "添加到当前聊天"} onClick={() => addFileToCurrentChat(chatFileMenu.file)}>
            <Paperclip aria-hidden="true" />添加到当前聊天
          </button>
          <button type="button" onClick={() => { void openArtifactFile(chatFileMenu.file, activeSessionId); setChatFileMenu(null); }}>
            <FolderOpen aria-hidden="true" />本地打开
          </button>
        </div>
      )}

      <section className="chat-pane">
        <header className="chat-header">
          <div>
            <h1>{activeSessionTitle}</h1>
            {activeProject && <small className="project-path" title={activeProject.path}>{activeProject.path}</small>}
          </div>
          <div className="chat-status">
            <span title={runtimeSnapshot.message}><Bot aria-hidden="true" />{runtimeSnapshot.status === "ready" ? "运行时已连接" : "等待运行时"}</span>
            <span title="当前企业账号"><CheckCircle2 aria-hidden="true" />{session.user.email}</span>
            <button
              className="icon-button"
              title={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              data-tooltip={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              data-tooltip-position="bottom-right"
              aria-label={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}
            </button>
            <button className="icon-button" title="通知与运行状态" data-tooltip="通知与运行状态" data-tooltip-position="bottom-right" aria-label="通知与运行状态" onClick={() => setNotificationsOpen((open) => !open)}>
              <Bell aria-hidden="true" />
            </button>
          </div>
          {notificationsOpen && (
            <section className="chat-popover-panel">
              <strong>运行状态</strong>
              <span>{sidecarStatus.message}</span>
              <span>{runtimeSnapshot.message}</span>
            </section>
          )}
        </header>

        <section className={`update-banner is-${updateStatus.state}${updateVisible ? "" : " is-hidden"}`}>
          {updateVisible && (
            <>
              <div>
                <strong>{updateStatus.version ? `EcoreX ${updateStatus.version}` : "EcoreX 更新"}</strong>
                <span>{updateStatus.message}</span>
              </div>
              <div className="update-actions">
                {typeof updateStatus.progress === "number" && updateStatus.state === "downloading" && (
                  <em>{Math.round(updateStatus.progress)}%</em>
                )}
                {updateStatus.state === "downloaded" ? (
                  <button className="primary-action" type="button" onClick={() => void handleInstallDownloadedUpdate()}>{updatePrimaryLabel}</button>
                ) : updateStatus.state === "downloading" ? (
                  <button type="button" disabled>{updatePrimaryLabel}</button>
                ) : (
                  <button className="primary-action" type="button" onClick={() => void openDownloadPage()}>{updatePrimaryLabel}</button>
                )}
                <button type="button" onClick={() => void handleCheckForUpdates()}>重新检查</button>
                <button className="icon-button" type="button" onClick={() => setUpdateStatus({ ...updateStatus, state: "idle", message: "已关闭更新提醒" })} title="关闭更新提醒" aria-label="关闭更新提醒">
                  <X aria-hidden="true" />
                </button>
              </div>
            </>
          )}
        </section>

        <div className="message-list" ref={messageListRef} onScroll={updateJumpLatestState}>
          {visibleMessages.length === 0 ? (
            <div className="empty-chat">
              <BrandMark />
              <strong>{activeProject ? activeProject.name : "可以直接开始"}</strong>
              <span>{activeProject ? "你可以把任何文件/图片/视频 参考扔到项目文件夹内 我会基于项目文件夹上下文回答你。" : "粘贴图片或文件，输入需求，EcoreX 会在需要能力包或权限时先确认。"}</span>
            </div>
          ) : (
            visibleMessages.map((message) => {
              const messageSessionId = activeSessionId;
              return (
              <article className={`message ${message.role}`} key={message.id}>
                <div className="message-body">
                  <button
                    type="button"
                    className="message-copy-button"
                    onClick={() => void copyMessageText(message)}
                    title={copiedMessageId === message.id ? "已复制" : "复制文本"}
                    aria-label={copiedMessageId === message.id ? "已复制" : "复制文本"}
                  >
                    <Copy aria-hidden="true" />
                  </button>
                  <MessageContent
                    role={message.role}
                    content={message.content}
                    pending={message.pending}
                    paused={message.paused}
                    cancelled={message.cancelled}
                    reasoning={message.reasoning}
                    steps={message.steps}
                    toolCalls={message.toolCalls}
                    artifacts={message.artifacts}
                    onOpenLocalFile={handleOpenMessageLocalFile}
                    localFilePreviewUrl={messageLocalFilePreviewUrl}
                    localFileJson={messageLocalJson}
                    localFileStat={messageLocalFileStat}
                    onLocalFileContextMenu={showChatFileMenu}
                  />
                  {message.attachments?.length ? (
                    <div className="message-files">
                      {message.attachments.map((file) => (
                        <button key={file.file_path} onClick={() => void openArtifactFile(file, messageSessionId)} onContextMenu={(event) => showChatFileMenu(event, file)} title="点击在本地打开">
                          {attachmentPreviewUrl(file) ? <img src={attachmentPreviewUrl(file)} alt={file.file_name} loading="lazy" /> : fileIcon(file)}<span>{file.file_name}</span>
                        </button>
                      ))}
                    </div>
                  ) : null}
                  {renderRecoveryActions(message, messageSessionId)}
                </div>
              </article>
              );
            })
          )}
        </div>

        {showJumpLatest && (
          <button
            type="button"
            className="jump-latest-button"
            onClick={() => scrollToLatest(true)}
            title="回到最新消息"
            aria-label="回到最新消息"
          >
            <ArrowDownToLine aria-hidden="true" />
          </button>
        )}

        <div className="composer-zone">
          {approval && (
            <section className={`approval-bar is-${approval.type}`}>
              <strong>{approval.title}</strong>
              <span>{approval.message}</span>
              <div>
                {approval.type === "capability" ? (
                  <>
                    {approval.pack.policyMode === "disabled" ? (
                      <button onClick={() => setApproval(null)}>知道了</button>
                    ) : (
                      <>
                        <button className="primary-action" onClick={() => void handleInstallPack(approval.pack)}>
                          {approval.pack.discoveryOnly ? "用 find skill" : "安装并继续"}
                        </button>
                        <button onClick={() => { setApproval(null); approval.resume(); }}>跳过继续</button>
                      </>
                    )}
                    <button onClick={() => setApproval(null)}>取消</button>
                  </>
                ) : approval.type === "open-file" ? (
                  <>
                    <button className="primary-action" onClick={() => void confirmOpenFile(approval.file)}>允许本次</button>
                    <button onClick={() => setApproval(null)}>取消</button>
                  </>
                ) : approval.actions?.length ? (
                  approval.actions.map((action) => <button key={action.label} className={action.primary ? "primary-action" : ""} onClick={action.onClick}>{action.label}</button>)
                ) : (
                  <button onClick={() => setApproval(null)}>知道了</button>
                )}
              </div>
            </section>
          )}

          <form
            className={`composer${composerDragActive ? " is-drag-active" : ""}`}
            onSubmit={(event) => { event.preventDefault(); void sendNow(); }}
            onDragEnter={handleComposerDragEnter}
            onDragOver={handleComposerDragOver}
            onDragLeave={handleComposerDragLeave}
            onDrop={handleComposerDrop}
          >
            {attachments.length > 0 && (
              <div className="attachment-tray">
                {attachments.map((file) => (
                  <article key={file.file_path}>
                    <button className="attachment-preview" type="button" onClick={() => previewOrOpenFile(file)} title={isImageAttachment(file) ? "点击预览图片" : "点击在本地打开"}>
                      {file.previewDataUrl ? <img src={file.previewDataUrl} alt="" /> : fileIcon(file)}
                      <span>{file.file_name}</span>
                    </button>
                    <button
                      className="attachment-remove"
                      type="button"
                      aria-label={`移除 ${file.file_name}`}
                      title="移除附件"
                      onClick={() => setAttachments((current) => current.filter((item) => item.file_path !== file.file_path))}
                    >
                      <X aria-hidden="true" />
                    </button>
                  </article>
                ))}
              </div>
            )}
            {(skillMentions.length > 0 || skillMentionNoResults) && (
              <div className="skill-mention-popover" role="listbox" aria-label="选择 Skill">
                {skillMentionGroups.map((group) => (
                  <div className="skill-mention-group" key={group.category}>
                    <div className="skill-mention-group-label">{group.label}</div>
                    {group.items.map((skill) => (
                      <button key={skill.key} type="button" onClick={() => insertSkillMention(skill)} title={skill.path || skill.source || "Skill"}>
                        <AtSign aria-hidden="true" />
                        <span>{skill.displayName || skill.name}</span>
                        <em>{skill.categoryLabel}</em>
                      </button>
                    ))}
                  </div>
                ))}
                {skillMentionNoResults && (
                  <div className="skill-mention-empty" role="option" aria-disabled="true">
                    <AtSign aria-hidden="true" />
                    <span>{hiddenSkillMentions.length ? "后台 / CLI 辅助" : "没有匹配的 Skill"}</span>
                    <em>{hiddenSkillMentions.length ? (hiddenSkillMentions[0].mentionHiddenReason || "后台触发") : "换个关键词试试"}</em>
                  </div>
                )}
              </div>
            )}
            <button type="button" className="round-button" onClick={chooseFiles} title="添加本地文件"><Paperclip aria-hidden="true" /></button>
            <textarea
              ref={composerRef}
              value={composerText}
              placeholder="给 EcoreX 发送消息，支持粘贴图片或文件"
              onChange={(event) => setComposerText(event.target.value)}
              onKeyDown={handleComposerKey}
              onPaste={(event) => void handlePaste(event)}
              rows={1}
            />
            <button type="button" className="mode-button" onClick={() => void loadRuntimeSnapshot().then(setRuntimeSnapshot).catch(() => undefined)} title={`当前模型：${currentModelName}`}>
              <Bot aria-hidden="true" />{currentModelName}<ChevronDown aria-hidden="true" />
            </button>
            {(activeSessionRequestId || hasPendingAssistantMessage) && !composerHasPayload ? (
              <button type="button" className="send-button stop" onClick={stopActiveRequest} title="停止当前回复"><Square aria-hidden="true" /></button>
            ) : (
              <button type="submit" className="send-button" disabled={!composerHasPayload} title={activeSessionRequestId || hasPendingAssistantMessage ? "发送并暂停上一条回复" : "发送，Enter 也可以发送"}>
                <SendHorizontal aria-hidden="true" />
              </button>
            )}
            <div className="composer-drop-overlay" aria-hidden="true">
              <Upload aria-hidden="true" />
              <span>松开添加</span>
            </div>
            <div className="composer-footer">
              <div className="composer-permission-row" aria-label="本机访问权限">
                <div className="composer-permission-menu">
                  <button
                    type="button"
                    className="composer-permission-trigger"
                    aria-haspopup="menu"
                    aria-expanded={permissionMenuOpen}
                    title={`本机访问权限：${composerPermissionTitle(currentComposerPermissionMode)}`}
                    onClick={() => setPermissionMenuOpen((open) => !open)}
                  >
                    {composerPermissionIcon(currentComposerPermissionMode)}
                    <span>{composerPermissionTitle(currentComposerPermissionMode)}</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  {permissionMenuOpen && (
                    <div className="composer-permission-popover" role="menu">
                      <div className="composer-permission-help">
                        <span>应如何批准 EcoreX 操作?</span>
                        <button type="button" onClick={() => { setSettingsSection("permissions"); setSettingsOpen(true); setPermissionMenuOpen(false); }}>
                          了解更多
                        </button>
                      </div>
                      {COMPOSER_PERMISSION_MENU_MODES.map((mode) => (
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={currentComposerPermissionMode === mode}
                          key={mode}
                          className={currentComposerPermissionMode === mode ? "is-active" : ""}
                          onClick={() => updatePermissionMode(mode).then((next) => {
                            setPermissionState(next);
                            setPermissionMenuOpen(false);
                          }).catch(() => setToast("权限模式切换失败"))}
                        >
                          {composerPermissionIcon(mode)}
                          <span>
                            <strong>{composerPermissionTitle(mode)}</strong>
                            <small>{composerPermissionDetail(mode)}</small>
                          </span>
                          {currentComposerPermissionMode === mode && <CheckCircle2 aria-hidden="true" />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              <div className="composer-metrics" aria-label="Token 和上下文用量">
                <div className="composer-token-meters" aria-label="Token 用量">
                  {tokenMeters.map((meter) => (
                    <div className={`composer-meter composer-meter-${meter.key}`} key={meter.key} title={meter.title} data-tooltip={meter.title} data-tooltip-position="top-right">
                      <span>{meter.label}</span>
                      <div className="composer-meter-track" aria-hidden="true">
                        <i style={{ width: `${meter.percent}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
                <div
                  className="composer-context-meter"
                  title={contextMeter.title}
                  data-tooltip={contextMeter.title}
                  data-tooltip-position="top-right"
                  style={{ "--context-meter-percent": `${contextMeter.percent}%` } as CSSProperties}
                >
                  <span>{contextMeter.label}</span>
                  <i aria-hidden="true" />
                </div>
              </div>
            </div>
          </form>
        </div>
      </section>

      {settingsOpen && (
        <div className="modal-backdrop" onClick={() => setSettingsOpen(false)}>
          <section className="settings-sheet" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <h2>设置</h2>
                <span>账号、项目、能力、权限、记忆和诊断</span>
              </div>
              <button className="icon-button" title="关闭设置" aria-label="关闭设置" onClick={() => setSettingsOpen(false)}><X aria-hidden="true" /></button>
            </header>
            <div className="settings-layout">
              <nav className="settings-nav" aria-label="设置分区">
                {settingsNav.map((item) => (
                  <button
                    key={item.id}
                    className={settingsSection === item.id ? "is-active" : ""}
                    onClick={() => setSettingsSection(item.id)}
                    title={item.label}
                  >
                    {item.icon}
                    <span>{item.label}</span>
                  </button>
                ))}
              </nav>
              <div className="settings-content">
                {settingsSection === "account" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>账号与外观</strong>
                      <span>{session.user.name} / {session.user.email}</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>主题</strong><span>当前为 {theme === "dark" ? "深色" : "明亮"} 模式</span></div>
                        <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}切换</button>
                      </article>
                      <article>
                        <div><strong>登录账号</strong><span>{session.user.email}</span></div>
                        <button onClick={logout}><LogOut aria-hidden="true" />退出</button>
                      </article>
                    </div>
                    <form className="password-form" onSubmit={submitPasswordChange}>
                      <strong>修改登录密码</strong>
                      <input value={passwordDraft.oldPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, oldPassword: event.target.value }))} type="password" placeholder="当前密码" autoComplete="current-password" required />
                      <input value={passwordDraft.newPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, newPassword: event.target.value }))} type="password" placeholder="新密码，至少 8 位" autoComplete="new-password" minLength={8} required />
                      <input value={passwordDraft.confirmPassword} onChange={(event) => setPasswordDraft((current) => ({ ...current, confirmPassword: event.target.value }))} type="password" placeholder="确认新密码" autoComplete="new-password" minLength={8} required />
                      <button type="submit" disabled={passwordBusy}>{passwordBusy ? "保存中" : "更新密码"}</button>
                    </form>
                  </section>
                )}

                {settingsSection === "projects" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>项目</strong>
                      <span>项目会话会自动引用项目文件夹，并把总结沉淀到项目记忆</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>添加项目</strong><span>选择一个本地文件夹作为项目工作区</span></div>
                        <button onClick={() => void addProject()}><FolderPlus aria-hidden="true" />添加</button>
                      </article>
                      {projects.map((project) => (
                        <article key={project.id}>
                          <div><strong>{project.name}</strong><span title={project.path}>{project.path}</span></div>
                          <button onClick={() => startNewSession(project)}><Plus aria-hidden="true" />开始会话</button>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                {settingsSection === "abilities" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>能力</strong>
                      <span>点击安装后由当前会话 agent 诊断、安装和修复；勾选只控制是否参与自动触发</span>
                    </div>
                    {installNotice && !installNotice.dismissed && (
                      <div className="install-notice" role="status">
                        <strong>{installNotice.message}</strong>
                        <span>安装会在当前会话继续执行，关闭提示不会中断任务。</span>
                        <button type="button" onClick={() => setInstallNotice((current) => current ? { ...current, dismissed: true } : current)}>
                          关闭
                        </button>
                      </div>
                    )}
                    <div className="ability-grid">
                      {abilityRows.map((ability) => (
                        <article key={ability.id} className={ability.enabled ? "is-ready" : "is-waiting"}>
                          {ability.icon}
                          <div><strong>{ability.name}</strong><span>{ability.detail}</span></div>
                          {"pack" in ability && ability.pack && !ability.enabled ? (
                            <button disabled={Boolean(installingPackIds[ability.pack.id])} onClick={() => void handleInstallPack(ability.pack!)}>
                              {packActionLabel(ability.pack, Boolean(installingPackIds[ability.pack.id]))}
                            </button>
                          ) : <em>{ability.enabled ? "已开启" : "待配置"}</em>}
                        </article>
                      ))}
                    </div>
                    <div className="skill-toggle-list">
                      <div className="toggle-list-head"><strong>Skill 生效开关</strong><span>可在聊天框用 @ 提及，关闭后不参与调用</span></div>
                      <div className="skill-category-heading"><strong>可 @ 提及</strong><span>{mentionableSkillRows.length}</span></div>
                      {mentionableSkillRows.map((skill) => {
                        const name = skill.name || "";
                        const enabled = skill.enabled;
                        return (
                          <label key={skill.key} className={`toggle-row${skill.toggleable ? "" : " is-readonly"}`} title={skill.path || skill.source || name}>
                            <input type="checkbox" checked={enabled} disabled={!skill.toggleable} onChange={(event) => void toggleRuntimeSkill(skill, event.currentTarget.checked)} />
                            <span><strong>{skill.displayName || name || "未命名 Skill"}</strong><small>{[skill.origin || skill.source || "skill", skill.status || (enabled ? "ready" : "disabled"), skill.policy].filter(Boolean).join(" · ")}</small></span>
                            <em>{enabled ? "已启用" : "已关闭"}</em>
                          </label>
                        );
                      })}
                      {backgroundSkillRows.length > 0 && (
                        <details className="skill-background-details">
                          <summary><strong>后台 / CLI 辅助</strong><span>{backgroundSkillRows.length}</span></summary>
                          {backgroundSkillRows.map((skill) => {
                            const name = skill.name || "";
                            const enabled = skill.enabled;
                            return (
                              <label key={skill.key} className={`toggle-row${skill.toggleable ? "" : " is-readonly"}`} title={skill.path || skill.source || name}>
                                <input type="checkbox" checked={enabled} disabled={!skill.toggleable} onChange={(event) => void toggleRuntimeSkill(skill, event.currentTarget.checked)} />
                                <span><strong>{skill.displayName || name || "未命名 Skill"}</strong><small>{[skill.mentionHiddenReason, skill.origin || skill.source || "skill", skill.status || (enabled ? "ready" : "disabled"), skill.policy].filter(Boolean).join(" · ")}</small></span>
                                <em>{enabled ? "已启用" : "已关闭"}</em>
                              </label>
                            );
                          })}
                        </details>
                      )}
                      {!skillDisplayRows.length && <div className="session-empty">运行时暂未返回 Skill 列表</div>}
                    </div>
                    <div className="pack-list">
                      {packs.map((pack) => (
                        <article key={pack.id}>
                          <label className="pack-toggle" title="控制该能力包是否参与自动触发；安装状态不会被删除">
                            <input type="checkbox" checked={capabilityPackEnabled(pack.id)} onChange={(event) => toggleCapabilityPack(pack, event.currentTarget.checked)} />
                            <span>生效</span>
                          </label>
                          <div>
                            <strong>{pack.name}</strong>
                            <span>{installingPackIds[pack.id] ? `${packActionLabel(pack, true)}，请稍候` : pack.message}</span>
                          </div>
                          <button disabled={pack.installed || pack.policyMode === "disabled" || Boolean(installingPackIds[pack.id])} onClick={() => void handleInstallPack(pack)}>
                            {installingPackIds[pack.id]
                              ? packActionLabel(pack, true)
                              : pack.installed
                                ? "已安装"
                                : pack.policyMode === "disabled"
                                  ? "管理员禁用"
                                  : packActionLabel(pack)}
                          </button>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                {settingsSection === "permissions" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>权限</strong>
                      <span>{permissionState ? `当前 ${permissionModeLabel(permissionState.mode)}，保存 ${permissionState.grantsCount} 条授权` : "权限状态暂不可用"}</span>
                    </div>
                    <div className="permission-modes">
                      {SETTINGS_PERMISSION_MODES.map((mode) => (
                        <button key={mode} className={permissionState?.mode === mode ? "is-active" : ""} onClick={() => updatePermissionMode(mode).then(setPermissionState)}>
                          {permissionModeLabel(mode)}
                        </button>
                      ))}
                    </div>
                    <div className="settings-list">
                      <article><div><strong>授权记录</strong><span>{permissionState?.auditPath || "暂无记录"}</span></div><button onClick={() => resetPermissionGrants().then(setPermissionState)}>清空授权</button></article>
                      <article><div><strong>文件预览</strong><span>点击附件才显示预览，系统打开前会进行权限确认</span></div><button onClick={() => setPreviewFile(null)}>关闭预览</button></article>
                    </div>
                  </section>
                )}

                {settingsSection === "memory" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>记忆</strong>
                      <span>{activeProject ? `项目记忆：${activeProject.name}` : "原项目记忆入口"}</span>
                    </div>
                    <div className="settings-list">
                      <article>
                        <div><strong>项目记忆</strong><span title={activeProjectMemoryPath}>{activeProjectMemoryPath || "选择项目后自动创建 .ecorex/project-memory.md"}</span></div>
                        <button disabled={!activeProject} onClick={() => activeProject?.memoryPath && requestOpenFile({ file_path: activeProject.memoryPath, file_name: "project-memory.md", file_type: "file" })}><BookOpen aria-hidden="true" />打开</button>
                      </article>
                      <article>
                        <div><strong>梦境蒸馏</strong><span>{activeProject ? "项目会话只写入项目梦境目录" : "通用会话保留原项目记忆入口"}</span></div>
                        <em>已开启</em>
                      </article>
                    </div>
                    <div className="memory-list">
                      {[...memoryFiles, ...dreamFiles].slice(0, 8).map((file) => (
                        <article key={`${file.category || ""}-${memoryFileName(file)}`}>
                          <Brain aria-hidden="true" />
                          <div><strong>{memoryFileName(file)}</strong><span>{memoryFileTime(file)}</span></div>
                        </article>
                      ))}
                      {!memoryFiles.length && !dreamFiles.length && <div className="session-empty">暂无可展示的原项目记忆文件</div>}
                    </div>
                  </section>
                )}

                {settingsSection === "diagnostics" && (
                  <section className="settings-section">
                    <div className="settings-section-head">
                      <strong>诊断</strong>
                      <span>{sidecarStatus.message}</span>
                    </div>
                    {runCenterDevVisible ? renderRunCenterPanel("settings") : null}
                    <div className="settings-list">
                      <article><div><strong>运行时</strong><span>{runtimeSnapshot.message}</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                      <article><div><strong>模型策略</strong><span>{runtimeSnapshot.modelsCount} 个企业模型映射</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>刷新</button></article>
                      <article><div><strong>Skill / MCP</strong><span>{runtimeSnapshot.skillsCount} 个 Skill，{runtimeSnapshot.toolsCount} 个工具通道，{runtimeSnapshot.extensionsCount || 0} 个扩展登记</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                      <article><div><strong>诊断包</strong><span>导出运行状态、活动请求和脱敏日志摘要</span></div><button onClick={() => void handleExportDiagnostics()}>生成</button></article>
                    </div>
                  </section>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      {runCenterDevVisible && runCenterOpen && (
        <div className="modal-backdrop run-center-backdrop" onClick={() => setRunCenterOpen(false)}>
          <section className="run-center-sheet" role="dialog" aria-modal="true" aria-labelledby="run-center-title" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>Runtime control</span>
                <h2 id="run-center-title">Run Center</h2>
              </div>
              <button className="icon-button" title="Close Run Center" aria-label="Close Run Center" onClick={() => setRunCenterOpen(false)}><X aria-hidden="true" /></button>
            </header>
            {renderRunCenterPanel("primary")}
          </section>
        </div>
      )}

      {releaseNotesOpen && releaseNotes && (
        <div className="modal-backdrop release-notes-backdrop" onClick={closeReleaseNotes}>
          <section className="release-notes-sheet" role="dialog" aria-modal="true" aria-labelledby="release-notes-title" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>EcoreX {releaseNotes.version}</span>
                <h2 id="release-notes-title">{releaseNotes.title || "更新说明"}</h2>
                {releaseNotes.summary && <p>{releaseNotes.summary}</p>}
              </div>
              <button className="icon-button" title="关闭更新说明" aria-label="关闭更新说明" onClick={closeReleaseNotes}><X aria-hidden="true" /></button>
            </header>
            <div className="release-notes-content">
              {!!releaseNotes.highlights?.length && (
                <section>
                  <strong>新增和改进</strong>
                  <ul>
                    {releaseNotes.highlights.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {!!releaseNotes.fixes?.length && (
                <section>
                  <strong>修复的问题</strong>
                  <ul>
                    {releaseNotes.fixes.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {!!releaseNotes.howTo?.length && (
                <section>
                  <strong>怎么用</strong>
                  <ul>
                    {releaseNotes.howTo.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </section>
              )}
              {releaseNotes.updatePolicy && (
                <section className="release-notes-update-policy">
                  <strong>更新方式</strong>
                  {releaseNotes.updatePolicy.windows && <p>{releaseNotes.updatePolicy.windows}</p>}
                  {releaseNotes.updatePolicy.macos && <p>{releaseNotes.updatePolicy.macos}</p>}
                  {releaseNotes.updatePolicy.webui && <p>{releaseNotes.updatePolicy.webui}</p>}
                </section>
              )}
            </div>
            <footer>
              <button type="button" onClick={closeReleaseNotes}>知道了</button>
            </footer>
          </section>
        </div>
      )}

      {previewFile && isImageAttachment(previewFile) && (
        <div className="preview-popover image-preview-popover" style={{ "--preview-zoom": previewZoom, "--preview-width": `${previewZoom * 100}%` } as CSSProperties}>
          <header>
            <strong>{previewFile.file_name}</strong>
            <span className="preview-actions">
              <button className="icon-button" title="缩小" aria-label="缩小" onClick={() => setPreviewZoom((value) => Math.max(0.5, Math.round((value - 0.25) * 100) / 100))}><ZoomOut aria-hidden="true" /></button>
              <button className="icon-button" title="放大" aria-label="放大" onClick={() => setPreviewZoom((value) => Math.min(3, Math.round((value + 0.25) * 100) / 100))}><ZoomIn aria-hidden="true" /></button>
              <button className="icon-button" title="关闭预览" aria-label="关闭预览" onClick={() => setPreviewFile(null)}><X aria-hidden="true" /></button>
            </span>
          </header>
          <div className="preview-image-frame">
            <img src={previewFile.previewDataUrl || filePreviewUrl(previewFile.file_path, sidecarStatus.webPort)} alt={previewFile.file_name} />
          </div>
          <button onClick={() => requestOpenFile(previewFile)}>在系统中打开</button>
        </div>
      )}

      {toast && <div className="toast" onAnimationEnd={() => setToast("")}>{toast}</div>}
    </main>
  );
}

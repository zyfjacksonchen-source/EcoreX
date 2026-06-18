import {
  ArrowDownToLine,
  AtSign,
  Bell,
  Bot,
  BookOpen,
  Brain,
  CheckCircle2,
  ChevronDown,
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
  X
} from "lucide-react";
import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, CSSProperties, MouseEvent, ReactNode } from "react";
import { MessageContent, type AgentStepDisclosure, type ToolCallDisclosure } from "./components/MessageContent";
import {
  cancelChatRequest,
  checkForUpdates,
  enterpriseChangePassword,
  checkEnterpriseQuota,
  chooseProjectFolder,
  chooseLocalFiles,
  decideToolPermission,
  deleteMessagePair,
  enableDefaultSkills,
  enterpriseLogin,
  enterpriseLogout,
  filePreviewUrl,
  generateSessionTitle,
  getEnterpriseSession,
  installDownloadedUpdate,
  listCapabilityPacks,
  loadPermissionState,
  loadMemoryFiles,
  loadRuntimeSnapshot,
  loadSessionHistory,
  openLocalPath,
  openRuntimePath,
  openDownloadPage,
  openMessageStream,
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
  updatePermissionMode,
  type CapabilityPack,
  type DesktopUpdateStatus,
  type AgentArtifact,
  type EnterpriseQuotaCheckResult,
  type EnterpriseSession,
  type FileAttachment,
  type MemoryFile,
  type PermissionMode,
  type PermissionState,
  type ProjectFolder,
  type RuntimeActiveRequest,
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
  message: string;
  pid?: number;
  webPort: number;
};
type SessionRow = {
  id: string;
  title: string;
  detail: string;
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
  userSeq?: number;
  botSeq?: number;
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
};
type ProjectContextMenu = {
  projectId: string;
  x: number;
  y: number;
} | null;
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

function isLocalAbsolutePath(value?: string) {
  const source = String(value || "").trim();
  return /^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("\\\\") || (/^\//.test(source) && !isRuntimePreviewPath(source));
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

const initialRuntime: RuntimeSnapshot = {
  status: "offline",
  message: "正在连接本地运行时",
  sessions: [],
  totalSessions: 0,
  toolsCount: 0,
  skillsCount: 0,
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
const PINNED_SESSIONS_STORAGE_KEY = "ecorex-pinned-sessions";
const PINNED_PROJECTS_STORAGE_KEY = "ecorex-pinned-projects";
const SESSION_UI_STORAGE_KEY = "ecorex-session-ui-state";
const LAST_ACTIVE_SESSION_STORAGE_KEY = "ecorex-last-active-session-id";
const CAPABILITY_ENABLED_STORAGE_KEY = "ecorex-capability-enabled";
const SKILL_DEFAULTS_STORAGE_KEY = "ecorex-skill-defaults-v1";
const RELEASE_NOTES_SEEN_STORAGE_KEY = "ecorex-release-notes-seen-version";
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
  const ageSeconds = Number(request.age_seconds || 0);
  return !Number.isFinite(ageSeconds) || ageSeconds < 30;
}

function BrandMark() {
  const [failed, setFailed] = useState(false);
  return (
    <div className="brand-mark" aria-hidden="true">
      {failed ? <Sparkles aria-hidden="true" /> : <img src={brandIconUrl} alt="" onError={() => setFailed(true)} />}
    </div>
  );
}

function WindowBrand() {
  return (
    <div className="window-brand" aria-hidden="true">
      <img src={brandIconUrl} alt="" />
      <span>EcoreX</span>
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
  sessionUiState: Record<string, SessionUiState>
): SessionRow[] {
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const activeRequestBySession = new Map<string, RuntimeActiveRequest>(
    (snapshot.activeRequests || [])
      .filter((request) => request.session_id && request.request_id)
      .filter(isRuntimeRequestUiActive)
      .map((request) => [String(request.session_id), request])
  );
  const rows: SessionRow[] = snapshot.sessions.map((session, index) => {
    const id = session.session_id || session.id || `runtime-${index}`;
    const projectId = sessionProjects[id] || null;
    const project = projectId ? projectById.get(projectId) : undefined;
    const activeRequest = activeRequestBySession.get(id);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    return {
      id,
      title: sessionTitles[id] || session.title || session.session_id || "未命名会话",
      detail: project ? project.name : "",
      updatedAt: activeRequestId && !session.last_active ? (isCancelling ? "正在停止" : "运行中") : session.last_active || "",
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
    const live = (cached.messages || []).some(isLiveAssistantMessage);
    const activeRequest = activeRequestBySession.get(sessionId);
    const activeRequestId = activeRequest?.request_id ? String(activeRequest.request_id) : undefined;
    const isCancelling = Boolean(activeRequest?.cancelled);
    rows.push({
      id: sessionId,
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
    rows.push({
      id: sessionId,
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
    rows.unshift({
      id: activeSessionId,
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
  return rows.sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
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
  const usedDetail = `${Math.round(used).toLocaleString("zh-CN")} tokens`;
  if (!limit) return `${label}：${usedDetail}，暂无上限数据`;
  const limitDetail = `${Math.round(limit).toLocaleString("zh-CN")} tokens`;
  return `${label}：${usedDetail} / ${limitDetail}，${Math.round(percentOf(used, limit))}%`;
}

function estimateContextTokens(messages: ChatItem[], draft: string, files: FileAttachment[]) {
  const history = messages.reduce((total, message) => {
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
    if (!item.requestId || staleSession) {
      changed = true;
      return pausePendingMessage(item, Boolean(staleSession));
    }
    if (activeRequestIds && !activeRequestIds.has(item.requestId)) {
      const createdAtMs = item.createdAt ? new Date(item.createdAt).getTime() : 0;
      const inGrace = Boolean(createdAtMs && Number.isFinite(createdAtMs) && nowMs - createdAtMs < inactiveGraceMs);
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

function mergeHistoryWithLocalMessages(history: ChatItem[], local: ChatItem[]) {
  if (!history.length || !local.length) return history.length ? history : local;
  const sequenceKeys = new Set(history.map(messageSequenceKey).filter(Boolean));
  const requestKeys = new Set(history.map(messageRequestKey).filter(Boolean));
  const contentKeys = new Set(history.map(messageContentKey).filter(Boolean));
  const preserved: ChatItem[] = [];
  let skipPendingAssistantAfterMatchedUser = false;

  for (const message of local) {
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

  return preserved.length ? [...history, ...preserved] : history;
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

function normalizeArtifactEntry(entry: unknown, index: number, requestId?: string): AgentArtifact | null {
  if (!entry || typeof entry !== "object") return null;
  const raw = entry as Record<string, unknown>;
  const path = String(raw.path || raw.file_path || raw.filePath || "").trim();
  const url = String(raw.url || "").trim();
  const relativePath = String(raw.relativePath || raw.relative_path || "").trim();
  const titleSource = String(raw.title || raw.file_name || raw.name || path || relativePath || url || "").trim();
  if (!path && !url && !relativePath && !titleSource) return null;
  const rawKind = String(raw.kind || raw.file_type || raw.type || "").toLowerCase();
  const kind: AgentArtifact["kind"] = rawKind === "image" || rawKind === "video" || rawKind === "audio" || rawKind === "directory" || rawKind === "url" || rawKind === "diff"
    ? rawKind
    : rawKind === "file" || path || relativePath
      ? "file"
      : "url";
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
    mimeType: String(raw.mimeType || raw.mime_type || "").trim() || undefined,
    sizeBytes: typeof raw.sizeBytes === "number" ? raw.sizeBytes : typeof raw.size_bytes === "number" ? raw.size_bytes : undefined,
    previewUrl: String(raw.previewUrl || raw.preview_url || "").trim() || undefined,
    thumbnailUrl: String(raw.thumbnailUrl || raw.thumbnail_url || "").trim() || undefined,
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

function mapRuntimeMessage(item: RuntimeMessage, sessionId: string, index: number): ChatItem {
  const steps = [
    ...(item.steps?.map(normalizeStep) || []),
    ...runtimeExtrasMediaSteps(item)
  ];
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
    userSeq: item.user_seq ?? item.seq,
    botSeq: item.role === "assistant" ? item.seq : undefined
  };
}

function toolEnabled(tools: RuntimeTool[] | undefined, name: string) {
  return Boolean((tools || []).some((tool) => tool.name === name));
}

function skillEnabled(skills: RuntimeSkill[] | undefined, name: string) {
  const skill = (skills || []).find((item) => item.name === name);
  return Boolean(skill && skill.enabled !== false);
}

function memoryFileName(file: MemoryFile) {
  return file.filename || file.name || "未命名记忆";
}

function memoryFileTime(file: MemoryFile) {
  return file.updated_at || file.updatedAt || "";
}

function AuthGate(props: { onLogin: (session: EnterpriseSession) => void }) {
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
      <WindowBrand />
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
  const [, setActiveRequestId] = useState("");
  const [approval, setApproval] = useState<ApprovalState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [releaseNotesOpen, setReleaseNotesOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [previewFile, setPreviewFile] = useState<FileAttachment | null>(null);
  const [packs, setPacks] = useState<CapabilityPack[]>([]);
  const [permissionState, setPermissionState] = useState<PermissionState | null>(null);
  const [projects, setProjects] = useState<ProjectFolder[]>(() => readStorage<ProjectFolder[]>(PROJECTS_STORAGE_KEY, []));
  const [sessionProjects, setSessionProjects] = useState<SessionProjectMap>(() => bootSessionProjects);
  const [sessionTitles, setSessionTitles] = useState<StringMap>(() => readStorage<StringMap>(SESSION_TITLES_STORAGE_KEY, {}));
  const [pinnedSessions, setPinnedSessions] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_SESSIONS_STORAGE_KEY, {}));
  const [pinnedProjects, setPinnedProjects] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_PROJECTS_STORAGE_KEY, {}));
  const [unreadSessionIds, setUnreadSessionIds] = useState<StringBoolMap>({});
  const [sessionUiState, setSessionUiState] = useState<Record<string, SessionUiState>>(() => readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {}));
  const [enabledCapabilityPacks, setEnabledCapabilityPacks] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(CAPABILITY_ENABLED_STORAGE_KEY, {}));
  const [sessionRequestIds, setSessionRequestIds] = useState<StringMap>({});
  const [activeProjectId, setActiveProjectId] = useState<string | null>(bootSession?.id ? bootSessionProjects[bootSession.id] || null : null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("account");
  const [projectMenu, setProjectMenu] = useState<ProjectContextMenu>(null);
  const [installingPackIds, setInstallingPackIds] = useState<StringBoolMap>({});
  const [installNotice, setInstallNotice] = useState<InstallNotice>(null);
  const [memoryFiles, setMemoryFiles] = useState<MemoryFile[]>([]);
  const [dreamFiles, setDreamFiles] = useState<MemoryFile[]>([]);
  const [toast, setToast] = useState("");
  const [copiedMessageId, setCopiedMessageId] = useState("");
  const [passwordDraft, setPasswordDraft] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const streamCleanup = useRef<null | (() => void)>(null);
  const streamCleanups = useRef<Record<string, () => void>>({});
  const streamCleanupRequestIds = useRef<StringMap>({});
  const streamDeltaBuffers = useRef<Record<string, { sessionId: string; assistantId: string; requestId: string; text: string; timer: number | null }>>({});
  const sessionRequestIdsRef = useRef<StringMap>({});
  const installWatchers = useRef<Record<string, number>>({});
  const queuedInstallRef = useRef<Array<{ pack: CapabilityPack; onInstalled?: () => void; sessionId: string }>>([]);
  const activeSessionIdRef = useRef(activeSessionId);
  const messagesRef = useRef(messages);
  const sessionSwitchSeq = useRef(0);
  const autoScrollRef = useRef(true);
  const completedRequestIds = useRef<StringBoolMap>({});
  const streamRetryCounts = useRef<Record<string, number>>({});
  const preloadDone = useRef(false);
  const bootHistoryRefreshDone = useRef(false);
  const releaseNotesDismissedVersion = useRef("");
  const uiStateLocalSyncTimer = useRef<number | null>(null);
  const pendingUiStateStorage = useRef<Record<string, SessionUiState> | null>(null);
  const uiStateSyncTimer = useRef<number | null>(null);

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
    writeStorage(PINNED_SESSIONS_STORAGE_KEY, pinnedSessions);
  }, [pinnedSessions]);

  useEffect(() => {
    writeStorage(PINNED_PROJECTS_STORAGE_KEY, pinnedProjects);
  }, [pinnedProjects]);

  useEffect(() => {
    writeStorage(CAPABILITY_ENABLED_STORAGE_KEY, enabledCapabilityPacks);
  }, [enabledCapabilityPacks]);

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
        title: activeSessionTitle,
        projectId,
        messages,
        composerText,
        attachments
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
    if (runtimeSnapshot.status !== "ready") return;
    if (runtimeSnapshot.activeRequestsStatus === "unavailable") return;
    const activeRequestIds = new Set(
      (runtimeSnapshot.activeRequests || [])
        .filter(isRuntimeRequestUiActive)
        .map((request) => request.request_id ? String(request.request_id) : "")
        .filter(Boolean)
    );
    const staleSessionIds = new Set(
      (runtimeSnapshot.staleLocks || [])
        .filter((lock) => lock.removed || lock.dead_owner || lock.stale)
        .map((lock) => lock.session_id ? String(lock.session_id) : "")
        .filter(Boolean)
    );
    const nowMs = Date.now();
    const nextState: Record<string, SessionUiState> = {};
    const settledSessionIds = new Set<string>();
    let changed = false;
    for (const [sessionId, state] of Object.entries(sessionUiState)) {
      const normalized = normalizePausedMessages(state.messages || [], {
        sessionId,
        activeRequestIds,
        staleSessionIds,
        nowMs,
        inactiveRequestGraceMs: 45_000
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
  }, [runtimeSnapshot, sessionUiState]);

  const allSessions = useMemo(() => (
    mapSessions(runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, sessionUiState)
  ), [runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, sessionUiState]);
  const visibleSessions = useMemo(() => {
    const needle = searchQuery.trim().toLowerCase();
    return needle ? allSessions.filter((row) => `${row.title} ${row.detail}`.toLowerCase().includes(needle)) : allSessions;
  }, [allSessions, searchQuery]);

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) || null,
    [projects, activeProjectId]
  );
  const resolveArtifactPath = (filePath: string) => {
    const raw = String(filePath || "").trim();
    if (!raw || isRuntimePreviewPath(raw) || isLocalAbsolutePath(raw) || /^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw;
    return activeProject?.path ? joinLocalPath(activeProject.path, raw) : raw;
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
  const mentionMatch = /@([\w\u4e00-\u9fa5-]*)$/.exec(composerText);
  const skillMentions = mentionMatch
    ? (runtimeSnapshot.skills || []).filter((skill) => {
        const label = skill.display_name || skill.name || "";
        return label && label.toLowerCase().includes(mentionMatch[1].toLowerCase());
      }).slice(0, 6)
    : [];
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
      return !sessionMessages.some((message) => message.pending);
    });
    if (nextIndex < 0) return;
    const [queued] = queue.splice(nextIndex, 1);
    window.setTimeout(() => void handleInstallPack(queued.pack, queued.onInstalled, queued.sessionId), 0);
  }, [activeSessionId, activeSessionRequestId, messages, sessionRequestIds, sessionUiState]);

  function capabilityPackEnabled(packId: string) {
    return enabledCapabilityPacks[packId] !== false;
  }

  function toggleCapabilityPack(pack: CapabilityPack, enabled: boolean) {
    setEnabledCapabilityPacks((current) => ({ ...current, [pack.id]: enabled }));
    setToast(enabled ? `${pack.name} 已启用` : `${pack.name} 已关闭`);
  }

  async function toggleRuntimeSkill(skill: RuntimeSkill, enabled: boolean) {
    const name = skill.name || "";
    if (!name) return;
    try {
      await setSkillEnabled(name, enabled);
      setRuntimeSnapshot(await loadRuntimeSnapshot());
      setToast(enabled ? `${skill.display_name || name} 已启用` : `${skill.display_name || name} 已关闭`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Skill 开关失败");
    }
  }

  function insertSkillMention(skill: RuntimeSkill) {
    const label = skill.display_name || skill.name || "";
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
      syncComposerHeight();
    };
    focus();
    window.requestAnimationFrame(focus);
    window.setTimeout(focus, 40);
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
    const nextMessages = normalizePausedMessages(cached.messages);
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
    if (liveMessage?.requestId && streamAvailable) {
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
      const history = await loadSessionHistory(row.id);
      if (sessionSwitchSeq.current !== switchSeq || activeSessionIdRef.current !== row.id) {
        return;
      }
      const mapped = normalizePausedMessages(history.map((item, index) => mapRuntimeMessage(item, row.id, index)));
      setSessionUiState((current) => ({
        ...current,
        [row.id]: {
          ...(current[row.id] || {
            title: row.title,
            composerText: "",
            attachments: []
          }),
          projectId: nextProjectId,
          messages: mapped
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
    if (!nextTitle || nextTitle === row.title) return;
    try {
      setSessionTitles((current) => ({ ...current, [row.id]: nextTitle }));
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

  async function chooseFiles() {
    try {
      const files = await chooseLocalFiles(sidecarStatus.webPort);
      setAttachments((current) => [...current, ...files]);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "选择文件失败");
    }
  }

  async function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files || []);
    if (!files.length) return;
    event.preventDefault();
    try {
      const saved = await Promise.all(files.map((file) => savePastedFile(file)));
      const nextFiles = saved.filter(Boolean) as FileAttachment[];
      if (!nextFiles.length) {
        setToast("未能添加粘贴的文件");
        return;
      }
      setAttachments((current) => [...current, ...nextFiles]);
      setToast("已添加粘贴的文件");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "粘贴附件失败");
    }
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
      return {
        ...current,
        [sessionId]: {
          ...existing,
          projectId: sessionProjects[sessionId] || null,
          messages: updater(existing.messages)
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

  async function refreshSessionFromHistory(sessionId: string) {
    try {
      const history = await loadSessionHistory(sessionId);
      const mapped = normalizePausedMessages(history.map((item, index) => mapRuntimeMessage(item, sessionId, index)));
      const hasFinalAssistant = mapped.some((message) => (
        message.role === "assistant"
        && !message.pending
        && (
          Boolean(message.content.trim())
          || Boolean(message.steps?.length)
          || Boolean(message.toolCalls?.length)
          || typeof message.botSeq === "number"
        )
      ));
      if (!hasFinalAssistant) return false;
      const localMessages = sessionId === activeSessionIdRef.current
        ? messagesRef.current
        : sessionUiState[sessionId]?.messages || [];
      const merged = mergeHistoryWithLocalMessages(mapped, localMessages);
      updateSessionMessages(sessionId, () => merged);
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

  function finishRunningSteps(message: ChatItem, reason: AgentFinishReason = "done"): ChatItem {
    return { ...message, steps: finishAgentSteps(message.steps, reason), toolCalls: message.toolCalls?.map((tool) => ({ ...tool, running: false })) };
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

  function appendMediaStep(message: ChatItem, item: StreamItem): ChatItem {
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
      pending: true,
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

  function appendArtifact(message: ChatItem, item: StreamItem): ChatItem {
    const incoming = item.artifact
      ? normalizeArtifactEntry(item.artifact, 0, item.request_id || message.requestId)
      : Array.isArray(item.artifacts)
        ? item.artifacts.map((entry, index) => normalizeArtifactEntry(entry, index, item.request_id || message.requestId)).filter((entry): entry is AgentArtifact => Boolean(entry))
        : [];
    const artifacts = Array.isArray(incoming) ? incoming : incoming ? [incoming] : [];
    if (!artifacts.length) return message;
    const nextArtifacts = [...(message.artifacts || [])];
    for (const artifact of artifacts) {
      const key = artifactDedupeKey(artifact);
      const index = nextArtifacts.findIndex((entry) => entry.id === artifact.id || artifactDedupeKey(entry) === key);
      if (index >= 0) {
        nextArtifacts[index] = { ...nextArtifacts[index], ...artifact };
      } else {
        nextArtifacts.push(artifact);
      }
    }
    return { ...message, pending: true, artifacts: nextArtifacts };
  }

  function isCurrentSessionRequest(sessionId: string, requestId?: string) {
    return !requestId || sessionRequestIdsRef.current[sessionId] === requestId;
  }

  function isPostDoneTailItem(item: StreamItem) {
    return item.type === "voice_attach";
  }

  function streamItemText(item: StreamItem) {
    return String(item.content ?? item.text ?? item.delta ?? "");
  }

  function shouldAcceptStreamItem(sessionId: string, requestId: string, item: StreamItem) {
    if (isCurrentSessionRequest(sessionId, requestId)) return true;
    return Boolean(completedRequestIds.current[requestId] && isPostDoneTailItem(item));
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
    updateAssistantMessage(buffer.sessionId, buffer.assistantId, (message) => ({
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
    buffer.timer = window.setTimeout(() => {
      const current = streamDeltaBuffers.current[key];
      if (current) current.timer = null;
      flushBufferedDelta(key);
    }, 34);
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
    flushStreamDeltaBuffers(sessionId, requestId);
    cleanup();
    delete streamCleanups.current[sessionId];
    delete streamCleanupRequestIds.current[sessionId];
    if (streamCleanup.current === cleanup) {
      streamCleanup.current = null;
    }
    clearStreamDeltaBuffers(sessionId, requestId);
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
    clearSessionRequestState(sessionId, requestId);
    closeSessionStream(sessionId, requestId);
    clearStreamDeltaBuffers(sessionId, requestId);
  }

  function scheduleStreamReconnect(sessionId: string, assistantId: string, requestId: string) {
    if (!isCurrentSessionRequest(sessionId, requestId)) return;
    const attempts = streamRetryCounts.current[sessionId] || 0;
    if (attempts >= 5) {
      void (async () => {
        const snapshot = await loadRuntimeSnapshot().catch(() => null);
        const active = (snapshot?.activeRequests || []).find((request) => (
          String(request.session_id || "") === sessionId
          && String(request.request_id || "") === requestId
          && isRuntimeRequestUiActive(request)
        ));
        if (snapshot) setRuntimeSnapshot(snapshot);
        if (active?.cancelled) {
          updateAssistantMessage(sessionId, assistantId, (message) => ({
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
          updateAssistantMessage(sessionId, assistantId, (message) => ({
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
        updateAssistantMessage(sessionId, assistantId, (message) => ({
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
    const existingRequestId = streamCleanupRequestIds.current[sessionId];
    if (existingRequestId === requestId && streamCleanups.current[sessionId]) return;
    if (existingRequestId && existingRequestId !== requestId) {
      closeSessionStream(sessionId, existingRequestId);
    }
    setActiveRequestId(requestId);
    sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [sessionId]: requestId };
    setSessionRequestIds((current) => ({ ...current, [sessionId]: requestId }));
    const cleanup = openMessageStream({
      requestId,
      webPort: sidecarStatus.webPort,
      onItem: (item) => {
        if (item.request_id && item.request_id !== requestId) return;
        if (!shouldAcceptStreamItem(sessionId, requestId, item)) return;
        const isDeltaItem = item.type === "message_update" || item.type === "delta";
        if (!isDeltaItem) flushStreamDeltaBuffers(sessionId, requestId);
        if (item.type === "cancelled") {
          updateAssistantMessage(sessionId, assistantId, (message) => ({
            ...finishRunningSteps(message, "cancelled"),
            content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
            pending: false,
            cancelled: true
          }));
          markSessionOutputReady(sessionId);
          finishSessionRequest(sessionId, requestId);
          return;
        }
        if (item.type === "done") {
          updateAssistantMessage(sessionId, assistantId, (message) => ({
            ...(item.artifact || item.artifacts ? appendArtifact(finishRunningSteps(message), item) : finishRunningSteps(message)),
            content: redactInternalPromptText(item.content || message.content),
            pending: false,
            userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
            botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
          }));
          completedRequestIds.current[requestId] = true;
          markSessionOutputReady(sessionId);
          clearSessionRequestState(sessionId, requestId);
          window.setTimeout(() => {
            void refreshSessionFromHistory(sessionId);
          }, 300);
          return;
        }
        if (item.type === "error") {
          const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
          const staleRequest = /invalid request_id/i.test(message);
          finishSessionRequest(sessionId, requestId);
          if (staleRequest) {
            recoverStaleRequestFromHistory(sessionId, assistantId, requestId);
          } else {
            updateAssistantMessage(sessionId, assistantId, (entry) => ({
              ...finishRunningSteps(entry, "error"),
              content: message,
              pending: false,
              paused: false
            }));
            markSessionOutputReady(sessionId);
          }
          void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId });
          return;
        }
        if (item.type === "reasoning" || item.type === "thinking") {
          const chunk = item.content || item.text || "";
          if (chunk) {
            updateAssistantMessage(sessionId, assistantId, (message) => appendReasoningStep(message, chunk));
          }
          return;
        }
        if (item.type === "message_end") {
          if (item.has_tool_calls) {
            updateAssistantMessage(sessionId, assistantId, (message) => flushIntermediateContent(finishRunningSteps(message)));
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
          updateAssistantMessage(sessionId, assistantId, (message) => ({
            ...message,
            pending: true,
            paused: false,
            steps: [
              ...(message.steps || []),
              {
                type: "phase",
                content: `等待本机工具授权：${item.tool || "tool"}`
              }
            ]
          }));
          return;
        }
        if (item.type === "tool_start") {
          updateAssistantMessage(sessionId, assistantId, (message) => appendToolStart(message, item));
          return;
        }
        if (item.type === "tool_end") {
          updateAssistantMessage(sessionId, assistantId, (message) => appendToolEnd(message, item));
          return;
        }
        if (item.type === "artifact") {
          updateAssistantMessage(sessionId, assistantId, (message) => appendArtifact(message, item));
          return;
        }
        if (item.type === "image" || item.type === "video" || item.type === "file" || item.type === "voice_attach") {
          updateAssistantMessage(sessionId, assistantId, (message) => {
            const next = appendMediaStep(message, item);
            return item.type === "voice_attach" ? { ...finishRunningSteps(next), pending: false, paused: false } : next;
          });
          if (item.type === "voice_attach") {
            markSessionOutputReady(sessionId);
            finishSessionRequest(sessionId, requestId);
            delete completedRequestIds.current[requestId];
          }
          return;
        }
            if (item.type === "phase" && (item.content || item.message)) {
              const phaseContent = redactInternalPromptText(item.content || item.message || "");
              if (!phaseContent) return;
              updateAssistantMessage(sessionId, assistantId, (message) => ({
                ...message,
                pending: true,
                paused: false,
                steps: [...(message.steps || []), { type: "phase", content: phaseContent }]
              }));
              return;
            }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(sessionId, assistantId, requestId, streamItemText(item));
            }
      },
      onError: () => {
        if (completedRequestIds.current[requestId]) {
          delete completedRequestIds.current[requestId];
          finishSessionRequest(sessionId, requestId);
          return;
        }
        if (!isCurrentSessionRequest(sessionId, requestId)) return;
        closeSessionStream(sessionId, requestId);
        scheduleStreamReconnect(sessionId, assistantId, requestId);
      }
    });
    streamCleanup.current = cleanup;
    streamCleanups.current[sessionId] = cleanup;
    streamCleanupRequestIds.current[sessionId] = requestId;
  }

  async function sendNow(skipCapabilityCheck = false) {
    const text = composerText.trim();
    if (!text && !attachments.length) return;
    if (activeSessionRequestId) {
      await cancelChatRequest({ requestId: activeSessionRequestId, sessionId: activeSessionId }).catch(() => null);
      closeSessionStream(activeSessionId, activeSessionRequestId);
      markSessionRequestsPaused(activeSessionId);
      clearSessionRequestState(activeSessionId, activeSessionRequestId);
      setApproval(null);
    } else if (messagesRef.current.some((message) => message.pending)) {
      markSessionRequestsPaused(activeSessionId);
    }

    const enabledPacks = packs.filter((pack) => capabilityPackEnabled(pack.id));
    const neededPack = skipCapabilityCheck ? null : detectNeededPack(text, attachments, enabledPacks);
    if (neededPack) {
      const resumeSessionId = activeSessionIdRef.current;
      setSettingsSection("abilities");
      setSettingsOpen(true);
      void handleInstallPack(neededPack, () => {
        if (activeSessionIdRef.current !== resumeSessionId) {
          setToast(`${neededPack.name} 已安装，请回到原会话继续发送`);
          return;
        }
        void sendNow(true);
      });
      return;
    }

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
    const hiddenContext = activeProject ? projectContextPrompt(activeProject) : "";

    const estimatedTokens = estimateTokens(`${hiddenContext}\n\n${displayText}`.trim(), outboundAttachments);
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
    updateSessionMessages(requestSessionId, (current) => [...current, userMessage, { id: assistantId, role: "assistant", content: "", pending: true, createdAt: new Date().toISOString() }]);
    setActiveSessionTitle((current) => {
      const nextTitle = current === "新对话" ? shortTitle(text) : current;
      setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
      return nextTitle;
    });
    if (activeProject) {
      setSessionProjects((current) => ({ ...current, [requestSessionId]: activeProject.id }));
    }
    setComposerText("");
    setAttachments([]);
    setApproval(null);

    const quota = await checkEnterpriseQuota(estimatedTokens).catch((error) => {
      setToast(error instanceof Error ? `额度检查暂不可用，已继续发送：${error.message}` : "额度检查暂不可用，已继续发送");
      return { ok: true, quota: { allowed: true } } as EnterpriseQuotaCheckResult;
    });
    if (quota.quota) {
      setQuotaSnapshot(quota.quota);
    }
    if (quota.quota && quota.quota.allowed === false) {
      const quotaMessage = quota.quota.reason || "当前账号暂时不能继续发送。";
      const authFailure = isEnterpriseAuthFailure(quota.quota) && !isQuotaLimitFailure(quota.quota);
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
        attachments: outboundAttachments
      });
      if (result.status === "error") throw new Error(result.message || "发送失败");
      if (result.inline_reply) {
        const inlineReply = redactInternalPromptText(result.inline_reply || "");
        streamTextChars += inlineReply.length;
        updateSessionMessages(requestSessionId, (current) => current.map((item) => item.id === assistantId ? { ...item, content: inlineReply, pending: false } : item));
        markSessionOutputReady(requestSessionId);
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
      }
      if (result.request_id && result.stream) {
        const requestId = result.request_id;
        setActiveRequestId(requestId);
        sessionRequestIdsRef.current = { ...sessionRequestIdsRef.current, [requestSessionId]: requestId };
        setSessionRequestIds((current) => ({ ...current, [requestSessionId]: requestId }));
        streamRetryCounts.current[requestSessionId] = 0;
        updateAssistantMessage(requestSessionId, assistantId, (message) => ({
          ...message,
          requestId
        }));
        const cleanup = openMessageStream({
          requestId,
          webPort: sidecarStatus.webPort,
          onItem: (item) => {
            if (item.request_id && item.request_id !== requestId) return;
            if (!shouldAcceptStreamItem(requestSessionId, requestId, item)) return;
            observeStreamUsage(item);
            const isDeltaItem = item.type === "message_update" || item.type === "delta";
            if (!isDeltaItem) flushStreamDeltaBuffers(requestSessionId, requestId);
            if (item.type === "cancelled") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...finishRunningSteps(message, "cancelled"),
                content: redactInternalPromptText(item.content || item.message || message.content || "已停止"),
                pending: false,
                cancelled: true
              }));
              markSessionOutputReady(requestSessionId);
              finishSessionRequest(requestSessionId, requestId);
              return;
            }
            if (item.type === "done") {
              updateSessionMessages(requestSessionId, (current) => current.map((message) => {
                if (message.id === userMessage.id && typeof item.user_seq === "number") {
                  return { ...message, userSeq: item.user_seq };
                }
                if (message.id === assistantId) {
                  const nextMessage = item.artifact || item.artifacts
                    ? appendArtifact(finishRunningSteps(message), item)
                    : finishRunningSteps(message);
                  return {
                    ...nextMessage,
                    content: redactInternalPromptText(item.content || message.content),
                    pending: false,
                    userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                    botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
                  };
                }
                return message;
              }));
              completedRequestIds.current[requestId] = true;
              markSessionOutputReady(requestSessionId);
              clearSessionRequestState(requestSessionId, requestId);
              window.setTimeout(() => {
                void refreshSessionFromHistory(requestSessionId);
              }, 300);
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              return;
            }
            if (item.type === "error") {
              const message = redactInternalPromptText(item.content || item.message || "运行时返回错误");
              const staleRequest = /invalid request_id/i.test(message);
              finishSessionRequest(requestSessionId, requestId);
              if (staleRequest) {
                recoverStaleRequestFromHistory(requestSessionId, assistantId, requestId);
              } else {
                updateSessionMessages(requestSessionId, (current) => current.map((entry) => entry.id === assistantId ? {
                  ...finishRunningSteps(entry, "error"),
                  content: message,
                  pending: false,
                  paused: false
                } : entry));
                markSessionOutputReady(requestSessionId);
              }
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: requestSessionId });
              return;
            }
            if (item.type === "reasoning" || item.type === "thinking") {
              const chunk = item.content || item.text || "";
              if (chunk) {
                updateAssistantMessage(requestSessionId, assistantId, (message) => appendReasoningStep(message, chunk));
              }
              return;
            }
            if (item.type === "message_end") {
              if (item.has_tool_calls) {
                updateAssistantMessage(requestSessionId, assistantId, (message) => flushIntermediateContent(finishRunningSteps(message)));
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
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...message,
                pending: true,
                steps: [
                  ...(message.steps || []),
                  {
                    type: "phase",
                    content: `等待本机工具授权：${item.tool || "tool"}`
                  }
                ]
              }));
              return;
            }
            if (item.type === "tool_start") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => appendToolStart(message, item));
              return;
            }
            if (item.type === "tool_end") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => appendToolEnd(message, item));
              return;
            }
            if (item.type === "artifact") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => appendArtifact(message, item));
              return;
            }
            if (item.type === "image" || item.type === "video" || item.type === "file" || item.type === "voice_attach") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => {
                const next = appendMediaStep(message, item);
                return item.type === "voice_attach" ? { ...finishRunningSteps(next), pending: false, paused: false } : next;
              });
              if (item.type === "voice_attach") {
                markSessionOutputReady(requestSessionId);
                finishSessionRequest(requestSessionId, requestId);
                delete completedRequestIds.current[requestId];
              }
              return;
            }
            if (item.type === "phase" && (item.content || item.message)) {
              const phaseContent = redactInternalPromptText(item.content || item.message || "");
              if (!phaseContent) return;
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...message,
                pending: true,
                steps: [...(message.steps || []), { type: "phase", content: phaseContent }]
              }));
              return;
            }
            if (item.type === "message_update" || item.type === "delta") {
              enqueueAssistantDelta(requestSessionId, assistantId, requestId, streamItemText(item));
            }
          },
          onError: () => {
            if (completedRequestIds.current[requestId]) {
              delete completedRequestIds.current[requestId];
              finishSessionRequest(requestSessionId, requestId);
              return;
            }
            if (!isCurrentSessionRequest(requestSessionId, requestId)) return;
            closeSessionStream(requestSessionId, requestId);
            scheduleStreamReconnect(requestSessionId, assistantId, requestId);
          }
        });
        streamCleanup.current = cleanup;
        streamCleanups.current[requestSessionId] = cleanup;
        streamCleanupRequestIds.current[requestSessionId] = requestId;
      } else if (!result.inline_reply) {
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
      }
      generateSessionTitle({ sessionId: requestSessionId, userMessage: text || activeProject?.name || "项目会话" }).then((title) => {
        if (!title) return;
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
    } catch (error) {
      const message = error instanceof Error ? error.message : "发送失败";
      updateSessionMessages(requestSessionId, (current) => current.map((item) => item.id === assistantId ? { ...finishRunningSteps(item), content: message, pending: false } : item));
      markSessionOutputReady(requestSessionId);
      finishSessionRequest(requestSessionId);
      void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: requestSessionId });
    }
  }

  async function stopActiveRequest() {
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

  async function undoLastTurn() {
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (!lastUser) return;
    const index = messages.findIndex((message) => message.id === lastUser.id);
    setMessages((current) => current.slice(0, index));
    if (typeof lastUser.userSeq === "number") {
      await deleteMessagePair({ sessionId: activeSessionId, userSeq: lastUser.userSeq }).catch(() => undefined);
    }
  }

  function handleComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
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
    if (isMeta && event.key.toLowerCase() === "z" && !composerText) {
      event.preventDefault();
      void undoLastTurn();
    }
  }

  async function syncRuntimeUiStateNow() {
    const projectId = sessionProjects[activeSessionId] || null;
    const mergedState = pruneSessionUiState({
      ...sessionUiState,
      [activeSessionId]: {
        title: activeSessionTitle,
        projectId,
        messages,
        composerText,
        attachments
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
    const userMessage: ChatItem = {
      id: `u-install-${Date.now()}`,
      role: "user",
      content: `安装能力包：${pack.name}`,
      createdAt: new Date().toISOString()
    };
    const assistantId = `a-install-${Date.now()}`;
    updateSessionMessages(requestSessionId, (current) => [
      ...current,
      userMessage,
      { id: assistantId, role: "assistant", content: "", pending: true, createdAt: new Date().toISOString() }
    ]);
    const currentTitle = sessionTitles[requestSessionId]
      || sessionUiState[requestSessionId]?.title
      || (activeSessionIdRef.current === requestSessionId ? activeSessionTitle : "新对话");
    const nextTitle = currentTitle === "新对话" ? `安装 ${pack.name}` : currentTitle;
    setSessionTitles((titles) => ({ ...titles, [requestSessionId]: nextTitle }));
    if (activeSessionIdRef.current === requestSessionId) {
      setActiveSessionTitle(nextTitle);
    }
    const result = await sendChatMessage({
      sessionId: requestSessionId,
      message: userMessage.content,
      visibleMessage: userMessage.content,
      hiddenContext: prompt,
      attachments: []
    });
    if (result.status === "error") {
      throw new Error(result.message || "发送安装任务失败");
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
      updateAssistantMessage(requestSessionId, assistantId, (message) => ({ ...message, requestId }));
      attachMessageStream(requestSessionId, assistantId, requestId);
    }
  }

  async function handleInstallPack(pack: CapabilityPack, onInstalled?: () => void, targetSessionId?: string) {
    const requestSessionId = targetSessionId || activeSessionIdRef.current;
    if (pack.policyMode === "disabled") {
      setSettingsSection("abilities");
      setSettingsOpen(true);
      setToast("管理员已禁用安装，请联系管理员预置能力包");
      return;
    }
    const targetRequestId = sessionRequestIdsRef.current[requestSessionId] || sessionRequestIds[requestSessionId] || "";
    const targetMessages = requestSessionId === activeSessionIdRef.current
      ? messagesRef.current
      : sessionUiState[requestSessionId]?.messages || [];
    if (targetRequestId || targetMessages.some((message) => message.pending)) {
      const alreadyQueued = queuedInstallRef.current.some((item) => item.sessionId === requestSessionId && item.pack.id === pack.id);
      if (!alreadyQueued) {
        queuedInstallRef.current.push({ pack, onInstalled, sessionId: requestSessionId });
      }
      setSettingsSection("abilities");
      setSettingsOpen(true);
      setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
      setInstallNotice({
        packId: pack.id,
        packName: pack.name,
        message: `${pack.name} 已排队，当前任务结束后自动安装`
      });
      setToast(`${pack.name} 已排队安装`);
      return;
    }
    setSettingsSection("abilities");
    setSettingsOpen(true);
    setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
    setInstallNotice({
      packId: pack.id,
      packName: pack.name,
      message: `${pack.name} 正在安装，请稍后`
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
      watchAgentPackInstall(pack, onInstalled);
      setToast(`${pack.name} 安装任务已交给 agent`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : `${pack.name} 安装任务创建失败`);
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

  async function openArtifactFile(file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"]; open_action?: "preview" | "open" | "reveal" | "copy" | "openWith" }) {
    const rawPath = String(file.file_path || "").trim();
    if (!rawPath) return;
    const action = file.open_action || "open";
    if (action === "preview") {
      setPreviewFile({
        file_path: rawPath,
        file_name: file.file_name,
        file_type: file.file_type || "file"
      });
      return;
    }
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const resolvedPath = resolveArtifactPath(rawPath);
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    if (activeProject?.path) {
      await registerProjectFolderPath(activeProject.path).catch(() => null);
    }
    let result = "";
    const openAction: OpenPathAction = action === "reveal" ? "reveal" : action === "openWith" ? "openWith" : "open";
    for (const candidate of candidates) {
      try {
        result = await openRuntimePath(candidate, openAction);
      } catch (error) {
        if (!isLocalAbsolutePath(candidate)) {
          result = error instanceof Error ? error.message : String(error || "");
        } else {
          result = await openLocalPath(candidate, openAction);
        }
      }
      if (!/path not found|not found|找不到|不存在/i.test(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

  async function legacyOpenArtifactFile(file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"] }) {
    const rawPath = String(file.file_path || "").trim();
    if (!rawPath) return;
    if (isRuntimePreviewPath(rawPath)) {
      setToast("该预览链接没有可直接打开的本地路径");
      return;
    }
    const resolvedPath = resolveArtifactPath(rawPath);
    const candidates = Array.from(new Set([resolvedPath, rawPath].filter(Boolean)));
    if (activeProject?.path) {
      await registerProjectFolderPath(activeProject.path).catch(() => null);
    }
    let result = "";
    for (const candidate of candidates) {
      try {
        result = await openRuntimePath(candidate);
      } catch (error) {
        if (!isLocalAbsolutePath(candidate)) {
          result = error instanceof Error ? error.message : String(error || "");
        } else {
          result = await openLocalPath(candidate);
        }
      }
      if (!/path not found|not found|找不到|不存在/i.test(result)) break;
    }
    if (result) {
      setToast(result.startsWith("denied") ? "已取消打开文件" : result);
    }
  }

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
      detail: skillEnabled(runtimeSnapshot.skills, "image-generation") ? "图像生成 Skill 已开启" : "等待开启图像生成 Skill",
      enabled: skillEnabled(runtimeSnapshot.skills, "image-generation"),
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
  const hasPendingAssistantMessage = messages.some(isLiveAssistantMessage);
  const visibleMessages = messages.filter((message) => !isSilentPausedAssistantMessage(message));
  const composerHasPayload = Boolean(composerText.trim() || attachments.length);
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
  const renderSessionRow = (row: SessionRow) => {
    const cachedMessages = sessionUiState[row.id]?.messages || [];
    const isRunning = row.status === "waiting" || row.status === "cancelling" || Boolean(row.requestId) || Boolean(sessionRequestIds[row.id]) || cachedMessages.some(isLiveAssistantMessage) || (row.id === activeSessionId && hasPendingAssistantMessage);
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

  if (!authChecked) {
    return <main className="auth-shell"><WindowBrand /><section className="auth-panel"><p>正在检查登录状态</p></section></main>;
  }

  if (!session) {
    return <AuthGate onLogin={(next) => {
      if (!next) return;
      setSession(next);
      setQuotaSnapshot((next.quota || null) as UsageQuota | null);
    }} />;
  }

  return (
    <main className="app-shell">
      <WindowBrand />
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
            <span>项目</span>
            <button className="icon-button" type="button" onClick={() => void addProject()} title="添加项目文件夹">
              <FolderPlus aria-hidden="true" />
            </button>
          </div>
          {projectSessionGroups.length === 0 ? (
            <button className="project-empty" type="button" onClick={() => void addProject()} title="选择一个本地文件夹作为项目">
              <FolderOpen aria-hidden="true" />
              <span>添加项目文件夹</span>
            </button>
          ) : (
            <div className="project-list">
              {projectSessionGroups.slice(0, 8).map(({ project, sessions }) => (
                <article className={`project-group ${project.id === activeProjectId ? "is-active" : ""}`} key={project.id}>
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
                    <button className="project-new-session-button" type="button" title={`为 ${project.name} 创建新会话`} aria-label={`为 ${project.name} 创建新会话`} onClick={() => startNewSession(project)}>
                      <Plus aria-hidden="true" />
                    </button>
                    <button className="project-menu-button" type="button" title="项目操作" aria-label="项目操作" onClick={(event) => showProjectMenu(event, project)}>
                      <MoreHorizontal aria-hidden="true" />
                    </button>
                  </div>
                  <div className="project-session-list" aria-label={`${project.name} 的会话`}>
                    {sessions.length ? (
                      sessions.map(renderSessionRow)
                    ) : (
                      <button className="project-session-empty" type="button" onClick={() => startNewSession(project)} title={`为 ${project.name} 创建项目会话`}>
                        新建项目会话
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <div className="session-list" aria-label="会话列表">
          <div className="sidebar-section-title"><span>通用会话</span><small>{generalSessions.length}</small></div>
          {generalSessions.length ? generalSessions.map(renderSessionRow) : <div className="session-empty">暂无通用会话</div>}
        </div>

        <div className="sidebar-footer">
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
            visibleMessages.map((message) => (
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
                    onOpenLocalFile={(file) => void openArtifactFile(file)}
                    localFilePreviewUrl={(filePath) => filePreviewUrl(filePath, sidecarStatus.webPort)}
                  />
                  {message.attachments?.length ? (
                    <div className="message-files">
                      {message.attachments.map((file) => (
                        <button key={file.file_path} onClick={() => void openArtifactFile(file)} title="点击在本地打开">
                          {attachmentPreviewUrl(file) ? <img src={attachmentPreviewUrl(file)} alt={file.file_name} loading="lazy" /> : fileIcon(file)}<span>{file.file_name}</span>
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              </article>
            ))
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
                        <button className="primary-action" onClick={() => void handleInstallPack(approval.pack)}>安装并继续</button>
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

          <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendNow(); }}>
            {attachments.length > 0 && (
              <div className="attachment-tray">
                {attachments.map((file) => (
                  <article key={file.file_path}>
                    <button className="attachment-preview" type="button" onClick={() => setPreviewFile(file)} title="点击预览，右侧弹层会显示文件">
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
            {skillMentions.length > 0 && (
              <div className="skill-mention-popover" role="listbox" aria-label="可提及的 Skill">
                {skillMentions.map((skill) => (
                  <button key={skill.name || skill.display_name} type="button" onClick={() => insertSkillMention(skill)} title={skill.path || skill.source || "Skill"}>
                    <AtSign aria-hidden="true" />
                    <span>{skill.display_name || skill.name}</span>
                    <em>{skill.enabled === false ? "未启用" : "已启用"}</em>
                  </button>
                ))}
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
                              {installingPackIds[ability.pack.id] ? "正在安装" : "安装"}
                            </button>
                          ) : <em>{ability.enabled ? "已开启" : "待配置"}</em>}
                        </article>
                      ))}
                    </div>
                    <div className="skill-toggle-list">
                      <div className="toggle-list-head"><strong>Skill 生效开关</strong><span>可在聊天框用 @ 提及，关闭后不参与调用</span></div>
                      {(runtimeSnapshot.skills || []).map((skill) => {
                        const name = skill.name || skill.display_name || "";
                        const enabled = skill.enabled !== false;
                        return (
                          <label key={name || skill.path} className="toggle-row" title={skill.path || skill.source || name}>
                            <input type="checkbox" checked={enabled} onChange={(event) => void toggleRuntimeSkill(skill, event.currentTarget.checked)} />
                            <span><strong>{skill.display_name || name || "未命名 Skill"}</strong><small>{skill.source || "skill"}</small></span>
                            <em>{enabled ? "已启用" : "已关闭"}</em>
                          </label>
                        );
                      })}
                      {!(runtimeSnapshot.skills || []).length && <div className="session-empty">运行时暂未返回 Skill 列表</div>}
                    </div>
                    <div className="pack-list">
                      {packs.map((pack) => (
                        <article key={pack.id}>
                          <label className="pack-toggle" title="控制该能力包是否参与自动触发；安装状态不会被删除">
                            <input type="checkbox" checked={capabilityPackEnabled(pack.id)} onChange={(event) => toggleCapabilityPack(pack, event.currentTarget.checked)} />
                            <span>生效</span>
                          </label>
                          <div><strong>{pack.name}</strong><span>{installingPackIds[pack.id] ? "正在安装，请稍候" : pack.message}</span></div>
                          <button disabled={pack.installed || pack.policyMode === "disabled" || Boolean(installingPackIds[pack.id])} onClick={() => void handleInstallPack(pack)}>
                            {installingPackIds[pack.id] ? "正在安装" : pack.installed ? "已安装" : pack.policyMode === "disabled" ? "管理员禁用" : "安装"}
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
                    <div className="settings-list">
                      <article><div><strong>运行时</strong><span>{runtimeSnapshot.message}</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                      <article><div><strong>模型策略</strong><span>{runtimeSnapshot.modelsCount} 个企业模型映射</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>刷新</button></article>
                      <article><div><strong>Skill / MCP</strong><span>{runtimeSnapshot.skillsCount} 个 Skill，{runtimeSnapshot.toolsCount} 个工具通道</span></div><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
                    </div>
                  </section>
                )}
              </div>
            </div>
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

      {previewFile && (
        <div className="preview-popover">
          <header><strong>{previewFile.file_name}</strong><button className="icon-button" title="关闭预览" aria-label="关闭预览" onClick={() => setPreviewFile(null)}><X aria-hidden="true" /></button></header>
          {previewFile.file_type === "image" && previewFile.previewDataUrl ? (
            <img src={previewFile.previewDataUrl} alt={previewFile.file_name} />
          ) : (
            <iframe src={filePreviewUrl(previewFile.file_path, sidecarStatus.webPort)} title={previewFile.file_name} />
          )}
          <button onClick={() => requestOpenFile(previewFile)}>在系统中打开</button>
        </div>
      )}

      {toast && <div className="toast" onAnimationEnd={() => setToast("")}>{toast}</div>}
    </main>
  );
}

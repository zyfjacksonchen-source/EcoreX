import {
  ArrowDownToLine,
  AtSign,
  Bell,
  Bot,
  BookOpen,
  Brain,
  CheckCircle2,
  ChevronDown,
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
  installCapabilityPack,
  listCapabilityPacks,
  loadPermissionState,
  loadMemoryFiles,
  loadRuntimeSnapshot,
  loadSessionHistory,
  openLocalPath,
  openMessageStream,
  reportDesktopEvent,
  resetPermissionGrants,
  deleteRuntimeSession,
  renameRuntimeSession,
  savePastedFile,
  sendChatMessage,
  setSkillEnabled,
  updatePermissionMode,
  type CapabilityPack,
  type EnterpriseSession,
  type FileAttachment,
  type MemoryFile,
  type PermissionMode,
  type PermissionState,
  type ProjectFolder,
  type RuntimeMessage,
  type RuntimeSkill,
  type RuntimeStep,
  type RuntimeToolCall,
  type RuntimeTool,
  type RuntimeSnapshot,
  type StreamItem,
  type TokenUsage,
  type UsageQuota
} from "./services/ecorexApi";
import { CHAT_SCROLL_THRESHOLD_PX, getChatScrollState, scrollElementToBottom } from "./utils/chatUx";

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
  status: "active" | "waiting" | "ready" | "failed";
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
  reasoning?: string;
  steps?: AgentStepDisclosure[];
  toolCalls?: ToolCallDisclosure[];
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

const brandIconUrl = new URL("../build/icon.png", import.meta.url).href;
document.documentElement.dataset.platform = window.ecorexDesktop?.platform || "web";

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
const PROJECTS_STORAGE_KEY = "ecorex-projects";
const SESSION_PROJECTS_STORAGE_KEY = "ecorex-session-projects";
const SESSION_TITLES_STORAGE_KEY = "ecorex-session-titles";
const PINNED_SESSIONS_STORAGE_KEY = "ecorex-pinned-sessions";
const PINNED_PROJECTS_STORAGE_KEY = "ecorex-pinned-projects";
const SESSION_UI_STORAGE_KEY = "ecorex-session-ui-state";
const CAPABILITY_ENABLED_STORAGE_KEY = "ecorex-capability-enabled";
const SKILL_DEFAULTS_STORAGE_KEY = "ecorex-skill-defaults-v1";
const CONTEXT_THRESHOLD_TOKENS = 258_000;
const EFFECTIVE_MODEL_FALLBACK = "gpt-5.5";
const EFFECTIVE_MODEL_ALIAS_PREFIXES = ["deepseek-"];

const coreAbilityNames = new Set([
  "bash",
  "read",
  "write",
  "edit",
  "ls",
  "vision",
  "web_search",
  "web_fetch",
  "browser",
  "memory_search",
  "memory_get"
]);

const skillAbilityNames = new Set(["image-generation", "knowledge-wiki", "skill-creator"]);

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
  return Object.fromEntries(
    Object.entries(state).slice(-24).map(([sessionId, value]) => [
      sessionId,
      {
        ...value,
        messages: value.messages.slice(-60),
        attachments: value.attachments.slice(0, 12)
      }
    ])
  );
}

function initialTheme(): ThemeMode {
  const saved = window.localStorage.getItem("ecorex-theme");
  if (saved === "dark" || saved === "light") return saved;
  return window.ecorexDesktop?.shouldUseDarkColors ? "dark" : "light";
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
  activeProjectId: string | null
): SessionRow[] {
  const projectById = new Map(projects.map((project) => [project.id, project]));
  const rows = snapshot.sessions.map((session, index) => {
    const id = session.session_id || session.id || `runtime-${index}`;
    const projectId = sessionProjects[id];
    const project = projectId ? projectById.get(projectId) : undefined;
    return {
      id,
      title: sessionTitles[id] || session.title || session.session_id || "未命名会话",
      detail: project ? `${project.name} · ${session.msg_count ?? 0} 条` : `${session.msg_count ?? 0} 条`,
      updatedAt: session.last_active || "最近",
      status: id === activeSessionId ? "active" : "ready",
      pinned: Boolean(pinnedSessions[id]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    } satisfies SessionRow;
  });
  if (!rows.some((row) => row.id === activeSessionId)) {
    const project = activeProjectId ? projectById.get(activeProjectId) : undefined;
    rows.unshift({
      id: activeSessionId,
      title: sessionTitles[activeSessionId] || localTitle || "新对话",
      detail: project ? `${project.name} · 草稿` : "草稿",
      updatedAt: "刚刚",
      status: "active",
      pinned: Boolean(pinnedSessions[activeSessionId]),
      ...(project ? { projectId: project.id, projectName: project.name } : {})
    });
  }
  return rows.sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
}

function estimateTokenCount(text: string, files: FileAttachment[]) {
  return Math.max(0, Math.ceil(text.length / 2) + files.length * 120);
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
  const history = messages.reduce((total, message) => total + estimateTokenCount(message.content || "", message.attachments || []), 0);
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
  return mode === "smart-ask"
    ? "智能确认"
    : mode === "always-ask"
      ? "每次询问"
      : mode === "read-only"
        ? "只读优先"
        : mode === "custom"
          ? "自定义"
          : "未设置";
}

function projectContextPrompt(project: ProjectFolder) {
  return [
    "【EcoreX 项目上下文】",
    "默认沟通风格：专业、严谨、克制，称呼用户为“同学”。",
    "对外身份始终是 EcoreX，不自称 CowAgent 或 COW。",
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
    result: tool.result,
    status: tool.status,
    is_error: tool.is_error,
    execution_time: tool.execution_time
  };
}

function normalizeStep(step: RuntimeStep): AgentStepDisclosure {
  const type = String(step.type || "").toLowerCase();
  const content = step.content || step.text || step.thinking || "";
  if (type === "thinking" || type === "reasoning") {
    return { type: "thinking", content };
  }
  if (type === "tool" || type === "tool_start" || type === "tool_end") {
    return {
      type: "tool",
      name: step.name || step.tool,
      arguments: step.arguments ?? step.input,
      result: step.result,
      status: step.status,
      is_error: step.is_error,
      execution_time: step.execution_time,
      running: type === "tool_start" || step.status === "running"
    };
  }
  if (type === "phase") {
    return { type: "phase", content };
  }
  if (type === "image" || type === "video" || type === "file" || step.file_type) {
    const fileType = step.file_type === "image" || step.file_type === "video" ? step.file_type : type === "image" || type === "video" ? type : "file";
    return {
      type: "media",
      fileType,
      url: step.path || content,
      fileName: step.file_name
    };
  }
  return { type: "content", content };
}

function mapRuntimeMessage(item: RuntimeMessage, sessionId: string, index: number): ChatItem {
  return {
    id: `${sessionId}-${index}`,
    role: item.role === "user" ? "user" : "assistant",
    content: item.content || "",
    createdAt: item.created_at ? new Date(item.created_at * 1000).toISOString() : new Date().toISOString(),
    reasoning: item.reasoning,
    steps: item.steps?.map(normalizeStep),
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
  const [theme, setTheme] = useState<ThemeMode>(initialTheme);
  const [session, setSession] = useState<EnterpriseSession | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [runtimeSnapshot, setRuntimeSnapshot] = useState(initialRuntime);
  const [sidecarStatus, setSidecarStatus] = useState(initialSidecar);
  const [quotaSnapshot, setQuotaSnapshot] = useState<UsageQuota | null>(null);
  const [activeSessionId, setActiveSessionId] = useState(`ecorex-${Date.now()}`);
  const [activeSessionTitle, setActiveSessionTitle] = useState("新对话");
  const [searchQuery, setSearchQuery] = useState("");
  const [messages, setMessages] = useState<ChatItem[]>([]);
  const [composerText, setComposerText] = useState("");
  const [attachments, setAttachments] = useState<FileAttachment[]>([]);
  const [, setActiveRequestId] = useState("");
  const [approval, setApproval] = useState<ApprovalState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [previewFile, setPreviewFile] = useState<FileAttachment | null>(null);
  const [packs, setPacks] = useState<CapabilityPack[]>([]);
  const [permissionState, setPermissionState] = useState<PermissionState | null>(null);
  const [projects, setProjects] = useState<ProjectFolder[]>(() => readStorage<ProjectFolder[]>(PROJECTS_STORAGE_KEY, []));
  const [sessionProjects, setSessionProjects] = useState<SessionProjectMap>(() => readStorage<SessionProjectMap>(SESSION_PROJECTS_STORAGE_KEY, {}));
  const [sessionTitles, setSessionTitles] = useState<StringMap>(() => readStorage<StringMap>(SESSION_TITLES_STORAGE_KEY, {}));
  const [pinnedSessions, setPinnedSessions] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_SESSIONS_STORAGE_KEY, {}));
  const [pinnedProjects, setPinnedProjects] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(PINNED_PROJECTS_STORAGE_KEY, {}));
  const [sessionUiState, setSessionUiState] = useState<Record<string, SessionUiState>>(() => readStorage<Record<string, SessionUiState>>(SESSION_UI_STORAGE_KEY, {}));
  const [enabledCapabilityPacks, setEnabledCapabilityPacks] = useState<StringBoolMap>(() => readStorage<StringBoolMap>(CAPABILITY_ENABLED_STORAGE_KEY, {}));
  const [sessionRequestIds, setSessionRequestIds] = useState<StringMap>({});
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("account");
  const [projectMenu, setProjectMenu] = useState<ProjectContextMenu>(null);
  const [installingPackIds, setInstallingPackIds] = useState<StringBoolMap>({});
  const [memoryFiles, setMemoryFiles] = useState<MemoryFile[]>([]);
  const [dreamFiles, setDreamFiles] = useState<MemoryFile[]>([]);
  const [toast, setToast] = useState("");
  const [passwordDraft, setPasswordDraft] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const streamCleanup = useRef<null | (() => void)>(null);
  const streamCleanups = useRef<Record<string, () => void>>({});
  const activeSessionIdRef = useRef(activeSessionId);
  const autoScrollRef = useRef(true);
  const completedRequestIds = useRef<StringBoolMap>({});
  const preloadDone = useRef(false);

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
    writeStorage(SESSION_UI_STORAGE_KEY, pruneSessionUiState(sessionUiState));
  }, [sessionUiState]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    setSessionUiState((current) => ({
      ...current,
      [activeSessionId]: {
        title: activeSessionTitle,
        projectId: activeProjectId,
        messages,
        composerText,
        attachments
      }
    }));
  }, [activeSessionId, activeSessionTitle, activeProjectId, messages, composerText, attachments]);

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
    getEnterpriseSession()
      .then((existing) => setSession(existing))
      .catch(() => setSession(null))
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    const unsubscribe = window.ecorexDesktop?.onSidecarStatus?.((status) => setSidecarStatus(status));
    window.ecorexDesktop?.getSidecarStatus?.().then((status) => setSidecarStatus(status)).catch(() => undefined);
    return () => {
      streamCleanup.current?.();
      Object.values(streamCleanups.current).forEach((cleanup) => cleanup());
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (preloadDone.current) return;
    if (sidecarStatus.state !== "running") return;
    preloadDone.current = true;
    void (async () => {
      const nextPacks = await listCapabilityPacks().catch(() => []);
      setPacks(nextPacks);
      const preinstallTargets = nextPacks.filter((pack) => !pack.installed && pack.policyMode === "preinstall");
      if (preinstallTargets.length) {
        setInstallingPackIds((current) => ({
          ...current,
          ...Object.fromEntries(preinstallTargets.map((pack) => [pack.id, true]))
        }));
        await Promise.all(preinstallTargets.map((pack) => installCapabilityPack(pack.id).catch(() => null)));
        setInstallingPackIds((current) => {
          const next = { ...current };
          preinstallTargets.forEach((pack) => delete next[pack.id]);
          return next;
        });
        setPacks(await listCapabilityPacks().catch(() => nextPacks));
      }
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

  const visibleSessions = useMemo(() => {
    const rows = mapSessions(runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, activeProjectId);
    const needle = searchQuery.trim().toLowerCase();
    return needle ? rows.filter((row) => `${row.title} ${row.detail}`.toLowerCase().includes(needle)) : rows;
  }, [runtimeSnapshot, activeSessionId, activeSessionTitle, sessionProjects, sessionTitles, pinnedSessions, projects, activeProjectId, searchQuery]);

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) || null,
    [projects, activeProjectId]
  );
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
    window.requestAnimationFrame(() => {
      composerRef.current?.focus();
      syncComposerHeight();
    });
  }

  function restoreCachedSession(sessionId: string) {
    const cached = sessionUiState[sessionId];
    if (!cached) return false;
    setMessages(cached.messages);
    setComposerText(cached.composerText);
    setAttachments(cached.attachments);
    setActiveSessionTitle(sessionTitles[sessionId] || cached.title || "新对话");
    setActiveProjectId(cached.projectId || sessionProjects[sessionId] || null);
    return true;
  }

  async function selectSession(row: SessionRow) {
    const nextProjectId = row.projectId || null;
    autoScrollRef.current = true;
    setShowJumpLatest(false);
    setActiveSessionId(row.id);
    setActiveSessionTitle(row.title);
    setActiveProjectId(nextProjectId);
    setPreviewFile(null);
    setApproval(null);
    if (restoreCachedSession(row.id)) {
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
      const mapped = history.map((item, index) => mapRuntimeMessage(item, row.id, index));
      setSessionUiState((current) => ({
        ...current,
        [row.id]: {
          ...(current[row.id] || {
            title: row.title,
            projectId: nextProjectId,
            composerText: "",
            attachments: []
          }),
          messages: mapped
        }
      }));
      if (activeSessionIdRef.current === row.id) {
        setMessages(mapped);
        focusComposerSoon();
      }
    } catch (error) {
      setToast(error instanceof Error ? error.message : "加载会话失败");
    }
  }

  function startNewSession(project?: ProjectFolder | null) {
    const id = project ? `ecorex-project-${project.id}-${Date.now()}` : `ecorex-${Date.now()}`;
    const title = project ? `${project.name} · 新会话` : "新对话";
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
          setActiveProjectId(existing.id);
          nextProject = { ...existing, updatedAt: project.updatedAt };
          return current.map((item) => item.path === project.path ? nextProject : item);
        }
        setActiveProjectId(project.id);
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
    void openLocalPath(project.path);
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
          messages: updater(existing.messages)
        }
      };
    });
    if (activeSessionIdRef.current === sessionId) {
      setMessages(updater);
    }
  }

  function updateAssistantMessage(sessionId: string, assistantId: string, updater: (message: ChatItem) => ChatItem) {
    updateSessionMessages(sessionId, (current) => current.map((message) => message.id === assistantId ? updater(message) : message));
  }

  function finishRunningSteps(message: ChatItem): ChatItem {
    const steps = (message.steps || []).map((step) => {
      if (step.type === "thinking" && step.running) {
        return { ...step, running: false };
      }
      if (step.type === "tool" && step.running) {
        return { ...step, running: false, status: step.status === "running" ? "done" : step.status };
      }
      return step;
    });
    return { ...message, steps };
  }

  function appendReasoningStep(message: ChatItem, chunk: string): ChatItem {
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
    const content = message.content.trim();
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
    const steps = [...(next.steps || [])];
    const toolIndex = steps.findIndex((step) => step.type === "tool" && step.name === toolName);
    const runningTool: Extract<AgentStepDisclosure, { type: "tool" }> = {
      type: "tool",
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
    let targetIndex = -1;
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const step = steps[index];
      if (step.type === "tool" && step.running && (!toolName || step.name === toolName)) {
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
      name: toolName,
      arguments: item.arguments ?? item.input,
      result: item.result ?? item.content ?? item.message,
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
        : "file";
    return {
      ...next,
      pending: true,
      steps: [
        ...(next.steps || []),
        {
          type: "media",
          fileType: type,
          url: item.path || item.url || item.content,
          fileName: item.file_name || item.name
        }
      ]
    };
  }

  function clearSessionRequestState(sessionId: string) {
    setActiveRequestId("");
    setSessionRequestIds((current) => {
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
  }

  function finishSessionRequest(sessionId: string) {
    clearSessionRequestState(sessionId);
    streamCleanups.current[sessionId]?.();
    delete streamCleanups.current[sessionId];
  }

  async function sendNow(skipCapabilityCheck = false) {
    const text = composerText.trim();
    if (!text && !attachments.length) return;
    if (activeSessionRequestId) return;

    const enabledPacks = packs.filter((pack) => capabilityPackEnabled(pack.id));
    const neededPack = skipCapabilityCheck ? null : detectNeededPack(text, attachments, enabledPacks);
    if (neededPack) {
      setSettingsSection("abilities");
      setSettingsOpen(true);
      void handleInstallPack(neededPack, () => void sendNow(true));
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
    const outboundText = activeProject
      ? `${projectContextPrompt(activeProject)}\n\n用户需求：${text || "请处理这些附件"}`
      : text;

    const estimatedTokens = estimateTokens(outboundText || text, outboundAttachments);
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
    const quota = await checkEnterpriseQuota(estimatedTokens);
    if (quota.quota) {
      setQuotaSnapshot(quota.quota);
    }
    if (quota.quota && quota.quota.allowed === false) {
      setApproval({ type: "quota", title: "额度已达到上限", message: quota.quota.reason || "当前账号暂时不能继续发送。" });
      return;
    }

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
      const result = await sendChatMessage({ sessionId: requestSessionId, message: outboundText, attachments: outboundAttachments });
      if (result.status === "error") throw new Error(result.message || "发送失败");
      if (result.inline_reply) {
        streamTextChars += result.inline_reply.length;
        updateSessionMessages(requestSessionId, (current) => current.map((item) => item.id === assistantId ? { ...item, content: result.inline_reply || "", pending: false } : item));
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
      }
      if (result.request_id && result.stream) {
        setActiveRequestId(result.request_id);
        setSessionRequestIds((current) => ({ ...current, [requestSessionId]: result.request_id || "" }));
        const cleanup = openMessageStream({
          requestId: result.request_id,
          webPort: sidecarStatus.webPort,
          onItem: (item) => {
            if (item.request_id && item.request_id !== result.request_id) return;
            observeStreamUsage(item);
            if (item.type === "cancelled") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...finishRunningSteps(message),
                content: message.content || "已停止",
                pending: false,
                cancelled: true
              }));
              clearSessionRequestState(requestSessionId);
              return;
            }
            if (item.type === "done") {
              updateSessionMessages(requestSessionId, (current) => current.map((message) => {
                if (message.id === userMessage.id && typeof item.user_seq === "number") {
                  return { ...message, userSeq: item.user_seq };
                }
                if (message.id === assistantId) {
                  return {
                    ...finishRunningSteps(message),
                    content: item.content || message.content,
                    pending: false,
                    userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq,
                    botSeq: typeof item.bot_seq === "number" ? item.bot_seq : message.botSeq
                  };
                }
                return message;
              }));
              completedRequestIds.current[result.request_id || ""] = true;
              clearSessionRequestState(requestSessionId);
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              return;
            }
            if (item.type === "error") {
              const message = item.content || item.message || "运行时返回错误";
              updateSessionMessages(requestSessionId, (current) => current.map((entry) => entry.id === assistantId ? {
                ...finishRunningSteps(entry),
                content: message,
                pending: false
              } : entry));
              finishSessionRequest(requestSessionId);
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
            if (item.type === "image" || item.type === "video" || item.type === "file" || item.type === "voice_attach") {
              updateAssistantMessage(requestSessionId, assistantId, (message) => appendMediaStep(message, item));
              if (item.type === "voice_attach") {
                finishSessionRequest(requestSessionId);
                delete completedRequestIds.current[result.request_id || ""];
              }
              return;
            }
            if (item.type === "phase" && (item.content || item.message)) {
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...message,
                pending: true,
                steps: [...(message.steps || []), { type: "phase", content: item.content || item.message }]
              }));
              return;
            }
            if ((item.type === "message_update" || item.type === "delta") && item.content) {
              updateAssistantMessage(requestSessionId, assistantId, (message) => ({
                ...finishRunningSteps(message),
                content: `${message.content}${item.content}`,
                pending: true
              }));
            }
          },
          onError: () => {
            if (completedRequestIds.current[result.request_id || ""]) {
              delete completedRequestIds.current[result.request_id || ""];
              finishSessionRequest(requestSessionId);
              return;
            }
            finishSessionRequest(requestSessionId);
            updateAssistantMessage(requestSessionId, assistantId, (message) => ({ ...finishRunningSteps(message), pending: false }));
            reportChatUsage(undefined, "estimated");
          }
        });
        streamCleanup.current = cleanup;
        streamCleanups.current[requestSessionId] = cleanup;
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
              projectId: sessionProjects[requestSessionId] || null,
              messages: [],
              composerText: "",
              attachments: []
            }),
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
      clearSessionRequestState(activeSessionId);
      updateSessionMessages(activeSessionId, (current) => current.map((message) => message.pending ? {
        ...finishRunningSteps(message),
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

  async function handleInstallPack(pack: CapabilityPack, onInstalled?: () => void) {
    if (pack.policyMode === "disabled") {
      setSettingsSection("abilities");
      setSettingsOpen(true);
      setToast("管理员已禁用安装，请联系管理员预置能力包");
      return;
    }
    setSettingsSection("abilities");
    setSettingsOpen(true);
    setInstallingPackIds((current) => ({ ...current, [pack.id]: true }));
    try {
      const result = await installCapabilityPack(pack.id);
      const nextPacks = await listCapabilityPacks();
      setPacks(nextPacks);
      if (result?.installed) {
        setToast(`${pack.name} 已安装`);
        onInstalled?.();
      } else {
        setToast(result?.message || `${pack.name} 安装失败`);
      }
    } finally {
      setInstallingPackIds((current) => {
        const next = { ...current };
        delete next[pack.id];
        return next;
      });
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

  async function openArtifactFile(file: { file_path: string; file_name: string; file_type?: FileAttachment["file_type"] }) {
    const result = await openLocalPath(file.file_path);
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
  const hasPendingAssistantMessage = messages.some((message) => message.pending);
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
    const isRunning = Boolean(sessionRequestIds[row.id]) || cachedMessages.some((message) => message.pending) || (row.id === activeSessionId && hasPendingAssistantMessage);
    const isActive = row.id === activeSessionId;
    const rowTitle = `${row.title}\n${row.detail}\n${formatTime(row.updatedAt)}`;
    return (
      <article className={`session-row is-${isRunning ? "waiting" : row.status}${isActive ? " is-active" : ""}`} key={row.id}>
        <button
          className="session-main"
          type="button"
          onClick={() => void selectSession(row)}
          title={rowTitle}
          data-tooltip={rowTitle}
          aria-current={isActive ? "page" : undefined}
        >
          {isRunning ? <ThinkingIndicator compact /> : row.projectId ? <FolderOpen aria-hidden="true" /> : <Bot aria-hidden="true" />}
          <span className="session-line"><strong>{row.title}</strong><small>{row.detail}</small></span>
          <em>{formatTime(row.updatedAt)}</em>
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
                      onClick={() => setActiveProjectId(project.id)}
                      title={`${project.name}\n${project.path}\n项目记忆：${project.memoryPath || ".ecorex/project-memory.md"}`}
                    >
                      <FolderOpen aria-hidden="true" />
                      <span>{project.name}</span>
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

        <div className="message-list" ref={messageListRef} onScroll={updateJumpLatestState}>
          {messages.length === 0 ? (
            <div className="empty-chat">
              <BrandMark />
              <strong>{activeProject ? activeProject.name : "可以直接开始"}</strong>
              <span>{activeProject ? "你可以把任何文件/图片/视频 参考扔到项目文件夹内 我会基于项目文件夹上下文回答你。" : "粘贴图片或文件，输入需求，EcoreX 会在需要能力包或权限时先确认。"}</span>
            </div>
          ) : (
            messages.map((message) => (
              <article className={`message ${message.role}`} key={message.id}>
                <div className="message-body">
                  <MessageContent
                    role={message.role}
                    content={message.content}
                    pending={message.pending}
                    cancelled={message.cancelled}
                    reasoning={message.reasoning}
                    steps={message.steps}
                    toolCalls={message.toolCalls}
                    onOpenLocalFile={(file) => void openArtifactFile(file)}
                    localFilePreviewUrl={(filePath) => filePreviewUrl(filePath, sidecarStatus.webPort)}
                  />
                  {message.attachments?.length ? (
                    <div className="message-files">
                      {message.attachments.map((file) => (
                        <button key={file.file_path} onClick={() => void openArtifactFile(file)} title="点击在本地打开">
                          {fileIcon(file)}{file.file_name}
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
            {activeSessionRequestId || hasPendingAssistantMessage ? (
              <button type="button" className="send-button stop" onClick={stopActiveRequest} title="停止当前回复"><Square aria-hidden="true" /></button>
            ) : (
              <button type="submit" className="send-button" disabled={!composerText.trim() && attachments.length === 0} title="发送，Enter 也可以发送">
                <SendHorizontal aria-hidden="true" />
              </button>
            )}
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
                      <span>登录前预加载/预安装；登录后在这里逐个勾选生效，默认全开</span>
                    </div>
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
                      {(["smart-ask", "always-ask", "read-only", "custom"] as PermissionMode[]).map((mode) => (
                        <button key={mode} className={permissionState?.mode === mode ? "is-active" : ""} onClick={() => updatePermissionMode(mode).then(setPermissionState)}>
                          {mode === "smart-ask" ? "智能确认" : mode === "always-ask" ? "每次询问" : mode === "read-only" ? "只读优先" : "自定义"}
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

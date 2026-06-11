import {
  Bell,
  Bot,
  CheckCircle2,
  ChevronDown,
  FileText,
  FolderOpen,
  LogOut,
  Moon,
  Paperclip,
  Plus,
  Search,
  Settings,
  Sparkles,
  Square,
  SunMedium,
  Upload,
  UserRound,
  X
} from "lucide-react";
import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelChatRequest,
  enterpriseChangePassword,
  checkEnterpriseQuota,
  chooseLocalFiles,
  deleteMessagePair,
  enterpriseLogin,
  enterpriseLogout,
  filePreviewUrl,
  generateSessionTitle,
  getEnterpriseSession,
  installCapabilityPack,
  listCapabilityPacks,
  loadPermissionState,
  loadRuntimeSnapshot,
  loadSessionHistory,
  openLocalPath,
  openMessageStream,
  reportDesktopEvent,
  resetPermissionGrants,
  savePastedFile,
  sendChatMessage,
  updatePermissionMode,
  type CapabilityPack,
  type EnterpriseSession,
  type FileAttachment,
  type PermissionMode,
  type PermissionState,
  type RuntimeSnapshot,
  type TokenUsage
} from "./services/ecorexApi";

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
  updatedAt: string;
  status: "active" | "waiting" | "ready" | "failed";
};
type ChatItem = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  attachments?: FileAttachment[];
  pending?: boolean;
  userSeq?: number;
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

function initialTheme(): ThemeMode {
  const saved = window.localStorage.getItem("ecorex-theme");
  if (saved === "dark" || saved === "light") return saved;
  return window.ecorexDesktop?.shouldUseDarkColors ? "dark" : "light";
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.dataset.theme = theme;
}

function formatTime(value?: string) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function shortTitle(text: string) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean ? clean.slice(0, 22) : "新对话";
}

function mapSessions(snapshot: RuntimeSnapshot, activeSessionId: string, localTitle: string): SessionRow[] {
  const rows = snapshot.sessions.map((session, index) => {
    const id = session.session_id || session.id || `runtime-${index}`;
    return {
      id,
      title: session.title || session.session_id || "未命名会话",
      detail: `${session.msg_count ?? 0} 条消息`,
      updatedAt: session.last_active || "最近",
      status: id === activeSessionId ? "active" : "ready"
    } satisfies SessionRow;
  });
  if (!rows.some((row) => row.id === activeSessionId)) {
    rows.unshift({
      id: activeSessionId,
      title: localTitle || "新对话",
      detail: "当前会话",
      updatedAt: "刚刚",
      status: "active"
    });
  }
  return rows;
}

function estimateTokens(text: string, files: FileAttachment[]) {
  return Math.max(1, Math.ceil(text.length / 2) + files.length * 120);
}

function usageTotal(usage?: TokenUsage | null) {
  if (!usage) return 0;
  const total = Number(usage.totalTokens || 0);
  if (Number.isFinite(total) && total > 0) return total;
  const input = Number(usage.inputTokens || 0);
  const output = Number(usage.outputTokens || 0);
  return Math.max(0, (Number.isFinite(input) ? input : 0) + (Number.isFinite(output) ? output : 0));
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
      <section className="auth-panel">
        <div className="brand-mark"><Sparkles aria-hidden="true" /></div>
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
  const [activeSessionId, setActiveSessionId] = useState(`ecorex-${Date.now()}`);
  const [activeSessionTitle, setActiveSessionTitle] = useState("新对话");
  const [searchQuery, setSearchQuery] = useState("");
  const [messages, setMessages] = useState<ChatItem[]>([]);
  const [composerText, setComposerText] = useState("");
  const [attachments, setAttachments] = useState<FileAttachment[]>([]);
  const [activeRequestId, setActiveRequestId] = useState("");
  const [approval, setApproval] = useState<ApprovalState | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [previewFile, setPreviewFile] = useState<FileAttachment | null>(null);
  const [packs, setPacks] = useState<CapabilityPack[]>([]);
  const [permissionState, setPermissionState] = useState<PermissionState | null>(null);
  const [toast, setToast] = useState("");
  const [passwordDraft, setPasswordDraft] = useState({ oldPassword: "", newPassword: "", confirmPassword: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const streamCleanup = useRef<null | (() => void)>(null);

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem("ecorex-theme", theme);
  }, [theme]);

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
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    async function refresh() {
      const [snapshot, nextPacks, nextPermissions] = await Promise.all([
        loadRuntimeSnapshot(),
        listCapabilityPacks(),
        loadPermissionState()
      ]);
      if (!cancelled) {
        setRuntimeSnapshot(snapshot);
        setPacks(nextPacks);
        setPermissionState(nextPermissions);
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
    const rows = mapSessions(runtimeSnapshot, activeSessionId, activeSessionTitle);
    const needle = searchQuery.trim().toLowerCase();
    return needle ? rows.filter((row) => `${row.title} ${row.detail}`.toLowerCase().includes(needle)) : rows;
  }, [runtimeSnapshot, activeSessionId, activeSessionTitle, searchQuery]);

  async function selectSession(row: SessionRow) {
    setActiveSessionId(row.id);
    setActiveSessionTitle(row.title);
    setPreviewFile(null);
    try {
      const history = await loadSessionHistory(row.id);
      setMessages(
        history.map((item, index) => ({
          id: `${row.id}-${index}`,
          role: item.role === "user" ? "user" : "assistant",
          content: item.content || "",
          createdAt: item.created_at ? new Date(item.created_at * 1000).toISOString() : new Date().toISOString(),
          userSeq: item.user_seq ?? item.seq
        }))
      );
    } catch (error) {
      setToast(error instanceof Error ? error.message : "加载会话失败");
    }
  }

  function startNewSession() {
    const id = `ecorex-${Date.now()}`;
    setActiveSessionId(id);
    setActiveSessionTitle("新对话");
    setMessages([]);
    setAttachments([]);
    setComposerText("");
    setApproval(null);
    composerRef.current?.focus();
  }

  async function chooseFiles() {
    try {
      const files = await chooseLocalFiles(sidecarStatus.webPort);
      setAttachments((current) => [...current, ...files]);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "选择文件失败");
    }
  }

  async function handlePaste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
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

  async function sendNow(skipCapabilityCheck = false) {
    const text = composerText.trim();
    if (!text && !attachments.length) return;
    if (activeRequestId) return;

    const neededPack = skipCapabilityCheck ? null : detectNeededPack(text, attachments, packs);
    if (neededPack) {
      setApproval({
        type: "capability",
        title: `需要安装 ${neededPack.name}`,
        message: neededPack.policyMode === "disabled" ? "管理员已禁用普通用户安装，请联系管理员预置。" : neededPack.message,
        pack: neededPack,
        resume: () => void sendNow(true)
      });
      return;
    }

    const estimatedTokens = estimateTokens(text, attachments);
    const quota = await checkEnterpriseQuota(estimatedTokens);
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
    setMessages((current) => [...current, userMessage, { id: assistantId, role: "assistant", content: "", pending: true, createdAt: new Date().toISOString() }]);
    setActiveSessionTitle((current) => (current === "新对话" ? shortTitle(text) : current));
    setComposerText("");
    setAttachments([]);
    setApproval(null);

    let usageReported = false;
    const reportChatUsage = (usage: TokenUsage | undefined, source: "provider" | "estimated") => {
      if (usageReported) return;
      usageReported = true;
      const totalTokens = usageTotal(usage) || estimatedTokens;
      void reportDesktopEvent({
        type: "usage",
        source: "Desktop",
        category: "chat",
        label: "message",
        amount: totalTokens,
        sessionId: activeSessionId,
        detail: {
          inputTokens: usage?.inputTokens || 0,
          outputTokens: usage?.outputTokens || 0,
          totalTokens,
          model: usage?.model || runtimeSnapshot.version,
          provider: usage?.provider || "",
          estimatedTokens,
          usageSource: source
        }
      });
    };

    try {
      const result = await sendChatMessage({ sessionId: activeSessionId, message: text, attachments });
      if (result.status === "error") throw new Error(result.message || "发送失败");
      if (result.inline_reply) {
        setMessages((current) => current.map((item) => item.id === assistantId ? { ...item, content: result.inline_reply || "", pending: false } : item));
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
      }
      if (result.request_id && result.stream) {
        setActiveRequestId(result.request_id);
        streamCleanup.current = openMessageStream({
          requestId: result.request_id,
          webPort: sidecarStatus.webPort,
          onItem: (item) => {
            if (item.type === "cancelled") {
              setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: message.content || "已停止", pending: false } : message));
              setActiveRequestId("");
              streamCleanup.current?.();
              return;
            }
            if (item.type === "done") {
              setMessages((current) => current.map((message) => {
                if (message.id === userMessage.id && typeof item.user_seq === "number") {
                  return { ...message, userSeq: item.user_seq };
                }
                if (message.id === assistantId) {
                  return {
                    ...message,
                    content: item.content || message.content,
                    pending: false,
                    userSeq: typeof item.user_seq === "number" ? item.user_seq : message.userSeq
                  };
                }
                return message;
              }));
              setActiveRequestId("");
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              return;
            }
            if (item.type === "error") {
              const message = item.content || item.message || "运行时返回错误";
              setMessages((current) => current.map((entry) => entry.id === assistantId ? {
                ...entry,
                content: message,
                pending: false
              } : entry));
              setActiveRequestId("");
              streamCleanup.current?.();
              reportChatUsage(item.usage, usageTotal(item.usage) ? "provider" : "estimated");
              void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: activeSessionId });
              return;
            }
            if ((item.type === "message_update" || item.type === "delta") && item.content) {
              setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: `${message.content}${item.content}`, pending: false } : message));
            }
          },
          onError: () => {
            setActiveRequestId("");
            setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, pending: false } : message));
            reportChatUsage(undefined, "estimated");
          }
        });
      } else if (!result.inline_reply) {
        reportChatUsage(result.usage, usageTotal(result.usage) ? "provider" : "estimated");
      }
      generateSessionTitle({ sessionId: activeSessionId, userMessage: text }).then((title) => {
        if (title) setActiveSessionTitle(title);
      }).catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "发送失败";
      setMessages((current) => current.map((item) => item.id === assistantId ? { ...item, content: message, pending: false } : item));
      void reportDesktopEvent({ type: "error", source: "Desktop", message, sessionId: activeSessionId });
    }
  }

  async function stopActiveRequest() {
    if (!activeRequestId) return;
    await cancelChatRequest({ requestId: activeRequestId, sessionId: activeSessionId });
    setActiveRequestId("");
    streamCleanup.current?.();
    setMessages((current) => current.map((message) => message.pending ? { ...message, content: message.content || "已停止", pending: false } : message));
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

  async function handleInstallPack(pack: CapabilityPack) {
    if (pack.policyMode === "disabled") {
      setApproval({ type: "error", title: "管理员已禁用安装", message: "请联系管理员预置该能力包。" });
      return;
    }
    setApproval({ type: "info", title: `正在安装 ${pack.name}`, message: "安装过程中可以继续查看当前会话，请勿关闭应用。" });
    const result = await installCapabilityPack(pack.id);
    const nextPacks = await listCapabilityPacks();
    setPacks(nextPacks);
    if (result?.installed) {
      setApproval({
        type: "info",
        title: "能力包已安装",
        message: result.message,
        actions: [{ label: "继续发送", primary: true, onClick: () => { setApproval(null); void sendNow(true); } }]
      });
    } else {
      setApproval({ type: "error", title: "安装失败", message: result?.message || "请稍后重试或联系管理员。" });
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

  async function logout() {
    await enterpriseLogout();
    setSession(null);
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

  if (!authChecked) {
    return <main className="auth-shell"><section className="auth-panel"><p>正在检查登录状态</p></section></main>;
  }

  if (!session) {
    return <AuthGate onLogin={(next) => setSession(next)} />;
  }

  return (
    <main className="app-shell">
      <aside className="session-sidebar">
        <div className="brand-row">
          <div className="brand-mark"><Sparkles aria-hidden="true" /></div>
          <div>
            <strong>EcoreX</strong>
            <span>亦芯广告 AI Agent</span>
          </div>
          <button className="icon-button" title={theme === "dark" ? "切换到明亮模式" : "切换到深色模式"} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}
          </button>
          <button className="icon-button" title="通知与运行状态" onClick={() => setNotificationsOpen((open) => !open)}>
            <Bell aria-hidden="true" />
          </button>
        </div>

        <div className="sidebar-actions">
          <button onClick={startNewSession} title="创建一段新会话"><Plus aria-hidden="true" />新对话</button>
          <label className="search-box" title="搜索会话标题和摘要">
            <Search aria-hidden="true" />
            <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="搜索会话" />
          </label>
        </div>

        {notificationsOpen && (
          <section className="popover-panel">
            <strong>运行状态</strong>
            <span>{sidecarStatus.message}</span>
            <span>{runtimeSnapshot.message}</span>
          </section>
        )}

        <div className="session-list" aria-label="会话列表">
          {visibleSessions.map((row) => (
            <button className={`session-row is-${row.status}`} key={row.id} onClick={() => void selectSession(row)} title={`${row.title}\n${row.detail}\n${formatTime(row.updatedAt)}`}>
              <FolderOpen aria-hidden="true" />
              <span><strong>{row.title}</strong><small>{row.detail}</small></span>
              <em>{formatTime(row.updatedAt)}</em>
            </button>
          ))}
        </div>

        <div className="sidebar-footer">
          <button onClick={() => setSettingsOpen(true)} title="设置、能力包、权限和诊断"><Settings aria-hidden="true" />设置</button>
          <button onClick={() => setSettingsOpen(true)} title={`${session.user.name} / ${session.user.email}`}><UserRound aria-hidden="true" />{session.user.name}</button>
        </div>
      </aside>

      <section className="chat-pane">
        <header className="chat-header">
          <div>
            <span>当前会话</span>
            <h1>{activeSessionTitle}</h1>
          </div>
          <div className="chat-status">
            <span title={runtimeSnapshot.message}><Bot aria-hidden="true" />{runtimeSnapshot.status === "ready" ? "运行时已连接" : "等待运行时"}</span>
            <span title="当前企业账号"><CheckCircle2 aria-hidden="true" />{session.user.email}</span>
          </div>
        </header>

        <div className="message-list">
          {messages.length === 0 ? (
            <div className="empty-chat">
              <Sparkles aria-hidden="true" />
              <strong>可以直接开始</strong>
              <span>粘贴图片或文件，输入需求，EcoreX 会在需要能力包或权限时先确认。</span>
            </div>
          ) : (
            messages.map((message) => (
              <article className={`message ${message.role}`} key={message.id}>
                <div className="message-body">
                  <p>{message.content || (message.pending ? "正在思考..." : "")}</p>
                  {message.attachments?.length ? (
                    <div className="message-files">
                      {message.attachments.map((file) => (
                        <button key={file.file_path} onClick={() => setPreviewFile(file)} title="点击预览文件">
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

          <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendNow(); }}>
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
            <button type="button" className="mode-button" onClick={() => setSettingsOpen(true)} title="自定义模型、权限和能力包">
              <Settings aria-hidden="true" />自定义<ChevronDown aria-hidden="true" />
            </button>
            {activeRequestId ? (
              <button type="button" className="send-button stop" onClick={stopActiveRequest} title="停止当前回复"><Square aria-hidden="true" /></button>
            ) : (
              <button type="submit" className="send-button" disabled={!composerText.trim() && attachments.length === 0} title="发送，Enter 也可以发送">
                <Upload aria-hidden="true" />
              </button>
            )}
          </form>
        </div>
      </section>

      {settingsOpen && (
        <div className="modal-backdrop" onClick={() => setSettingsOpen(false)}>
          <section className="settings-sheet" onClick={(event) => event.stopPropagation()}>
            <header><h2>设置</h2><button className="icon-button" title="关闭设置" aria-label="关闭设置" onClick={() => setSettingsOpen(false)}><X aria-hidden="true" /></button></header>
            <div className="settings-grid">
              <article><strong>账号</strong><span>{session.user.name} / {session.user.email}</span><button onClick={logout}><LogOut aria-hidden="true" />退出登录</button></article>
              <article><strong>主题</strong><span>当前为 {theme === "dark" ? "深色" : "明亮"} 模式</span><button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <SunMedium aria-hidden="true" /> : <Moon aria-hidden="true" />}切换主题</button></article>
              <article><strong>权限</strong><span>{permissionState ? `模式 ${permissionModeLabel(permissionState.mode)}，已保存 ${permissionState.grantsCount} 条授权` : "权限状态暂不可用"}</span><button onClick={() => resetPermissionGrants().then(setPermissionState)}>清空授权</button></article>
              <article><strong>Skill / MCP</strong><span>{runtimeSnapshot.skillsCount} 个 Skill，{runtimeSnapshot.toolsCount} 个工具通道</span><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
              <article><strong>模型策略</strong><span>{runtimeSnapshot.modelsCount} 个企业模型映射，{runtimeSnapshot.status === "ready" ? "已同步" : "等待同步"}</span><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>刷新策略</button></article>
              <article><strong>文件预览</strong><span>点击附件后再显示预览；系统打开前会进行权限确认。</span><button onClick={() => setPreviewFile(null)}>关闭预览</button></article>
              <article><strong>诊断</strong><span>{sidecarStatus.message}</span><button onClick={() => loadRuntimeSnapshot().then(setRuntimeSnapshot)}>重新检查</button></article>
            </div>
            <form className="password-form" onSubmit={submitPasswordChange}>
              <strong>修改登录密码</strong>
              <input
                value={passwordDraft.oldPassword}
                onChange={(event) => setPasswordDraft((current) => ({ ...current, oldPassword: event.target.value }))}
                type="password"
                placeholder="当前密码"
                autoComplete="current-password"
                required
              />
              <input
                value={passwordDraft.newPassword}
                onChange={(event) => setPasswordDraft((current) => ({ ...current, newPassword: event.target.value }))}
                type="password"
                placeholder="新密码，至少 8 位"
                autoComplete="new-password"
                minLength={8}
                required
              />
              <input
                value={passwordDraft.confirmPassword}
                onChange={(event) => setPasswordDraft((current) => ({ ...current, confirmPassword: event.target.value }))}
                type="password"
                placeholder="确认新密码"
                autoComplete="new-password"
                minLength={8}
                required
              />
              <button type="submit" disabled={passwordBusy}>{passwordBusy ? "正在保存" : "更新密码"}</button>
            </form>
            <div className="permission-modes">
              {(["smart-ask", "always-ask", "read-only", "custom"] as PermissionMode[]).map((mode) => (
                <button key={mode} className={permissionState?.mode === mode ? "is-active" : ""} onClick={() => updatePermissionMode(mode).then(setPermissionState)}>
                  {mode === "smart-ask" ? "智能确认" : mode === "always-ask" ? "每次询问" : mode === "read-only" ? "只读优先" : "自定义"}
                </button>
              ))}
            </div>
            <div className="pack-list">
              {packs.map((pack) => (
                <article key={pack.id}>
                  <div><strong>{pack.name}</strong><span>{pack.message}</span></div>
                  <button disabled={pack.installed || pack.policyMode === "disabled"} onClick={() => void handleInstallPack(pack)}>
                    {pack.installed ? "已安装" : pack.policyMode === "disabled" ? "管理员禁用" : "安装"}
                  </button>
                </article>
              ))}
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

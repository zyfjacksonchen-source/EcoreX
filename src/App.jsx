import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Box,
  Brain,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CheckCheck,
  CircleCheck,
  CircleDashed,
  Clock3,
  Code2,
  Copy,
  Database,
  Eye,
  FileText,
  Filter,
  Globe2,
  HelpCircle,
  Keyboard,
  Layers3,
  LayoutDashboard,
  Loader2,
  LogOut,
  Lock,
  Mail,
  Maximize2,
  Minus,
  MoreHorizontal,
  Network,
  Pause,
  Pencil,
  Play,
  Plus,
  Search,
  Send,
  Settings,
  ShieldCheck,
  SquareTerminal,
  Upload,
  User,
  UsersRound,
  Workflow,
  X,
  Zap
} from 'lucide-react';

const fallbackPlugins = [
  {
    name: 'agent-sdk-dev',
    category: 'development',
    description: 'Agent SDK 开发套件，用于扩展 EcoreX Agent 的业务能力',
    commands: 1,
    agents: 2,
    skills: 0,
    hooks: 0,
    available: true
  },
  {
    name: 'feature-dev',
    category: 'development',
    description: '覆盖需求拆解、架构探索、实现与复核的功能开发工作流',
    commands: 1,
    agents: 3,
    skills: 0,
    hooks: 0,
    available: true
  },
  {
    name: 'pr-review-toolkit',
    category: 'productivity',
    description: '面向测试、类型、错误处理、注释和代码质量的 PR 审查工具集',
    commands: 1,
    agents: 6,
    skills: 0,
    hooks: 0,
    available: true
  },
  {
    name: 'plugin-dev',
    category: 'development',
    description: '插件创建工具包，包含 Hook、MCP、命令、Agent 和 Skill 模板',
    commands: 1,
    agents: 3,
    skills: 7,
    hooks: 0,
    available: true
  },
  {
    name: 'security-guidance',
    category: 'security',
    description: '安全提醒 Hook，辅助识别命令注入、XSS 与高风险代码模式',
    commands: 0,
    agents: 0,
    skills: 0,
    hooks: 1,
    available: true
  },
  {
    name: 'code-review',
    category: 'productivity',
    description: '自动化代码审查工作流，可调用多个专门 Agent 交叉检查',
    commands: 1,
    agents: 0,
    skills: 0,
    hooks: 0,
    available: true
  }
];

const recentChats = [
  ['双碳园区月度碳排复盘与减排建议', '10:24'],
  ['ESG 披露数据缺口核查', '昨天'],
  ['供应链 Scope 3 排放因子校准', '昨天'],
  ['绿色电力采购收益测算', '5月20日'],
  ['碳资产项目开发路径梳理', '5月19日'],
  ['工厂能耗异常诊断', '5月18日'],
  ['客户 ESG 周报自动生成', '5月16日'],
  ['碳盘查访谈纪要整理', '5月15日']
];

const quickActions = [
  ['碳排数据分析', '诊断排放强度、能耗结构与异常波动', BarChart3],
  ['生成 ESG 周报', '自动输出披露进展、风险与行动项', FileText],
  ['核查排放因子', '比对行业口径、年度版本与数据来源', BookOpen],
  ['创建减排任务', '拆解责任人、截止时间与验收标准', Activity]
];

const abilityCards = [
  ['碳排分析', BarChart3],
  ['ESG 报告', FileText],
  ['任务管理', Check],
  ['风险预警', Zap],
  ['合规核查', ShieldCheck],
  ['知识问答', BookOpen]
];

const mcpServices = [
  {
    name: '碳核算知识库',
    url: 'https://mcp.ecorex.com/carbon-knowledge',
    tags: ['排放因子', '核算 SOP', 'RAG'],
    auth: 'API Key',
    authState: '已授权',
    status: '在线',
    ping: '42ms',
    sync: '2分钟前',
    permissions: '读写',
    icon: FileText,
    tone: 'orange'
  },
  {
    name: 'ESG 数据连接器',
    url: 'https://mcp.ecorex.com/esg-data',
    tags: ['披露指标', '问卷', '同步'],
    auth: 'OAuth 2.0',
    authState: '需授权',
    status: '需授权',
    ping: '待授权',
    sync: '1小时前',
    permissions: '只读',
    icon: User,
    tone: 'blue'
  },
  {
    name: '能耗数据仓库',
    url: 'https://mcp.ecorex.com/energy-warehouse',
    tags: ['电表数据', 'SQL', '报表'],
    auth: 'API Key',
    authState: '已授权',
    status: '在线',
    ping: '67ms',
    sync: '49分钟前',
    permissions: '读写',
    icon: Database,
    tone: 'purple'
  },
  {
    name: '项目协同系统',
    url: 'https://mcp.ecorex.com/carbon-task',
    tags: ['减排项目', '运营', '自动化'],
    auth: 'Bearer Token',
    authState: '需授权',
    status: '离线',
    ping: '-',
    sync: '1小时前',
    permissions: '读写',
    icon: Workflow,
    tone: 'orange'
  },
  {
    name: '票据与凭证解析器',
    url: 'https://mcp.ecorex.com/voucher-parser',
    tags: ['OCR', '发票', '凭证'],
    auth: 'API Key',
    authState: '已授权',
    status: '在线',
    ping: '31ms',
    sync: '55分钟前',
    permissions: '读写',
    icon: FileText,
    tone: 'teal'
  }
];

const initialTimeline = [
  ['已读取 cli.js.map 后端源码索引', '已完成', '10:24:01', 'success'],
  ['注入 EcoreX 亦芯 AI Agent 身份', '已完成', '10:24:02', 'success'],
  ['加载 Claude Code CLI 能力', '已完成', '10:24:03', 'success'],
  ['准备碳排与 ESG 数据分析上下文', '进行中', '10:24:07', 'running'],
  ['等待用户确认下一步任务', '待确认', '--', 'pending']
];

const messageStates = {
  sending: { label: '发送中', icon: CircleDashed, tone: 'sending' },
  sent: { label: '已发送', icon: CircleCheck, tone: 'sent' },
  read: { label: '已读', icon: CheckCheck, tone: 'read' },
  thinking: { label: 'AI 思考中', icon: Brain, tone: 'thinking' },
  generating: { label: 'AI 正在生成', icon: Loader2, tone: 'generating' },
  complete: { label: '已完成', icon: Check, tone: 'complete' },
  cancelled: { label: '已取消', icon: Pause, tone: 'cancelled' },
  error: { label: '错误 / 重试', icon: AlertTriangle, tone: 'error' }
};

function Logo({ compact = false }) {
  const iconSrc = `${import.meta.env.BASE_URL}icon.png`;

  return (
    <div className={`brand ${compact ? 'brand-compact' : ''}`}>
      <img className="brand-icon" alt="" src={iconSrc} />
      {!compact && <span className="brand-name">EcoreX</span>}
    </div>
  );
}

function App() {
  const [loggedIn, setLoggedIn] = useState(() => localStorage.getItem('ecorex-session') === '1');
  const [page, setPage] = useState('chat');
  const [backendStatus, setBackendStatus] = useState(null);
  const [capabilities, setCapabilities] = useState(null);
  const [backendError, setBackendError] = useState('');

  useEffect(() => {
    document.documentElement.dataset.theme = 'dark';
    localStorage.setItem('ecorex-theme', 'dark');
  }, []);

  useEffect(() => {
    refreshBackend();
  }, []);

  async function refreshBackend() {
    if (!window.ecorex) return;
    try {
      setBackendError('');
      const [status, caps] = await Promise.all([
        window.ecorex.getBackendStatus(),
        window.ecorex.getCapabilities()
      ]);
      setBackendStatus(status);
      setCapabilities(caps);
    } catch (error) {
      setBackendError(error?.message || 'Backend bridge failed');
      setBackendStatus((current) => current || { ok: false });
      setCapabilities((current) => current || null);
    }
  }

  function handleLogin() {
    localStorage.setItem('ecorex-session', '1');
    setLoggedIn(true);
    refreshBackend();
  }

  if (!loggedIn) {
    return (
      <AppFrame>
        <LoginPage
          backendStatus={backendStatus}
          onLogin={handleLogin}
          onOpenAuth={() => window.ecorex?.openAuth()}
        />
      </AppFrame>
    );
  }

  return (
    <AppFrame>
      <MainShell
        page={page}
        setPage={setPage}
        backendStatus={backendStatus}
        capabilities={capabilities}
        refreshBackend={refreshBackend}
        logout={() => {
          localStorage.removeItem('ecorex-session');
          setLoggedIn(false);
        }}
      />
    </AppFrame>
  );
}

function AppFrame({ children }) {
  return (
    <div className="app-frame">
      <AppTitleBar />
      <div className="app-content">{children}</div>
    </div>
  );
}

function AppTitleBar() {
  const platform = window.ecorex?.platform || (navigator.userAgent.includes('Mac') ? 'darwin' : 'win32');
  const isMac = platform === 'darwin';
  const control = (action) => window.ecorex?.windowControl(action);

  return (
    <header className={`app-titlebar ${isMac ? 'is-mac' : 'is-win'}`} onDoubleClick={() => control('maximize')}>
      {isMac && (
        <div className="mac-window-controls">
          <button className="close" type="button" aria-label="关闭" onClick={() => control('close')} />
          <button className="minimize" type="button" aria-label="最小化" onClick={() => control('minimize')} />
          <button className="maximize" type="button" aria-label="最大化" onClick={() => control('maximize')} />
        </div>
      )}
      <div className="titlebar-title">EcoreX Agent</div>
      {!isMac && (
        <div className="win-window-controls">
          <button type="button" aria-label="最小化" onClick={() => control('minimize')}><Minus size={15} /></button>
          <button type="button" aria-label="最大化" onClick={() => control('maximize')}><Maximize2 size={14} /></button>
          <button className="close" type="button" aria-label="关闭" onClick={() => control('close')}><X size={16} /></button>
        </div>
      )}
    </header>
  );
}

function LoginPage({ onLogin, onOpenAuth }) {
  const [loginType, setLoginType] = useState('password');
  const [showPassword, setShowPassword] = useState(false);
  const loginImage = `${import.meta.env.BASE_URL}ui/login-dark.png`;
  const iconSrc = `${import.meta.env.BASE_URL}icon.png`;

  return (
    <div className="login-screen login-dark">
      <div className="login-art">
        <img alt="" src={loginImage} />
      </div>
      <div className="login-brand-overlay">
        <img alt="" src={iconSrc} />
      </div>
      <form
        className="login-panel"
        onSubmit={(event) => {
          event.preventDefault();
          onLogin();
        }}
      >
        <div className="login-tabs">
          <button
            className={loginType === 'password' ? 'active' : ''}
            type="button"
            onClick={() => setLoginType('password')}
          >
            密码登录
          </button>
          <button
            className={loginType === 'code' ? 'active' : ''}
            type="button"
            onClick={() => setLoginType('code')}
          >
            验证码登录
          </button>
        </div>

        <label className="field-label">企业邮箱</label>
        <div className="input-shell">
          <Mail size={20} />
          <input placeholder="请输入企业邮箱" type="email" />
        </div>

        <label className="field-label">{loginType === 'password' ? '密码' : '验证码'}</label>
        <div className="input-shell">
          <Lock size={20} />
          <input
            placeholder={loginType === 'password' ? '请输入密码' : '请输入验证码'}
            type={showPassword ? 'text' : 'password'}
          />
          <button type="button" onClick={() => setShowPassword((value) => !value)}>
            <Eye size={20} />
          </button>
        </div>

        <div className="login-meta">
          <label>
            <input type="checkbox" defaultChecked />
            <span>记住我</span>
          </label>
          <button type="button">忘记密码?</button>
        </div>

        <button className="primary wide" type="submit">
          登录
        </button>

        <div className="divider">
          <span />
          <em>{loginType === 'password' ? '或' : '其他登录方式'}</em>
          <span />
        </div>

        <button className="sso" type="button" onClick={onOpenAuth}>
          <ShieldCheck size={20} />
          SSO 登录 / 企业统一身份认证
        </button>

        <p className="login-help">
          还没有账号?<button type="button">请联系管理员</button>
        </p>

        <p className="terms">
          登录即表示您同意 <b>《服务协议》</b> 与 <b>《隐私政策》</b>
        </p>
      </form>
    </div>
  );
}

function MainShell({
  page,
  setPage,
  backendStatus,
  capabilities,
  refreshBackend,
  logout
}) {
  return (
    <div className="app-shell">
      <Sidebar page={page} setPage={setPage} logout={logout} />
      <main className={`workspace workspace-${page}`}>
        {page === 'chat' && (
          <ChatView
            backendStatus={backendStatus}
            capabilities={capabilities}
            refreshBackend={refreshBackend}
            setPage={setPage}
          />
        )}
        {page === 'skills' && (
          <SkillsView
            capabilities={capabilities}
            setPage={setPage}
          />
        )}
        {page === 'mcp' && (
          <McpView
            backendStatus={backendStatus}
            setPage={setPage}
          />
        )}
      </main>
    </div>
  );
}

function Sidebar({ page, setPage, logout }) {
  const [profileOpen, setProfileOpen] = useState(false);

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <Logo />
        <button className="icon-button small" type="button">
          <ChevronLeft size={20} />
        </button>
      </div>
      <button className="new-chat" type="button" onClick={() => setPage('chat')}>
        <Plus size={22} />
        新会话
      </button>
      <nav className="side-nav">
        <button className={page === 'mcp' ? 'active' : ''} type="button" onClick={() => setPage('mcp')}>
          <Box size={25} />
          MCP 管理
        </button>
        <button className={page === 'skills' ? 'active' : ''} type="button" onClick={() => setPage('skills')}>
          <Layers3 size={25} />
          Skill 管理
        </button>
      </nav>
      <div className="recent">
        <h3>最近对话</h3>
        {recentChats.map(([title, time], index) => (
          <button className={index === 0 ? 'active' : ''} key={title} type="button" onClick={() => setPage('chat')}>
            <Bot size={16} />
            <span>{title}</span>
            <em>{time}</em>
          </button>
        ))}
      </div>
      <div className="user-card">
        <button className="profile-trigger" type="button" onClick={() => setProfileOpen((value) => !value)}>
          <div className="avatar avatar-photo">张</div>
          <div>
            <strong>张晓明</strong>
            <span>zhang.xm@ecorex.com</span>
            <em className="profile-mini-status"><i />个人资料 · 在线</em>
          </div>
          <ChevronDown size={18} />
        </button>
        {profileOpen && (
          <div className="profile-popover">
            <header>
              <div className="avatar avatar-photo">张</div>
              <div>
                <strong>张晓明</strong>
                <span>zhang.xm@ecorex.com</span>
                <em><i />在线</em>
              </div>
            </header>
            <button className="profile-team" type="button">
              <UsersRound size={24} />
              <span>
                <strong>智能创新团队 / 研发一部</strong>
                <em>团队人数 23 人</em>
              </span>
              <ChevronRight size={18} />
            </button>
            <div className="profile-menu-grid">
              <button type="button"><User size={20} />个人资料</button>
              <button type="button"><Settings size={20} />偏好设置</button>
              <button type="button"><HelpCircle size={20} />帮助中心</button>
              <button type="button"><Keyboard size={20} />快捷键</button>
            </div>
            <button className="profile-logout" type="button" onClick={logout}>
              <LogOut size={20} />
              退出登录
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}

function formatAgentEventTime(event) {
  if (event?.time) {
    try {
      return new Date(event.time).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
    } catch {
      return event.time;
    }
  }
  return new Date().toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

function timelineItemFromAgentEvent(event) {
  const statusMap = {
    status: event.status || '进行中',
    tool: event.status || '工具调用',
    stderr: '日志',
    assistant: '生成中',
    result: '生成结果',
    done: '已完成',
    cancelled: '已取消',
    error: '失败'
  };
  const toneMap = {
    status: 'running',
    tool: 'running',
    stderr: 'warn',
    assistant: 'running',
    result: 'success',
    done: 'success',
    cancelled: 'pending',
    error: 'danger'
  };

  return [
    event.text || event.status || event.kind || 'Agent 事件',
    statusMap[event.kind] || event.status || '进行中',
    formatAgentEventTime(event),
    toneMap[event.kind] || 'running'
  ];
}

function appendTimeline(timeline = [], item, limit = 80) {
  return [...timeline, item].slice(-limit);
}

function mergeAssistantText(existing = '', incoming = '') {
  if (!incoming) return existing;
  if (!existing) return incoming;
  if (incoming.startsWith(existing)) return incoming;
  if (existing.endsWith(incoming)) return existing;
  return `${existing}${incoming}`;
}

function ChatView({ backendStatus, capabilities, refreshBackend, setPage }) {
  const [prompt, setPrompt] = useState('');
  const [running, setRunning] = useState(false);
  const [permissionMode, setPermissionMode] = useState('acceptEdits');
  const [model, setModel] = useState('sonnet');
  const [railExpanded, setRailExpanded] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [messages, setMessages] = useState(() => [
    {
      id: 'user-demo',
      role: 'user',
      text: '请帮我分析 5 月园区碳排放与 ESG 披露数据，找出排放强度波动原因，并给出下一轮减排任务建议。',
      status: 'read',
      time: '10:24'
    },
    {
      id: 'assistant-demo',
      role: 'assistant',
      text: '好的，已为您整理园区碳排与 ESG 数据的关键洞察，并将可执行减排动作拆解为任务清单。',
      rich: true,
      status: 'complete',
      time: '10:24'
    }
  ]);
  const [timeline, setTimeline] = useState(initialTimeline);
  const sessionMap = useRef(new Map());
  const runningRef = useRef(false);
  const currentSessionIdRef = useRef(null);
  const eventQueueRef = useRef([]);
  const flushTimerRef = useRef(null);
  const pendingCancelsRef = useRef(new Set());
  const statusTimers = useRef([]);

  const selectedPlugins = useMemo(() => {
    const plugins = capabilities?.plugins?.length ? capabilities.plugins : fallbackPlugins;
    return plugins
      .filter((plugin) => ['feature-dev', 'code-review', 'security-guidance', 'plugin-dev'].includes(plugin.name))
      .map((plugin) => plugin.name);
  }, [capabilities]);

  const permissionOptions = useMemo(() => {
    const modes = capabilities?.permissionModes;
    if (Array.isArray(modes) && modes.length) {
      return modes.map((item) => [item.value, item.label]);
    }
    return [
      ['acceptEdits', 'Accept'],
      ['auto', 'Auto'],
      ['plan', 'Plan'],
      ['default', 'Default']
    ];
  }, [capabilities]);

  const modelOptions = useMemo(() => {
    const models = capabilities?.models;
    if (Array.isArray(models) && models.length) {
      return models.map((item) => [item.value, item.label]);
    }
    return [
      ['sonnet', 'Sonnet'],
      ['opus', 'Opus']
    ];
  }, [capabilities]);

  function finishSession(sessionId) {
    if (currentSessionIdRef.current === sessionId) {
      currentSessionIdRef.current = null;
      runningRef.current = false;
      setCurrentSessionId(null);
      setRunning(false);
    }
    sessionMap.current.delete(sessionId);
  }

  function applyAgentEvents(events) {
    const now = Date.now();
    const relevantEvents = [];
    const pendingEvents = [];

    for (const event of events) {
      if (sessionMap.current.has(event.sessionId)) {
        relevantEvents.push(event);
      } else if (now - (event.__queuedAt || now) < 5000) {
        pendingEvents.push({ ...event, __queuedAt: event.__queuedAt || now });
      }
    }

    if (pendingEvents.length) {
      eventQueueRef.current = [...pendingEvents, ...eventQueueRef.current].slice(-200);
    }

    if (!relevantEvents.length) return pendingEvents.length > 0;

    setMessages((items) => {
      let nextItems = items;
      for (const event of relevantEvents) {
        const messageId = sessionMap.current.get(event.sessionId);
        const timelineItem = timelineItemFromAgentEvent(event);
        nextItems = nextItems.map((item) => {
          if (item.id !== messageId) return item;

          const withTimeline = {
            ...item,
            timeline: appendTimeline(item.timeline || [], timelineItem)
          };

          if (['status', 'tool', 'stderr', 'debug'].includes(event.kind)) {
            return {
              ...withTimeline,
              status: item.status === 'generating' ? 'generating' : 'thinking',
              backendStatus: event.status || event.kind
            };
          }

          if (event.kind === 'assistant') {
            return {
              ...withTimeline,
              text: mergeAssistantText(item.text, event.text),
              streaming: true,
              status: 'generating'
            };
          }

          if (event.kind === 'result') {
            return {
              ...withTimeline,
              text: event.text || item.text,
              streaming: false,
              status: 'complete',
              meta: event.costUsd
                ? `成本 $${Number(event.costUsd).toFixed(4)} · ${Math.round((event.durationMs || 0) / 1000)} 秒`
                : item.meta || ''
            };
          }

          if (event.kind === 'done') {
            return {
              ...withTimeline,
              streaming: false,
              status: 'complete',
              error: false,
              text: item.text || event.text
            };
          }

          if (event.kind === 'cancelled') {
            return {
              ...withTimeline,
              streaming: false,
              status: 'cancelled',
              error: false,
              text: item.text || event.text || '当前任务已取消。'
            };
          }

          if (event.kind === 'error') {
            return {
              ...withTimeline,
              streaming: false,
              status: 'error',
              error: true,
              text: item.text || event.text || 'Agent 执行失败。'
            };
          }

          return withTimeline;
        });
      }
      return nextItems;
    });

    setTimeline((items) => [
      ...items,
      ...relevantEvents.map((event) => timelineItemFromAgentEvent(event))
    ].slice(-80));

    relevantEvents
      .filter((event) => ['done', 'error', 'cancelled'].includes(event.kind))
      .forEach((event) => finishSession(event.sessionId));

    return pendingEvents.length > 0;
  }

  function flushAgentEvents() {
    flushTimerRef.current = null;
    const events = eventQueueRef.current.splice(0);
    const hasPending = applyAgentEvents(events);
    if (hasPending && !flushTimerRef.current) {
      flushTimerRef.current = setTimeout(flushAgentEvents, 120);
    }
  }

  function queueAgentEvent(event) {
    eventQueueRef.current.push({ ...event, __queuedAt: event.__queuedAt || Date.now() });
    eventQueueRef.current = eventQueueRef.current.slice(-200);
    if (!flushTimerRef.current) {
      flushTimerRef.current = setTimeout(flushAgentEvents, 60);
    }
  }

  useEffect(() => {
    if (!window.ecorex) return undefined;
    return window.ecorex.onAgentEvent((event) => {
      queueAgentEvent(event);
    });
  }, []);

  useEffect(() => () => {
    statusTimers.current.forEach((timer) => clearTimeout(timer));
    clearTimeout(flushTimerRef.current);
  }, []);

  function scheduleMessageStatus(id, status, delay) {
    const timer = setTimeout(() => {
      setMessages((items) =>
        items.map((item) => (item.id === id ? { ...item, status } : item))
      );
    }, delay);
    statusTimers.current.push(timer);
  }

  async function sendPrompt(text = prompt) {
    const cleanPrompt = String(text || '').trim();
    if (!cleanPrompt || runningRef.current) return;

    runningRef.current = true;
    const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const userId = `user-${Date.now()}`;
    const assistantId = `assistant-${Date.now()}`;
    const requestedSessionId = window.crypto?.randomUUID?.() || `session-${Date.now()}`;
    const submitted = ['提交用户任务到 Claude Code CLI', '进行中', now, 'running'];
    sessionMap.current.set(requestedSessionId, assistantId);
    currentSessionIdRef.current = requestedSessionId;

    setMessages((items) => [
      ...items,
      { id: userId, role: 'user', text: cleanPrompt, time: now, status: 'sending' },
      {
        id: assistantId,
        role: 'assistant',
        text: '',
        time: now,
        streaming: true,
        status: 'thinking',
        sessionId: requestedSessionId,
        originalPrompt: cleanPrompt,
        timeline: [submitted]
      }
    ]);
    setPrompt('');
    setRunning(true);
    setCurrentSessionId(requestedSessionId);
    setTimeline((items) => appendTimeline(items, submitted));
    scheduleMessageStatus(userId, 'sent', 280);
    scheduleMessageStatus(userId, 'read', 760);

    if (!window.ecorex) {
      scheduleMessageStatus(assistantId, 'generating', 420);
      const timer = setTimeout(() => {
        setMessages((items) =>
          items.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                  streaming: false,
                  status: 'complete',
                  text: '当前运行在浏览器预览模式，Electron 后端桥接不可用。请使用 npm run dev 启动桌面端。'
                }
              : item
          )
        );
        runningRef.current = false;
        currentSessionIdRef.current = null;
        sessionMap.current.delete(requestedSessionId);
        setRunning(false);
        setCurrentSessionId(null);
      }, 800);
      statusTimers.current.push(timer);
      return;
    }

    try {
      const result = await window.ecorex.runPrompt({
        sessionId: requestedSessionId,
        prompt: cleanPrompt,
        permissionMode,
        model,
        plugins: selectedPlugins
      });

      if (!result.ok) {
        throw new Error(result.error || 'Agent 后端启动失败。');
      }

      const sessionId = result.sessionId || requestedSessionId;
      if (sessionId !== requestedSessionId) {
        sessionMap.current.delete(requestedSessionId);
        sessionMap.current.set(sessionId, assistantId);
      }
      currentSessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);
      setMessages((items) =>
        items.map((item) => (item.id === assistantId ? { ...item, sessionId } : item))
      );
      if (pendingCancelsRef.current.has(requestedSessionId) || pendingCancelsRef.current.has(sessionId)) {
        pendingCancelsRef.current.delete(requestedSessionId);
        pendingCancelsRef.current.delete(sessionId);
        await window.ecorex.stopPrompt(sessionId);
        return;
      }
      if (result.initialEvent) queueAgentEvent(result.initialEvent);
    } catch (error) {
      if (pendingCancelsRef.current.has(requestedSessionId)) {
        pendingCancelsRef.current.delete(requestedSessionId);
        finishSession(requestedSessionId);
        return;
      }
      runningRef.current = false;
      currentSessionIdRef.current = null;
      sessionMap.current.delete(requestedSessionId);
      setCurrentSessionId(null);
      setRunning(false);
      setMessages((items) =>
        items.map((item) =>
          item.id === assistantId
            ? {
                ...item,
                streaming: false,
                error: true,
                status: 'error',
                text: error?.message || 'Agent 后端启动失败。'
              }
            : item
        )
      );
    }
  }

  async function cancelPrompt() {
    const sessionId = currentSessionIdRef.current;
    if (!sessionId) return;
    pendingCancelsRef.current.add(sessionId);
    const messageId = sessionMap.current.get(sessionId);
    setMessages((items) =>
      items.map((item) =>
        item.id === messageId
          ? {
              ...item,
              streaming: false,
              status: 'cancelled',
              text: item.text || '当前任务已取消。'
            }
          : item
      )
    );
    setTimeline((items) => appendTimeline(items, ['用户取消当前 Agent 任务', '已取消', formatAgentEventTime(), 'pending']));
    runningRef.current = false;
    setRunning(false);
    if (window.ecorex?.stopPrompt) {
      try {
        await window.ecorex.stopPrompt(sessionId);
      } catch {
        // The backend may still be starting; sendPrompt will stop it after the session id is acknowledged.
      }
    }
  }

  function retryMessage(message) {
    if (message?.originalPrompt) sendPrompt(message.originalPrompt);
  }

  return (
    <div className={`chat-layout ${railExpanded ? 'rail-expanded' : 'rail-collapsed'}`}>
      <section className="chat-main panel">
        <HeaderBar
          title="EcoreX"
          badge="亦芯 AI Agent"
          subtitle="面向碳排放、ESG 披露、能耗管理与减排项目协同的自主思考型 AI Agent"
          backendStatus={backendStatus}
          onRefresh={refreshBackend}
        />

        <div className="messages">
          {messages.map((message) => (
            <ChatMessage
              key={message.id}
              message={message}
              timeline={message.timeline || timeline}
              sourceMap={backendStatus?.sourceMap}
              showTrace={message.rich || message.streaming || Boolean(message.timeline?.length)}
              onRetry={retryMessage}
            />
          ))}
          <StatusTree
            compact
            timeline={timeline}
            sourceMap={backendStatus?.sourceMap}
            title="当前任务状态树"
          />
        </div>

        <Composer
          prompt={prompt}
          setPrompt={setPrompt}
          running={running}
          currentSessionId={currentSessionId}
          sendPrompt={sendPrompt}
          cancelPrompt={cancelPrompt}
          permissionMode={permissionMode}
          setPermissionMode={setPermissionMode}
          model={model}
          setModel={setModel}
          permissionOptions={permissionOptions}
          modelOptions={modelOptions}
        />
      </section>

      <aside className={`right-rail ${railExpanded ? 'expanded' : 'collapsed'}`}>
        <ProjectCard
          backendStatus={backendStatus}
          expanded={railExpanded}
          onToggle={() => setRailExpanded((next) => !next)}
        />
        {railExpanded && (
          <>
            <TaskOverview timeline={timeline} />
            <QuickActions onRun={sendPrompt} />
            <AbilityGrid setPage={setPage} />
          </>
        )}
      </aside>
    </div>
  );
}

function HeaderBar({ title, badge, subtitle, backendStatus, onRefresh }) {
  return (
    <header className="view-header">
      <div>
        <h1>
          {title} {badge && <span>{badge}</span>}
        </h1>
        <p>{subtitle}</p>
      </div>
      <div className="header-actions">
        <button className="icon-button" type="button" onClick={onRefresh}>
          <Bell size={20} />
          <span className={backendStatus?.auth?.loggedIn ? 'status-dot ok' : 'status-dot warn'} />
        </button>
        <button className="icon-button" type="button">
          <MoreHorizontal size={22} />
        </button>
      </div>
    </header>
  );
}

function ChatMessage({ message, timeline, sourceMap, showTrace = false, onRetry }) {
  if (message.role === 'user') {
    return (
      <div className="user-row">
        <div className="user-bubble">
          <span>{message.text}</span>
          <MessageStatus status={message.status || 'read'} time={message.time} compact />
        </div>
        <div className="avatar user-avatar">张</div>
      </div>
    );
  }

  return (
    <div className={`assistant-row ${message.error ? 'error' : ''}`}>
      <Logo compact />
      <div className="assistant-card">
        <div className="assistant-head">
          <span className="time">{message.time}</span>
          <MessageStatus status={message.status || (message.streaming ? 'thinking' : 'complete')} compact />
        </div>
        <p>{message.text || (message.streaming ? '正在连接 Claude Code 后端...' : '')}</p>
        {showTrace && <InlineAgentTrace timeline={timeline} sourceMap={sourceMap} />}
        {message.rich && <CarbonPerformanceReport />}
        {message.streaming && <ThinkingIndicator phase={message.status} />}
        {message.meta && <div className="message-meta">{message.meta}</div>}
        {!message.streaming && (
          <div className="message-actions">
            {message.error && (
              <button type="button" onClick={() => onRetry?.(message)}>
                <Loader2 size={16} />
                重试
              </button>
            )}
            <button type="button">
              <Copy size={16} />
              复制结果
            </button>
            <button type="button">
              <FileText size={16} />
              生成报告
            </button>
            <button type="button">
              <Workflow size={16} />
              创建任务
            </button>
            <button className="icon-only" type="button">
              <Check size={16} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function MessageStatus({ status = 'complete', time, compact = false }) {
  const state = messageStates[status] || messageStates.complete;
  const Icon = state.icon;
  return (
    <span className={`message-status ${state.tone} ${compact ? 'compact' : ''}`}>
      <Icon size={compact ? 14 : 16} />
      <span>{state.label}</span>
      {time && <em>{time}</em>}
    </span>
  );
}

function ThinkingIndicator({ phase = 'thinking' }) {
  const label = phase === 'generating' ? 'AI 正在生成' : 'AI 思考中';
  return (
    <div className={`thinking-indicator ${phase === 'generating' ? 'generating' : 'thinking'}`}>
      <span className="thinking-orbit"><i /><i /><i /></span>
      <strong>{label}</strong>
      <span className="thinking-wave"><i /><i /><i /></span>
    </div>
  );
}

function AgentRuntimePanel() {
  const states = ['sending', 'sent', 'read', 'thinking', 'generating', 'error'];
  return (
    <div className="agent-runtime-panel">
      <ThinkingIndicator phase="thinking" />
      <div className="runtime-status-grid">
        {states.map((state) => (
          <MessageStatus key={state} status={state} compact />
        ))}
      </div>
      <div className="permission-mini-card">
        <ShieldCheck size={22} />
        <div>
          <strong>授权确认</strong>
          <span>运行本地项目分析命令</span>
        </div>
        <button type="button">允许一次</button>
      </div>
    </div>
  );
}

function CarbonPerformanceReport() {
  return (
    <div className="ad-performance-report">
      <div className="insights">
        <h4>关键洞察</h4>
        <ul>
          <li>2024 年总碳排放量为 23,541 tCO2e，较 2023 年下降 8.7%。</li>
          <li>排放主要来自电力 62%、天然气 21% 和原材料 15%。</li>
          <li>4 季度电力排放环比上升 6%，主要受产能提升影响。</li>
        </ul>
      </div>
      <div className="chart-card">
        <h4>排放结构（按来源）</h4>
        <div className="chart-wrap">
          <div className="donut">
            <strong>23,541</strong>
            <span>tCO2e</span>
          </div>
          <div className="legend">
            <span><i className="c1" />电力 62%</span>
            <span><i className="c2" />天然气 21%</span>
            <span><i className="c3" />原材料 15%</span>
            <span><i className="c4" />其他 2%</span>
          </div>
        </div>
      </div>
      <div className="report-bottom">
        <div className="suggestions">
          {[
            ['优化电力结构', '增加绿电采购比例至 40%，预计年减排 3,200 tCO2e。', Zap, '高'],
            ['提升能效管理', '升级高能耗设备并优化运行策略，预计年减排 1,850 tCO2e。', Activity, '中'],
            ['原材料低碳替代', '优先选择低碳替代材料，预计年减排 1,120 tCO2e。', Box, '中']
          ].map(([title, desc, Icon, level]) => (
            <div className="suggestion" key={title}>
              <span><Icon size={22} /></span>
              <div>
                <strong>{title}</strong>
                <p>{desc}</p>
              </div>
              <em>优先级：{level}</em>
            </div>
          ))}
        </div>
        <AgentRuntimePanel />
      </div>
    </div>
  );
}

function Composer({
  prompt,
  setPrompt,
  running,
  currentSessionId,
  sendPrompt,
  cancelPrompt,
  permissionMode,
  setPermissionMode,
  model,
  setModel,
  permissionOptions,
  modelOptions
}) {
  const textareaRef = useRef(null);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const minHeight = 28;
    const maxHeight = 86;
    textarea.style.height = `${minHeight}px`;
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, minHeight), maxHeight);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }, [prompt]);

  return (
    <div className="composer">
      <textarea
        ref={textareaRef}
        value={prompt}
        onChange={(event) => setPrompt(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !running) sendPrompt();
        }}
        placeholder="输入碳排、ESG、能耗或项目协同问题，也可以使用 / 调用智能体能力..."
      />
      <div className="composer-bottom">
        <div className="tool-row">
          <button type="button">
            <Upload size={18} />
            上传文件
          </button>
          <button
            type="button"
            onClick={() => setPrompt('请分析这份碳排放与能耗数据，输出关键洞察、风险点和可执行减排任务。')}
          >
            <BarChart3 size={18} />
            数据分析
          </button>
          <button
            type="button"
            onClick={() => setPrompt('请把当前减排方案拆解成可跟踪的项目任务，并给出负责人、截止时间和验收标准。')}
          >
            <FileText size={18} />
            创建任务
          </button>
          <button
            type="button"
            onClick={() => setPrompt('请检索最新 ESG 披露规则、碳核算口径与行业案例，并总结对当前项目的影响。')}
          >
            <Globe2 size={18} />
            联网检索
          </button>
        </div>
        <div className="composer-controls">
          <AgentSelect
            value={permissionMode}
            onChange={setPermissionMode}
            options={permissionOptions}
          />
          <AgentSelect
            value={model}
            onChange={setModel}
            options={modelOptions}
          />
          <button
            className="send"
            title={running ? '取消当前任务' : '发送'}
            type="button"
            onClick={() => (running && currentSessionId ? cancelPrompt() : sendPrompt())}
          >
            {running ? <Pause size={22} /> : <Send size={24} />}
          </button>
        </div>
      </div>
    </div>
  );
}

function AgentSelect({ value, onChange, options }) {
  const [open, setOpen] = useState(false);
  const selected = options.find(([optionValue]) => optionValue === value) || options[0];

  return (
    <div
      className={`agent-select ${open ? 'open' : ''}`}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <button type="button" onClick={() => setOpen((next) => !next)}>
        <span>{selected[1]}</span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="agent-select-menu">
          {options.map(([optionValue, label]) => (
            <button
              className={optionValue === value ? 'active' : ''}
              key={optionValue}
              type="button"
              onClick={() => {
                onChange(optionValue);
                setOpen(false);
              }}
            >
              <Bot size={15} />
              <span>{label}</span>
              {optionValue === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ProjectCard({ backendStatus, expanded = true, onToggle }) {
  const map = backendStatus?.sourceMap;
  if (!expanded) {
    return (
      <section className="rail-card project-card project-card-collapsed">
        <button className="project-collapse-button" type="button" onClick={onToggle} title="展开当前项目">
          <LayoutDashboard size={22} />
          <span>项目</span>
          <ChevronLeft size={14} />
        </button>
      </section>
    );
  }

  return (
    <section className="rail-card project-card">
      <header>
        <h3>当前项目</h3>
        <button type="button" onClick={onToggle}>收起 <ChevronRight size={14} /></button>
      </header>
      <div className="project-body">
        <div className="project-icon"><LayoutDashboard size={30} /></div>
        <div>
          <strong>华东园区双碳与 ESG 披露项目</strong>
          <span>Agent 身份：EcoreX 亦芯 AI Agent</span>
        </div>
      </div>
      <div className="progress-row">
        <span>进度 68%</span>
        <div><i /></div>
      </div>
      {map?.available && <small>源码索引 {map.sizeMb}MB · {map.sourceCount || '多'} 模块</small>}
    </section>
  );
}

function TaskOverview({ timeline = [] }) {
  const runningCount = timeline.filter((item) => item[3] === 'running').length || 0;
  const doneCount = timeline.filter((item) => item[3] === 'success').length || 0;
  const waitingCount = timeline.filter((item) => item[3] === 'pending').length || 0;
  const errorCount = timeline.filter((item) => item[3] === 'danger' || item[3] === 'warn').length || 0;

  return (
    <section className="rail-card">
      <header>
        <h3>任务概览</h3>
        <button type="button">查看全部 <ChevronRight size={14} /></button>
      </header>
      <div className="task-grid">
        {[
          ['进行中', String(runningCount), Upload, 'orange'],
          ['已完成', String(doneCount), Check, 'green'],
          ['待确认', String(waitingCount), Clock3, 'amber'],
          ['异常', String(errorCount), Bell, 'red']
        ].map(([label, value, Icon, tone]) => (
          <div key={label} className={`task-tile ${tone}`}>
            <Icon size={23} />
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      <TaskChecklistTree timeline={timeline} />
    </section>
  );
}

function TaskChecklistTree({ timeline = [] }) {
  const visible = timeline.slice(-3);
  return (
    <div className="task-checklist-tree">
      {(visible.length ? visible : [
        ['读取数据', '已完成', 'success'],
        ['分析异常', '进行中', 'running'],
        ['生成报告', '待开始', 'muted']
      ]).map((item) => {
        const title = item[0];
        const status = item[1];
        const tone = item[3] || item[2] || 'muted';
        return (
          <div className={`task-branch ${tone}`} key={title}>
            <span />
            <strong>{title}</strong>
            <em>{status}</em>
          </div>
        );
      })}
    </div>
  );
}

function QuickActions({ onRun }) {
  return (
    <section className="rail-card">
      <header>
        <h3>快捷操作</h3>
      </header>
      <div className="quick-list">
        {quickActions.map(([title, desc, Icon]) => (
          <button key={title} type="button" onClick={() => onRun(`请执行：${title}。要求：${desc}，并输出可执行结果。`)}>
            <span><Icon size={22} /></span>
            <div>
              <strong>{title}</strong>
              <em>{desc}</em>
            </div>
            <ChevronRight size={18} />
          </button>
        ))}
      </div>
    </section>
  );
}

function AbilityGrid({ setPage }) {
  return (
    <section className="rail-card">
      <header>
        <h3>智能体能力</h3>
        <button type="button" onClick={() => setPage('skills')}>更多能力 <ChevronRight size={14} /></button>
      </header>
      <div className="ability-grid">
        {abilityCards.map(([label, Icon]) => (
          <button key={label} type="button" onClick={() => setPage('skills')}>
            <Icon size={18} />
            {label}
          </button>
        ))}
      </div>
    </section>
  );
}

function InlineAgentTrace({ timeline = [], sourceMap }) {
  const visibleItems = timeline.slice(-5);

  return (
    <div className="agent-trace">
      <div className="agent-trace-head">
        <span>当前任务状态树</span>
        {sourceMap?.available && <em>cli.js.map 路 {sourceMap.sizeMb}MB</em>}
      </div>
      <div className="agent-trace-list">
        {visibleItems.map(([label, status, time, tone], index) => (
          <div className={`agent-trace-row ${tone}`} key={`${label}-${index}`}>
            <span className="agent-trace-node" />
            <strong>{label}</strong>
            <em>{status}</em>
            <small>{time}</small>
          </div>
        ))}
      </div>
      {sourceMap?.categories?.length > 0 && (
        <div className="agent-trace-sources">
          {sourceMap.categories.filter((item) => item.count > 0).slice(0, 4).map((item) => (
            <span key={item.label}>{item.label} {item.count}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function StatusTree({ timeline, sourceMap, compact = false, title = '执行状态树' }) {
  if (compact) return null;

  return (
    <section className={compact ? 'message-status-tree status-tree-card' : 'rail-card status-tree-card'}>
      <header>
        <h3>{title}</h3>
      </header>
      <div className="status-tree">
        {timeline.map(([label, status, time, tone], index) => (
          <div className={`tree-item ${tone}`} key={`${label}-${index}`}>
            <span className="node" />
            <div>
              <strong>{label}</strong>
              <em>{status}</em>
            </div>
            <small>{time}</small>
          </div>
        ))}
      </div>
      {sourceMap?.categories?.length > 0 && (
        <div className="source-buckets">
          {sourceMap.categories.filter((item) => item.count > 0).slice(0, 4).map((item) => (
            <span key={item.label}>{item.label} {item.count}</span>
          ))}
        </div>
      )}
    </section>
  );
}

function SkillsView({ capabilities }) {
  const plugins = capabilities?.plugins?.length ? capabilities.plugins : fallbackPlugins;
  const totals = capabilities?.totals || plugins.reduce(
    (acc, plugin) => {
      acc.commands += plugin.commands || 0;
      acc.agents += plugin.agents || 0;
      acc.skills += plugin.skills || 0;
      acc.hooks += plugin.hooks || 0;
      return acc;
    },
    { commands: 0, agents: 0, skills: 0, hooks: 0 }
  );

  const enabled = plugins.filter((plugin) => plugin.available).length;

  return (
    <section className="management panel">
      <HeaderBar
        title="Skill 管理"
        subtitle="编排 ESG、碳核算、能耗诊断与项目协同常用 AI 技能，构建从分析到执行的一体化能力库"
      />
      <div className="management-toolbar">
        <div className="tabs">
          <button className="active" type="button">全部技能</button>
          <button type="button">已启用</button>
          <button type="button">工作流</button>
          <button type="button">模板</button>
        </div>
        <div className="toolbar-actions">
          <label>
            <Search size={18} />
            <input placeholder="搜索 Skill 名称或描述" />
          </label>
          <button className="select-button" type="button"><Filter size={18} /> 全部分类 <ChevronDown size={16} /></button>
          <button className="primary" type="button"><Plus size={18} /> 创建 Skill</button>
        </div>
      </div>
      <div className="stats-row">
        {[
          ['全部 Skill', plugins.length, Box, '+4'],
          ['已启用 Skill', enabled, Check, '启用率 82%'],
          ['工作流自动化', totals.commands + totals.agents, Network, '+2'],
          ['最近执行次数', '1,284', Clock3, '+18%']
        ].map(([label, value, Icon, delta]) => (
          <div className="stat-card" key={label}>
            <span><Icon size={31} /></span>
            <div>
              <em>{label}</em>
              <strong>{value}<small> 项</small></strong>
              <p>较上周 {delta}</p>
            </div>
          </div>
        ))}
      </div>
      <div className="skill-grid">
        {plugins.map((plugin, index) => (
          <SkillCard plugin={plugin} key={plugin.name} index={index} />
        ))}
      </div>
      <div className="pagination">
        <button type="button"><ChevronLeft size={18} /></button>
        <button className="active" type="button">1</button>
        <button type="button">2</button>
        <button type="button">3</button>
        <button type="button"><ChevronRight size={18} /></button>
        <select defaultValue="10">
          <option value="10">10 条 / 页</option>
        </select>
      </div>
    </section>
  );
}

function SkillCard({ plugin, index }) {
  const icons = [BarChart3, FileText, Globe2, BookOpen, Workflow, Network, ShieldCheck, Code2, Brain, SquareTerminal];
  const Icon = icons[index % icons.length];
  const active = plugin.available !== false;

  return (
    <article className={`skill-card ${index === 0 ? 'featured' : ''}`}>
      <div className="skill-top">
        <span className={`skill-icon tone-${index % 5}`}><Icon size={32} /></span>
        <button className={`toggle ${active ? 'on' : ''}`} type="button"><span /></button>
      </div>
      <h3>{formatPluginName(plugin.name)}</h3>
      <p>{plugin.description}</p>
      <div className="skill-metrics">
        <span>命令 {plugin.commands || 0}</span>
        <span>Agent {plugin.agents || 0}</span>
        <span>Skill {plugin.skills || 0}</span>
        <span>Hook {plugin.hooks || 0}</span>
      </div>
      <footer>
        <span>更新 2 小时前</span>
        <div className="avatar mini">张</div>
        <em>{active ? '启用' : '停用'}</em>
      </footer>
    </article>
  );
}

function normalizeMcpService(service, index) {
  const icons = [FileText, User, Database, Workflow, Globe2];
  const rawStatus = service.status || '';
  const online = rawStatus === 'online' || rawStatus === '在线' || rawStatus === '已连接';
  return {
    name: service.name || `MCP ${index + 1}`,
    url: service.url || service.raw || '-',
    tags: Array.isArray(service.tags) && service.tags.length ? service.tags : ['Claude MCP'],
    auth: service.auth || service.authType || 'Local',
    authState: service.authState || (online ? '已授权' : '需授权'),
    status: online ? '在线' : (service.status || '离线'),
    ping: service.ping || (service.pingMs ? `${service.pingMs}ms` : '-'),
    sync: service.sync || service.lastSync || '刚刚',
    permissions: service.permissions || '读写',
    icon: icons[index % icons.length],
    tone: service.tone || ['orange', 'blue', 'purple', 'orange', 'teal'][index % 5]
  };
}

function McpView({ backendStatus }) {
  const hasRealMcpServices = Boolean(backendStatus?.mcp?.services?.length);
  const services = hasRealMcpServices
    ? backendStatus.mcp.services.map(normalizeMcpService)
    : mcpServices.map((service) => ({
        ...service,
        authState: '待接入',
        status: '模板',
        ping: '-',
        sync: '未连接',
        permissions: '待配置'
      }));

  return (
    <section className="management panel mcp-view">
      <HeaderBar
        title="MCP 管理"
        subtitle="集中管理碳核算、ESG 数据、票据凭证与项目协同工具，让 EcoreX Agent 直接服务真实工作流"
      />
      <div className="mcp-toolbar">
        <label>
          <Search size={18} />
          <input placeholder="搜索 MCP 服务名称或描述..." />
        </label>
        <button type="button"><Filter size={18} /> 筛选</button>
        <button type="button">状态：全部 <ChevronDown size={16} /></button>
        <button className="primary" type="button"><Plus size={18} /> 新增 MCP 服务</button>
      </div>
      <div className="table-head">
        <span>服务信息</span>
        <span>权限与认证</span>
        <span>连接状态</span>
        <span>最后同步</span>
        <span>操作</span>
      </div>
      <div className="mcp-list">
        {services.map((service) => (
          <McpRow service={service} key={service.name} />
        ))}
      </div>
      <div className="backend-note">
        <ShieldCheck size={18} />
        <span>{backendStatus?.mcp?.configured ? '已检测到本机 Claude MCP 配置' : '本机暂无 MCP 服务，界面已预置企业连接模板，可通过 Claude Code 后端接入。'}</span>
      </div>
    </section>
  );
}

function McpRow({ service }) {
  const Icon = service.icon;
  const online = service.status === '在线' || service.status === 'online';

  return (
    <article className="mcp-row">
      <div className="mcp-info">
        <span className={`mcp-icon ${service.tone}`}><Icon size={30} /></span>
        <div>
          <strong>{service.name}</strong>
          <em>{service.url}</em>
          <div>{service.tags.map((tag) => <small key={tag}>{tag}</small>)}</div>
        </div>
      </div>
      <div className="mcp-auth">
        <ShieldCheck size={22} />
        <strong>{service.auth}</strong>
        <span className={service.authState === '已授权' ? 'ok' : 'warn'}>{service.authState}</span>
        <em>权限：{service.permissions || '读写'}</em>
      </div>
      <div className="mcp-status">
        <span className={online ? 'dot ok' : 'dot warn'} />
        <strong>{service.status}</strong>
        <em>{service.ping}</em>
      </div>
      <div className="mcp-sync">
        <strong>2026-05-20 10:24:36</strong>
        <em>{service.sync}</em>
      </div>
      <div className="mcp-actions">
        <button type="button"><Play size={18} /></button>
        <button type="button"><Pencil size={18} /></button>
        <button type="button"><MoreHorizontal size={18} /></button>
      </div>
    </article>
  );
}

function formatPluginName(name) {
  const names = {
    'agent-sdk-dev': 'Agent SDK 开发',
    'claude-opus-4-5-migration': '模型迁移助手',
    'code-review': '代码审查',
    'commit-commands': '提交工作流',
    'explanatory-output-style': '解释型输出',
    'feature-dev': '功能开发',
    'frontend-design': '前端设计',
    hookify: 'Hook 编排',
    'learning-output-style': '学习模式',
    'plugin-dev': '插件开发',
    'pr-review-toolkit': 'PR 审查工具箱',
    'ralph-wiggum': '迭代循环',
    'security-guidance': '安全指导'
  };
  return names[name] || name;
}

export default App;


import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Archive,
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Box,
  Brain,
  Building2,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CheckCheck,
  CircleCheck,
  CircleDashed,
  Clock3,
  Code2,
  ClipboardList,
  Copy,
  Database,
  Download,
  Eye,
  FileText,
  Filter,
  FolderOpen,
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
  RotateCcw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  SquareTerminal,
  Target,
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
    description: '助手开发套件，用于扩展 EcoreX 亦芯的业务能力',
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
    description: '能力创建工具包，包含自动化、数据连接、命令、助手和能力模板',
    commands: 1,
    agents: 3,
    skills: 7,
    hooks: 0,
    available: true
  },
  {
    name: 'security-guidance',
    category: 'security',
    description: '安全提醒能力，辅助识别命令注入、XSS 与高风险代码模式',
    commands: 0,
    agents: 0,
    skills: 0,
    hooks: 1,
    available: true
  },
  {
    name: 'code-review',
    category: 'productivity',
    description: '自动化代码审查工作流，可调用多个专门助手交叉检查',
    commands: 1,
    agents: 0,
    skills: 0,
    hooks: 0,
    available: true
  }
];

const previewSkillItems = [
  {
    name: 'carbon-analysis',
    title: '碳排数据分析',
    category: '碳核算',
    description: '识别排放强度波动、能耗结构变化与数据缺口。',
    commands: 2,
    agents: 1,
    skills: 3,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'esg-disclosure',
    title: 'ESG 披露助手',
    category: 'ESG',
    description: '整理披露口径、缺失指标、证据材料与整改动作。',
    commands: 2,
    agents: 2,
    skills: 4,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'energy-diagnosis',
    title: '能耗异常诊断',
    category: '能耗',
    description: '定位设备、产线与时段维度的能耗异常原因。',
    commands: 1,
    agents: 1,
    skills: 2,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'reduction-workflow',
    title: '减排任务编排',
    category: '项目协同',
    description: '把减排建议拆解为负责人、节点、验收标准和风险提醒。',
    commands: 2,
    agents: 2,
    skills: 3,
    hooks: 0,
    installed: false,
    enabled: false
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
    url: 'https://data.ecorex.com/carbon-knowledge',
    tags: ['排放因子', '核算指引', '智能检索'],
    auth: '密钥认证',
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
    url: 'https://data.ecorex.com/esg-data',
    tags: ['披露指标', '问卷', '同步'],
    auth: '企业授权',
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
    url: 'https://data.ecorex.com/energy-warehouse',
    tags: ['电表数据', '数据查询', '报表'],
    auth: '密钥认证',
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
    url: 'https://data.ecorex.com/carbon-task',
    tags: ['减排项目', '运营', '自动化'],
    auth: '令牌认证',
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
    url: 'https://data.ecorex.com/voucher-parser',
    tags: ['票据识别', '发票', '凭证'],
    auth: '密钥认证',
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
  ['已读取本地能力索引', '已完成', '10:24:01', 'success'],
  ['确认 EcoreX 亦芯助手身份', '已完成', '10:24:02', 'success'],
  ['加载本地执行能力', '已完成', '10:24:03', 'success'],
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
  timeout: { label: '已超时', icon: Clock3, tone: 'error' },
  error: { label: '错误 / 重试', icon: AlertTriangle, tone: 'error' }
};

const PREVIEW_SESSION_KEY = 'ecorex-session';
const PREVIEW_MODEL_PROFILES_KEY = 'ecorex-preview-model-profiles';
const DEFAULT_IMAGE_MODEL_NAME = 'gpt-image-2';
const DEFAULT_PERMISSION_MODE_KEY = 'ecorex-default-permission-mode';
const MESSAGE_WINDOW_SIZE = 40;
const MESSAGE_WINDOW_STEP = 30;
const ASSISTANT_COLLAPSE_CHARS = 1400;
const AGENT_EVENT_QUEUE_LIMIT = 1600;
const AGENT_EVENT_FLUSH_BATCH = 120;
const AGENT_EVENT_FLUSH_DELAY_MS = 40;
const AGENT_EVENT_PENDING_DELAY_MS = 140;
const PENDING_AGENT_EVENT_TTL_MS = 7000;
const AGENT_TIMELINE_BATCH_LIMIT = 24;
const AGENT_EVENT_TERMINAL_KINDS = new Set(['done', 'error', 'cancelled', 'timeout']);
const MANAGED_SECRET_DEFINITIONS = [
  { key: 'ANTHROPIC_API_KEY', label: '模型服务密钥', hint: '用于亦芯调用模型服务' },
  { key: 'ANTHROPIC_AUTH_TOKEN', label: '模型授权令牌', hint: '用于企业授权会话' },
  { key: 'ECOREX_LICENSE_KEY', label: 'EcoreX 授权码', hint: '用于工作台授权校验' }
];

const DEFAULT_PERMISSION_OPTION = {
  value: 'default',
  label: '默认权限',
  description: '按默认安全规则运行，文件写入、命令执行和系统目录访问会继续请求确认。',
  tone: 'default'
};

const FULL_ACCESS_PERMISSION_OPTION = {
  value: 'fullAccess',
  label: '完全访问权限',
  description: '跳过本地执行确认，可能直接读写当前工作区并运行命令，仅适合完全可信目录。',
  tone: 'full'
};

function isUnauthorizedError(error) {
  const message = String(error?.message || error?.error || error || '').toLowerCase();
  return error?.code === 'UNAUTHORIZED' || error?.status === 401 || message.includes('unauthorized') || message.includes('401');
}

function normalizeAuthStatus(status, fallbackLoggedIn = false) {
  if (!status) return { loggedIn: fallbackLoggedIn, mode: window.ecorex ? 'desktop' : 'preview' };
  const loggedIn = Boolean(status.loggedIn ?? status.authenticated ?? status.ok ?? status.user);
  return { ...status, loggedIn, mode: status.mode || (window.ecorex ? 'desktop' : 'preview') };
}

function normalizeAccessMode(value, fallback = 'default') {
  const text = String(value || fallback || 'default').trim();
  if (['fullAccess', 'bypassPermissions', 'dangerously-skip-permissions', '--dangerously-skip-permissions'].includes(text)) {
    return 'fullAccess';
  }
  return 'default';
}

function permissionModeFromAccessMode(value) {
  return normalizeAccessMode(value) === 'fullAccess' ? 'fullAccess' : 'default';
}

function permissionOptionsForUi() {
  return [DEFAULT_PERMISSION_OPTION, FULL_ACCESS_PERMISSION_OPTION];
}

function permissionOptionByValue(value) {
  const accessMode = normalizeAccessMode(value);
  return permissionOptionsForUi().find((option) => option.value === accessMode) || DEFAULT_PERMISSION_OPTION;
}

function fullAccessConfirmationFields(accessMode) {
  return normalizeAccessMode(accessMode) === 'fullAccess'
    ? { fullAccessConfirmed: true, fullAccessConfirmation: 'fullAccess' }
    : {};
}

function customWorkspaceConfirmationFields(workspaceRoot) {
  return String(workspaceRoot || '').trim()
    ? { customWorkspaceConfirmed: true, workspaceRootConfirmed: true, workspaceRootConfirmation: 'customWorkspace' }
    : {};
}

function confirmFullAccessChange() {
  if (typeof window === 'undefined' || typeof window.confirm !== 'function') return true;
  return window.confirm([
    '确认启用完全访问权限？',
    '',
    '启用后，亦芯会跳过本地执行确认，可能直接读写当前工作区并运行命令。',
    '请只在可信目录和明确任务中使用；你可以随时撤销为默认权限。'
  ].join('\n'));
}

function confirmCustomWorkspaceChange(workspaceRoot) {
  const nextRoot = String(workspaceRoot || '').trim();
  if (!nextRoot || typeof window === 'undefined' || typeof window.confirm !== 'function') return true;
  return window.confirm([
    '确认使用这个自定义工作区？',
    '',
    nextRoot,
    '',
    '亦芯会把它作为默认上下文目录读取项目文件，并可能按你的任务执行本地操作。',
    '请确认这是可信项目目录。'
  ].join('\n'));
}

function readStoredDefaultAccessMode() {
  try {
    return normalizeAccessMode(localStorage.getItem(DEFAULT_PERMISSION_MODE_KEY), 'default');
  } catch {
    return 'default';
  }
}

function storeDefaultAccessMode(value) {
  const accessMode = normalizeAccessMode(value);
  try {
    localStorage.setItem(DEFAULT_PERMISSION_MODE_KEY, accessMode);
  } catch {
    // Local storage may be unavailable in locked-down preview contexts.
  }
  return accessMode;
}

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
  const [authStatus, setAuthStatus] = useState(() => normalizeAuthStatus(null, !window.ecorex && localStorage.getItem(PREVIEW_SESSION_KEY) === '1'));
  const [loggedIn, setLoggedIn] = useState(() => authStatus.loggedIn);
  const [page, setPage] = useState('chat');
  const [backendStatus, setBackendStatus] = useState(null);
  const [capabilities, setCapabilities] = useState(null);
  const [backendError, setBackendError] = useState('');
  const [authNotice, setAuthNotice] = useState('');

  useEffect(() => {
    document.documentElement.dataset.theme = 'dark';
    localStorage.setItem('ecorex-theme', 'dark');
  }, []);

  useEffect(() => {
    refreshAuthStatus();
    refreshBackend();
  }, []);

  async function refreshAuthStatus() {
    if (!window.ecorex?.getAuthStatus) {
      const previewStatus = normalizeAuthStatus(null, localStorage.getItem(PREVIEW_SESSION_KEY) === '1');
      setAuthStatus(previewStatus);
      setLoggedIn(previewStatus.loggedIn);
      return previewStatus;
    }

    try {
      const status = normalizeAuthStatus(await window.ecorex.getAuthStatus());
      setAuthStatus(status);
      setLoggedIn(status.loggedIn);
      if (status.loggedIn) setAuthNotice('');
      return status;
    } catch (error) {
      const status = normalizeAuthStatus({ ok: false, error: error?.message || 'Auth status failed' });
      setAuthStatus(status);
      setLoggedIn(false);
      setAuthNotice('登录状态校验失败，请重新登录。');
      return status;
    }
  }

  async function refreshBackend(options = {}) {
    if (!window.ecorex) return;
    const requestOptions = options && typeof options === 'object' && (options.refresh || options.forceRefresh)
      ? { refresh: Boolean(options.refresh || options.forceRefresh) }
      : {};
    try {
      setBackendError('');
      const [status, caps] = await Promise.all([
        window.ecorex.getBackendStatus(requestOptions),
        window.ecorex.getCapabilities()
      ]);
      setBackendStatus(status);
      setCapabilities(caps);
      if (!window.ecorex?.getAuthStatus && status?.auth) {
        const nextAuth = normalizeAuthStatus(status.auth);
        setAuthStatus(nextAuth);
        setLoggedIn(nextAuth.loggedIn);
      }
    } catch (error) {
      if (isUnauthorizedError(error)) {
        handleUnauthorized();
        return;
      }
      setBackendError(sanitizeDisplayText(error?.message, '本地能力服务未就绪'));
      setBackendStatus((current) => current || { ok: false });
      setCapabilities((current) => current || null);
    }
  }

  function handleUnauthorized() {
    localStorage.removeItem(PREVIEW_SESSION_KEY);
    setAuthStatus((current) => normalizeAuthStatus({ ...current, loggedIn: false, ok: false, error: 'Unauthorized' }));
    setLoggedIn(false);
    setAuthNotice('登录状态已过期，请重新登录。');
  }

  async function handleLogin(credentials = {}) {
    setAuthNotice('');
    if (!window.ecorex?.authLogin) {
      localStorage.setItem(PREVIEW_SESSION_KEY, '1');
      const previewStatus = normalizeAuthStatus({ loggedIn: true, user: { email: credentials.email || 'preview@ecorex.local' } }, true);
      setAuthStatus(previewStatus);
      setLoggedIn(true);
      refreshBackend();
      return;
    }

    try {
      const result = await window.ecorex.authLogin(credentials);
      if (result?.ok === false) throw new Error(result.error || '登录失败');
      const status = normalizeAuthStatus(result?.auth || result?.status || result, true);
      setAuthStatus(status);
      setLoggedIn(status.loggedIn);
      await refreshBackend();
    } catch (error) {
      setAuthNotice(error?.message || '登录失败，请检查账号或稍后重试。');
      setLoggedIn(false);
    }
  }

  async function handleLogout() {
    try {
      if (window.ecorex?.authLogout) {
        await window.ecorex.authLogout();
      }
    } catch {
      // Keep the UI logout decisive even if the backend is already signed out.
    }
    localStorage.removeItem(PREVIEW_SESSION_KEY);
    setAuthStatus(normalizeAuthStatus({ loggedIn: false }));
    setLoggedIn(false);
    setAuthNotice('');
  }

  if (!loggedIn) {
    return (
      <AppFrame>
        <LoginPage
          backendStatus={backendStatus}
          authStatus={authStatus}
          authNotice={authNotice}
          onLogin={handleLogin}
          onOpenAuth={async () => {
            try {
              const result = await window.ecorex?.openAuth?.();
              if (result?.ok === false) {
                setAuthNotice(result.error === 'Unauthorized'
                  ? '请先完成应用登录，再打开企业统一身份认证。'
                  : result.error || '无法打开企业统一身份认证。');
                return;
              }
              await refreshAuthStatus();
            } catch (error) {
              setAuthNotice(error?.message || '无法打开企业统一身份认证。');
            }
          }}
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
        backendError={backendError}
        capabilities={capabilities}
        authStatus={authStatus}
        refreshBackend={refreshBackend}
        onUnauthorized={handleUnauthorized}
        logout={handleLogout}
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
      <div className="titlebar-title">EcoreX 亦芯</div>
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

function LoginPage({ authStatus, authNotice, onLogin, onOpenAuth }) {
  const [loginType, setLoginType] = useState('password');
  const [showPassword, setShowPassword] = useState(false);
  const [email, setEmail] = useState('');
  const [secret, setSecret] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const loginImage = `${import.meta.env.BASE_URL}ui/login-dark.png`;
  const iconSrc = `${import.meta.env.BASE_URL}icon.png`;
  const setupRequired = Boolean(authStatus?.setupRequired);
  const authMode = window.ecorex?.authLogin ? (setupRequired ? '首次绑定' : '桌面认证') : '预览模式';

  useEffect(() => {
    if (setupRequired && loginType !== 'password') setLoginType('password');
  }, [setupRequired, loginType]);

  async function submitLogin(event) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await onLogin?.({
        email,
        password: loginType === 'password' ? secret : undefined,
        code: loginType === 'code' ? secret : undefined,
        loginType
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen login-dark">
      <div className="login-art">
        <img alt="" src={loginImage} />
      </div>
      <div className="login-brand-overlay">
        <img alt="" src={iconSrc} />
        <span>EcoreX</span>
      </div>
      <form
        className="login-panel"
        data-testid="login-form"
        onSubmit={submitLogin}
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
            disabled={setupRequired}
            type="button"
            title={setupRequired ? '首次启动请使用密码绑定本机管理员账号' : '验证码登录'}
            onClick={() => setLoginType('code')}
          >
            验证码登录
          </button>
        </div>

        <label className="field-label">企业邮箱</label>
        <div className="input-shell">
          <Mail size={20} />
          <input
            autoComplete="email"
            data-testid="login-email-input"
            onChange={(event) => setEmail(event.target.value)}
            placeholder="请输入企业邮箱"
            type="email"
            value={email}
          />
        </div>

        <label className="field-label">{loginType === 'password' ? '密码' : '验证码'}</label>
        <div className="input-shell">
          <Lock size={20} />
          <input
            autoComplete={loginType === 'password' ? 'current-password' : 'one-time-code'}
            data-testid="login-secret-input"
            onChange={(event) => setSecret(event.target.value)}
            placeholder={loginType === 'password' ? '请输入密码' : '请输入验证码'}
            type={showPassword ? 'text' : 'password'}
            value={secret}
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

        <button className="primary wide" data-testid="login-submit-button" type="submit" disabled={submitting}>
          {submitting ? '登录中' : setupRequired ? '绑定并进入' : '登录'}
        </button>

        {(authNotice || authStatus?.error) && (
          <p className="auth-notice">
            <AlertTriangle size={16} />
            {authNotice || sanitizeDisplayText(authStatus.error, '登录状态校验失败，请重新登录。')}
          </p>
        )}

        <div className="backend-mini">
          <ShieldCheck size={16} />
          <span>{authMode}</span>
          <em className="pill">{authStatus?.loggedIn ? '已登录' : '待登录'}</em>
        </div>

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
          {setupRequired ? '首次使用会将该邮箱绑定为本机管理员。' : '还没有账号?'}<button type="button">请联系管理员</button>
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
  backendError,
  capabilities,
  authStatus,
  refreshBackend,
  onUnauthorized,
  logout
}) {
  return (
    <div className="app-shell" data-testid="app-shell">
      <Sidebar page={page} setPage={setPage} logout={logout} />
      <main className={`workspace workspace-${page}`} data-testid="workspace">
        {page === 'chat' && (
          <ChatView
            backendStatus={backendStatus}
            backendError={backendError}
            capabilities={capabilities}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
            setPage={setPage}
          />
        )}
        {page === 'skills' && (
          <SkillsView
            backendStatus={backendStatus}
            capabilities={capabilities}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
            setPage={setPage}
          />
        )}
        {page === 'mcp' && (
          <McpView
            backendStatus={backendStatus}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
            setPage={setPage}
          />
        )}
        {page === 'diagnostics' && (
          <DiagnosticsView
            backendStatus={backendStatus}
            backendError={backendError}
            capabilities={capabilities}
            authStatus={authStatus}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
          />
        )}
        {page === 'projects' && (
          <ProjectsView
            backendStatus={backendStatus}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
            setPage={setPage}
          />
        )}
      </main>
    </div>
  );
}

function Sidebar({ page, setPage, logout }) {
  const [profileOpen, setProfileOpen] = useState(false);
  const [modelConfigOpen, setModelConfigOpen] = useState(false);
  const [currentModelLabel, setCurrentModelLabel] = useState('默认模型');

  useEffect(() => {
    let cancelled = false;
    const refreshCurrentModel = () => loadModelProfiles('sonnet').then((result) => {
      if (cancelled) return;
      const current = getCurrentModelProfile(result.profiles);
      setCurrentModelLabel(modelProfilePrimaryLabel(current));
    });
    refreshCurrentModel();
    window.addEventListener?.('ecorex:model-profiles-changed', refreshCurrentModel);
    return () => {
      cancelled = true;
      window.removeEventListener?.('ecorex:model-profiles-changed', refreshCurrentModel);
    };
  }, []);

  function openModelConfig() {
    setProfileOpen(false);
    setModelConfigOpen(true);
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <Logo />
        <button className="icon-button small" type="button">
          <ChevronLeft size={20} />
        </button>
      </div>
      <button className="new-chat" type="button" title="新会话" onClick={() => setPage('chat')}>
        <Plus size={22} />
        新会话
      </button>
      <nav className="side-nav">
        <button className={page === 'mcp' ? 'active' : ''} type="button" title="数据连接" onClick={() => setPage('mcp')}>
          <Box size={25} />
          数据连接
        </button>
        <button className={page === 'skills' ? 'active' : ''} type="button" title="能力中心" onClick={() => setPage('skills')}>
          <Layers3 size={25} />
          能力中心
        </button>
        <button className={page === 'diagnostics' ? 'active' : ''} data-testid="nav-diagnostics" type="button" title="诊断 / 设置" onClick={() => setPage('diagnostics')}>
          <Settings size={25} />
          诊断 / 设置
        </button>
        <button className={page === 'projects' ? 'active' : ''} type="button" title="项目" onClick={() => setPage('projects')}>
          <LayoutDashboard size={25} />
          项目
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
            <em className="profile-mini-status"><i />模型 · {currentModelLabel}</em>
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
              <button type="button" onClick={() => setPage('diagnostics')}><Settings size={20} />偏好设置</button>
              <button type="button" onClick={openModelConfig}><Database size={20} />模型配置</button>
              <button type="button"><HelpCircle size={20} />帮助中心</button>
              <button type="button"><Keyboard size={20} />快捷键</button>
            </div>
            <button className="profile-logout" type="button" onClick={logout}>
              <LogOut size={20} />
              退出登录
            </button>
          </div>
        )}
        <ModelConfigModal
          open={modelConfigOpen}
          initialModelName={currentModelLabel}
          onClose={() => setModelConfigOpen(false)}
          onCurrentChange={(profile) => setCurrentModelLabel(modelProfilePrimaryLabel(profile))}
        />
      </div>
    </aside>
  );
}

function normalizeModelProfile(raw = {}, index = 0, currentId = '') {
  const id = String(raw.id || raw.profileId || raw.key || raw.name || `profile-${index + 1}`);
  const modelName = String(raw.modelName || raw.model || raw.defaultModel || '').trim();
  const imageModelName = String(raw.imageModelName || raw.imageModel || raw.visionModel || DEFAULT_IMAGE_MODEL_NAME).trim();
  const statusText = String(raw.status || raw.state || '').toLowerCase();
  const latencyValue = raw.latencyMs ?? raw.latency ?? raw.lastLatencyMs ?? raw.responseTimeMs;
  const latencyMs = Number.isFinite(Number(latencyValue)) ? Math.max(0, Math.round(Number(latencyValue))) : null;
  const active = Boolean(raw.active ?? raw.current ?? raw.isActive ?? (currentId && id === currentId));
  const apiKeyConfigured = Boolean(raw.apiKeyConfigured ?? raw.apiKeySet ?? raw.hasApiKey ?? raw.keyConfigured ?? raw.secretConfigured);

  return {
    id,
    name: String(raw.name || raw.title || raw.displayName || modelName || `模型配置 ${index + 1}`).trim(),
    baseUrl: String(raw.baseUrl || raw.baseURL || raw.endpoint || raw.url || '').trim(),
    modelName,
    imageModelName,
    active,
    status: raw.statusLabel || (statusText ? statusLabelFrom(statusText, statusText === 'error' ? 'danger' : 'success') : (active ? '当前模型' : '待测试')),
    latencyMs,
    apiKeyConfigured,
    maskedKey: raw.maskedKey || raw.apiKeyMasked || raw.keyPreview || raw.apiKeyPreview || (apiKeyConfigured ? '已保存' : ''),
    error: raw.error || raw.lastError || '',
    updatedAt: raw.updatedAt || raw.updated || raw.lastUsedAt || ''
  };
}

function getCurrentModelProfile(profiles = []) {
  return profiles.find((profile) => profile.active) || profiles[0] || null;
}

function modelProfilePrimaryLabel(profile) {
  if (!profile) return '默认模型';
  return profile.modelName || profile.name || '默认模型';
}

function modelProfileDraft(profile = {}) {
  return {
    id: profile.id || '',
    name: profile.name || '',
    baseUrl: profile.baseUrl || '',
    apiKey: '',
    modelName: profile.modelName || '',
    imageModelName: profile.imageModelName || profile.imageModel || DEFAULT_IMAGE_MODEL_NAME
  };
}

function emitModelProfilesChanged(profile = null) {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
  window.dispatchEvent(new CustomEvent('ecorex:model-profiles-changed', { detail: { profile } }));
}

function maskApiKey(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (text.length <= 8) return '已保存';
  return `${text.slice(0, 4)}****${text.slice(-4)}`;
}

function readPreviewModelProfileStore(defaultModelName = 'sonnet') {
  const fallback = {
    activeId: 'preview-default',
    profiles: [{
      id: 'preview-default',
      name: '预览默认配置',
      baseUrl: '',
      modelName: defaultModelName || 'sonnet',
      imageModelName: DEFAULT_IMAGE_MODEL_NAME,
      active: true,
      status: '预览就绪',
      latencyMs: null,
      apiKeySet: false
    }]
  };

  try {
    const parsed = JSON.parse(localStorage.getItem(PREVIEW_MODEL_PROFILES_KEY) || 'null');
    if (!parsed || !Array.isArray(parsed.profiles)) return fallback;
    const profiles = parsed.profiles.length ? parsed.profiles : fallback.profiles;
    return { activeId: parsed.activeId || profiles[0]?.id || fallback.activeId, profiles };
  } catch {
    return fallback;
  }
}

function writePreviewModelProfileStore(store) {
  try {
    localStorage.setItem(PREVIEW_MODEL_PROFILES_KEY, JSON.stringify(store));
  } catch {
    // Preview storage is best-effort; desktop IPC remains the source of truth.
  }
}

function normalizeModelProfileStore(store, defaultModelName = 'sonnet') {
  const activeId = store.activeId || store.currentId || store.activeProfileId || '';
  let profiles = (store.profiles || []).map((profile, index) => normalizeModelProfile(profile, index, activeId));
  if (!profiles.length) {
    profiles = readPreviewModelProfileStore(defaultModelName).profiles.map((profile, index) => normalizeModelProfile(profile, index, 'preview-default'));
  }
  const current = profiles.find((profile) => profile.active) || profiles.find((profile) => profile.id === activeId) || profiles[0];
  return {
    profiles: profiles.map((profile) => ({ ...profile, active: current ? profile.id === current.id : false })),
    activeId: current?.id || ''
  };
}

async function loadModelProfiles(defaultModelName = 'sonnet') {
  const result = await callEcorex(['listModelProfiles']);
  if (result?.unauthorized) return { unauthorized: true, profiles: [], activeId: '' };

  if (!result || result.missing) {
    const preview = normalizeModelProfileStore(readPreviewModelProfileStore(defaultModelName), defaultModelName);
    return { ok: true, preview: true, ...preview };
  }

  if (result.ok === false) {
    return { ok: false, error: result.error || '模型配置加载失败', profiles: [], activeId: '' };
  }

  const currentRaw = result.currentProfile || result.current || result.activeProfile || null;
  const activeId = result.activeId || result.currentId || result.activeProfileId || currentRaw?.id || currentRaw?.profileId || '';
  let items = extractCollection(result, ['profiles', 'modelProfiles', 'items']);
  if (currentRaw && !items.some((item) => (item.id || item.profileId || item.name) === (currentRaw.id || currentRaw.profileId || currentRaw.name))) {
    items = [currentRaw, ...items];
  }
  return { ok: true, preview: false, ...normalizeModelProfileStore({ profiles: items, activeId }, defaultModelName) };
}

function savePreviewModelProfile(draft) {
  const store = readPreviewModelProfileStore(draft.modelName || 'sonnet');
  const id = draft.id || `profile-${Date.now()}`;
  const existing = store.profiles.find((profile) => profile.id === id) || {};
  const nextProfile = {
    ...existing,
    id,
    name: draft.name || draft.modelName || '未命名模型',
    baseUrl: draft.baseUrl,
    modelName: draft.modelName,
    imageModelName: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME,
    maskedKey: draft.apiKey ? maskApiKey(draft.apiKey) : existing.maskedKey,
    apiKeySet: Boolean(draft.apiKey || existing.apiKeySet || existing.maskedKey),
    status: existing.status || '待测试',
    latencyMs: existing.latencyMs ?? null,
    updatedAt: new Date().toISOString()
  };
  const profiles = store.profiles.some((profile) => profile.id === id)
    ? store.profiles.map((profile) => (profile.id === id ? nextProfile : profile))
    : [nextProfile, ...store.profiles];
  const activeId = store.activeId || id;
  writePreviewModelProfileStore({ activeId, profiles });
  return { ok: true, preview: true, profile: nextProfile, activeId };
}

async function saveModelProfile(draft) {
  const payload = {
    id: draft.id || undefined,
    profileId: draft.id || undefined,
    name: draft.name,
    label: draft.name,
    baseUrl: draft.baseUrl,
    model: draft.modelName,
    modelName: draft.modelName,
    imageModel: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME,
    imageModelName: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME
  };
  if (String(draft.apiKey || '').trim()) payload.apiKey = draft.apiKey.trim();

  if (!hasEcorexFunction(['saveModelProfile'])) return savePreviewModelProfile(draft);
  const result = await callEcorex(['saveModelProfile'], payload);
  if (result?.missing) return savePreviewModelProfile(draft);
  return result;
}

function deletePreviewModelProfile(profileId) {
  const store = readPreviewModelProfileStore();
  const profiles = store.profiles.filter((profile) => profile.id !== profileId);
  const activeId = store.activeId === profileId ? (profiles[0]?.id || '') : store.activeId;
  writePreviewModelProfileStore({ activeId, profiles });
  return { ok: true, preview: true, activeId };
}

async function deleteModelProfile(profileId) {
  if (!hasEcorexFunction(['deleteModelProfile'])) return deletePreviewModelProfile(profileId);
  const result = await callEcorexAction(['deleteModelProfile'], { id: profileId, profileId, name: profileId });
  if (result?.missing) return deletePreviewModelProfile(profileId);
  return result;
}

function activatePreviewModelProfile(profileId) {
  const store = readPreviewModelProfileStore();
  const profiles = store.profiles.map((profile) => ({ ...profile, active: profile.id === profileId }));
  writePreviewModelProfileStore({ activeId: profileId, profiles });
  return { ok: true, preview: true, activeId: profileId };
}

async function activateModelProfile(profileId) {
  if (!hasEcorexFunction(['activateModelProfile'])) return activatePreviewModelProfile(profileId);
  const result = await callEcorexAction(['activateModelProfile'], { id: profileId, profileId, name: profileId });
  if (result?.missing) return activatePreviewModelProfile(profileId);
  return result;
}

function testPreviewModelProfile(draft) {
  const latencyMs = 96 + Math.round(Math.random() * 180);
  return {
    ok: true,
    preview: true,
    status: draft.baseUrl || draft.modelName ? '预览测试通过' : '预览就绪',
    latencyMs
  };
}

function validateModelConnectionDraft(draft = {}, selectedProfile = null) {
  const missing = [];
  if (!String(draft.baseUrl || '').trim()) missing.push('Base URL');
  if (!String(draft.modelName || '').trim()) missing.push('模型名称');
  if (!String(draft.apiKey || '').trim() && !selectedProfile?.apiKeyConfigured && !selectedProfile?.maskedKey) missing.push('API Key');
  if (missing.length) {
    return {
      ok: false,
      message: `缺少${missing.join('、')}，未发起模型调用。`
    };
  }
  return { ok: true };
}

async function testModelAdapterProfile(draft) {
  const payload = {
    id: draft.id || undefined,
    profileId: draft.id || undefined,
    name: draft.name,
    baseUrl: draft.baseUrl,
    model: draft.modelName,
    modelName: draft.modelName,
    imageModel: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME,
    imageModelName: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME
  };
  if (String(draft.apiKey || '').trim()) payload.apiKey = draft.apiKey.trim();

  if (!hasEcorexFunction(['testModelAdapterProfile'])) return testModelProfile(draft);
  const startedAt = performance.now();
  const result = await callEcorex(['testModelAdapterProfile'], payload);
  if (result?.missing) return testPreviewModelProfile(draft);
  const measuredLatency = Math.max(1, Math.round(performance.now() - startedAt));
  return {
    ...result,
    latencyMs: Math.round(Number(result?.latencyMs ?? result?.latency ?? result?.responseTimeMs ?? measuredLatency)),
    status: result?.statusLabel || (result?.ok === false ? '适配器连接失败' : '适配器连接正常')
  };
}

async function testModelProfile(draft) {
  const payload = {
    id: draft.id || undefined,
    profileId: draft.id || undefined,
    name: draft.name,
    baseUrl: draft.baseUrl,
    model: draft.modelName,
    modelName: draft.modelName,
    imageModel: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME,
    imageModelName: draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME
  };
  if (String(draft.apiKey || '').trim()) payload.apiKey = draft.apiKey.trim();

  if (!hasEcorexFunction(['testModelProfile'])) return testPreviewModelProfile(draft);
  const startedAt = performance.now();
  const result = await callEcorex(['testModelProfile'], payload);
  if (result?.missing) return testPreviewModelProfile(draft);
  const measuredLatency = Math.max(1, Math.round(performance.now() - startedAt));
  return {
    ...result,
    latencyMs: Math.round(Number(result?.latencyMs ?? result?.latency ?? result?.responseTimeMs ?? measuredLatency)),
    status: result?.statusLabel || (result?.ok === false ? '连接失败' : '连接正常')
  };
}

function extractImagePreviewSrc(result) {
  const data = result?.data;
  const first = Array.isArray(data?.data) ? data.data[0] : Array.isArray(data) ? data[0] : data;
  const url = first?.url || first?.imageUrl || first?.image_url || result?.url || result?.imageUrl;
  if (url) return String(url);
  const base64 = first?.b64_json || first?.base64 || first?.imageBase64 || result?.b64_json || result?.base64;
  if (!base64) return '';
  return `data:image/png;base64,${String(base64).replace(/^data:image\/[a-z]+;base64,/, '')}`;
}

async function generateModelImagePreview(draft) {
  const imageModelName = draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME;
  const payload = {
    id: draft.id || undefined,
    profileId: draft.id || undefined,
    name: draft.name,
    baseUrl: draft.baseUrl,
    model: imageModelName,
    imageModel: imageModelName,
    imageModelName,
    prompt: 'A compact desktop AI agent interface for carbon data analysis, dark mode, crisp UI screenshot style.',
    size: '1024x1024',
    quality: 'low',
    n: 1
  };
  if (String(draft.apiKey || '').trim()) payload.apiKey = draft.apiKey.trim();

  if (!hasEcorexFunction(['generateModelImage'])) {
    return { ok: true, preview: true, status: '预览模式图像生成就绪', latencyMs: 120, model: imageModelName, imageSrc: '' };
  }

  const startedAt = performance.now();
  const result = await callEcorex(['generateModelImage'], payload);
  if (result?.missing) {
    return { ok: true, preview: true, status: '预览模式图像生成就绪', latencyMs: 120, model: imageModelName, imageSrc: '' };
  }
  return {
    ...result,
    latencyMs: Math.round(Number(result?.latencyMs ?? result?.latency ?? performance.now() - startedAt)),
    status: result?.ok === false ? sanitizeDisplayText(result.error, '图像生成失败') : '图像生成完成',
    imageSrc: extractImagePreviewSrc(result)
  };
}

function ModelConfigModal({ open, initialModelName, onClose, onCurrentChange }) {
  const [profiles, setProfiles] = useState([]);
  const [draft, setDraft] = useState(() => modelProfileDraft({ modelName: initialModelName }));
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [lastTest, setLastTest] = useState(null);
  const [imageTest, setImageTest] = useState(null);

  async function refreshProfiles({ silent = false } = {}) {
    if (!silent) setBusy('load');
    const result = await loadModelProfiles(initialModelName || 'sonnet');
    if (result.unauthorized) {
      setNotice('登录状态已过期，请重新登录后管理模型配置。');
      setBusy('');
      return;
    }
    if (result.ok === false) {
      setNotice(`模型配置加载失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
      setBusy('');
      return;
    }

    const nextProfiles = result.profiles || [];
    const current = getCurrentModelProfile(nextProfiles);
    setProfiles(nextProfiles);
    setDraft((currentDraft) => {
      if (currentDraft.id && nextProfiles.some((profile) => profile.id === currentDraft.id)) return currentDraft;
      return modelProfileDraft(current || { modelName: initialModelName });
    });
    if (current) onCurrentChange?.(current);
    if (!silent) setNotice(result.preview ? '预览模式：模型配置 IPC 未就绪，当前仅模拟保存、切换与测速。' : '');
    setBusy('');
  }

  useEffect(() => {
    if (open) refreshProfiles();
  }, [open]);

  if (!open) return null;

  const selectedProfile = profiles.find((profile) => profile.id === draft.id) || null;
  const canSave = Boolean(String(draft.name || draft.modelName).trim() && String(draft.modelName).trim());

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function handleSave() {
    if (!canSave) {
      setNotice('请至少填写配置名称和模型名称。');
      return;
    }
    setBusy('save');
    const result = await saveModelProfile({
      ...draft,
      name: draft.name || draft.modelName,
      baseUrl: draft.baseUrl.trim(),
      modelName: draft.modelName.trim(),
      imageModelName: draft.imageModelName.trim() || DEFAULT_IMAGE_MODEL_NAME
    });
    if (result?.unauthorized) {
      setNotice('登录状态已过期，请重新登录后保存模型配置。');
    } else if (result?.ok === false) {
      setNotice(result.missing ? '模型配置服务未就绪。' : `保存失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setNotice(result?.preview ? '预览配置已保存。' : '模型配置已保存。');
      emitModelProfilesChanged(result.profile || null);
      await refreshProfiles({ silent: true });
    }
    setBusy('');
  }

  async function handleDelete(profile) {
    if (!profile?.id) return;
    const confirmed = window.confirm(`删除模型配置「${profile.name}」？`);
    if (!confirmed) return;
    setBusy(`delete-${profile.id}`);
    const result = await deleteModelProfile(profile.id);
    if (result?.unauthorized) {
      setNotice('登录状态已过期，请重新登录后删除模型配置。');
    } else if (result?.ok === false) {
      setNotice(result.missing ? '模型配置服务未就绪。' : `删除失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setDraft(modelProfileDraft({ modelName: initialModelName }));
      setNotice(result?.preview ? '预览配置已删除。' : '模型配置已删除。');
      emitModelProfilesChanged(null);
      await refreshProfiles({ silent: true });
    }
    setBusy('');
  }

  async function handleActivate(profile) {
    if (!profile?.id || profile.active) return;
    setBusy(`activate-${profile.id}`);
    const result = await activateModelProfile(profile.id);
    if (result?.unauthorized) {
      setNotice('登录状态已过期，请重新登录后切换模型。');
    } else if (result?.ok === false) {
      setNotice(result.missing ? '模型配置服务未就绪。' : `切换失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      onCurrentChange?.(profile);
      setNotice(result?.preview ? '预览当前模型已切换。' : '当前模型已切换。');
      emitModelProfilesChanged(profile);
      await refreshProfiles({ silent: true });
    }
    setBusy('');
  }

  async function handleTest() {
    const validation = validateModelConnectionDraft(draft, selectedProfile);
    if (!validation.ok) {
      const nextTest = { ok: false, status: validation.message, latencyMs: null };
      setLastTest(nextTest);
      setNotice(validation.message);
      return;
    }
    setBusy('test');
    try {
      const result = await testModelAdapterProfile({
        ...draft,
        imageModelName: draft.imageModelName.trim() || DEFAULT_IMAGE_MODEL_NAME
      });
      if (result?.unauthorized) {
        setNotice('登录状态已过期，请重新登录后测试连接。');
      } else if (result?.ok === false) {
        const nextTest = {
          ok: false,
          status: result.missing ? '模型测试服务未就绪' : sanitizeDisplayText(result.error, '连接失败'),
          latencyMs: result.latencyMs || null
        };
        setLastTest(nextTest);
        setNotice(nextTest.status);
      } else {
        const nextTest = {
          ok: true,
          status: result.status || '连接正常',
          latencyMs: Number.isFinite(Number(result.latencyMs)) ? Math.round(Number(result.latencyMs)) : null
        };
        setLastTest(nextTest);
        setNotice(result.preview ? '预览测速完成。' : '连接测试完成。');
        if (draft.id) {
          setProfiles((items) => items.map((profile) => (
            profile.id === draft.id
              ? { ...profile, status: nextTest.status, latencyMs: nextTest.latencyMs }
              : profile
          )));
        }
      }
    } catch (error) {
      const nextTest = { ok: false, status: sanitizeDisplayText(error?.message, '测速失败，请检查模型配置'), latencyMs: null };
      setLastTest(nextTest);
      setNotice(nextTest.status);
    } finally {
      setBusy('');
    }
  }

  async function handleImageTest() {
    const validation = validateModelConnectionDraft(draft, selectedProfile);
    if (!validation.ok) {
      const nextImageTest = { ok: false, status: validation.message, latencyMs: null, imageSrc: '' };
      setImageTest(nextImageTest);
      setNotice(validation.message);
      return;
    }
    setBusy('image');
    try {
      const result = await generateModelImagePreview({
        ...draft,
        imageModelName: draft.imageModelName.trim() || DEFAULT_IMAGE_MODEL_NAME
      });
      if (result?.unauthorized) {
        setNotice('登录状态已过期，请重新登录后测试图像模型。');
      } else if (result?.ok === false) {
        const nextImageTest = {
          ok: false,
          status: result.missing ? '图像生成服务未就绪' : sanitizeDisplayText(result.error, '图像生成失败'),
          latencyMs: result.latencyMs || null,
          imageSrc: ''
        };
        setImageTest(nextImageTest);
        setNotice(nextImageTest.status);
      } else {
        const nextImageTest = {
          ok: true,
          status: result.status || '图像生成完成',
          latencyMs: Number.isFinite(Number(result.latencyMs)) ? Math.round(Number(result.latencyMs)) : null,
          imageSrc: result.imageSrc || ''
        };
        setImageTest(nextImageTest);
        setNotice(result.preview ? '预览图像模型检查完成。' : '图像模型生成测试完成。');
      }
    } catch (error) {
      const nextImageTest = { ok: false, status: sanitizeDisplayText(error?.message, '图像测试失败，请检查模型配置'), latencyMs: null, imageSrc: '' };
      setImageTest(nextImageTest);
      setNotice(nextImageTest.status);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="modal-backdrop model-config-backdrop" role="presentation">
      <section className="model-config-modal" role="dialog" aria-modal="true" aria-label="模型配置">
        <header className="model-config-head">
          <div>
            <span>偏好设置 / 模型配置</span>
            <h3>模型配置</h3>
          </div>
          <button className="icon-button small" type="button" onClick={onClose} title="关闭">
            <X size={18} />
          </button>
        </header>

        <div className="model-current-strip">
          <Brain size={18} />
          <div>
            <span>当前模型</span>
            <strong>{modelProfilePrimaryLabel(getCurrentModelProfile(profiles))}</strong>
          </div>
          <em>{window.ecorex && hasEcorexFunction(['listModelProfiles']) ? '桌面配置' : '预览模式'}</em>
        </div>

        <div className="model-config-body">
          <aside className="model-profile-list" aria-label="模型配置列表">
            <button
              className="model-profile-new"
              type="button"
              onClick={() => {
                setDraft(modelProfileDraft({ modelName: initialModelName === '默认模型' ? '' : initialModelName }));
                setLastTest(null);
                setImageTest(null);
              }}
            >
              <Plus size={16} />
              新建配置
            </button>
            <div>
              {(profiles.length ? profiles : [normalizeModelProfile({ id: 'empty', name: '暂无配置', modelName: '待添加' })]).map((profile) => (
                <article
                  className={`model-profile-row ${profile.id === draft.id ? 'selected' : ''} ${profile.active ? 'active' : ''}`}
                  key={profile.id}
                >
                  <button
                    type="button"
                    onClick={() => {
                      if (profile.id === 'empty') return;
                      setDraft(modelProfileDraft(profile));
                      setLastTest(null);
                      setImageTest(null);
                    }}
                  >
                    <div>
                      <strong>{profile.name}</strong>
                      <span>{profile.modelName || '模型名称待填写'}</span>
                    </div>
                    <em>{profile.active ? '当前' : profile.status}</em>
                    <small>{profile.latencyMs ? `${profile.latencyMs} ms` : '未测速'}</small>
                  </button>
                  {profile.id !== 'empty' && (
                    <div className="model-row-actions">
                      <button type="button" onClick={() => handleActivate(profile)} disabled={profile.active || Boolean(busy)}>
                        {profile.active ? '已启用' : '切换'}
                      </button>
                      <button type="button" onClick={() => handleDelete(profile)} disabled={Boolean(busy)}>
                        <X size={13} />
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          </aside>

          <form className="model-config-form" onSubmit={(event) => { event.preventDefault(); handleSave(); }}>
            <div className="model-form-grid">
              <label>
                <span>配置名称</span>
                <input value={draft.name} onChange={(event) => updateDraft('name', event.target.value)} placeholder="例如：生产模型" />
              </label>
              <label>
                <span>模型名称</span>
                <input value={draft.modelName} onChange={(event) => updateDraft('modelName', event.target.value)} placeholder="例如：gpt-5.5 / sonnet" />
              </label>
              <label className="wide">
                <span>Base URL</span>
                <input value={draft.baseUrl} onChange={(event) => updateDraft('baseUrl', event.target.value)} placeholder="https://api.example.com/v1" />
              </label>
              <label>
                <span>API Key</span>
                <input
                  type="password"
                  value={draft.apiKey}
                  onChange={(event) => updateDraft('apiKey', event.target.value)}
                  placeholder={selectedProfile?.maskedKey || '留空则不更新已保存密钥'}
                  autoComplete="off"
                />
              </label>
              <label>
                <span>图像模型名称</span>
                <input value={draft.imageModelName} onChange={(event) => updateDraft('imageModelName', event.target.value)} placeholder="gpt-image-2" />
              </label>
            </div>

            <div className="model-test-grid">
              <div className={`model-test-result ${lastTest?.ok === false ? 'danger' : lastTest?.ok ? 'ok' : ''}`}>
                <Activity size={16} />
                <div>
                  <strong>{lastTest?.status || selectedProfile?.status || '等待模型测速'}</strong>
                  <span>{lastTest?.latencyMs || selectedProfile?.latencyMs ? `${lastTest?.latencyMs || selectedProfile?.latencyMs} ms` : '延迟待返回'}</span>
                </div>
              </div>
              <div className={`model-test-result image ${imageTest?.ok === false ? 'danger' : imageTest?.ok ? 'ok' : ''}`}>
                {imageTest?.imageSrc ? <img src={imageTest.imageSrc} alt="" /> : <Eye size={16} />}
                <div>
                  <strong>{imageTest?.status || '等待图像模型测试'}</strong>
                  <span>{imageTest?.latencyMs ? `${imageTest.latencyMs} ms · ${draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME}` : draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME}</span>
                </div>
              </div>
            </div>

            {notice && <p className="model-config-notice">{notice}</p>}

            <div className="model-config-actions">
              <button type="button" onClick={handleTest} disabled={busy === 'test'}>
                <Zap size={15} />
                {busy === 'test' ? '测试中' : '模型测速'}
              </button>
              <button type="button" onClick={handleImageTest} disabled={busy === 'image'}>
                <Eye size={15} />
                {busy === 'image' ? '生成中' : '图像测试'}
              </button>
              <button type="submit" disabled={!canSave || busy === 'save'}>
                <Check size={15} />
                {busy === 'save' ? '保存中' : '保存配置'}
              </button>
            </div>
          </form>
        </div>
      </section>
    </div>
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
  const taskLabel = sanitizeDisplayText(
    event?.task?.name || event?.taskName || '',
    ''
  );
  const labelMap = {
    status: taskLabel || '准备执行任务',
    tool: taskLabel || '调用本地工具',
    stderr: '记录执行日志',
    debug: '同步任务进度',
    assistant: '生成回复内容',
    result: '整理最终结果',
    done: '任务执行完成',
    cancelled: '任务已取消',
    error: '执行遇到异常'
  };
  const statusMap = {
    status: event.status || event.state || '进行中',
    tool: event.status || event.state || '工具调用',
    stderr: '日志',
    debug: event.status || event.state || '同步中',
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
    debug: 'pending',
    assistant: 'running',
    result: 'success',
    done: 'success',
    cancelled: 'pending',
    error: 'danger'
  };

  return [
    sanitizeDisplayText(labelMap[event.kind] || taskLabel || '执行步骤', '执行步骤').slice(0, 120),
    sanitizeDisplayText(statusMap[event.kind] || event.status || event.state || '进行中', '进行中').slice(0, 40),
    formatAgentEventTime(event),
    toneMap[event.kind] || 'running'
  ];
}

function appendTimeline(timeline = [], item, limit = 80) {
  return [...timeline, item].slice(-limit);
}

function appendTimelineItems(timeline = [], items = [], limit = 80) {
  if (!items.length) return timeline;
  return [...timeline, ...items].slice(-limit);
}

function normalizeAgentEventKind(kind) {
  const normalized = String(kind || '').toLowerCase();
  const aliases = {
    complete: 'done',
    completed: 'done',
    final: 'result',
    output: 'assistant',
    message: 'assistant',
    text: 'assistant',
    cancel: 'cancelled',
    canceled: 'cancelled',
    failure: 'error'
  };
  return aliases[normalized] || normalized || 'status';
}

function normalizeAgentEventPayload(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload.flatMap((item) => normalizeAgentEventPayload(item));
  const source = Array.isArray(payload.events)
    ? payload.events
    : Array.isArray(payload.items)
      ? payload.items
      : Array.isArray(payload.data)
        ? payload.data
        : null;
  if (source) {
    const events = source.flatMap((item) => normalizeAgentEventPayload(item));
    const dropped = Number(payload.dropped || payload.droppedCount || 0);
    if (dropped > 0) {
      events.push({
        sessionId: payload.sessionId || events[0]?.sessionId,
        kind: 'status',
        status: 'dropped',
        text: `事件流压力过高，已压缩 ${dropped} 条低优先级状态。`,
        dropped
      });
    }
    return events;
  }
  return [payload];
}

function normalizeAgentEvent(raw = {}, sequence = 0) {
  const sessionId = raw.sessionId || raw.session_id || raw.session?.id || raw.runId || raw.run_id || raw.id;
  if (!sessionId) return null;
  return {
    ...raw,
    sessionId,
    kind: normalizeAgentEventKind(raw.kind || raw.type || raw.event),
    text: raw.text || raw.content || raw.message || raw.delta || '',
    __seq: raw.__seq || sequence,
    __queuedAt: raw.__queuedAt || Date.now()
  };
}

function compactAgentEventQueue(queue, limit = AGENT_EVENT_QUEUE_LIMIT) {
  if (queue.length <= limit) return queue;
  const overflow = queue.length - limit;
  const recent = queue.slice(overflow);
  const preserved = queue
    .slice(0, overflow)
    .filter((event) => event.kind === 'assistant' || event.kind === 'result' || AGENT_EVENT_TERMINAL_KINDS.has(event.kind))
    .slice(-Math.floor(limit / 3));
  return [...preserved, ...recent].sort((left, right) => (left.__seq || 0) - (right.__seq || 0));
}

function compactTimelineEvents(events = []) {
  const keep = new Map();
  const nonAssistant = events.filter((event) => event.kind !== 'assistant').slice(-AGENT_TIMELINE_BATCH_LIMIT);
  for (const event of nonAssistant) keep.set(event.__seq || `${event.kind}-${keep.size}`, event);
  const lastAssistant = [...events].reverse().find((event) => event.kind === 'assistant');
  if (lastAssistant) keep.set(lastAssistant.__seq || 'assistant', lastAssistant);
  for (const event of events) {
    if (event.kind === 'result' || AGENT_EVENT_TERMINAL_KINDS.has(event.kind)) {
      keep.set(event.__seq || `${event.kind}-${keep.size}`, event);
    }
  }
  return [...keep.values()].sort((left, right) => (left.__seq || 0) - (right.__seq || 0));
}

function mergeAssistantText(existing = '', incoming = '') {
  if (!incoming) return existing;
  if (!existing) return incoming;
  if (incoming.startsWith(existing)) return incoming;
  if (existing.endsWith(incoming)) return existing;
  return `${existing}${incoming}`;
}

function createInitialMessages() {
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  return [
    {
      id: 'assistant-welcome',
      role: 'assistant',
      text: window.ecorex
        ? 'EcoreX 亦芯已就绪。发送任务后，我会在回复内展示真实执行步骤、能力调用和最终结果。'
        : '当前为浏览器预览模式，本地能力服务未就绪。界面可预览，真实任务需要使用桌面端启动。',
      status: window.ecorex ? 'complete' : 'error',
      error: !window.ecorex,
      time,
      timeline: [[
        window.ecorex ? '等待用户提交任务' : '本地能力服务未就绪',
        window.ecorex ? '就绪' : '离线',
        time,
        window.ecorex ? 'success' : 'warn'
      ]]
    }
  ];
}

function ChatView({ backendStatus, backendError, capabilities, refreshBackend, onUnauthorized, setPage }) {
  const [prompt, setPrompt] = useState('');
  const [running, setRunning] = useState(false);
  const [permissionMode, setPermissionMode] = useState(() => readStoredDefaultAccessMode());
  const [model, setModel] = useState('sonnet');
  const [modelProfiles, setModelProfiles] = useState([]);
  const [railExpanded, setRailExpanded] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [visibleMessageCount, setVisibleMessageCount] = useState(MESSAGE_WINDOW_SIZE);
  const [messages, setMessages] = useState(createInitialMessages);
  const [timeline, setTimeline] = useState(initialTimeline);
  const [runningSessions, setRunningSessions] = useState([]);
  const sessionMap = useRef(new Map());
  const runningRef = useRef(false);
  const runningSessionsRef = useRef(new Set());
  const runningSessionRowsRef = useRef([]);
  const currentSessionIdRef = useRef(null);
  const eventQueueRef = useRef([]);
  const eventSeqRef = useRef(0);
  const flushTimerRef = useRef(null);
  const pendingCancelsRef = useRef(new Set());
  const statusTimers = useRef([]);

  const selectedPlugins = useMemo(() => {
    return ['feature-dev', 'code-review', 'security-guidance', 'plugin-dev'];
  }, [capabilities]);

  const permissionOptions = useMemo(() => {
    return permissionOptionsForUi();
  }, [capabilities]);

  const modelOptions = useMemo(() => {
    const options = new Map();
    for (const profile of modelProfiles) {
      if (!profile.modelName) continue;
      options.set(profile.modelName, [
        profile.modelName,
        profile.active ? `${profile.modelName} · 当前` : (profile.name ? `${profile.name} · ${profile.modelName}` : profile.modelName)
      ]);
    }
    const models = capabilities?.models;
    if (Array.isArray(models) && models.length) {
      for (const item of models) {
        if (!item?.value) continue;
        if (!options.has(item.value)) options.set(item.value, [item.value, item.label || item.value]);
      }
    }
    for (const option of [
      ['sonnet', 'Sonnet'],
      ['opus', 'Opus']
    ]) {
      if (!options.has(option[0])) options.set(option[0], option);
    }
    return Array.from(options.values());
  }, [capabilities, modelProfiles]);

  const visibleMessages = useMemo(() => messages.slice(-visibleMessageCount), [messages, visibleMessageCount]);
  const hiddenMessageCount = Math.max(messages.length - visibleMessages.length, 0);

  function commitRunningSessionRows(updater) {
    const currentRows = runningSessionRowsRef.current;
    const nextRows = typeof updater === 'function' ? updater(currentRows) : updater;
    const activeRows = (Array.isArray(nextRows) ? nextRows : []).filter(isRunningSessionActive);
    runningSessionRowsRef.current = activeRows;
    setRunningSessions(activeRows);
    syncRunningSessions(activeRows);
    return activeRows;
  }

  function syncRunningSessions(rows = runningSessionRowsRef.current) {
    const hasRunningSessions = runningSessionsRef.current.size > 0 || rows.some(isRunningSessionActive);
    runningRef.current = hasRunningSessions;
    setRunning(hasRunningSessions);
  }

  function trackSession(sessionId, meta = {}) {
    if (!sessionId) return;
    runningSessionsRef.current.add(sessionId);
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    commitRunningSessionRows((rows) => mergeRunningSessionRows(rows, [
      normalizeRunningSession({
        sessionId,
        id: sessionId,
        status: 'running',
        state: 'running',
        prompt: meta.prompt,
        promptPreview: meta.prompt,
        messageId: meta.messageId,
        accessMode: meta.accessMode || permissionMode,
        permissionMode: permissionModeFromAccessMode(meta.accessMode || permissionMode),
        startedAt: Date.now(),
        updatedAt: Date.now()
      }, rows.length, meta.source || 'local')
    ]));
  }

  function transferSession(previousSessionId, nextSessionId) {
    if (!nextSessionId || previousSessionId === nextSessionId) return;
    runningSessionsRef.current.delete(previousSessionId);
    runningSessionsRef.current.add(nextSessionId);
    const messageId = sessionMap.current.get(previousSessionId);
    if (messageId) {
      sessionMap.current.delete(previousSessionId);
      sessionMap.current.set(nextSessionId, messageId);
    }
    currentSessionIdRef.current = nextSessionId;
    setCurrentSessionId(nextSessionId);
    commitRunningSessionRows((rows) => rows.map((row) => (
      row.sessionId === previousSessionId
        ? {
            ...row,
            sessionId: nextSessionId,
            id: nextSessionId,
            messageId: row.messageId || messageId,
            updatedAt: Date.now()
          }
        : row
    )));
  }

  function finishSession(sessionId) {
    runningSessionsRef.current.delete(sessionId);
    sessionMap.current.delete(sessionId);
    const nextRows = commitRunningSessionRows((rows) => rows.filter((row) => row.sessionId !== sessionId));
    if (currentSessionIdRef.current === sessionId) {
      const nextSessionId = Array.from(runningSessionsRef.current).at(-1) || nextRows[0]?.sessionId || null;
      currentSessionIdRef.current = nextSessionId;
      setCurrentSessionId(nextSessionId);
    }
  }

  function applyAgentEvents(events) {
    if (!events.length) return false;
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
      eventQueueRef.current = compactAgentEventQueue([...pendingEvents, ...eventQueueRef.current]);
    }

    if (!relevantEvents.length) return pendingEvents.length > 0;

    const eventsByMessage = new Map();
    for (const event of relevantEvents) {
      const messageId = sessionMap.current.get(event.sessionId);
      if (!messageId) continue;
      const items = eventsByMessage.get(messageId) || [];
      items.push(event);
      eventsByMessage.set(messageId, items);
    }

    commitRunningSessionRows((rows) => mergeRunningSessionRows(
      rows,
      relevantEvents
        .filter((event) => !AGENT_EVENT_TERMINAL_KINDS.has(event.kind))
        .map((event, index) => normalizeRunningSession({
          sessionId: event.sessionId,
          status: event.kind === 'error' ? 'error' : 'running',
          state: event.kind,
          prompt: rows.find((row) => row.sessionId === event.sessionId)?.prompt,
          messageId: sessionMap.current.get(event.sessionId),
          updatedAt: Date.now()
        }, index, 'local'))
    ));

    setMessages((items) => {
      return items.map((item) => {
        const messageEvents = eventsByMessage.get(item.id);
        if (!messageEvents?.length) return item;
        const timelineItems = compactTimelineEvents(messageEvents).map((event) => timelineItemFromAgentEvent(event));
        let nextItem = {
          ...item,
          timeline: appendTimelineItems(item.timeline || [], timelineItems)
        };

        for (const event of messageEvents) {
          if (['status', 'tool', 'stderr', 'debug'].includes(event.kind)) {
            nextItem = {
              ...nextItem,
              status: nextItem.status === 'generating' ? 'generating' : 'thinking',
              backendStatus: event.status || event.kind
            };
            continue;
          }

          if (event.kind === 'assistant') {
            nextItem = {
              ...nextItem,
              text: mergeAssistantText(nextItem.text, event.text),
              streaming: true,
              status: 'generating'
            };
            continue;
          }

          if (event.kind === 'result') {
            nextItem = {
              ...nextItem,
              text: event.text || nextItem.text,
              streaming: false,
              status: 'complete',
              meta: event.costUsd
                ? `成本 $${Number(event.costUsd).toFixed(4)} · ${Math.round((event.durationMs || 0) / 1000)} 秒`
                : nextItem.meta || ''
            };
            continue;
          }

          if (event.kind === 'done') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'complete',
              error: false,
              text: nextItem.text || event.text
            };
            continue;
          }

          if (event.kind === 'cancelled') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'cancelled',
              error: false,
              text: nextItem.text || event.text || agentRecoveryText(event)
            };
            continue;
          }

          if (event.kind === 'timeout') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'timeout',
              error: true,
              text: nextItem.text || event.text || agentRecoveryText(event)
            };
            continue;
          }

          if (event.kind === 'error') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'error',
              error: true,
              text: nextItem.text || event.text || agentRecoveryText(event)
            };
          }
        }

        return nextItem;
      });
    });

    setTimeline((items) => appendTimelineItems(
      items,
      compactTimelineEvents(relevantEvents).map((event) => timelineItemFromAgentEvent(event))
    ));

    relevantEvents
      .filter((event) => AGENT_EVENT_TERMINAL_KINDS.has(event.kind))
      .forEach((event) => finishSession(event.sessionId));

    return pendingEvents.length > 0;
  }

  function scheduleAgentFlush(delay = AGENT_EVENT_FLUSH_DELAY_MS) {
    if (!flushTimerRef.current) {
      flushTimerRef.current = setTimeout(flushAgentEvents, delay);
    }
  }

  function flushAgentEvents() {
    flushTimerRef.current = null;
    const events = eventQueueRef.current.splice(0, AGENT_EVENT_FLUSH_BATCH);
    const hasPending = applyAgentEvents(events);
    if (eventQueueRef.current.length || hasPending) {
      scheduleAgentFlush(eventQueueRef.current.length ? AGENT_EVENT_FLUSH_DELAY_MS : AGENT_EVENT_PENDING_DELAY_MS);
    }
  }

  function queueAgentEvents(payload) {
    const events = normalizeAgentEventPayload(payload)
      .map((event) => normalizeAgentEvent(event, ++eventSeqRef.current))
      .filter(Boolean);
    if (!events.length) return;
    eventQueueRef.current = compactAgentEventQueue([...eventQueueRef.current, ...events]);
    scheduleAgentFlush();
  }

  async function refreshAgentSessions() {
    if (!window.ecorex?.getAgentSessions) return;
    const result = await callEcorex(['getAgentSessions', 'agent.getSessions']);
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false && result.missing) return;
    if (result?.ok === false) return;

    const recoveredMessages = [];
    const backendRows = extractAgentSessionRows(result).map((row, index) => {
      const sessionId = row.sessionId || row.id;
      let messageId = row.messageId || sessionMap.current.get(sessionId);
      if (sessionId && !messageId) {
        messageId = `assistant-resumed-${sessionId}`;
        sessionMap.current.set(sessionId, messageId);
        recoveredMessages.push({
          id: messageId,
          role: 'assistant',
          text: '正在恢复本地运行会话，新的输出会继续追加到这里。',
          time: formatAgentEventTime(row),
          streaming: true,
          status: row.status === 'error' ? 'error' : 'thinking',
          sessionId,
          originalPrompt: row.prompt || row.promptPreview || '',
          timeline: [[
            `恢复运行会话 ${index + 1}`,
            formatSessionStatus(row.status || row.state || 'running'),
            formatAgentEventTime(row),
            row.status === 'error' ? 'danger' : 'running'
          ]]
        });
      } else if (sessionId && messageId) {
        sessionMap.current.set(sessionId, messageId);
      }
      return {
        ...row,
        source: 'api',
        messageId
      };
    });
    if (recoveredMessages.length) {
      setMessages((items) => {
        const existing = new Set(items.map((item) => item.id));
        const nextRecovered = recoveredMessages.filter((item) => !existing.has(item.id));
        return nextRecovered.length ? [...items, ...nextRecovered] : items;
      });
    }
    const nextRows = commitRunningSessionRows((rows) => mergeRunningSessionRows(rows, backendRows, { replaceSources: ['api'] }));
    if (!currentSessionIdRef.current && nextRows.length) {
      currentSessionIdRef.current = nextRows[0].sessionId;
      setCurrentSessionId(nextRows[0].sessionId);
    }
  }

  useEffect(() => {
    if (!window.ecorex) return undefined;
    if (typeof window.ecorex.onAgentEvents === 'function') {
      try {
        return window.ecorex.onAgentEvents((events) => queueAgentEvents(events));
      } catch {
        // Fall through to the single-event bridge when the desktop side exposes the older contract.
      }
    }
    if (typeof window.ecorex.onAgentEvent === 'function') {
      return window.ecorex.onAgentEvent((event) => queueAgentEvents(event));
    }
    return undefined;
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadDefaultPermission() {
      if (!window.ecorex) return;
      const result = await callEcorex(['getSettings', 'settings.get']);
      if (cancelled || result?.ok === false || result?.unauthorized) return;
      const settings = result?.settings || result;
      const nextAccessMode = normalizeAccessMode(settings?.defaultPermissionMode || settings?.permissionMode || settings?.accessMode);
      storeDefaultAccessMode(nextAccessMode);
      setPermissionMode(nextAccessMode);
    }
    loadDefaultPermission();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function refreshModelProfiles() {
      const result = await loadModelProfiles(model || 'sonnet');
      if (cancelled || result?.unauthorized) return;
      const profiles = result.profiles || [];
      const current = getCurrentModelProfile(profiles);
      setModelProfiles(profiles);
      if (current?.modelName) setModel(current.modelName);
    }
    refreshModelProfiles();
    const listener = () => refreshModelProfiles();
    window.addEventListener?.('ecorex:model-profiles-changed', listener);
    return () => {
      cancelled = true;
      window.removeEventListener?.('ecorex:model-profiles-changed', listener);
    };
  }, []);

  useEffect(() => {
    refreshAgentSessions({ silent: true });
    if (!window.ecorex?.getAgentSessions) return undefined;
    const timer = setInterval(() => refreshAgentSessions({ silent: true }), running ? 3000 : 7000);
    return () => clearInterval(timer);
  }, [running]);

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
    if (!cleanPrompt) return;

    const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const userId = `user-${Date.now()}`;
    const assistantId = `assistant-${Date.now()}`;
    const requestedSessionId = window.crypto?.randomUUID?.() || `session-${Date.now()}`;
    const submitted = ['提交用户任务到本地能力', '进行中', now, 'running'];
    const accessMode = normalizeAccessMode(permissionMode);
    const requestedPermissionMode = permissionModeFromAccessMode(accessMode);
    sessionMap.current.set(requestedSessionId, assistantId);
    trackSession(requestedSessionId, {
      messageId: assistantId,
      prompt: cleanPrompt,
      accessMode,
      source: 'local'
    });

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
                  text: '当前运行在浏览器预览模式，本地能力服务未就绪。请使用桌面端启动真实任务。',
                  timeline: appendTimeline(item.timeline || [], ['本地能力服务未就绪', '离线', formatAgentEventTime(), 'warn'])
                }
              : item
          )
        );
        finishSession(requestedSessionId);
      }, 800);
      statusTimers.current.push(timer);
      return;
    }

    try {
      const result = await window.ecorex.runPrompt({
        sessionId: requestedSessionId,
        prompt: cleanPrompt,
        accessMode,
        permissionMode: requestedPermissionMode,
        defaultPermissionMode: requestedPermissionMode,
        ...fullAccessConfirmationFields(accessMode),
        model,
        plugins: selectedPlugins
      });

      if (!result.ok) {
        const unauthorized = Boolean(result.unauthorized);
        if (unauthorized) onUnauthorized?.();
        finishSession(requestedSessionId);
        setMessages((items) =>
          items.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                  streaming: false,
                  error: true,
                  status: result.status === 'timeout' ? 'timeout' : result.status === 'cancelled' ? 'cancelled' : 'error',
                  text: unauthorized ? '登录状态已过期，请重新登录后继续。' : agentRunFailureMessage(result),
                  timeline: appendTimeline(item.timeline || [], [
                    result.code === 'too-many-sessions' ? '运行会话已达上限' : '本地执行启动失败',
                    formatSessionStatus(result.status || result.code || 'failed'),
                    formatAgentEventTime(),
                    'danger'
                  ])
                }
              : item
          )
        );
        return;
      }

      const sessionId = result.sessionId || requestedSessionId;
      if (sessionId !== requestedSessionId) {
        transferSession(requestedSessionId, sessionId);
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
      if (result.initialEvent) queueAgentEvents(result.initialEvent);
    } catch (error) {
      const unauthorized = isUnauthorizedError(error);
      if (unauthorized) {
        onUnauthorized?.();
      }
      if (pendingCancelsRef.current.has(requestedSessionId)) {
        pendingCancelsRef.current.delete(requestedSessionId);
        finishSession(requestedSessionId);
        return;
      }
      finishSession(requestedSessionId);
      setMessages((items) =>
        items.map((item) =>
          item.id === assistantId
            ? {
                ...item,
                streaming: false,
                error: true,
                status: 'error',
                text: unauthorized ? '登录状态已过期，请重新登录后继续。' : agentRunFailureMessage({ error: error?.message }),
                timeline: appendTimeline(item.timeline || [], [
                  unauthorized ? '登录状态已过期' : '本地执行启动失败',
                  '失败',
                  formatAgentEventTime(),
                  'danger'
                ])
              }
            : item
        )
      );
    }
  }

  async function cancelPrompt(sessionId = currentSessionIdRef.current) {
    if (!sessionId) return;
    pendingCancelsRef.current.add(sessionId);
    const messageId = sessionMap.current.get(sessionId) || runningSessionRowsRef.current.find((row) => row.sessionId === sessionId)?.messageId;
    setMessages((items) =>
      items.map((item) =>
        item.id === messageId
          ? {
              ...item,
              streaming: false,
              status: 'cancelled',
              text: item.text || '当前任务已取消。',
              timeline: appendTimeline(item.timeline || [], ['用户取消当前任务', '已取消', formatAgentEventTime(), 'pending'])
            }
          : item
      )
    );
    setTimeline((items) => appendTimeline(items, ['用户取消当前任务', '已取消', formatAgentEventTime(), 'pending']));
    if (window.ecorex?.stopPrompt) {
      try {
        const result = await window.ecorex.stopPrompt(sessionId);
        if (result?.ok === false && result.reason === 'not-found') {
          finishSession(sessionId);
        } else {
          const fallbackTimer = setTimeout(() => {
            if (sessionMap.current.has(sessionId)) finishSession(sessionId);
          }, 5000);
          statusTimers.current.push(fallbackTimer);
        }
      } catch (error) {
        if (isUnauthorizedError(error)) onUnauthorized?.();
        // The backend may still be starting; sendPrompt will stop it after the session id is acknowledged.
      } finally {
        pendingCancelsRef.current.delete(sessionId);
      }
    } else {
      pendingCancelsRef.current.delete(sessionId);
      finishSession(sessionId);
    }
  }

  function retryMessage(message) {
    if (message?.originalPrompt) sendPrompt(message.originalPrompt);
  }

  function changePermissionMode(value) {
    const accessMode = normalizeAccessMode(value);
    if (accessMode === 'fullAccess' && normalizeAccessMode(permissionMode) !== 'fullAccess' && !confirmFullAccessChange()) {
      return;
    }
    storeDefaultAccessMode(accessMode);
    setPermissionMode(accessMode);
  }

  function selectRunningSession(sessionId) {
    if (!sessionId) return;
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    const row = runningSessionRowsRef.current.find((item) => item.sessionId === sessionId);
    const messageId = row?.messageId || sessionMap.current.get(sessionId);
    if (!messageId) return;
    const messageIndex = messages.findIndex((message) => message.id === messageId);
    if (messageIndex >= 0) {
      setVisibleMessageCount((count) => Math.max(count, messages.length - messageIndex));
    }
    window.setTimeout(() => {
      document.getElementById(`message-${messageId}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 80);
  }

  return (
    <div className={`chat-layout ${railExpanded ? 'rail-expanded' : 'rail-collapsed'}`}>
      <section className="chat-main panel">
        <HeaderBar
          title="EcoreX"
          badge="亦芯助手"
          subtitle="面向碳排放、ESG 披露、能耗管理与减排项目协同的自主思考型助手"
          backendStatus={backendStatus}
          onRefresh={refreshBackend}
        />

        <div className="messages">
          {!window.ecorex && (
            <ChatSystemNotice
              tone="warn"
              title="桌面服务未连接"
              text="当前只展示前端预览。真实任务运行和本地能力管理需要桌面端服务。"
            />
          )}
          {window.ecorex && backendError && (
            <ChatSystemNotice
              tone="error"
              title="服务状态异常"
              text={backendError}
            />
          )}
          {window.ecorex && backendStatus?.ok === false && !backendError && (
            <ChatSystemNotice
              tone="warn"
              title="服务待连接"
              text={sanitizeDisplayText(backendStatus?.error, '服务状态暂不可用，发送任务可能失败。')}
            />
          )}
          <RunningSessionStrip
            sessions={runningSessions}
            currentSessionId={currentSessionId}
            onOpenSessions={() => setPage('diagnostics')}
            onSelect={selectRunningSession}
            onStop={cancelPrompt}
          />
          {hiddenMessageCount > 0 && (
            <button
              className="show-history"
              type="button"
              onClick={() => setVisibleMessageCount((count) => count + MESSAGE_WINDOW_STEP)}
            >
              显示更多历史（还有 {hiddenMessageCount} 条）
            </button>
          )}
          {visibleMessages.map((message) => (
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
          setPermissionMode={changePermissionMode}
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
          onUnauthorized={onUnauthorized}
          onOpenProjects={() => setPage('projects')}
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

function ChatSystemNotice({ title, text, tone = 'warn' }) {
  return (
    <div className={`chat-system-note ${tone}`}>
      <AlertTriangle size={15} />
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function SessionRecoveryNotice({ onOpenSessions }) {
  return (
    <div className="session-recovery-note">
      <Clock3 size={15} />
      <strong>任务正在运行</strong>
      <span>关闭或重启窗口后，可在运行会话中继续查看进度。</span>
      <button type="button" onClick={onOpenSessions}>查看</button>
    </div>
  );
}

function RunningSessionStrip({ sessions = [], currentSessionId, onSelect, onStop, onOpenSessions }) {
  const activeSessions = sessions.filter(isRunningSessionActive);
  if (!activeSessions.length) return null;

  return (
    <div className="running-session-strip">
      <div className="running-session-head">
        <span>
          <Clock3 size={15} />
          <strong>运行会话</strong>
          <em>{activeSessions.length} 个</em>
        </span>
        <button type="button" onClick={onOpenSessions}>查看全部</button>
      </div>
      <div className="running-session-list-compact">
        {activeSessions.map((session, index) => {
          const active = session.sessionId === currentSessionId;
          return (
            <div className={`running-session-pill ${active ? 'active' : ''}`} key={session.sessionId}>
              <button type="button" onClick={() => onSelect?.(session.sessionId)} title="切换查看">
                <span className={`dot ${session.tone === 'danger' ? 'warn' : session.tone === 'running' ? 'running-dot' : 'ok'}`} />
                <strong>{session.title || `会话 ${index + 1}`}</strong>
                <em>{session.prompt || '正在执行任务'}</em>
              </button>
              <button
                className="session-stop"
                type="button"
                title={active ? '停止当前会话' : '停止此会话'}
                onClick={() => onStop?.(session.sessionId)}
              >
                <Pause size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function HeaderBar({ title, badge, subtitle, backendStatus, onRefresh }) {
  const [refreshNotice, setRefreshNotice] = useState('');
  const lastRefreshAt = useRef(0);
  const noticeTimer = useRef(null);

  useEffect(() => () => clearTimeout(noticeTimer.current), []);

  function showRefreshNotice(text) {
    setRefreshNotice(text);
    clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setRefreshNotice(''), 2200);
  }

  function handleRefresh() {
    if (!onRefresh) return;
    const now = Date.now();
    if (now - lastRefreshAt.current < 3500) {
      showRefreshNotice('刚刚刷新过，稍后再试。');
      return;
    }
    lastRefreshAt.current = now;
    showRefreshNotice('正在更新状态...');
    try {
      Promise.resolve(onRefresh({ refresh: true })).finally(() => showRefreshNotice('状态已更新。'));
    } catch {
      showRefreshNotice('状态暂未更新。');
    }
  }

  return (
    <header className="view-header">
      <div>
        <h1>
          {title} {badge && <span>{badge}</span>}
        </h1>
        <p>{subtitle}</p>
      </div>
      <div className="header-actions">
        {refreshNotice && <span className="header-toast">{refreshNotice}</span>}
        <button className="icon-button" type="button" title="刷新状态" onClick={handleRefresh}>
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
  const [expanded, setExpanded] = useState(false);
  if (message.role === 'user') {
    return (
      <div className="user-row" id={`message-${message.id}`}>
        <div className="user-bubble">
          <span>{message.text}</span>
          <MessageStatus status={message.status || 'read'} time={message.time} compact />
        </div>
        <div className="avatar user-avatar">张</div>
      </div>
    );
  }

  const rawText = message.text || (message.streaming ? '正在连接本地能力...' : '');
  const shouldCollapse = message.role === 'assistant' && rawText.length > ASSISTANT_COLLAPSE_CHARS;
  const displayText = shouldCollapse && !expanded
    ? `${rawText.slice(0, ASSISTANT_COLLAPSE_CHARS)}...`
    : rawText;

  return (
    <div className={`assistant-row ${message.error ? 'error' : ''}`} id={`message-${message.id}`}>
      <Logo compact />
      <div className="assistant-card">
        <div className="assistant-head">
          <span className="time">{message.time}</span>
          <MessageStatus status={message.status || (message.streaming ? 'thinking' : 'complete')} compact />
        </div>
        <p className={shouldCollapse && !expanded ? 'assistant-text collapsed' : 'assistant-text'}>{displayText}</p>
        {shouldCollapse && (
          <button className="text-expand" type="button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起长回复' : `展开全文（${rawText.length.toLocaleString('zh-CN')} 字符）`}
          </button>
        )}
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
  const selectedPermission = permissionOptionByValue(permissionMode);
  const fullAccessSelected = selectedPermission.value === 'fullAccess';

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    const minHeight = 28;
    const maxHeight = 112;
    textarea.style.height = `${minHeight}px`;
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, minHeight), maxHeight);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }, [prompt]);

  return (
    <div className="composer" data-testid="chat-composer">
      <textarea
        data-testid="chat-input"
        ref={textareaRef}
        value={prompt}
        onChange={(event) => setPrompt(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') sendPrompt();
        }}
        placeholder="输入碳排、ESG、能耗或项目协同问题，也可以使用 / 调用亦芯能力..."
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
          <PermissionSelect
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
            data-testid="chat-send-button"
            title={running ? '停止当前会话' : '发送'}
            type="button"
            onClick={() => (running && currentSessionId ? cancelPrompt() : sendPrompt())}
          >
            {running ? <Pause size={22} /> : <Send size={24} />}
          </button>
        </div>
      </div>
      {fullAccessSelected ? (
        <div className="permission-inline-note warn">
          <AlertTriangle size={14} />
          <span>当前会话将使用完全访问权限，本地执行确认会被跳过，仅适合可信工作区。</span>
          <button type="button" onClick={() => setPermissionMode('default')}>
            撤销
          </button>
        </div>
      ) : (
        <div className="permission-inline-note safe">
          <ShieldCheck size={14} />
          <span>默认权限会在文件写入、命令执行和系统目录访问前继续确认。</span>
        </div>
      )}
    </div>
  );
}

function PermissionSelect({ value, onChange, options }) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === normalizeAccessMode(value)) || DEFAULT_PERMISSION_OPTION;

  return (
    <div
      className={`permission-select ${open ? 'open' : ''} ${selected.value === 'fullAccess' ? 'full' : 'default'}`}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === 'Escape') setOpen(false);
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((next) => !next)}
        title={`${selected.label}：${selected.description}`}
      >
        {selected.value === 'fullAccess' ? <AlertTriangle size={15} /> : <ShieldCheck size={15} />}
        <span>{selected.label}</span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="permission-select-menu" role="menu">
          {options.map((option) => (
            <button
              className={option.value === selected.value ? 'active' : ''}
              key={option.value}
              role="menuitemradio"
              aria-checked={option.value === selected.value}
              title={option.description}
              type="button"
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              {option.value === 'fullAccess' ? <AlertTriangle size={15} /> : <ShieldCheck size={15} />}
              <span>
                <strong>{option.label}</strong>
                <em>{option.description}</em>
              </span>
              {option.value === selected.value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
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

function ProjectCard({ backendStatus, expanded = true, onToggle, onUnauthorized, onOpenProjects }) {
  const [projectState, setProjectState] = useState({
    apiReady: false,
    loading: false,
    projects: [],
    currentProject: null,
    status: '项目服务未就绪',
    notice: ''
  });
  const [busyProject, setBusyProject] = useState('');

  async function refreshProjects({ silent = false } = {}) {
    if (!silent) setProjectState((current) => ({ ...current, loading: true, notice: '' }));
    const result = await loadProjectState();
    if (result.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({
        ...current,
        loading: false,
        apiReady: true,
        status: '登录状态已过期',
        notice: '请重新登录后查看项目工作区。'
      }));
      return;
    }
    setProjectState({
      apiReady: !result.missing,
      loading: false,
      projects: result.projects || [],
      currentProject: result.currentProject || null,
      status: result.status || (result.missing ? '项目服务未就绪' : '等待项目'),
      notice: result.missing ? '项目服务未就绪' : ''
    });
  }

  async function handleSwitchProject(project) {
    if (!project?.id || project.current || project.archived || !projectState.apiReady) return;
    setBusyProject(project.id);
    const result = await switchManagedProject(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后切换项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目切换失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: '当前项目已切换。' }));
      await refreshProjects({ silent: true });
    }
    setBusyProject('');
  }

  useEffect(() => {
    refreshProjects({ silent: true });
  }, [backendStatus?.ok]);

  const currentProject = projectState.currentProject;
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
        <div className="project-header-actions">
          <button type="button" onClick={() => refreshProjects()} disabled={projectState.loading}>
            <Loader2 size={13} className={projectState.loading ? 'spin-icon' : ''} />
          </button>
          <button type="button" onClick={onOpenProjects} title="打开项目页">管理</button>
          <button type="button" onClick={onToggle}>收起 <ChevronRight size={14} /></button>
        </div>
      </header>
      <div className={`project-body ${projectState.apiReady ? 'ready' : 'offline'}`}>
        <div className="project-icon"><LayoutDashboard size={30} /></div>
        <div>
          <strong>{currentProject?.name || '项目工作区未连接'}</strong>
          <span>{projectState.apiReady ? (currentProject ? projectBusinessSummary(currentProject) : projectState.status || '等待选择项目') : '项目服务未就绪'}</span>
        </div>
      </div>
      <div className="project-metrics">
        <span><strong>{currentProject ? currentProject.fileCount : '-'}</strong><em>项目文件</em></span>
        <span><strong>{currentProject ? currentProject.statusLabel : '-'}</strong><em>状态</em></span>
        <span><strong>{currentProject?.updatedAt ? formatDateTime(currentProject.updatedAt) : '-'}</strong><em>更新时间</em></span>
      </div>
      <div className="project-meta-mini">
        <span title={currentProject?.client || '客户待补充'}><Building2 size={13} />{currentProject?.client || '客户待补充'}</span>
        <span title={currentProject?.goal || '目标待补充'}><Target size={13} />{currentProject?.goal || '目标待补充'}</span>
        <span title={[currentProject?.budget, currentProject?.period].filter(Boolean).join(' / ') || '预算周期待补充'}>
          <Clock3 size={13} />{[currentProject?.budget, currentProject?.period].filter(Boolean).join(' / ') || '预算周期待补充'}
        </span>
      </div>
      <div className="project-list-mini">
        {(projectState.projects.length ? projectState.projects.slice(0, 3) : [{ id: 'empty', name: projectState.apiReady ? '暂无项目' : '项目服务未就绪', status: projectState.status }]).map((project) => (
          <button
            className={`${project.current ? 'active' : ''} ${project.archived ? 'archived' : ''}`}
            key={project.id}
            type="button"
            onClick={() => handleSwitchProject(project)}
            disabled={!projectState.apiReady || project.id === 'empty' || project.archived || busyProject === project.id}
          >
            <span>{project.name}</span>
            <em>{project.current ? '当前' : sanitizeDisplayText(project.statusLabel || project.status, project.archived ? '已归档' : '可切换')}</em>
          </button>
        ))}
      </div>
      {projectState.notice && <p className="project-card-note">{projectState.notice}</p>}
      {map?.available && <small>能力索引 {map.sizeMb}MB · {map.sourceCount || '多'} 组内容</small>}
    </section>
  );
}

function ProjectsView({ backendStatus, refreshBackend, onUnauthorized, setPage }) {
  const [projectState, setProjectState] = useState({
    apiReady: false,
    loading: false,
    projects: [],
    currentProject: null,
    status: '项目服务未就绪',
    notice: ''
  });
  const [createDraft, setCreateDraft] = useState(() => emptyProjectDraft());
  const [editDraft, setEditDraft] = useState(() => emptyProjectDraft());
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [busy, setBusy] = useState('');

  async function refreshProjects({ silent = false, preferId = '' } = {}) {
    if (!silent) setProjectState((current) => ({ ...current, loading: true, notice: '' }));
    const result = await loadProjectState();
    if (result.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({
        ...current,
        loading: false,
        apiReady: true,
        status: '登录状态已过期',
        notice: '请重新登录后查看项目。'
      }));
      return;
    }
    const nextProjects = result.projects || [];
    const nextCurrent = result.currentProject || null;
    setProjectState({
      apiReady: !result.missing,
      loading: false,
      projects: nextProjects,
      currentProject: nextCurrent,
      status: result.status || (result.missing ? '项目服务未就绪' : '等待项目'),
      notice: result.missing ? '项目服务未就绪' : ''
    });
    setSelectedProjectId((current) => {
      const preferred = preferId && nextProjects.find((project) => project.id === preferId);
      if (preferred) return preferred.id;
      if (current && nextProjects.find((project) => project.id === current)) return current;
      return nextCurrent?.id || nextProjects[0]?.id || '';
    });
  }

  useEffect(() => {
    refreshProjects({ silent: true });
  }, [backendStatus?.ok]);

  const selectedProject = projectState.projects.find((project) => project.id === selectedProjectId) || projectState.currentProject || projectState.projects[0] || null;

  useEffect(() => {
    setEditDraft(projectDraftFromProject(selectedProject || {}));
  }, [selectedProject?.id, selectedProject?.updatedAt]);

  function updateCreateField(field, value) {
    setCreateDraft((current) => ({ ...current, [field]: value }));
  }

  function updateEditField(field, value) {
    setEditDraft((current) => ({ ...current, [field]: value }));
  }

  async function createProject(event) {
    event.preventDefault();
    const payload = projectPayloadFromDraft(createDraft);
    if (!payload.name || !projectState.apiReady) return;
    setBusy('create');
    const result = await createManagedProject(payload);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后创建项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目创建失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setCreateDraft(emptyProjectDraft());
      setProjectState((current) => ({ ...current, notice: '项目已创建，并切换为当前项目。' }));
      await refreshProjects({ silent: true, preferId: result.project?.id });
      refreshBackend?.({ refresh: true });
    }
    setBusy('');
  }

  async function saveProject() {
    if (!selectedProject?.id || !projectState.apiReady) return;
    const payload = projectPayloadFromDraft(editDraft);
    if (!payload.name) return;
    setBusy(`save:${selectedProject.id}`);
    const result = await updateManagedProject(selectedProject.id, payload);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后保存项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目保存失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: '项目信息已保存。' }));
      await refreshProjects({ silent: true, preferId: selectedProject.id });
      refreshBackend?.({ refresh: true });
    }
    setBusy('');
  }

  async function switchProject(project = selectedProject) {
    if (!project?.id || project.current || project.archived || !projectState.apiReady) return;
    setBusy(`switch:${project.id}`);
    const result = await switchManagedProject(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后切换项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目切换失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: '当前项目已切换。' }));
      await refreshProjects({ silent: true, preferId: project.id });
      refreshBackend?.({ refresh: true });
    }
    setBusy('');
  }

  async function toggleArchive(project = selectedProject) {
    if (!project?.id || !projectState.apiReady) return;
    setBusy(`archive:${project.id}`);
    const result = project.archived
      ? await updateManagedProject(project.id, { status: 'active' })
      : await archiveManagedProject(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后更新项目状态。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目状态更新失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: project.archived ? '项目已恢复。' : '项目已归档。' }));
      await refreshProjects({ silent: true, preferId: project.id });
      refreshBackend?.({ refresh: true });
    }
    setBusy('');
  }

  const activeProjects = projectState.projects.filter((project) => !project.archived);
  const archivedProjects = projectState.projects.filter((project) => project.archived);
  const currentProject = projectState.currentProject;
  const canCreateProject = projectState.apiReady && hasEcorexFunction(['createProject', 'projects.create']);
  const canUpdateProject = projectState.apiReady && hasEcorexFunction(['updateProject', 'projects.update']);
  const canArchiveProject = projectState.apiReady && hasEcorexFunction(['archiveProject', 'projects.archive', 'updateProject', 'projects.update']);
  const canSwitchProject = projectState.apiReady && hasEcorexFunction(['switchProject', 'projects.switch']);

  return (
    <section className="projects-page panel">
      <HeaderBar
        title="项目"
        badge="广告 Agent"
        subtitle="以客户项目为单位隔离工作目录、长期记忆、投放目标、预算周期与交付物"
        backendStatus={backendStatus}
        onRefresh={() => {
          refreshBackend?.({ refresh: true });
          refreshProjects();
        }}
      />

      <div className="projects-overview">
        {[
          ['当前项目', currentProject?.name || '未选择', currentProject ? projectBusinessSummary(currentProject) : '发送任务前建议先选择项目', LayoutDashboard, currentProject ? 'ok' : 'warn'],
          ['活跃项目', `${activeProjects.length} 个`, `${projectState.projects.length} 个项目 · ${archivedProjects.length} 个已归档`, Target, activeProjects.length ? 'ok' : 'warn'],
          ['项目记忆', currentProject?.memoryLabel || '待初始化', currentProject ? 'runPrompt 会绑定当前项目记忆' : '选择项目后自动创建记忆文件', ClipboardList, currentProject ? 'ok' : 'running']
        ].map(([label, value, detail, Icon, tone]) => (
          <article className={`project-overview-card ${tone}`} key={label}>
            <span><Icon size={22} /></span>
            <div>
              <em>{label}</em>
              <strong>{value}</strong>
              <small>{detail}</small>
            </div>
          </article>
        ))}
      </div>

      <div className="projects-grid">
        <section className="projects-list-panel">
          <header>
            <div>
              <h3>项目列表</h3>
              <small>{projectState.apiReady ? `${activeProjects.length} 个活跃 · ${archivedProjects.length} 个归档` : '项目服务未就绪'}</small>
            </div>
            <button type="button" onClick={() => refreshProjects()} disabled={projectState.loading}>
              <Loader2 size={14} className={projectState.loading ? 'spin-icon' : ''} />
              刷新
            </button>
          </header>
          <div className="project-list-full">
            {(projectState.projects.length ? projectState.projects : [{ id: 'empty', name: projectState.apiReady ? '暂无项目' : '项目服务未就绪', statusLabel: projectState.status }]).map((project) => (
              <button
                className={`project-list-entry ${selectedProject?.id === project.id ? 'selected' : ''} ${project.current ? 'active' : ''} ${project.archived ? 'archived' : ''}`}
                disabled={project.id === 'empty'}
                key={project.id}
                type="button"
                onClick={() => setSelectedProjectId(project.id)}
              >
                <LayoutDashboard size={17} />
                <div>
                  <strong>{project.name}</strong>
                  <em>{projectBusinessSummary(project)}</em>
                </div>
                <span>{project.current ? '当前' : project.statusLabel || projectStatusLabel(project.status)}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="project-detail-panel">
          <header>
            <div>
              <h3>{selectedProject?.name || '项目详情'}</h3>
              <small>{selectedProject ? `${selectedProject.statusLabel} · ${selectedProject.pathLabel || 'workspace:/'}` : '选择项目后编辑广告业务上下文'}</small>
            </div>
            <div className="project-detail-actions">
              <button type="button" onClick={() => switchProject(selectedProject)} disabled={!selectedProject || selectedProject.current || selectedProject.archived || !canSwitchProject || busy === `switch:${selectedProject?.id}`}>
                {busy === `switch:${selectedProject?.id}` ? <Loader2 size={14} className="spin-icon" /> : <Check size={14} />}
                {selectedProject?.current ? '当前' : '切换'}
              </button>
              <button type="button" onClick={() => toggleArchive(selectedProject)} disabled={!selectedProject || !canArchiveProject || busy === `archive:${selectedProject?.id}`}>
                {busy === `archive:${selectedProject?.id}` ? <Loader2 size={14} className="spin-icon" /> : <Archive size={14} />}
                {selectedProject?.archived ? '恢复' : '归档'}
              </button>
            </div>
          </header>

          {selectedProject ? (
            <>
              <div className="project-context-strip">
                <span><Building2 size={15} />{selectedProject.client || '客户待补充'}</span>
                <span><Target size={15} />{selectedProject.goal || '目标待补充'}</span>
                <span><Clock3 size={15} />{[selectedProject.budget, selectedProject.period].filter(Boolean).join(' / ') || '预算周期待补充'}</span>
              </div>
              <div className="project-edit-form">
                <label>
                  <span>项目名称</span>
                  <input value={editDraft.name} onChange={(event) => updateEditField('name', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>客户 / 品牌</span>
                  <input value={editDraft.client} onChange={(event) => updateEditField('client', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>行业</span>
                  <input value={editDraft.industry} onChange={(event) => updateEditField('industry', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>场景 / 渠道</span>
                  <input value={editDraft.scenario} onChange={(event) => updateEditField('scenario', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>预算</span>
                  <input value={editDraft.budget} onChange={(event) => updateEditField('budget', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>周期</span>
                  <input value={editDraft.period} onChange={(event) => updateEditField('period', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label>
                  <span>状态</span>
                  <select value={editDraft.status} onChange={(event) => updateEditField('status', event.target.value)} disabled={!canUpdateProject}>
                    {PROJECT_STATUS_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                  </select>
                </label>
                <label className="wide">
                  <span>广告目标</span>
                  <textarea value={editDraft.goal} onChange={(event) => updateEditField('goal', event.target.value)} disabled={!canUpdateProject} />
                </label>
                <label className="wide">
                  <span>交付物</span>
                  <textarea value={editDraft.deliverablesText} onChange={(event) => updateEditField('deliverablesText', event.target.value)} disabled={!canUpdateProject} />
                </label>
              </div>
              <div className="project-detail-footer">
                <span>{selectedProject.fileCount || 0} 文件 · {selectedProject.sessionCount || 0} 会话 · {selectedProject.updatedAt ? formatDateTime(selectedProject.updatedAt) : '更新时间待返回'}</span>
                <button type="button" onClick={saveProject} disabled={!canUpdateProject || !editDraft.name.trim() || busy === `save:${selectedProject.id}`}>
                  {busy === `save:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <Check size={15} />}
                  保存
                </button>
              </div>
            </>
          ) : (
            <div className="project-empty-state">
              <LayoutDashboard size={34} />
              <strong>暂无项目</strong>
              <span>先创建一个客户项目，亦芯会把任务目录和长期记忆隔离在该项目下。</span>
            </div>
          )}
        </section>

        <section className="project-create-panel">
          <header>
            <div>
              <h3>新建广告项目</h3>
              <small>客户、目标、预算周期和交付物会写入项目元数据</small>
            </div>
          </header>
          <form className="project-create-form" onSubmit={createProject}>
            <label>
              <span>项目名称</span>
              <input value={createDraft.name} onChange={(event) => updateCreateField('name', event.target.value)} placeholder="如：618 短视频投放" disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label>
              <span>客户 / 品牌</span>
              <input value={createDraft.client} onChange={(event) => updateCreateField('client', event.target.value)} placeholder="客户或品牌名" disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label>
              <span>行业</span>
              <input value={createDraft.industry} onChange={(event) => updateCreateField('industry', event.target.value)} placeholder="美妆、汽车、本地生活..." disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label>
              <span>场景 / 渠道</span>
              <input value={createDraft.scenario} onChange={(event) => updateCreateField('scenario', event.target.value)} placeholder="信息流、搜索、达人种草..." disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label>
              <span>预算</span>
              <input value={createDraft.budget} onChange={(event) => updateCreateField('budget', event.target.value)} placeholder="如：50 万 / 月" disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label>
              <span>周期</span>
              <input value={createDraft.period} onChange={(event) => updateCreateField('period', event.target.value)} placeholder="如：2026 Q2" disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label className="wide">
              <span>广告目标</span>
              <textarea value={createDraft.goal} onChange={(event) => updateCreateField('goal', event.target.value)} placeholder="转化、线索、品牌曝光、A/B 测试目标..." disabled={!canCreateProject || busy === 'create'} />
            </label>
            <label className="wide">
              <span>交付物</span>
              <textarea value={createDraft.deliverablesText} onChange={(event) => updateCreateField('deliverablesText', event.target.value)} placeholder="投放计划、素材脚本、复盘报告..." disabled={!canCreateProject || busy === 'create'} />
            </label>
            <button type="submit" disabled={!canCreateProject || !createDraft.name.trim() || busy === 'create'}>
              {busy === 'create' ? <Loader2 size={16} className="spin-icon" /> : <Plus size={16} />}
              新建并切换
            </button>
          </form>
          <div className="project-memory-note">
            <ClipboardList size={16} />
            <span>每个项目会拥有独立目录与 `.ecorex-memory/project-memory.md`，后续任务默认只读取当前项目上下文。</span>
          </div>
          {projectState.notice && <p className="diagnostics-notice compact">{projectState.notice}</p>}
        </section>
      </div>
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
        <h3>亦芯能力</h3>
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
  const [expanded, setExpanded] = useState(false);
  const visibleItems = expanded ? timeline.slice(-14) : timeline.slice(-5);

  return (
    <div className="agent-trace">
      <div className="agent-trace-head">
        <span>当前任务状态树</span>
        {sourceMap?.available && <em>能力索引 {sourceMap.sizeMb}MB</em>}
      </div>
      <div className="agent-trace-list">
        {(visibleItems.length ? visibleItems : [['等待任务进度', '待开始', '--', 'pending']]).map(([label, status, time, tone], index) => (
          <div className={`agent-trace-row ${tone}`} key={`${label}-${index}`}>
            <span className="agent-trace-node" />
            <strong>{label}</strong>
            <em>{status}</em>
            <small>{time}</small>
          </div>
        ))}
      </div>
      {timeline.length > 5 && (
        <button className="agent-trace-toggle" type="button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起步骤' : `查看全部 ${timeline.length} 步`}
        </button>
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
    </section>
  );
}

async function callEcorex(candidates, ...args) {
  const bridge = window.ecorex;
  if (!bridge) return { ok: false, missing: true, error: '本地能力服务未就绪' };

  for (const candidate of candidates) {
    const parts = candidate.split('.');
    const fn = parts.reduce((target, part) => target?.[part], bridge);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(...args);
    } catch (error) {
      return { ok: false, unauthorized: isUnauthorizedError(error), error: error?.message || String(error) };
    }
  }

  return { ok: false, missing: true, error: '本地能力服务未就绪' };
}

function getBridgeFunction(candidate) {
  const bridge = window.ecorex;
  if (!bridge) return null;
  return candidate.split('.').reduce((target, part) => target?.[part], bridge);
}

async function callEcorexAction(candidates, payload = {}) {
  if (!window.ecorex) return { ok: false, missing: true, error: '本地能力服务未就绪' };

  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(payload);
    } catch (firstError) {
      if (!payload.id && !payload.name) {
        return { ok: false, unauthorized: isUnauthorizedError(firstError), error: firstError?.message || String(firstError) };
      }
      try {
        return await fn(payload.id || payload.name, payload);
      } catch (secondError) {
        return { ok: false, unauthorized: isUnauthorizedError(secondError), error: secondError?.message || firstError?.message || String(secondError) };
      }
    }
  }

  return { ok: false, missing: true, error: '本地能力服务未就绪' };
}

function extractCollection(result, keys = []) {
  if (!result) return [];
  if (Array.isArray(result)) return result;
  for (const key of keys) {
    const value = key.split('.').reduce((target, part) => target?.[part], result);
    if (Array.isArray(value)) return value;
  }
  if (Array.isArray(result.items)) return result.items;
  if (Array.isArray(result.data)) return result.data;
  return [];
}

function formatDateTime(value) {
  if (!value) return '-';
  if (typeof value === 'string' && !Number.isNaN(Date.parse(value))) {
    return new Date(value).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    });
  }
  return String(value);
}

function selectedWorkspacePathFromResult(result) {
  if (!result) return '';
  if (typeof result === 'string') return result.trim();
  if (Array.isArray(result)) return String(result[0] || '').trim();
  if (result.canceled || result.cancelled) return '';
  const filePaths = Array.isArray(result.filePaths) ? result.filePaths : [];
  return String(
    result.workspaceRoot ||
    result.directory ||
    result.path ||
    result.folder ||
    result.root ||
    filePaths[0] ||
    ''
  ).trim();
}

function promptWorkspaceDirectoryFallback(currentRoot = '') {
  if (typeof window === 'undefined' || typeof window.prompt !== 'function') {
    return { ok: false, missing: true, error: '目录选择接口未就绪' };
  }
  const value = window.prompt('目录选择接口暂未就绪。可临时输入完整目录路径，或取消保持当前设置。', currentRoot || '');
  if (value === null) return { canceled: true };
  return { ok: true, workspaceRoot: String(value || '').trim(), fallback: true };
}

function diagnosticsExportFileName() {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `ecorex-diagnostics-${stamp}.json`;
}

function directoryFromFilePath(filePath) {
  const value = String(filePath || '').trim();
  if (!value) return '';
  const index = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
  return index > 0 ? value.slice(0, index) : '';
}

function formatDiagnosticsExportSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

function normalizeDiagnosticsExportResult(result = {}) {
  const fileName = sanitizeDisplayText(result.fileName || result.name, diagnosticsExportFileName());
  const filePath = String(result.path || result.filePath || result.savedPath || result.outputPath || '').trim();
  const directory = String(result.directory || result.folder || result.exportDir || result.outputDir || directoryFromFilePath(filePath)).trim();
  const pathLabel = sanitizeDisplayText(
    result.pathLabel || result.location || filePath || (directory ? `${directory}/${fileName}` : fileName),
    fileName
  );
  const exportedAt = result.generatedAt || result.exportedAt || new Date().toISOString();

  return {
    ok: result.ok !== false,
    fallback: Boolean(result.fallback),
    saved: Boolean(result.saved ?? (filePath || directory)),
    fileName,
    path: filePath,
    directory,
    pathLabel,
    bytes: Number(result.bytes || result.size || 0),
    sizeLabel: formatDiagnosticsExportSize(result.bytes || result.size),
    exportedAt
  };
}

function buildDiagnosticsBundleSnapshot({
  diagnostics,
  backendStatus,
  settings,
  workspace,
  sessions,
  projectState,
  modelHealth,
  crashRecovery
}) {
  return {
    exportedAt: new Date().toISOString(),
    app: 'EcoreX Agent',
    diagnostics: diagnostics || null,
    backendStatus: backendStatus || null,
    settings: {
      defaultModel: settings?.defaultModel,
      accessMode: settings?.accessMode,
      workspaceRoot: settings?.workspaceRoot || '',
      maxPromptChars: settings?.maxPromptChars,
      autoRefreshBackend: settings?.autoRefreshBackend
    },
    workspace: {
      workspaceRoot: workspace?.workspaceRoot || settings?.workspaceRoot || '',
      entries: Array.isArray(workspace?.entries) ? workspace.entries.slice(0, 100) : []
    },
    sessions: Array.isArray(sessions) ? sessions : [],
    projectState: projectState || null,
    modelHealth: modelHealth || null,
    crashRecovery: crashRecovery || null
  };
}

async function exportDiagnosticsBundleFallback(snapshot) {
  if (typeof document === 'undefined' || typeof Blob === 'undefined' || typeof URL === 'undefined') {
    return { ok: false, missing: true, error: '诊断导出接口未就绪' };
  }
  const fileName = diagnosticsExportFileName();
  const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return {
    ok: true,
    fallback: true,
    fileName,
    pathLabel: `浏览器下载目录/${fileName}`,
    saved: false
  };
}

async function copyTextToClipboard(text) {
  const value = String(text || '').trim();
  if (!value) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall back to the selection API below.
  }

  if (typeof document === 'undefined') return false;
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.left = '-9999px';
  document.body.appendChild(input);
  input.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }
  input.remove();
  return copied;
}

async function openDiagnosticsExportWithBridge(exportInfo = {}) {
  const targetPath = exportInfo.path || exportInfo.directory || '';
  const payload = {
    path: exportInfo.path,
    filePath: exportInfo.path,
    directory: exportInfo.directory,
    folder: exportInfo.directory,
    reveal: true
  };
  const candidates = [
    'openDiagnosticsExportLocation',
    'diagnostics.openExportLocation',
    'openFileLocation',
    'openPath',
    'shell.openPath',
    'showItemInFolder',
    'revealInFolder',
    'openFolder'
  ];

  if (!targetPath) return { ok: false, missing: true, error: '导出路径未返回' };

  let lastError = null;
  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(payload);
    } catch (payloadError) {
      try {
        return await fn(targetPath);
      } catch (pathError) {
        lastError = {
          ok: false,
          unauthorized: isUnauthorizedError(pathError),
          error: pathError?.message || payloadError?.message || String(pathError)
        };
      }
    }
  }

  if (lastError) return lastError;
  return { ok: false, missing: true, error: '打开位置接口未就绪' };
}

function formatWorkspaceEntryType(entry = {}) {
  const type = String(entry.type || '').toLowerCase();
  if (entry.isDirectory || ['folder', 'directory', 'dir'].includes(type)) return '文件夹';
  if (['file', 'document'].includes(type)) return '文件';
  if (type === 'info') return '提示';
  return sanitizeDisplayText(entry.type, '条目');
}

function formatSessionStatus(status) {
  const normalized = String(status || '').toLowerCase();
  const names = {
    idle: '待命',
    running: '运行中',
    active: '运行中',
    complete: '已完成',
    completed: '已完成',
    done: '已完成',
    error: '异常',
    failed: '异常',
    timeout: '已超时',
    'too-many-sessions': '会话已满',
    'duplicate-session': '已有会话',
    'duplicate-start': '重复启动',
    'not-found': '未找到',
    cancelled: '已取消',
    canceled: '已取消'
  };
  return names[normalized] || sanitizeDisplayText(status, '待命');
}

function sessionStatusTone(status) {
  const normalized = String(status || '').toLowerCase();
  if (['error', 'failed', 'timeout', '异常', '失败', '已超时'].includes(normalized)) return 'danger';
  if (['complete', 'completed', 'done', 'cancelled', 'canceled', '已完成', '已取消'].includes(normalized)) return 'pending';
  if (['running', 'active', 'starting', 'queued', '运行中', '进行中'].includes(normalized)) return 'running';
  return 'pending';
}

function isRunningSessionActive(session = {}) {
  const normalized = String(session.status || session.state || '').toLowerCase();
  return !['complete', 'completed', 'done', 'idle', 'cancelled', 'canceled', 'error', 'failed', 'timeout'].includes(normalized);
}

function sessionTimestamp(value) {
  if (!value) return Date.now();
  if (typeof value === 'number') return value;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

function normalizeRunningSession(raw = {}, index = 0, source = 'api') {
  const sessionId = raw.sessionId || raw.session_id || raw.id || raw.runId || raw.run_id || `session-${index + 1}`;
  const status = raw.status || raw.state || 'running';
  const prompt = sanitizeDisplayText(raw.promptPreview || raw.prompt || raw.title || raw.summary, '正在执行任务');
  const accessMode = normalizeAccessMode(raw.accessMode || raw.permissionMode || raw.defaultPermissionMode);
  const updatedAt = sessionTimestamp(raw.updatedAt || raw.lastActivityAt || raw.lastActivity || raw.startedAt || raw.startedAtIso);
  return {
    id: sessionId,
    sessionId,
    status,
    state: raw.state || status,
    title: sanitizeDisplayText(raw.title || raw.name || `会话 ${index + 1}`, `会话 ${index + 1}`),
    prompt,
    messageId: raw.messageId || raw.message_id || '',
    accessMode,
    permissionMode: permissionModeFromAccessMode(accessMode),
    accessLabel: permissionOptionByValue(accessMode).label,
    startedAt: sessionTimestamp(raw.startedAt || raw.startedAtIso || raw.startedAtMs),
    updatedAt,
    source,
    tone: sessionStatusTone(status)
  };
}

function mergeRunningSessionRows(current = [], incoming = [], options = {}) {
  const replaceSources = new Set(options.replaceSources || []);
  const rows = replaceSources.size ? current.filter((row) => !replaceSources.has(row.source)) : [...current];
  const byId = new Map(rows.map((row) => [row.sessionId, row]));
  incoming.forEach((row) => {
    if (!row?.sessionId || !isRunningSessionActive(row)) return;
    const existing = byId.get(row.sessionId) || {};
    byId.set(row.sessionId, {
      ...existing,
      ...row,
      title: row.title || existing.title,
      prompt: row.prompt || existing.prompt || '正在执行任务',
      messageId: row.messageId || existing.messageId || '',
      accessMode: row.accessMode || existing.accessMode || 'default',
      accessLabel: row.accessLabel || existing.accessLabel || permissionOptionByValue(row.accessMode || existing.accessMode).label,
      source: row.source || existing.source || 'local',
      updatedAt: row.updatedAt || existing.updatedAt || Date.now()
    });
  });
  return Array.from(byId.values())
    .filter(isRunningSessionActive)
    .sort((left, right) => (right.updatedAt || 0) - (left.updatedAt || 0))
    .slice(0, 12);
}

function extractAgentSessionRows(result) {
  const rows = extractCollection(result, ['sessions', 'runningSessions', 'activeSessions', 'items', 'data']);
  return rows.map((session, index) => normalizeRunningSession(session, index, 'api')).filter(isRunningSessionActive);
}

function boolFrom(value, fallback = false) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value > 0;
  if (typeof value === 'string') {
    const normalized = value.toLowerCase();
    if (['true', '1', 'yes', 'enabled', 'active', 'online', 'connected'].includes(normalized)) return true;
    if (['false', '0', 'no', 'disabled', 'inactive', 'offline'].includes(normalized)) return false;
  }
  return fallback;
}

function statusToneFrom(status, error, enabled = true) {
  const normalized = String(status || '').toLowerCase();
  if (error) return 'danger';
  if (!enabled || ['disabled', '停用', '已禁用'].includes(normalized)) return 'pending';
  if (['online', 'connected', 'ready', 'ok', 'success', '在线', '已连接', '正常'].includes(normalized)) return 'success';
  if (['connecting', 'loading', 'running', 'syncing', '连接中', '同步中'].includes(normalized)) return 'running';
  if (['offline', 'disconnected', 'failed', 'error', '离线', '失败', '异常'].includes(normalized)) return 'danger';
  return 'pending';
}

function statusLabelFrom(status, tone, enabled = true) {
  if (!enabled) return '已禁用';
  if (status) return String(status);
  const labels = {
    success: '在线',
    running: '连接中',
    danger: '异常',
    pending: '待连接'
  };
  return labels[tone] || '未知';
}

function sanitizeDisplayText(value, fallback = '信息待返回') {
  const text = String(value || '').trim();
  if (!text) return fallback;
  if (/([A-Za-z]:\\|\\\\|\/\.|\/Users\/|\/home\/|node_modules|cache|projectPath|installPath|\braw\b|\bpath\b)/i.test(text)) {
    return fallback;
  }
  return text
    .replace(/--dangerously-skip-permissions/gi, '完全访问权限')
    .replace(/\bbypassPermissions\b/gi, '完全访问权限')
    .replace(/\bfullAccess\b/gi, '完全访问权限')
    .replace(/\bClaude MCP\b/gi, 'EcoreX 数据连接')
    .replace(/\bClaude\s*Code\b/gi, '本地能力')
    .replace(/\bClaude\b/gi, '本地')
    .replace(/\bAgent\b/gi, '亦芯助手')
    .replace(/\bCLI\b/g, '本地')
    .replace(/\bMCP\b/g, '数据连接')
    .replace(/\bplugins\b/gi, '能力')
    .replace(/\bplugin\b/gi, '能力');
}

function agentRecoveryText(source = {}) {
  const raw = typeof source === 'string' ? { code: source } : source || {};
  const code = String(raw.code || raw.status || raw.state || raw.reason || raw.kind || '').toLowerCase();
  if (code === 'too-many-sessions') return '运行会话已达上限。请先停止一个会话，或等待当前任务完成后重试。';
  if (code === 'duplicate-session' || code === 'duplicate-start') return '相同任务已经在启动或运行中。请切换到当前会话查看进度，稍后再重试。';
  if (code === 'timeout' || code === 'idle-timeout') return '任务已超时。可以缩小任务范围、拆成更小步骤，或调大超时时间后重试。';
  if (code === 'cancelled' || code === 'canceled') return '任务已取消。可以查看已返回内容，确认后重新发起任务。';
  if (code === 'not-found') return '该会话已经结束或不存在。请刷新运行会话后再操作。';
  if (raw.recoveryHint) return sanitizeDisplayText(raw.recoveryHint, '请检查本地运行状态后重试。');
  return '请检查本地运行引擎状态，必要时缩小提示词后重试。';
}

function agentRunFailureMessage(result = {}, fallback = '本地执行启动失败。') {
  const primary = sanitizeDisplayText(result.error || result.message, fallback);
  const recovery = agentRecoveryText(result);
  return primary === recovery ? primary : `${primary}\n${recovery}`;
}

function formatSkillCategory(category) {
  const normalized = String(category || '').toLowerCase();
  const names = {
    development: '开发协同',
    productivity: '效率',
    security: '安全',
    workflow: '工作流',
    uncategorized: '通用能力'
  };
  return names[normalized] || sanitizeDisplayText(category, '通用能力');
}

function isNativeSkillItem(item = {}) {
  const raw = [
    item.id,
    item.name,
    item.slug,
    item.packageName,
    item.path,
    item.installPath,
    item.cachePath,
    item.projectPath,
    item.source
  ].filter(Boolean).join(' ').toLowerCase();
  return [
    'claude',
    '@anthropic',
    'claude-code',
    'node_modules',
    'plugin-dev',
    'agent-sdk-dev',
    'commit-commands',
    'output-style',
    'hookify'
  ].some((token) => raw.includes(token));
}

function isNativeConnectorItem(item = {}) {
  const raw = [
    item.id,
    item.name,
    item.title,
    item.command,
    item.path,
    item.installPath,
    item.cachePath,
    item.projectPath,
    item.source
  ].filter(Boolean).join(' ').toLowerCase();
  return ['claude', '@anthropic', 'claude-code', 'node_modules'].some((token) => raw.includes(token)) || /\bcli\b/.test(raw);
}

function connectorEndpointLabel(service = {}) {
  const value = service.displayUrl || service.endpointLabel || service.label || service.url || service.endpoint;
  const text = sanitizeDisplayText(value, '');
  if (!text || /mcp\.|\/mcp|command|stdio/i.test(text)) return 'EcoreX 数据连接端点';
  return text;
}

function normalizeSettingsState(raw = {}) {
  const accessMode = normalizeAccessMode(
    raw.accessMode || raw.defaultPermissionMode || raw.permissionMode || raw.permissions || readStoredDefaultAccessMode()
  );
  return {
    defaultModel: raw.defaultModel || raw.model || 'sonnet',
    permissionMode: permissionModeFromAccessMode(accessMode),
    defaultPermissionMode: permissionModeFromAccessMode(accessMode),
    accessMode,
    workspaceRoot: raw.workspaceRoot || raw.workspace || '',
    maxPromptChars: raw.maxPromptChars || 80000,
    autoRefreshBackend: raw.autoRefreshBackend !== false
  };
}

function pickFirstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== '');
}

function formatHealthValue(value, fallback = '待返回') {
  if (typeof value === 'boolean') return value ? '正常' : '需检查';
  if (typeof value === 'number') return String(value);
  return sanitizeDisplayText(value, fallback);
}

function toneFromHealth(value, fallbackTone = 'warn') {
  const normalized = String(value || '').toLowerCase();
  if (value === true || ['ok', 'ready', 'valid', 'verified', 'signed', 'normal', 'success', 'configured', 'present', '正常', '已就绪', '已配置', '已签名', '通过'].includes(normalized)) {
    return 'ok';
  }
  if (['running', 'checking', 'pending', 'loading', '进行中', '检测中'].includes(normalized)) return 'running';
  if (value === false || ['missing', 'invalid', 'unsigned', 'error', 'failed', 'absent', '缺失', '未签名', '异常', '失败'].includes(normalized)) {
    return 'warn';
  }
  return fallbackTone;
}

function releasePackageSummary(diagnostics = {}, backendStatus = {}) {
  const release = diagnostics?.release || backendStatus?.release || {};
  const installers = Array.isArray(release.installers) ? release.installers : [];
  const installerCount = installers.filter((item) => item?.exists !== false).length;
  const status = pickFirstDefined(release.status, release.packageStatus, release.exists, installerCount > 0);
  return {
    value: installerCount ? `${installerCount} 个包` : formatHealthValue(status, '未检测'),
    detail: installerCount ? '发行包已检测' : (release.exists ? '目录已就绪' : '等待打包'),
    tone: installerCount ? 'ok' : toneFromHealth(status)
  };
}

function signatureStatusSummary(diagnostics = {}, backendStatus = {}) {
  const signature = diagnostics?.signature || diagnostics?.release?.signature || backendStatus?.signature || backendStatus?.release?.signature || {};
  const status = pickFirstDefined(signature.status, signature.signed, signature.verified, signature.ok);
  const configured = Boolean(pickFirstDefined(signature.configured, signature.provider, signature.method, status));
  const tone = configured ? toneFromHealth(status, 'warn') : 'running';
  return {
    value: configured ? formatHealthValue(pickFirstDefined(signature.label, signature.summary, status), tone === 'ok' ? '已签名' : '待配置') : '待配置',
    detail: configured ? formatHealthValue(signature.updatedAt ? formatDateTime(signature.updatedAt) : signature.provider || signature.method, tone === 'ok' ? '签名校验通过' : '等待签名状态') : '证书签名不作为本轮目标',
    tone
  };
}

function runtimeEngineSummary(diagnostics = {}, backendStatus = {}) {
  const bridge = diagnostics?.agentBridge || diagnostics?.runtime || backendStatus?.agentBridge || backendStatus?.runtime || {};
  const version = pickFirstDefined(bridge.version, diagnostics?.claude?.version, backendStatus?.claude?.version);
  const available = pickFirstDefined(bridge.available, bridge.ok, Boolean(version));
  return {
    value: version ? sanitizeDisplayText(version, '已就绪') : (available ? '已就绪' : '未检测'),
    detail: available ? '本地运行引擎可用' : '等待运行引擎',
    tone: available ? 'ok' : 'warn'
  };
}

function modelConfigSummary(modelHealth = {}, settings = {}, capabilities = {}) {
  const profiles = Array.isArray(modelHealth.profiles) ? modelHealth.profiles : [];
  const models = Array.isArray(capabilities?.models) ? capabilities.models : [];
  if (modelHealth.error) {
    return { value: '读取失败', detail: sanitizeDisplayText(modelHealth.error, '模型配置状态不可用'), tone: 'warn' };
  }
  if (!modelHealth.apiReady && modelHealth.notice) {
    return { value: '服务未就绪', detail: modelHealth.notice, tone: 'warn' };
  }
  const configuredCount = profiles.filter((profile) => profile.apiKeyConfigured || profile.configured || profile.active || profile.isActive).length;
  const activeProfile = profiles.find((profile) => profile.active || profile.isActive);
  const missing = [];
  if (!activeProfile && !profiles.length) missing.push('配置');
  if (activeProfile && !activeProfile.baseUrl) missing.push('Base URL');
  if (activeProfile && !activeProfile.apiKeyConfigured && !activeProfile.maskedKey) missing.push('API Key');
  if (activeProfile && !activeProfile.modelName) missing.push('模型名称');
  const hasConfig = configuredCount > 0 || Boolean(activeProfile) || Boolean(settings.defaultModel);
  return {
    value: missing.length ? '需补充' : (hasConfig ? '已配置' : '未配置'),
    detail: missing.length ? `缺少${missing.join('、')}` : (activeProfile?.name || `${Math.max(configuredCount, models.length)} 个模型选项`),
    tone: missing.length || !hasConfig ? 'warn' : 'ok'
  };
}

function localBuildSummary(diagnostics = {}) {
  const backend = diagnostics?.backend || {};
  const items = [backend.dist, backend.source, backend.sourceMap, backend.nativeBinaryPackage, backend.packagedBackend];
  const checked = items.filter((item) => item?.exists).length;
  return {
    value: checked === items.length && items.length ? '通过' : `${checked}/${items.length || 5}`,
    detail: checked ? '本地构建文件已检测' : '等待 npm run build',
    tone: checked >= 2 ? 'ok' : 'warn'
  };
}

function permissionModeSummary(settings = {}) {
  const option = permissionOptionByValue(settings.accessMode || settings.permissionMode);
  return {
    value: option.label,
    detail: option.value === 'fullAccess' ? '完全访问需显式确认' : '默认安全规则',
    tone: option.value === 'fullAccess' ? 'running' : 'ok'
  };
}

function projectDirectorySummary(projectState = {}, workspace = {}, settings = {}) {
  const root = workspace.workspaceRoot || settings.workspaceRoot || '';
  const currentProject = projectState.currentProject;
  if (currentProject) {
    return {
      value: currentProject.name,
      detail: `${currentProject.fileCount || 0} 个文件 · ${root ? '目录已配置' : '目录待返回'}`,
      tone: 'ok'
    };
  }
  return {
    value: root ? '目录已配置' : '未配置',
    detail: root || projectState.status || '项目目录待返回',
    tone: root ? 'ok' : 'warn'
  };
}

function recentTaskSummary(sessions = [], recentHistory = []) {
  const runningCount = sessions.filter((session) => isRunningSessionActive(session)).length;
  if (runningCount) {
    return { value: `${runningCount} 个运行中`, detail: '可在运行会话中停止', tone: 'running' };
  }
  return {
    value: recentHistory.length ? `${recentHistory.length} 条记录` : '无最近任务',
    detail: recentHistory[0]?.detail || '暂无历史记录',
    tone: recentHistory.length ? 'ok' : 'warn'
  };
}

function normalizeCrashRecoveryEntry(item, index = 0) {
  const raw = typeof item === 'string' ? { message: item } : (item || {});
  const status = String(raw.status || raw.state || raw.kind || raw.type || '').toLowerCase();
  const recovered = boolFrom(
    raw.recovered ?? raw.restored ?? raw.handled ?? raw.recoveryComplete,
    ['recovered', 'restored', 'handled', 'resolved'].includes(status)
  );
  const failed = ['crash', 'crashed', 'fatal', 'error', 'failed', 'panic'].some((token) => status.includes(token));
  return {
    id: raw.id || raw.crashId || raw.eventId || `crash-recovery-${index}`,
    title: sanitizeDisplayText(
      raw.title || raw.reason || raw.error || raw.name,
      recovered ? '恢复记录' : '崩溃记录'
    ),
    detail: sanitizeDisplayText(
      raw.recoveryHint || raw.detail || raw.message || raw.summary,
      recovered ? '已恢复，可继续使用' : '等待恢复状态'
    ),
    time: formatDateTime(raw.at || raw.time || raw.timestamp || raw.crashAt || raw.recoveredAt || raw.updatedAt || raw.createdAt),
    tone: failed && !recovered ? 'warn' : 'ok'
  };
}

function normalizeCrashRecoveryStatus(raw = {}, diagnostics = {}, backendStatus = {}) {
  const fallback = diagnostics?.crashRecovery || diagnostics?.recovery || backendStatus?.crashRecovery || backendStatus?.recovery || {};
  const rawObject = raw && typeof raw === 'object' ? raw : { status: raw };
  const nested = rawObject.crashRecovery || rawObject.recovery || rawObject.data;
  const source = { ...fallback, ...(nested && typeof nested === 'object' && !Array.isArray(nested) ? nested : rawObject) };
  const missing = Boolean(source.missing || (source.ok === false && source.missing));
  const lastCrashAt = pickFirstDefined(
    source.lastCrashAt,
    source.lastCrashTime,
    source.crashedAt,
    source.crashAt,
    source.lastCrash?.at,
    source.lastCrash?.time,
    source.lastCrash?.timestamp
  );
  const lastRecoveryAt = pickFirstDefined(
    source.lastRecoveryAt,
    source.recoveredAt,
    source.restoredAt,
    source.recoveryAt,
    source.lastRestoreAt
  );
  const pending = boolFrom(source.pendingRecovery ?? source.needsRecovery ?? source.recoveryPending, false);
  const restored = boolFrom(source.restored ?? source.recovered ?? source.recoveryComplete, Boolean(lastRecoveryAt));
  const entries = extractCollection(source, ['events', 'crashes', 'recentCrashes', 'history', 'recoveries', 'items', 'data'])
    .map((entry, index) => normalizeCrashRecoveryEntry(entry, index))
    .slice(0, 4);

  if (!entries.length && (source.lastCrash || lastCrashAt)) {
    entries.push(normalizeCrashRecoveryEntry(source.lastCrash || { crashAt: lastCrashAt, status: restored ? 'recovered' : 'crash' }, 0));
  }

  const status = String(pickFirstDefined(
    source.status,
    source.state,
    pending ? 'pending' : '',
    restored ? 'recovered' : '',
    lastCrashAt ? 'attention' : '',
    missing ? 'missing' : 'stable'
  )).toLowerCase();

  if (missing && !Object.keys(fallback || {}).length) {
    return {
      apiReady: false,
      value: '未返回',
      detail: '崩溃恢复接口未就绪',
      summary: '暂未读取到最近崩溃与恢复状态。',
      tone: 'running',
      entries: []
    };
  }

  if (pending || status.includes('pending') || status.includes('recovering')) {
    return {
      apiReady: !missing,
      value: '恢复中',
      detail: lastCrashAt ? `最近崩溃：${formatDateTime(lastCrashAt)}` : '等待恢复完成',
      summary: '检测到恢复流程仍在进行。',
      tone: 'running',
      entries
    };
  }

  if (restored || status.includes('recovered') || status.includes('restored')) {
    return {
      apiReady: !missing,
      value: '已恢复',
      detail: lastRecoveryAt ? `恢复时间：${formatDateTime(lastRecoveryAt)}` : '恢复记录已确认',
      summary: lastCrashAt ? `最近崩溃已处理：${formatDateTime(lastCrashAt)}` : '最近恢复状态正常。',
      tone: 'ok',
      entries
    };
  }

  if (lastCrashAt || status.includes('crash') || status.includes('failed') || status.includes('error') || status.includes('attention')) {
    return {
      apiReady: !missing,
      value: '需关注',
      detail: lastCrashAt ? `最近崩溃：${formatDateTime(lastCrashAt)}` : '存在异常记录',
      summary: '检测到最近崩溃记录，请确认恢复状态后继续。',
      tone: 'warn',
      entries
    };
  }

  return {
    apiReady: !missing,
    value: '稳定',
    detail: '未发现最近崩溃',
    summary: '最近没有崩溃或待恢复任务。',
    tone: 'ok',
    entries
  };
}

function normalizeRecentHistoryItem(item, index = 0) {
  if (typeof item === 'string') {
    return {
      id: `recent-history-${index}`,
      title: item === '暂无会话记录' ? item : `历史记录 ${index + 1}`,
      detail: '时间待返回',
      tone: item === '暂无会话记录' ? 'warn' : 'ok'
    };
  }
  const status = item.status || item.state || 'history';
  return {
    id: item.sessionId || item.id || `recent-history-${index}`,
    title: sanitizeDisplayText(item.title || item.promptPreview || item.prompt || `历史记录 ${index + 1}`, `历史记录 ${index + 1}`),
    detail: formatDateTime(item.endedAt || item.updatedAt || item.modifiedAt || item.lastActivity || item.startedAt),
    tone: sessionStatusTone(status) === 'danger' ? 'warn' : 'ok'
  };
}

function hasEcorexFunction(candidates = []) {
  return candidates.some((candidate) => typeof getBridgeFunction(candidate) === 'function');
}

function normalizeSecretStatus(definition, raw = {}) {
  const statusText = String(raw.status || raw.state || '').toLowerCase();
  const configured = boolFrom(
    raw.configured ?? raw.exists ?? raw.present ?? raw.available ?? raw.hasValue ?? raw.ok,
    ['configured', 'ready', 'saved', 'ok', 'valid', 'authorized', 'active'].includes(statusText)
  );
  const maskedCandidate = raw.masked || raw.mask || raw.displayValue || raw.preview;
  const masked = configured
    ? (typeof maskedCandidate === 'string' && /[*•]/.test(maskedCandidate) ? maskedCandidate : '已安全保存')
    : '未配置';
  const labelMap = {
    configured: '已授权',
    ready: '已授权',
    saved: '已保存',
    ok: '已授权',
    valid: '已授权',
    authorized: '已授权',
    active: '已授权',
    missing: '未配置',
    empty: '未配置',
    insecure: '需检查',
    invalid: '需检查',
    error: '需检查'
  };

  return {
    ...definition,
    configured,
    masked,
    status: labelMap[statusText] || (configured ? '已授权' : '未配置'),
    updatedAt: raw.updatedAt || raw.updated || raw.modifiedAt || raw.lastUpdatedAt || '',
    tone: configured ? 'ok' : 'warn'
  };
}

function secretMapFromList(result) {
  if (!result || result?.ok === false) return new Map();
  const source = result.secrets || result.items || result.data || result;
  const rows = new Map();

  if (Array.isArray(source)) {
    source.forEach((item) => {
      if (typeof item === 'string') {
        rows.set(item, { key: item, exists: true, configured: true });
        return;
      }
      const key = item?.key || item?.name || item?.id;
      if (key) rows.set(key, item);
    });
    return rows;
  }

  if (source && typeof source === 'object') {
    Object.entries(source).forEach(([key, value]) => {
      rows.set(key, typeof value === 'object' ? { key, ...value } : { key, exists: Boolean(value), configured: Boolean(value) });
    });
  }

  return rows;
}

async function loadManagedSecretStatuses() {
  const hasList = hasEcorexFunction(['listSecrets', 'secrets.list']);
  const hasStatus = hasEcorexFunction(['getSecretStatus', 'secrets.status', 'secrets.getStatus']);
  if (!hasList && !hasStatus) {
    return { ok: false, missing: true, rows: MANAGED_SECRET_DEFINITIONS.map((definition) => normalizeSecretStatus(definition)) };
  }

  let listMap = new Map();
  let unauthorized = false;
  let listMissing = false;
  if (hasList) {
    const listResult = await callEcorex(['listSecrets', 'secrets.list']);
    if (listResult?.unauthorized) unauthorized = true;
    if (listResult?.missing) listMissing = true;
    listMap = secretMapFromList(listResult);
  }

  let statusMissingCount = 0;
  const rows = await Promise.all(MANAGED_SECRET_DEFINITIONS.map(async (definition) => {
    let raw = listMap.get(definition.key);
    if (hasStatus) {
      const statusResult = await callEcorex(['getSecretStatus', 'secrets.status', 'secrets.getStatus'], definition.key);
      if (statusResult?.unauthorized) unauthorized = true;
      if (statusResult?.missing) statusMissingCount += 1;
      if (statusResult && statusResult.ok !== false) {
        const nextRaw = statusResult.secret || statusResult.data || statusResult;
        raw = typeof nextRaw === 'string' ? { status: nextRaw } : nextRaw;
      }
    }
    return normalizeSecretStatus(definition, raw || {});
  }));

  const missing = listMissing || (hasStatus && statusMissingCount === MANAGED_SECRET_DEFINITIONS.length && !listMap.size);
  return { ok: !unauthorized && !missing, unauthorized, missing, rows };
}

async function setManagedSecret(key, value) {
  const fn = getBridgeFunction('setSecret') || getBridgeFunction('secrets.set');
  if (typeof fn !== 'function') return { ok: false, missing: true, error: '安全存储未就绪' };
  try {
    const result = await fn({ key, value });
    if (result?.ok === false) throw new Error(result.error || '密钥保存失败');
    return result || { ok: true };
  } catch (firstError) {
    try {
      const result = await fn(key, value);
      if (result?.ok === false) throw new Error(result.error || firstError?.message || '密钥保存失败');
      return result || { ok: true };
    } catch (secondError) {
      return { ok: false, unauthorized: isUnauthorizedError(secondError), error: secondError?.message || firstError?.message || String(secondError) };
    }
  }
}

async function deleteManagedSecret(key) {
  const fn = getBridgeFunction('deleteSecret') || getBridgeFunction('secrets.delete');
  if (typeof fn !== 'function') return { ok: false, missing: true, error: '安全存储未就绪' };
  try {
    const result = await fn({ key });
    if (result?.ok === false) throw new Error(result.error || '密钥删除失败');
    return result || { ok: true };
  } catch (firstError) {
    try {
      const result = await fn(key);
      if (result?.ok === false) throw new Error(result.error || firstError?.message || '密钥删除失败');
      return result || { ok: true };
    } catch (secondError) {
      return { ok: false, unauthorized: isUnauthorizedError(secondError), error: secondError?.message || firstError?.message || String(secondError) };
    }
  }
}

function projectApiAvailable() {
  return hasEcorexFunction(['listProjects', 'projects.list', 'createProject', 'projects.create', 'switchProject', 'projects.switch', 'getProjectStatus', 'projects.status']);
}

const PROJECT_STATUS_OPTIONS = [
  { value: 'planning', label: '筹备中' },
  { value: 'active', label: '进行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'completed', label: '已完成' },
  { value: 'archived', label: '已归档' }
];

function projectStatusLabel(status) {
  return PROJECT_STATUS_OPTIONS.find((option) => option.value === status)?.label || sanitizeDisplayText(status, '进行中');
}

function normalizeProjectStatusValue(value, archived = false) {
  const normalized = String(value || '').trim().toLowerCase();
  if (archived) return 'archived';
  const aliases = {
    current: 'active',
    running: 'active',
    enabled: 'active',
    done: 'completed',
    complete: 'completed',
    finished: 'completed',
    stopped: 'paused',
    disabled: 'archived',
    inactive: 'archived'
  };
  const status = aliases[normalized] || normalized || 'active';
  return PROJECT_STATUS_OPTIONS.some((option) => option.value === status) ? status : 'active';
}

function normalizeProjectDeliverables(value) {
  const items = Array.isArray(value) ? value : String(value || '').split(/[\n,，;；、]+/);
  return items.map((item) => sanitizeDisplayText(item, '')).filter(Boolean).slice(0, 12);
}

function projectBusinessSummary(project = {}) {
  return [
    project.client,
    project.goal,
    [project.industry, project.scenario].filter(Boolean).join(' / ')
  ].filter(Boolean).join(' · ') || project.description || project.statusLabel || '广告项目上下文待补充';
}

function emptyProjectDraft() {
  return {
    name: '',
    client: '',
    goal: '',
    industry: '',
    scenario: '',
    budget: '',
    period: '',
    deliverablesText: '',
    status: 'active'
  };
}

function projectDraftFromProject(project = {}) {
  return {
    name: project.name || '',
    client: project.client || '',
    goal: project.goal || '',
    industry: project.industry || '',
    scenario: project.scenario || '',
    budget: project.budget || '',
    period: project.period || '',
    deliverablesText: normalizeProjectDeliverables(project.deliverables).join('、'),
    status: normalizeProjectStatusValue(project.status, project.archived)
  };
}

function projectPayloadFromDraft(draft = {}) {
  return {
    name: String(draft.name || '').trim(),
    client: String(draft.client || '').trim(),
    goal: String(draft.goal || '').trim(),
    industry: String(draft.industry || '').trim(),
    scenario: String(draft.scenario || '').trim(),
    budget: String(draft.budget || '').trim(),
    period: String(draft.period || '').trim(),
    deliverables: normalizeProjectDeliverables(draft.deliverablesText),
    status: normalizeProjectStatusValue(draft.status)
  };
}

function normalizeProjectItem(raw = {}, index = 0, currentId = '') {
  const id = raw.id || raw.projectId || raw.key || raw.slug || raw.name || `project-${index}`;
  const fileCount = Number(raw.fileCount ?? raw.filesCount ?? raw.stats?.fileCount ?? raw.stats?.files ?? raw.workspace?.fileCount ?? (Array.isArray(raw.files) ? raw.files.length : 0)) || 0;
  const sessionCount = Number(raw.sessionCount ?? raw.sessionsCount ?? raw.stats?.sessionCount ?? raw.stats?.sessions ?? (Array.isArray(raw.sessions) ? raw.sessions.length : 0)) || 0;
  const status = normalizeProjectStatusValue(raw.status || raw.state, raw.archived);
  const deliverables = normalizeProjectDeliverables(raw.deliverables || raw.outputs);
  const normalized = {
    id,
    name: sanitizeDisplayText(raw.name || raw.title || raw.displayName || id, `项目 ${index + 1}`),
    current: Boolean(raw.current ?? raw.active ?? raw.selected ?? (currentId && id === currentId)),
    fileCount,
    sessionCount,
    updatedAt: raw.updatedAt || raw.updated || raw.modifiedAt || raw.lastActiveAt || raw.lastOpenedAt || '',
    status,
    statusLabel: sanitizeDisplayText(raw.statusLabel || projectStatusLabel(status), projectStatusLabel(status)),
    client: sanitizeDisplayText(raw.client || raw.customer || raw.brand, ''),
    goal: sanitizeDisplayText(raw.goal || raw.objective || raw.target, ''),
    industry: sanitizeDisplayText(raw.industry || raw.vertical, ''),
    scenario: sanitizeDisplayText(raw.scenario || raw.scene || raw.channel, ''),
    budget: sanitizeDisplayText(raw.budget || raw.spend, ''),
    period: sanitizeDisplayText(raw.period || raw.timeline || raw.cycle, ''),
    deliverables,
    memoryLabel: sanitizeDisplayText(raw.memoryLabel || raw.memoryPath, ''),
    pathLabel: sanitizeDisplayText(raw.pathLabel || raw.workspacePath, ''),
    archived: status === 'archived'
  };
  return {
    ...normalized,
    description: sanitizeDisplayText(raw.description || raw.summary || raw.note || projectBusinessSummary(normalized), projectBusinessSummary(normalized))
  };
}

function extractProjectItems(result) {
  if (!result || result?.ok === false) return [];
  const collection = extractCollection(result, ['projects', 'items', 'data', 'workspace.projects']);
  if (collection.length) return collection;
  if (result.projects && typeof result.projects === 'object') {
    return Object.entries(result.projects).map(([id, project]) => ({ id, ...(typeof project === 'object' ? project : { name: id }) }));
  }
  return [];
}

function normalizeProjectState(listResult, statusResult) {
  const currentRaw = statusResult?.currentProject || statusResult?.project || statusResult?.current || listResult?.currentProject || listResult?.project || null;
  const currentId = statusResult?.currentProjectId || statusResult?.projectId || listResult?.currentProjectId || currentRaw?.id || currentRaw?.projectId || '';
  let projects = extractProjectItems(listResult).map((project, index) => normalizeProjectItem(project, index, currentId));

  if (currentRaw) {
    const normalizedCurrent = normalizeProjectItem(currentRaw, projects.length, currentId);
    const existingIndex = projects.findIndex((project) => project.id === normalizedCurrent.id);
    if (existingIndex >= 0) {
      projects[existingIndex] = { ...projects[existingIndex], ...normalizedCurrent, current: true };
    } else {
      projects = [{ ...normalizedCurrent, current: true }, ...projects];
    }
  }

  const currentProject = projects.find((project) => project.current && !project.archived) || projects.find((project) => !project.archived) || null;
  return {
    apiReady: true,
    projects: projects.map((project) => ({ ...project, current: currentProject ? project.id === currentProject.id : project.current })),
    currentProject: currentProject ? { ...currentProject, current: true } : null,
    status: sanitizeDisplayText(statusResult?.status || statusResult?.state || currentProject?.status, currentProject ? '已连接' : '等待项目')
  };
}

async function loadProjectState() {
  const hasList = hasEcorexFunction(['listProjects', 'projects.list']);
  const hasStatus = hasEcorexFunction(['getProjectStatus', 'projects.status']);
  if (!projectApiAvailable() || (!hasList && !hasStatus)) {
    return { ok: false, missing: true, apiReady: false, projects: [], currentProject: null, status: '项目服务未就绪' };
  }

  const [listResult, statusResult] = await Promise.all([
    hasList ? callEcorex(['listProjects', 'projects.list']) : Promise.resolve(null),
    hasStatus ? callEcorex(['getProjectStatus', 'projects.status']) : Promise.resolve(null)
  ]);

  if ([listResult, statusResult].some((result) => result?.unauthorized)) {
    return { ok: false, unauthorized: true, apiReady: true, projects: [], currentProject: null, status: '登录状态已过期' };
  }

  return { ok: true, ...normalizeProjectState(listResult, statusResult) };
}

async function createManagedProject(input) {
  const payload = typeof input === 'string' ? { name: input } : input;
  const result = await callEcorexAction(['createProject', 'projects.create'], payload);
  return result;
}

async function switchManagedProject(projectId) {
  const result = await callEcorexAction(['switchProject', 'projects.switch'], { id: projectId, projectId });
  return result;
}

async function updateManagedProject(projectId, patch = {}) {
  return callEcorexAction(['updateProject', 'projects.update'], { ...patch, id: projectId, projectId });
}

async function archiveManagedProject(projectId) {
  return callEcorexAction(['archiveProject', 'projects.archive'], { id: projectId, projectId });
}

function DiagnosticsView({ backendStatus, backendError, capabilities, authStatus, refreshBackend, onUnauthorized }) {
  const [diagnostics, setDiagnostics] = useState(null);
  const [settings, setSettings] = useState(() => normalizeSettingsState(backendStatus?.settings));
  const [workspace, setWorkspace] = useState({ entries: [], workspaceRoot: '' });
  const [sessions, setSessions] = useState([]);
  const [secretState, setSecretState] = useState({
    apiReady: false,
    loading: false,
    rows: MANAGED_SECRET_DEFINITIONS.map((definition) => normalizeSecretStatus(definition)),
    notice: ''
  });
  const [secretDrafts, setSecretDrafts] = useState({});
  const [secretBusy, setSecretBusy] = useState('');
  const [modelHealth, setModelHealth] = useState({ apiReady: false, profiles: [], notice: '' });
  const [projectState, setProjectState] = useState({
    apiReady: false,
    loading: false,
    projects: [],
    currentProject: null,
    status: '项目服务未就绪',
    notice: ''
  });
  const [newProjectName, setNewProjectName] = useState('');
  const [projectBusy, setProjectBusy] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState('');
  const [notice, setNotice] = useState('');
  const [sessionBusy, setSessionBusy] = useState('');
  const [diagnosticsExporting, setDiagnosticsExporting] = useState(false);
  const [diagnosticsExportResult, setDiagnosticsExportResult] = useState(null);
  const [diagnosticsOpenBusy, setDiagnosticsOpenBusy] = useState(false);
  const [crashRecovery, setCrashRecovery] = useState(() => normalizeCrashRecoveryStatus(
    backendStatus?.crashRecovery || backendStatus?.recovery || { ok: false, missing: true },
    backendStatus,
    backendStatus
  ));

  const permissionOptions = useMemo(() => {
    return permissionOptionsForUi();
  }, [capabilities]);

  const modelOptions = useMemo(() => {
    const models = capabilities?.models;
    if (Array.isArray(models) && models.length) return models.map((item) => [item.value, item.label]);
    return [
      ['sonnet', 'Sonnet'],
      ['opus', 'Opus']
    ];
  }, [capabilities]);

  async function loadDiagnostics() {
    setLoading(true);
    setNotice('');
    try {
      const [diagResult, settingsResult, workspaceResult, sessionResult, crashResult] = await Promise.all([
        callEcorex(['getDiagnostics', 'diagnostics.get']),
        callEcorex(['getSettings', 'settings.get']),
        callEcorex(['listWorkspace', 'workspace.list'], { relativePath: '' }),
        callEcorex(['getAgentSessions', 'agent.getSessions']),
        callEcorex(['getCrashRecoveryStatus', 'crashRecovery.getStatus', 'diagnostics.getCrashRecoveryStatus'])
      ]);

      const nextDiagnostics = diagResult?.ok === false ? null : (diagResult?.diagnostics || diagResult);
      const nextSettings = settingsResult?.settings || nextDiagnostics?.settings || backendStatus?.settings;
      if ([diagResult, settingsResult, workspaceResult, sessionResult, crashResult].some((result) => result?.unauthorized)) {
        onUnauthorized?.();
        setNotice('登录状态已过期，请重新登录后查看诊断信息。');
        return;
      }
      setDiagnostics(nextDiagnostics);
      setSettings(normalizeSettingsState(nextSettings || settings));
      setWorkspace({
        entries: workspaceResult?.entries || nextDiagnostics?.workspace?.entries || [],
        workspaceRoot: workspaceResult?.workspaceRoot || nextDiagnostics?.workspaceRoot || nextSettings?.workspaceRoot || ''
      });
      setSessions((Array.isArray(sessionResult) ? sessionResult : (sessionResult?.sessions || nextDiagnostics?.runningSessions || []))
        .map((session, index) => normalizeRunningSession(session, index, 'api'))
        .filter(isRunningSessionActive));
      setCrashRecovery(normalizeCrashRecoveryStatus(
        crashResult?.ok === false ? crashResult : (crashResult?.status || crashResult?.crashRecovery || crashResult?.recovery || crashResult),
        nextDiagnostics,
        backendStatus
      ));
      if (diagResult?.missing || settingsResult?.missing || workspaceResult?.missing || crashResult?.missing) {
        setNotice('部分本地能力服务未就绪，已使用现有状态与本地默认值展示。');
      }
    } finally {
      setLoading(false);
    }
  }

  async function refreshSecretState({ silent = false } = {}) {
    if (!silent) setSecretState((current) => ({ ...current, loading: true, notice: '' }));
    const result = await loadManagedSecretStatuses();
    if (result.unauthorized) {
      onUnauthorized?.();
      setSecretState((current) => ({
        ...current,
        loading: false,
        apiReady: true,
        notice: '登录状态已过期，请重新登录后查看密钥授权。'
      }));
      return;
    }
    setSecretState({
      apiReady: !result.missing,
      loading: false,
      rows: result.rows || MANAGED_SECRET_DEFINITIONS.map((definition) => normalizeSecretStatus(definition)),
      notice: result.missing ? '安全存储未就绪' : ''
    });
  }

  async function refreshProjectState({ silent = false } = {}) {
    if (!silent) setProjectState((current) => ({ ...current, loading: true, notice: '' }));
    const result = await loadProjectState();
    if (result.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({
        ...current,
        loading: false,
        apiReady: true,
        status: '登录状态已过期',
        notice: '请重新登录后查看项目工作区。'
      }));
      return;
    }
    setProjectState({
      apiReady: !result.missing,
      loading: false,
      projects: result.projects || [],
      currentProject: result.currentProject || null,
      status: result.status || (result.missing ? '项目服务未就绪' : '等待项目'),
      notice: result.missing ? '项目服务未就绪' : ''
    });
  }

  async function refreshModelHealth() {
    const result = await loadModelProfiles(settings.defaultModel || 'sonnet');
    if (result?.unauthorized) {
      onUnauthorized?.();
      setModelHealth({ apiReady: true, profiles: [], notice: '登录状态已过期' });
      return;
    }
    if (result?.ok === false) {
      setModelHealth({
        apiReady: !result?.missing,
        profiles: [],
        notice: result?.missing ? '模型配置服务未就绪' : '模型配置读取失败',
        error: result?.error || ''
      });
      return;
    }
    setModelHealth({
      apiReady: !result?.missing,
      profiles: result?.profiles || [],
      notice: result?.missing ? '模型配置服务未就绪' : (result?.preview ? '预览模式，仅检查本地配置元数据' : ''),
      error: ''
    });
  }

  async function refreshDiagnosticsPage() {
    await Promise.all([
      loadDiagnostics(),
      refreshSecretState({ silent: true }),
      refreshProjectState({ silent: true }),
      refreshModelHealth()
    ]);
  }

  useEffect(() => {
    refreshDiagnosticsPage();
  }, []);

  async function saveSecret(key) {
    const value = String(secretDrafts[key] || '').trim();
    if (!value || !secretState.apiReady) return;
    setSecretBusy(key);
    const result = await setManagedSecret(key, value);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setSecretState((current) => ({ ...current, notice: '登录状态已过期，请重新登录后保存授权。' }));
    } else if (result?.ok === false) {
      setSecretState((current) => ({ ...current, notice: result.missing ? '安全存储未就绪' : `授权保存失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setSecretDrafts((current) => ({ ...current, [key]: '' }));
      setSecretState((current) => ({ ...current, notice: '授权信息已安全保存。' }));
      await refreshSecretState({ silent: true });
    }
    setSecretBusy('');
  }

  async function removeSecret(key) {
    if (!secretState.apiReady) return;
    setSecretBusy(key);
    const result = await deleteManagedSecret(key);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setSecretState((current) => ({ ...current, notice: '登录状态已过期，请重新登录后删除授权。' }));
    } else if (result?.ok === false) {
      setSecretState((current) => ({ ...current, notice: result.missing ? '安全存储未就绪' : `授权删除失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setSecretDrafts((current) => ({ ...current, [key]: '' }));
      setSecretState((current) => ({ ...current, notice: '授权信息已删除。' }));
      await refreshSecretState({ silent: true });
    }
    setSecretBusy('');
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name || !projectState.apiReady) return;
    setProjectBusy('create');
    const result = await createManagedProject(name);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后创建项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目创建失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setNewProjectName('');
      setProjectState((current) => ({ ...current, notice: '项目已创建。' }));
      await refreshProjectState({ silent: true });
    }
    setProjectBusy('');
  }

  async function switchProject(project) {
    if (!project?.id || project.current || !projectState.apiReady) return;
    setProjectBusy(project.id);
    const result = await switchManagedProject(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后切换项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目切换失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: '当前项目已切换。' }));
      await refreshProjectState({ silent: true });
    }
    setProjectBusy('');
  }

  async function saveSettings(patch) {
    const nextSettings = normalizeSettingsState({ ...settings, ...patch });
    setSettings(nextSettings);
    if (
      Object.prototype.hasOwnProperty.call(patch, 'permissionMode') ||
      Object.prototype.hasOwnProperty.call(patch, 'defaultPermissionMode') ||
      Object.prototype.hasOwnProperty.call(patch, 'accessMode')
    ) {
      storeDefaultAccessMode(nextSettings.accessMode);
    }
    setSaving(Object.keys(patch)[0] || 'settings');
    setNotice('');
    const permissionPatch =
      Object.prototype.hasOwnProperty.call(patch, 'permissionMode') ||
      Object.prototype.hasOwnProperty.call(patch, 'defaultPermissionMode') ||
      Object.prototype.hasOwnProperty.call(patch, 'accessMode');
    const result = await callEcorex(
      ['updateSettings', 'settings.update'],
      permissionPatch ? { ...patch, ...fullAccessConfirmationFields(nextSettings.accessMode) } : patch
    );
    if (result?.ok === false) {
      if (result.unauthorized) {
        onUnauthorized?.();
        setNotice('登录状态已过期，请重新登录后保存设置。');
        setSaving('');
        return result;
      }
        setNotice(result.missing ? '设置服务未就绪，当前仅更新界面预览值。' : `设置保存失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setSettings(normalizeSettingsState(result.settings || nextSettings));
      setNotice('设置已保存。');
      refreshBackend?.();
    }
    setSaving('');
    return result;
  }

  async function selectWorkspaceDirectory() {
    setSaving('workspaceRoot');
    setNotice('正在打开系统目录选择器...');
    let selection = await callEcorex([
      'selectWorkspaceDirectory',
      'workspace.selectDirectory',
      'workspace.selectWorkspaceDirectory',
      'workspace.select'
    ], { current: settings.workspaceRoot });

    if (selection?.unauthorized) {
      onUnauthorized?.();
      setNotice('登录状态已过期，请重新登录后选择工作区。');
      setSaving('');
      return;
    }

    if (selection?.ok === false && selection.missing) {
      selection = promptWorkspaceDirectoryFallback(settings.workspaceRoot);
    } else if (selection?.ok === false) {
      setNotice(`目录选择失败：${sanitizeDisplayText(selection.error, '请稍后重试')}`);
      setSaving('');
      return;
    }

    const workspaceRoot = selectedWorkspacePathFromResult(selection);
    if (!workspaceRoot) {
      setNotice(selection?.canceled || selection?.cancelled ? '已取消选择工作区。' : '未选择有效目录。');
      setSaving('');
      return;
    }

    setNotice('目录已选择，等待确认保存。');
    if (!confirmCustomWorkspaceChange(workspaceRoot)) {
      setNotice('已取消自定义工作区变更。');
      setSaving('');
      return;
    }

    setSaving('');
    const result = await saveSettings({
      workspaceRoot,
      ...customWorkspaceConfirmationFields(workspaceRoot)
    });
    if (result?.ok === false && !result.missing) return;
    await loadDiagnostics();
    setNotice(selection?.fallback ? '自定义工作区已确认。目录选择接口未就绪，已使用手动确认值。' : '自定义工作区已确认并保存，建议完成一次校验。');
  }

  async function restoreDefaultWorkspace() {
    if (!settings.workspaceRoot) {
      setNotice('当前已经使用默认工作区。');
      return;
    }
    if (typeof window !== 'undefined' && typeof window.confirm === 'function' && !window.confirm([
      '恢复默认工作区？',
      '',
      settings.workspaceRoot,
      '',
      '自定义目录将不再作为默认上下文，后续任务会回到应用默认工作区。'
    ].join('\n'))) {
      setNotice('已取消恢复默认工作区。');
      return;
    }
    const result = await saveSettings({
      workspaceRoot: '',
      customWorkspaceConfirmed: false,
      workspaceRootConfirmed: false
    });
    if (result?.ok === false && !result.missing) return;
    await loadDiagnostics();
    setNotice('已恢复默认工作区，自定义目录不再作为默认上下文。');
  }

  async function revokeFullAccessSetting() {
    const result = await saveSettings({
      accessMode: 'default',
      permissionMode: 'default',
      defaultPermissionMode: 'default'
    });
    if (result?.ok === false && !result.missing) return;
    setNotice('已撤销完全访问权限，恢复默认安全规则。');
  }

  async function exportDiagnosticsBundle() {
    setDiagnosticsExporting(true);
    setNotice('');
    setDiagnosticsExportResult(null);
    const snapshot = buildDiagnosticsBundleSnapshot({
      diagnostics,
      backendStatus,
      settings,
      workspace,
      sessions,
      projectState,
      modelHealth,
      crashRecovery
    });

    const result = await callEcorex([
      'exportDiagnosticsBundle',
      'diagnostics.exportBundle',
      'diagnostics.exportDiagnosticsBundle',
      'diagnostics.export'
    ], snapshot);

    if (result?.unauthorized) {
      onUnauthorized?.();
      setNotice('登录状态已过期，请重新登录后导出诊断包。');
      setDiagnosticsExporting(false);
      return;
    }

    if (result?.ok === false && result.missing) {
      const fallbackResult = await exportDiagnosticsBundleFallback(snapshot);
      if (fallbackResult?.ok) setDiagnosticsExportResult(normalizeDiagnosticsExportResult(fallbackResult));
      setNotice(fallbackResult?.ok
        ? '诊断包已导出为备用 JSON 文件。浏览器下载位置由系统决定。'
        : '诊断导出接口未就绪，当前环境也无法下载备用 JSON。');
    } else if (result?.ok === false) {
      setNotice(`诊断包导出失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else if (result?.canceled || result?.cancelled) {
      setNotice('已取消导出诊断包。');
    } else {
      const exportResult = normalizeDiagnosticsExportResult(result);
      setDiagnosticsExportResult(exportResult);
      setNotice(exportResult.pathLabel ? `诊断包已导出：${exportResult.pathLabel}` : '诊断包已导出。');
    }
    setDiagnosticsExporting(false);
  }

  async function openDiagnosticsExportLocation() {
    if (!diagnosticsExportResult) return;
    setDiagnosticsOpenBusy(true);
    const result = await openDiagnosticsExportWithBridge(diagnosticsExportResult);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setNotice('登录状态已过期，请重新登录后打开诊断包位置。');
      setDiagnosticsOpenBusy(false);
      return;
    }

    if (result?.ok === false) {
      const target = diagnosticsExportResult.path || diagnosticsExportResult.directory || diagnosticsExportResult.pathLabel;
      const copied = await copyTextToClipboard(target);
      setNotice(result.missing
        ? (copied ? '打开位置接口未就绪，已复制诊断包位置。' : '打开位置接口未就绪，请在下载目录查看诊断包。')
        : `打开位置失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setNotice('已打开诊断包所在位置。');
    }
    setDiagnosticsOpenBusy(false);
  }

  async function copyDiagnosticsExportLocation() {
    if (!diagnosticsExportResult) return;
    const target = diagnosticsExportResult.path || diagnosticsExportResult.directory || diagnosticsExportResult.pathLabel;
    const copied = await copyTextToClipboard(target);
    setNotice(copied ? '诊断包位置已复制。' : '无法复制诊断包位置，请手动查看导出结果。');
  }

  async function ensureWorkspace() {
    setSaving('workspaceRoot');
    const result = await callEcorex(['ensureWorkspace', 'workspace.ensure'], { workspaceRoot: settings.workspaceRoot });
    if (result?.ok === false) {
      if (result.unauthorized) {
        onUnauthorized?.();
        setNotice('登录状态已过期，请重新登录后校验工作区。');
        setSaving('');
        return;
      }
      setNotice(result.missing ? '工作区服务未就绪，无法创建或校验目录。' : `工作区校验失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setNotice('工作区已校验。');
      loadDiagnostics();
    }
    setSaving('');
  }

  async function stopSession(sessionId) {
    if (!sessionId || sessionId === 'empty') return;
    setSessionBusy(sessionId);
    const result = await callEcorex(['stopPrompt', 'agent.stop'], sessionId);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setNotice('登录状态已过期，请重新登录后停止运行会话。');
    } else if (result?.ok === false) {
      setNotice(result.missing ? '运行会话服务未就绪。' : `停止会话失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
    } else {
      setSessions((items) => items.filter((session) => session.sessionId !== sessionId));
      setNotice('运行会话已停止。');
    }
    setSessionBusy('');
  }

  const effectiveAuth = normalizeAuthStatus(authStatus || backendStatus?.auth);
  const recentSessionFiles = diagnostics?.recentSessionFiles || backendStatus?.recentSessionFiles || authStatus?.recentSessionFiles || [];
  const recentHistory = (diagnostics?.recentSessionHistory || backendStatus?.recentSessionHistory || recentSessionFiles || [])
    .slice(0, 6)
    .map((item, index) => normalizeRecentHistoryItem(item, index));
  const currentProject = projectState.currentProject;
  const projectFileCount = currentProject ? currentProject.fileCount : 0;
  const projectSessionCount = currentProject ? currentProject.sessionCount : 0;
  const canCreateProject = projectState.apiReady && hasEcorexFunction(['createProject', 'projects.create']);
  const canSwitchProject = projectState.apiReady && hasEcorexFunction(['switchProject', 'projects.switch']);
  const releaseHealth = releasePackageSummary(diagnostics, backendStatus);
  const signatureHealth = signatureStatusSummary(diagnostics, backendStatus);
  const runtimeHealth = runtimeEngineSummary(diagnostics, backendStatus);
  const modelHealthSummary = modelConfigSummary(modelHealth, settings, capabilities);
  const localBuildHealth = localBuildSummary(diagnostics || {});
  const permissionHealth = permissionModeSummary(settings);
  const projectDirectoryHealth = projectDirectorySummary(projectState, workspace, settings);
  const recentTaskHealth = recentTaskSummary(sessions, recentHistory);
  const fullAccessEnabled = normalizeAccessMode(settings.accessMode) === 'fullAccess';
  const hasCustomWorkspace = Boolean(String(settings.workspaceRoot || '').trim());
  const workspaceHelperText = saving === 'workspaceRoot'
    ? '正在处理工作区目录...'
    : (hasCustomWorkspace ? '目录已通过二次确认；更换或恢复默认都会再次提示。' : '当前使用应用默认工作区；选择系统目录后会要求二次确认。');

  const statusItems = [
    ['认证状态', effectiveAuth.loggedIn ? '已登录' : '待登录', effectiveAuth.user?.email || effectiveAuth.account || effectiveAuth.mode || '本地认证', ShieldCheck, effectiveAuth.loggedIn ? 'ok' : 'warn'],
    ['服务状态', backendStatus?.ok ? '正常' : '待连接', sanitizeDisplayText(backendError || backendStatus?.error, '本地能力服务'), Activity, backendStatus?.ok ? 'ok' : 'warn'],
    ['运行引擎', runtimeHealth.value, runtimeHealth.detail, SquareTerminal, runtimeHealth.tone],
    ['工作区', workspace.workspaceRoot || settings.workspaceRoot || backendStatus?.workspaceRoot ? '已配置' : '未配置', `${workspace.entries.length} 个顶层条目`, LayoutDashboard, workspace.workspaceRoot || settings.workspaceRoot ? 'ok' : 'warn'],
    ['项目', currentProject?.name || (projectState.apiReady ? '暂无项目' : '未连接'), projectState.apiReady ? `${projectState.projects.length} 个项目 · ${projectFileCount} 个文件` : '项目服务未就绪', Box, currentProject ? 'ok' : 'warn'],
    ['模型配置', modelHealthSummary.value, modelHealthSummary.detail, Brain, modelHealthSummary.tone]
  ];

  const healthItems = [
    ['本地构建', localBuildHealth.value, localBuildHealth.detail, Upload, localBuildHealth.tone],
    ['Agent 引擎', runtimeHealth.value, runtimeHealth.detail, SquareTerminal, runtimeHealth.tone],
    ['模型配置', modelHealthSummary.value, modelHealth.notice || modelHealthSummary.detail, Brain, modelHealthSummary.tone],
    ['权限模式', permissionHealth.value, permissionHealth.detail, ShieldCheck, permissionHealth.tone],
    ['项目目录', projectDirectoryHealth.value, projectDirectoryHealth.detail, LayoutDashboard, projectDirectoryHealth.tone],
    ['最近任务', recentTaskHealth.value, recentTaskHealth.detail, Clock3, recentTaskHealth.tone],
    ['恢复状态', crashRecovery.value, crashRecovery.detail, AlertTriangle, crashRecovery.tone],
    ['签名状态', signatureHealth.value, signatureHealth.detail, Lock, signatureHealth.tone]
  ];

  const buildItems = [
    ['前端构建', diagnostics?.backend?.dist],
    ['本地服务', diagnostics?.backend?.source],
    ['能力索引', diagnostics?.backend?.sourceMap],
    ['运行组件', diagnostics?.backend?.nativeBinaryPackage],
    ['打包组件', diagnostics?.backend?.packagedBackend]
  ];

  return (
    <section className="diagnostics-page panel" data-testid="diagnostics-page">
      <HeaderBar
        title="诊断 / 设置"
        badge={window.ecorex ? '桌面端' : '预览模式'}
        subtitle="集中查看服务健康、本地能力、工作区、安装状态与运行会话，并维护亦芯默认运行参数"
        backendStatus={backendStatus}
        onRefresh={() => {
          refreshBackend?.({ refresh: true });
          refreshDiagnosticsPage();
        }}
      />

      <div className="diagnostics-summary">
        {statusItems.map(([label, value, detail, Icon, tone]) => (
          <article className={`diagnostic-card ${tone}`} key={label}>
            <span><Icon size={22} /></span>
            <div>
              <em>{label}</em>
              <strong>{value}</strong>
              <small>{detail}</small>
            </div>
          </article>
        ))}
      </div>

      <div className="diagnostics-content">
        <section className="settings-panel">
          <header>
            <h3>默认设置</h3>
            <div className="settings-header-actions">
              <button type="button" data-testid="diagnostics-export-button" onClick={exportDiagnosticsBundle} disabled={diagnosticsExporting}>
                {diagnosticsExporting ? <Loader2 size={16} className="spin-icon" /> : <Download size={16} />}
                导出诊断包
              </button>
              <button type="button" data-testid="diagnostics-refresh-button" onClick={refreshDiagnosticsPage} disabled={loading || secretState.loading || projectState.loading}>
                <Loader2 size={16} className={loading ? 'spin-icon' : ''} />
                刷新
              </button>
            </div>
          </header>
          {diagnosticsExportResult && (
            <div className="diagnostics-export-result" data-testid="diagnostics-export-result">
              <FileText size={17} />
              <div>
                <strong>{diagnosticsExportResult.fileName}</strong>
                <span title={diagnosticsExportResult.path || diagnosticsExportResult.pathLabel}>
                  {diagnosticsExportResult.pathLabel}
                </span>
                <em>
                  {diagnosticsExportResult.sizeLabel || '大小待返回'} · {formatDateTime(diagnosticsExportResult.exportedAt)}
                </em>
              </div>
              <div className="diagnostics-export-actions">
                <button
                  type="button"
                  onClick={openDiagnosticsExportLocation}
                  disabled={diagnosticsOpenBusy || diagnosticsExporting}
                  title={diagnosticsExportResult.path ? '打开诊断包所在位置' : '打开位置不可用时会复制导出信息'}
                >
                  {diagnosticsOpenBusy ? <Loader2 size={14} className="spin-icon" /> : <FolderOpen size={14} />}
                  打开位置
                </button>
                <button type="button" onClick={copyDiagnosticsExportLocation} disabled={diagnosticsExporting} title="复制诊断包路径或文件名">
                  <Copy size={14} />
                  复制
                </button>
              </div>
            </div>
          )}
          <div className="settings-grid">
            <label>
              <span>默认模型</span>
              <select value={settings.defaultModel} onChange={(event) => saveSettings({ defaultModel: event.target.value })}>
                {modelOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
              </select>
            </label>
            <div className="setting-block permission-setting-block" data-testid="permission-setting-block">
              <span className="setting-title">默认权限策略</span>
              <select
                data-testid="permission-mode-select"
                value={settings.accessMode}
                aria-label="默认权限策略"
                title={fullAccessEnabled ? FULL_ACCESS_PERMISSION_OPTION.description : DEFAULT_PERMISSION_OPTION.description}
                onChange={(event) => {
                  const accessMode = normalizeAccessMode(event.target.value);
                  if (accessMode === 'fullAccess' && normalizeAccessMode(settings.accessMode) !== 'fullAccess' && !confirmFullAccessChange()) {
                    return;
                  }
                  const nextPermissionMode = permissionModeFromAccessMode(accessMode);
                  saveSettings({
                    accessMode,
                    permissionMode: nextPermissionMode,
                    defaultPermissionMode: nextPermissionMode
                  });
                }}
              >
                {permissionOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
              <div className={`permission-status-strip ${fullAccessEnabled ? 'warn' : 'ok'}`}>
                {fullAccessEnabled ? <AlertTriangle size={16} /> : <ShieldCheck size={16} />}
                <div>
                  <strong>{fullAccessEnabled ? '当前默认：完全访问权限' : '当前默认：默认权限'}</strong>
                  <span>
                    {fullAccessEnabled
                      ? '本地执行确认会被跳过，仅建议用于可信工作区。'
                      : '敏感本地操作会继续请求确认。'}
                  </span>
                </div>
                {fullAccessEnabled && (
                  <button type="button" onClick={revokeFullAccessSetting} disabled={Boolean(saving)}>
                    撤销
                  </button>
                )}
              </div>
              <div className="permission-risk-grid">
                <span className="safe">
                  <ShieldCheck size={13} />
                  默认权限会在文件写入、命令执行和系统目录访问前继续确认。
                </span>
                <span className="risk">
                  <AlertTriangle size={13} />
                  完全访问会跳过本地执行确认，只适合可信工作区和明确任务。
                </span>
              </div>
            </div>
            <div className="setting-block wide workspace-setting-block" data-testid="workspace-setting-block">
              <span className="setting-title">默认工作区</span>
              <div className={`workspace-root-card ${hasCustomWorkspace ? 'custom' : 'default'}`} data-testid="workspace-root-card">
                <FolderOpen size={17} />
                <div>
                  <strong title={settings.workspaceRoot || '使用系统默认工作区'}>
                    {hasCustomWorkspace ? settings.workspaceRoot : '使用系统默认工作区'}
                  </strong>
                  <span>{hasCustomWorkspace ? '已确认自定义目录' : '未指定自定义目录'}</span>
                </div>
              </div>
              <div className={`workspace-confirmation-strip ${hasCustomWorkspace ? 'custom' : 'default'}`}>
                {hasCustomWorkspace ? <Check size={14} /> : <RotateCcw size={14} />}
                <span>{workspaceHelperText}</span>
              </div>
              <div className="workspace-action-row">
                <button type="button" onClick={selectWorkspaceDirectory} disabled={saving === 'workspaceRoot'} title="打开系统目录选择器；选择后需要二次确认">
                  {saving === 'workspaceRoot' ? <Loader2 size={15} className="spin-icon" /> : <FolderOpen size={15} />}
                  选择目录
                </button>
                <button type="button" data-testid="workspace-ensure-button" onClick={ensureWorkspace} disabled={saving === 'workspaceRoot'} title="校验当前工作区目录是否可用">
                  {saving === 'workspaceRoot' ? <Loader2 size={15} className="spin-icon" /> : <Check size={15} />}
                  校验
                </button>
                <button type="button" onClick={restoreDefaultWorkspace} disabled={!hasCustomWorkspace || saving === 'workspaceRoot'} title="恢复应用默认工作区">
                  <RotateCcw size={15} />
                  恢复默认
                </button>
              </div>
              <small>{hasCustomWorkspace ? '更换自定义目录时会要求显式确认。' : '可选择可信项目目录作为默认上下文。'}</small>
            </div>
            <label>
              <span>最大提示长度</span>
              <input
                min="1000"
                step="1000"
                type="number"
                value={settings.maxPromptChars}
                onChange={(event) => setSettings((current) => ({ ...current, maxPromptChars: Number(event.target.value) }))}
                onBlur={() => saveSettings({ maxPromptChars: settings.maxPromptChars })}
              />
            </label>
            <label className="switch-row">
              <span>自动刷新服务状态</span>
              <button
                className={`toggle ${settings.autoRefreshBackend ? 'on' : ''}`}
                type="button"
                onClick={() => saveSettings({ autoRefreshBackend: !settings.autoRefreshBackend })}
              >
                <span />
              </button>
            </label>
          </div>
          <div className="settings-actions">
            <button type="button" onClick={() => saveSettings(settings)} disabled={Boolean(saving)}>
              <Settings size={16} />
              {saving ? '保存中' : '保存设置'}
            </button>
          </div>
          {notice && <p className="diagnostics-notice">{notice}</p>}
          <div className="auth-diagnostics health-check-panel">
            <h4>可发布前检查</h4>
            <div className="health-check-grid">
              {healthItems.map(([label, value, detail, Icon, tone]) => (
                <div className={`health-check-item ${tone}`} key={label}>
                  <Icon size={16} />
                  <div>
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <em>{detail}</em>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="auth-diagnostics">
            <h4>认证与最近会话记录</h4>
            <div className="auth-state-row">
              <ShieldCheck size={17} />
              <strong>{effectiveAuth.loggedIn ? '已登录' : '未登录'}</strong>
              <span>{effectiveAuth.user?.email || effectiveAuth.account || effectiveAuth.mode || '认证状态待返回'}</span>
            </div>
            <div className="recent-session-files">
              {(recentHistory.length ? recentHistory : [normalizeRecentHistoryItem('暂无会话记录')]).map((item) => (
                <div className={`recent-session-file ${item.tone}`} key={item.id}>
                  <FileText size={15} />
                  <span>{item.title}</span>
                  <em>{item.detail}</em>
                </div>
              ))}
            </div>
          </div>
          <div className="secret-panel">
            <div className="secret-panel-head">
              <h4>密钥与授权</h4>
              <button type="button" onClick={() => refreshSecretState()} disabled={secretState.loading}>
                <Loader2 size={14} className={secretState.loading ? 'spin-icon' : ''} />
                更新
              </button>
            </div>
            {!secretState.apiReady && <p className="security-status-note">安全存储未就绪</p>}
            <div className="secret-list">
              {secretState.rows.map((secret) => (
                <div className={`secret-entry ${secret.tone}`} key={secret.key}>
                  <div className="secret-title">
                    <Lock size={15} />
                    <div>
                      <strong>{secret.label}</strong>
                      <em>{secret.configured ? '授权已加密保存' : '等待本机授权'}</em>
                    </div>
                  </div>
                  <div className="secret-status">
                    <span>{secret.masked}</span>
                    <em>{secret.status}</em>
                  </div>
                  <input
                    type="password"
                    value={secretDrafts[secret.key] || ''}
                    onChange={(event) => setSecretDrafts((current) => ({ ...current, [secret.key]: event.target.value }))}
                    placeholder={secret.configured ? '输入新授权值以更新' : '输入授权值'}
                    disabled={!secretState.apiReady || secretBusy === secret.key}
                  />
                  <div className="secret-actions">
                    <button
                      type="button"
                      onClick={() => saveSecret(secret.key)}
                      disabled={!secretState.apiReady || !String(secretDrafts[secret.key] || '').trim() || secretBusy === secret.key}
                    >
                      <Check size={14} />
                      保存
                    </button>
                    <button
                      type="button"
                      onClick={() => removeSecret(secret.key)}
                      disabled={!secretState.apiReady || !secret.configured || secretBusy === secret.key}
                    >
                      <X size={14} />
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
            {secretState.notice && (secretState.apiReady || secretState.notice !== '安全存储未就绪') && (
              <p className="security-status-note">{secretState.notice}</p>
            )}
          </div>
        </section>

        <section className="diagnostics-list-panel">
          <header>
            <h3>工作区</h3>
            <small>{workspace.workspaceRoot || settings.workspaceRoot ? '已配置' : '未连接'}</small>
          </header>
          <div className="workspace-entry-list">
            {(workspace.entries.length ? workspace.entries : [
              { name: '本地能力服务未就绪', type: 'info', size: 0, modified: '使用降级预览' }
            ]).map((entry) => (
              <div className="workspace-entry" key={`${entry.path || entry.name}-${entry.type}`}>
                <FileText size={17} />
                <strong>{sanitizeDisplayText(entry.name, '工作区条目')}</strong>
                <span>{formatWorkspaceEntryType(entry)}</span>
                <em>{entry.modifiedAt || entry.modified || entry.size || '-'}</em>
              </div>
            ))}
          </div>
        </section>

        <section className="diagnostics-list-panel project-workspace-panel" data-testid="project-workspace-panel">
          <header>
            <h3>项目工作区</h3>
            <small>{projectState.apiReady ? `${projectState.projects.length} 个项目` : '项目服务未就绪'}</small>
          </header>
          <div className={`project-current-summary ${currentProject ? 'ok' : 'warn'}`} data-testid="project-current-summary">
            <LayoutDashboard size={18} />
            <div>
              <strong>{currentProject?.name || (projectState.apiReady ? '暂无当前项目' : '项目工作区未连接')}</strong>
              <span>
                {projectState.apiReady
                  ? `${projectFileCount} 个文件 · ${projectSessionCount} 个会话 · ${currentProject?.updatedAt ? formatDateTime(currentProject.updatedAt) : '更新时间待返回'}`
                  : '项目服务未就绪，暂不能创建或切换项目'}
              </span>
            </div>
          </div>
          <div className="project-create-row">
            <input
              data-testid="project-create-input"
              value={newProjectName}
              onChange={(event) => setNewProjectName(event.target.value)}
              placeholder={canCreateProject ? '输入项目名称' : '项目创建暂不可用'}
              disabled={!canCreateProject || projectBusy === 'create'}
            />
            <button type="button" data-testid="project-create-button" onClick={createProject} disabled={!canCreateProject || !newProjectName.trim() || projectBusy === 'create'}>
              <Plus size={15} />
              新建
            </button>
          </div>
          <div className="project-workspace-list">
            {(projectState.projects.length ? projectState.projects : [{ id: 'empty', name: projectState.apiReady ? '暂无项目' : '项目服务未就绪', fileCount: 0, sessionCount: 0, updatedAt: '', status: projectState.status }]).map((project) => (
              <div className={`project-workspace-entry ${project.current ? 'active' : ''}`} data-testid="project-workspace-entry" key={project.id}>
                <LayoutDashboard size={16} />
                <div>
                  <strong>{project.name}</strong>
                  <em>{project.updatedAt ? formatDateTime(project.updatedAt) : sanitizeDisplayText(project.status, '等待项目')}</em>
                </div>
                <span>{project.fileCount || 0} 文件</span>
                <span>{project.sessionCount || 0} 会话</span>
                <button
                  data-testid="project-switch-button"
                  type="button"
                  onClick={() => switchProject(project)}
                  disabled={!canSwitchProject || project.id === 'empty' || project.current || projectBusy === project.id}
                >
                  {project.current ? '当前' : '切换'}
                </button>
              </div>
            ))}
          </div>
          {projectState.notice && <p className="diagnostics-notice compact">{projectState.notice}</p>}
        </section>

        <section className="diagnostics-list-panel">
          <header>
            <h3>构建包</h3>
            <small>{diagnostics?.app?.version ? `v${diagnostics.app.version}` : '本地构建状态'}</small>
          </header>
          <div className="build-entry-list">
            {buildItems.map(([label, item]) => (
              <div className={`build-entry ${item?.exists ? 'ok' : 'warn'}`} key={label}>
                <span className={item?.exists ? 'dot ok' : 'dot warn'} />
                <strong>{label}</strong>
                <em>{item?.exists ? '已检测' : '未检测'}</em>
                <small>{item?.exists ? (item.sizeMb ? `${item.sizeMb}MB` : '存在') : '缺失'}</small>
              </div>
            ))}
          </div>
        </section>

        <section className="diagnostics-list-panel sessions-panel">
          <header>
            <h3>运行会话</h3>
            <div className="panel-header-actions">
              <small>{sessions.length} 个活动会话</small>
              <button type="button" onClick={loadDiagnostics} disabled={loading} title="刷新运行会话和崩溃恢复状态">
                <Loader2 size={13} className={loading ? 'spin-icon' : ''} />
                刷新
              </button>
            </div>
          </header>
          <div className={`crash-recovery-status ${crashRecovery.tone}`}>
            <div className="crash-recovery-main">
              <span>{crashRecovery.tone === 'ok' ? <ShieldCheck size={17} /> : <AlertTriangle size={17} />}</span>
              <div>
                <strong>最近崩溃 / 恢复状态</strong>
                <em>{crashRecovery.summary}</em>
                <small className="crash-recovery-detail">{crashRecovery.detail}</small>
              </div>
              <small>{crashRecovery.apiReady ? crashRecovery.value : '降级展示'}</small>
            </div>
            <div className="crash-recovery-list">
              {(crashRecovery.entries.length ? crashRecovery.entries : [{
                id: 'crash-recovery-empty',
                title: crashRecovery.value || '状态待返回',
                detail: crashRecovery.detail || '暂无最近崩溃记录',
                time: '-',
                tone: crashRecovery.tone
              }]).map((item) => (
                <div className={`crash-recovery-entry ${item.tone}`} key={item.id}>
                  <span className={`dot ${item.tone === 'ok' ? 'ok' : 'warn'}`} />
                  <strong>{item.title}</strong>
                  <em>{item.detail}</em>
                  <small>{item.time}</small>
                </div>
              ))}
            </div>
          </div>
          <div className="session-list">
            {(sessions.length ? sessions : [{ sessionId: 'empty', status: 'idle', prompt: '当前没有运行中的本地任务' }]).map((session, index) => (
              <div className="session-entry" key={session.sessionId || session.id}>
                <Bot size={17} />
                <strong>{sessions.length ? `会话 ${index + 1}` : '暂无运行会话'}</strong>
                <span>{formatSessionStatus(session.status || session.state)}</span>
                <em>{sanitizeDisplayText(session.prompt, '等待任务提交')}</em>
                {sessions.length > 0 && (
                  <button type="button" onClick={() => stopSession(session.sessionId || session.id)} disabled={sessionBusy === (session.sessionId || session.id)}>
                    {sessionBusy === (session.sessionId || session.id) ? '停止中' : '停止'}
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>
      </div>
    </section>
  );
}

function normalizeSkillItem(skill = {}, index = 0, source = 'api') {
  const rawName = skill.name || skill.id || skill.slug || `skill-${index + 1}`;
  const installed = boolFrom(skill.installed ?? skill.available ?? skill.local, source !== 'preview');
  const enabled = boolFrom(skill.enabled ?? skill.active ?? skill.available, installed);
  const updateAvailable = boolFrom(skill.updateAvailable ?? skill.hasUpdate ?? skill.outdated, false);
  const category = formatSkillCategory(skill.category || skill.type || skill.group || skill.kind || 'uncategorized');
  const error = sanitizeDisplayText(skill.error || skill.lastError || '', '');
  const status = sanitizeDisplayText(skill.status || (error ? '异常' : (enabled ? '已启用' : (installed ? '已停用' : '未安装'))), enabled ? '已启用' : '未安装');
  const tone = error ? 'danger' : (updateAvailable ? 'running' : (enabled ? 'success' : 'pending'));

  return {
    id: skill.id || skill.key || rawName,
    name: rawName,
    title: sanitizeDisplayText(skill.title || skill.displayName || formatPluginName(rawName), `能力 ${index + 1}`),
    description: sanitizeDisplayText(skill.description || skill.summary, '能力说明待返回。'),
    category,
    installed,
    enabled,
    updateAvailable,
    status,
    tone,
    version: skill.version || skill.currentVersion || '',
    latestVersion: skill.latestVersion || skill.nextVersion || '',
    commands: Number(skill.commands || skill.commandCount || 0),
    agents: Number(skill.agents || skill.agentCount || 0),
    skills: Number(skill.skills || skill.skillCount || 0),
    hooks: Number(skill.hooks || skill.hookCount || 0),
    updatedAt: skill.updatedAt || skill.updated || skill.modifiedAt || skill.lastUpdated || '',
    error,
    source,
    raw: skill
  };
}

function SkillsView({ backendStatus, capabilities, refreshBackend, onUnauthorized }) {
  const [skills, setSkills] = useState([]);
  const [view, setView] = useState('all');
  const [category, setCategory] = useState('all');
  const [query, setQuery] = useState('');
  const [state, setState] = useState(window.ecorex ? 'loading' : 'offline');
  const [notice, setNotice] = useState('');
  const [actionKey, setActionKey] = useState('');
  const [lastLoadedAt, setLastLoadedAt] = useState('');

  async function loadSkills({ silent = false } = {}) {
    if (!silent) {
      setState((current) => (current === 'offline' ? 'offline' : 'loading'));
      setNotice('');
    }

    if (!window.ecorex) {
      setSkills([]);
      setState('offline');
      setNotice('预览模式：EcoreX 默认不展示内置 Skill，后续可在应用托管能力库中自行安装。');
      setLastLoadedAt(formatDateTime(new Date().toISOString()));
      return;
    }

    const result = await callEcorex([
      'listSkills',
      'getSkills',
      'skills.list',
      'skill.list',
      'listPlugins',
      'getPlugins',
      'plugins.list',
      'plugin.list'
    ]);

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后管理能力。');
      return;
    }

    if (result?.ok === false && !result.missing) {
      setSkills([]);
      setState('error');
      setNotice(sanitizeDisplayText(result.error, '能力列表加载失败。'));
      return;
    }

    let source = 'api';
    let items = result?.ok === false && result.missing
      ? []
      : extractCollection(result, ['skills', 'plugins', 'capabilities.skills', 'capabilities.plugins']);

    if (!items.length && result?.missing) {
      setSkills([]);
      setState('unsupported');
      setNotice('本地能力服务未就绪。');
      return;
    }

    const nextSkills = items
      .filter((item) => !isNativeSkillItem(item))
      .map((item, index) => normalizeSkillItem(item, index, source));
    setSkills(nextSkills);
    setState(nextSkills.length ? 'ready' : 'empty');
    setLastLoadedAt(formatDateTime(new Date().toISOString()));
    if (source !== 'api') {
      setNotice('本地能力服务未完全就绪，当前展示可用能力快照；操作会继续等待确认。');
    } else if (!silent) {
      setNotice('');
    }
  }

  useEffect(() => {
    loadSkills();
  }, []);

  async function runSkillAction(skill, action) {
    const actionLabel = {
      install: '安装',
      update: '更新',
      enable: '启用',
      disable: '禁用'
    }[action] || '操作';
    const payload = {
      id: skill.id,
      name: skill.name,
      skill,
      enabled: action === 'enable'
    };
    const candidates = {
      install: ['installSkill', 'skills.install', 'skill.install', 'installPlugin', 'plugins.install', 'plugin.install'],
      update: ['updateSkill', 'skills.update', 'skill.update', 'updatePlugin', 'plugins.update', 'plugin.update'],
      enable: ['enableSkill', 'skills.enable', 'skill.enable', 'setSkillEnabled', 'skills.setEnabled', 'enablePlugin', 'plugins.enable'],
      disable: ['disableSkill', 'skills.disable', 'skill.disable', 'setSkillEnabled', 'skills.setEnabled', 'disablePlugin', 'plugins.disable']
    }[action] || [];

    setActionKey(`${skill.id}:${action}`);
    setNotice('');
    const result = await callEcorexAction(candidates, payload);
    setActionKey('');

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后继续操作。');
      return;
    }

    if (result?.ok === false) {
      setNotice(result.missing
        ? `本地能力服务未就绪，未执行${actionLabel}。`
        : `能力${actionLabel}失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
      return;
    }

    setNotice(`能力${actionLabel}已确认。`);
    refreshBackend?.();
    await loadSkills({ silent: true });
  }

  const categories = useMemo(() => {
    const values = [...new Set(skills.map((skill) => skill.category).filter(Boolean))];
    return ['all', ...values];
  }, [skills]);

  const filteredSkills = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return skills.filter((skill) => {
      const matchesView =
        view === 'all' ||
        (view === 'enabled' && skill.enabled) ||
        (view === 'updates' && skill.updateAvailable) ||
        (view === 'errors' && skill.error);
      const matchesCategory = category === 'all' || skill.category === category;
      const text = `${skill.title} ${skill.name} ${skill.description} ${skill.category}`.toLowerCase();
      return matchesView && matchesCategory && (!keyword || text.includes(keyword));
    });
  }, [skills, view, category, query]);

  const totals = useMemo(() => ({
    total: skills.length,
    installed: skills.filter((skill) => skill.installed).length,
    enabled: skills.filter((skill) => skill.enabled).length,
    updates: skills.filter((skill) => skill.updateAvailable).length,
    errors: skills.filter((skill) => skill.error).length
  }), [skills]);

  const actionDisabled = state === 'offline' || state === 'unsupported' || state === 'unauthorized';

  return (
    <section className="management panel">
      <HeaderBar
        title="能力中心"
        badge={state === 'offline' ? '预览' : undefined}
        subtitle="管理本机可用能力、协同助手与工作流入口；安装、更新和启停动作都会等待确认"
        backendStatus={backendStatus}
        onRefresh={() => {
          refreshBackend?.({ refresh: true });
          loadSkills();
        }}
      />
      <div className="management-toolbar">
        <div className="tabs">
          {[
            ['all', '全部'],
            ['enabled', '已启用'],
            ['updates', '可更新'],
            ['errors', '异常']
          ].map(([value, label]) => (
            <button className={view === value ? 'active' : ''} key={value} type="button" onClick={() => setView(value)}>
              {label}
            </button>
          ))}
        </div>
        <div className="toolbar-actions">
          <label>
            <Search size={16} />
            <input
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索能力"
              value={query}
            />
          </label>
          <select className="select-button" value={category} onChange={(event) => setCategory(event.target.value)}>
            {categories.map((item) => (
              <option key={item} value={item}>{item === 'all' ? '全部分类' : item}</option>
            ))}
          </select>
          <button type="button" onClick={() => loadSkills()} disabled={state === 'loading'}>
            <Loader2 size={16} className={state === 'loading' ? 'spin-icon' : ''} />
            刷新
          </button>
        </div>
      </div>

      <div className="stats-row compact">
        {[
          ['全部能力', totals.total, Box, lastLoadedAt || '未加载'],
          ['已安装', totals.installed, Layers3, '来自真实列表'],
          ['已启用', totals.enabled, Check, `${totals.enabled}/${Math.max(totals.total, 1)}`],
          [totals.errors ? '异常项' : '可更新', totals.errors || totals.updates, totals.errors ? AlertTriangle : Clock3, totals.errors ? '需处理' : '等待更新']
        ].map(([label, value, Icon, detail]) => (
          <div className="stat-card" key={label}>
            <span><Icon size={24} /></span>
            <div>
              <em>{label}</em>
              <strong>{value}<small> 项</small></strong>
              <p>{detail}</p>
            </div>
          </div>
        ))}
      </div>

      {notice && <ManagementBanner tone={state === 'error' || state === 'unauthorized' ? 'error' : 'warn'} text={notice} />}

      <div className="management-scroll">
        {state === 'loading' && !skills.length && (
          <ManagementState icon={Loader2} spin title="正在加载能力库" text="正在读取本机真实能力列表。" />
        )}
        {state === 'unsupported' && (
          <ManagementState title="本地能力服务未就绪" text="暂时无法读取能力库，因此不会展示模板为真实数据。" />
        )}
        {state === 'unauthorized' && (
          <ManagementState title="登录状态已过期" text="请重新登录后再查看和管理能力。" />
        )}
        {state === 'error' && (
          <ManagementState title="能力库加载失败" text={notice || '请刷新或查看诊断页。'} />
        )}
        {state !== 'loading' && state !== 'unsupported' && state !== 'unauthorized' && state !== 'error' && !filteredSkills.length && (
          <ManagementState
            icon={Box}
            title="暂无匹配能力"
            text={skills.length ? '当前筛选条件下没有结果。' : '暂未返回可展示能力。'}
            actionLabel={skills.length ? '清空筛选' : '重新加载'}
            onAction={() => {
              if (skills.length) {
                setView('all');
                setCategory('all');
                setQuery('');
              } else {
                loadSkills();
              }
            }}
          />
        )}
        {filteredSkills.length > 0 && (
          <div className="skill-grid">
            {filteredSkills.map((skill, index) => (
              <SkillCard
                actionDisabled={actionDisabled}
                actionKey={actionKey}
                key={skill.id}
                onAction={runSkillAction}
                plugin={skill}
                index={index}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function SkillCard({ plugin, index, onAction, actionKey, actionDisabled }) {
  const icons = [BarChart3, FileText, Globe2, BookOpen, Workflow, Network, ShieldCheck, Code2, Brain, SquareTerminal];
  const Icon = icons[index % icons.length];
  const busy = actionKey.startsWith(`${plugin.id}:`);

  return (
    <article className={`skill-card ${plugin.tone}`}>
      <div className="skill-top">
        <span className={`skill-icon tone-${index % 5}`}><Icon size={24} /></span>
        <div className="skill-state">
          <span className={`dot ${plugin.tone === 'success' ? 'ok' : 'warn'}`} />
          <em>{plugin.status}</em>
        </div>
      </div>
      <div className="skill-title-row">
        <h3>{plugin.title}</h3>
        <span>{plugin.category}</span>
      </div>
      <p>{plugin.description}</p>
      {plugin.error && <div className="row-error"><AlertTriangle size={14} />{plugin.error}</div>}
      <div className="skill-metrics">
        <span>指令 {plugin.commands}</span>
        <span>助手 {plugin.agents}</span>
        <span>能力 {plugin.skills}</span>
        <span>自动化 {plugin.hooks}</span>
      </div>
      <footer className="skill-card-actions">
        <span>{plugin.version ? `v${plugin.version}` : '版本待返回'}{plugin.latestVersion ? ` -> ${plugin.latestVersion}` : ''}</span>
        <button
          type="button"
          onClick={() => onAction(plugin, plugin.installed ? 'update' : 'install')}
          disabled={actionDisabled || busy || (plugin.installed && !plugin.updateAvailable)}
        >
          {busy && (actionKey.endsWith(':install') || actionKey.endsWith(':update')) ? <Loader2 size={14} className="spin-icon" /> : <Upload size={14} />}
          {plugin.installed ? '更新' : '安装'}
        </button>
        <button
          className={`toggle ${plugin.enabled ? 'on' : ''}`}
          type="button"
          title={plugin.enabled ? '禁用能力' : '启用能力'}
          onClick={() => onAction(plugin, plugin.enabled ? 'disable' : 'enable')}
          disabled={actionDisabled || busy || !plugin.installed}
        >
          <span />
        </button>
      </footer>
    </article>
  );
}

function ManagementBanner({ text, tone = 'warn' }) {
  return (
    <div className={`management-banner ${tone}`}>
      <AlertTriangle size={15} />
      <span>{text}</span>
    </div>
  );
}

function ManagementState({ title, text, icon: Icon = AlertTriangle, spin = false, actionLabel, onAction }) {
  return (
    <div className="management-state">
      <Icon size={24} className={spin ? 'spin-icon' : ''} />
      <strong>{title}</strong>
      <span>{text}</span>
      {actionLabel && (
        <button type="button" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}

function normalizeMcpService(service = {}, index = 0, source = 'api') {
  const icons = [FileText, User, Database, Workflow, Globe2, SquareTerminal];
  const rawStatus = service.status || service.state || service.connectionState || '';
  const error = service.error || service.lastError || service.message || '';
  const enabledValue = service.enabled ?? service.active ?? service.available ?? (typeof service.disabled === 'boolean' ? !service.disabled : undefined);
  const enabled = source === 'preview'
    ? false
    : boolFrom(enabledValue, service.disabled !== true);
  const connected = boolFrom(
    service.connected ?? service.online,
    ['online', 'connected', 'ready', 'ok', '在线', '已连接'].includes(String(rawStatus).toLowerCase())
  );
  const tone = source === 'preview'
    ? 'pending'
    : statusToneFrom(rawStatus || (connected ? 'online' : ''), error, enabled);
  const tags = service.tags || service.capabilities || service.tools || service.scopes || [];

  return {
    id: service.id || service.key || service.name || `mcp-${index + 1}`,
    name: sanitizeDisplayText(service.displayName || service.name || service.title, `连接器 ${index + 1}`),
    url: connectorEndpointLabel(service),
    tags: Array.isArray(tags) && tags.length ? tags.slice(0, 5).map((tag) => sanitizeDisplayText(tag, '能力')) : ['连接器'],
    auth: sanitizeDisplayText(service.auth || service.authType || service.authProvider, '本地认证'),
    authState: sanitizeDisplayText(service.authState || service.authorization || (enabled ? '已配置' : '未启用'), enabled ? '已配置' : '未启用'),
    status: source === 'preview' ? '预览模板' : sanitizeDisplayText(statusLabelFrom(rawStatus, tone, enabled), '待连接'),
    ping: service.ping || (service.pingMs ? `${service.pingMs}ms` : '-'),
    sync: formatDateTime(service.lastSync || service.syncedAt || service.updatedAt || service.modifiedAt),
    permissions: sanitizeDisplayText(service.permissions || service.access || service.scope, '待返回'),
    enabled,
    connected,
    error,
    source,
    icon: icons[index % icons.length],
    tone: service.tone || ['orange', 'blue', 'purple', 'orange', 'teal'][index % 5],
    statusTone: tone,
    raw: service
  };
}

function McpView({ backendStatus, refreshBackend, onUnauthorized }) {
  const [services, setServices] = useState([]);
  const [state, setState] = useState(window.ecorex ? 'loading' : 'offline');
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [actionKey, setActionKey] = useState('');
  const [lastLoadedAt, setLastLoadedAt] = useState('');

  async function loadMcpServices({ silent = false } = {}) {
    if (!silent) {
      setState((current) => (current === 'offline' ? 'offline' : 'loading'));
      setNotice('');
    }

    if (!window.ecorex) {
      setServices([]);
      setState('offline');
      setNotice('预览模式：EcoreX 默认不展示内置 MCP/数据连接，后续可在应用内自行添加。');
      setLastLoadedAt(formatDateTime(new Date().toISOString()));
      return;
    }

    const result = await callEcorex([
      'listMcpServices',
      'listMcpServers',
      'getMcpServices',
      'getMcpServers',
      'mcp.listServices',
      'mcp.listServers',
      'mcp.list',
      'mcp.getServices',
      'mcp.getServers'
    ]);

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后管理数据连接。');
      return;
    }

    if (result?.ok === false && !result.missing) {
      setServices([]);
      setState('error');
      setNotice(sanitizeDisplayText(result.error, '数据连接列表加载失败。'));
      return;
    }

    let source = 'api';
    let items = result?.ok === false && result.missing
      ? []
      : extractCollection(result, ['services', 'servers', 'mcp.services', 'mcp.servers']);

    if (!items.length && result?.missing) {
      setServices([]);
      setState('unsupported');
      setNotice('本地能力服务未就绪。');
      return;
    }

    const nextServices = items
      .filter((item) => !isNativeConnectorItem(item))
      .map((item, index) => normalizeMcpService(item, index, source));
    setServices(nextServices);
    setState(nextServices.length ? 'ready' : 'empty');
    setLastLoadedAt(formatDateTime(new Date().toISOString()));
    if (source === 'backend') {
      setNotice('本地能力服务未完全就绪，当前展示数据连接状态快照；操作会继续等待确认。');
    } else if (!silent) {
      setNotice('');
    }
  }

  useEffect(() => {
    loadMcpServices();
  }, []);

  async function runMcpAction(service, action) {
    if (action === 'configure' && !service) {
      setNotice('请选择具体数据连接后查看或调整配置。');
      return;
    }
    const label = {
      connect: '连接',
      configure: '配置',
      enable: '启用',
      disable: '禁用'
    }[action] || '操作';
    const payload = {
      id: service?.id,
      name: service?.name,
      service,
      enabled: action === 'enable'
    };
    const candidates = {
      connect: ['connectMcpService', 'connectMcpServer', 'getMcpServer', 'refreshMcpStatus', 'mcp.connectService', 'mcp.connectServer', 'mcp.connect'],
      configure: ['configureMcpService', 'openMcpConfig', 'getMcpServer', 'updateMcpConfig', 'mcp.configureService', 'mcp.openConfig', 'mcp.configure'],
      enable: ['enableMcpService', 'enableMcpServer', 'setMcpEnabled', 'mcp.enableService', 'mcp.enableServer', 'mcp.enable', 'mcp.setEnabled'],
      disable: ['disableMcpService', 'disableMcpServer', 'setMcpEnabled', 'mcp.disableService', 'mcp.disableServer', 'mcp.disable', 'mcp.setEnabled']
    }[action] || [];

    setActionKey(`${service?.id || 'global'}:${action}`);
    setNotice('');
    const result = await callEcorexAction(candidates, payload);
    setActionKey('');

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后继续操作。');
      return;
    }

    if (result?.ok === false) {
      setNotice(result.missing
        ? `本地能力服务未就绪，未执行${label}。`
        : `数据连接${label}失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
      return;
    }

    setNotice(`数据连接${label}已确认。`);
    refreshBackend?.();
    await loadMcpServices({ silent: true });
  }

  const filteredServices = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return services.filter((service) => {
      const matchesStatus =
        statusFilter === 'all' ||
        (statusFilter === 'enabled' && service.enabled) ||
        (statusFilter === 'connected' && service.connected) ||
        (statusFilter === 'errors' && service.error);
      const text = `${service.name} ${service.url} ${service.tags.join(' ')} ${service.status}`.toLowerCase();
      return matchesStatus && (!keyword || text.includes(keyword));
    });
  }, [services, query, statusFilter]);

  const totals = useMemo(() => ({
    total: services.length,
    enabled: services.filter((service) => service.enabled).length,
    connected: services.filter((service) => service.connected).length,
    errors: services.filter((service) => service.error).length
  }), [services]);

  const actionDisabled = state === 'offline' || state === 'unsupported' || state === 'unauthorized';

  return (
    <section className="management panel mcp-view">
      <HeaderBar
        title="EcoreX 数据连接"
        badge={state === 'offline' ? '预览' : undefined}
        subtitle="读取本机真实数据连接，展示连接、启用、授权与错误状态；不把模板数据伪装成已接入"
        backendStatus={backendStatus}
        onRefresh={() => {
          refreshBackend?.({ refresh: true });
          loadMcpServices();
        }}
      />
      <div className="mcp-toolbar">
        <label>
          <Search size={16} />
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索数据连接"
            value={query}
          />
        </label>
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="all">状态：全部</option>
          <option value="enabled">已启用</option>
          <option value="connected">已连接</option>
          <option value="errors">异常</option>
        </select>
        <button type="button" onClick={() => loadMcpServices()} disabled={state === 'loading'}>
          <Loader2 size={16} className={state === 'loading' ? 'spin-icon' : ''} />
          刷新
        </button>
        <button className="primary" type="button" onClick={() => runMcpAction(null, 'configure')} disabled={actionDisabled || actionKey === 'global:configure'}>
          <Settings size={16} />
          连接配置
        </button>
      </div>

      <div className="stats-row compact">
        {[
          ['全部连接', totals.total, Box, lastLoadedAt || '未加载'],
          ['已启用', totals.enabled, Check, '本机状态'],
          ['已连接', totals.connected, Network, `${totals.connected}/${Math.max(totals.total, 1)}`],
          ['异常', totals.errors, AlertTriangle, totals.errors ? '需处理' : '无错误']
        ].map(([label, value, Icon, detail]) => (
          <div className="stat-card" key={label}>
            <span><Icon size={24} /></span>
            <div>
              <em>{label}</em>
              <strong>{value}<small> 项</small></strong>
              <p>{detail}</p>
            </div>
          </div>
        ))}
      </div>

      {notice && <ManagementBanner tone={state === 'error' || state === 'unauthorized' ? 'error' : 'warn'} text={notice} />}

      <div className="table-head">
        <span>连接信息</span>
        <span>权限与认证</span>
        <span>连接状态</span>
        <span>最后同步</span>
        <span>操作</span>
      </div>
      <div className="mcp-list">
        {state === 'loading' && !services.length && (
          <ManagementState icon={Loader2} spin title="正在加载数据连接" text="正在读取本机真实数据连接列表。" />
        )}
        {state === 'unsupported' && (
          <ManagementState title="本地能力服务未就绪" text="暂时无法读取数据连接列表，因此不会展示模板为真实服务。" />
        )}
        {state === 'unauthorized' && (
          <ManagementState title="登录状态已过期" text="请重新登录后再查看和管理数据连接。" />
        )}
        {state === 'error' && (
          <ManagementState title="数据连接加载失败" text={notice || '请刷新或查看诊断页。'} />
        )}
        {state !== 'loading' && state !== 'unsupported' && state !== 'unauthorized' && state !== 'error' && !filteredServices.length && (
          <ManagementState
            icon={Box}
            title="暂无匹配数据连接"
            text={services.length ? '当前筛选条件下没有结果。' : '暂未返回可展示数据连接。'}
            actionLabel={services.length ? '清空筛选' : '重新加载'}
            onAction={() => {
              if (services.length) {
                setQuery('');
                setStatusFilter('all');
              } else {
                loadMcpServices();
              }
            }}
          />
        )}
        {filteredServices.map((service) => (
          <McpRow
            actionDisabled={actionDisabled}
            actionKey={actionKey}
            onAction={runMcpAction}
            service={service}
            key={service.id}
          />
        ))}
      </div>
    </section>
  );
}

function McpRow({ service, onAction, actionKey, actionDisabled }) {
  const Icon = service.icon;
  const busy = actionKey.startsWith(`${service.id}:`);
  const connected = service.connected || service.statusTone === 'success';

  return (
    <article className={`mcp-row state-${service.statusTone}`}>
      <div className="mcp-info">
        <span className={`mcp-icon ${service.tone}`}><Icon size={24} /></span>
        <div>
          <strong>{service.name}</strong>
          <em>{service.url}</em>
          <div>{service.tags.map((tag) => <small key={tag}>{tag}</small>)}</div>
        </div>
      </div>
      <div className="mcp-auth">
        <ShieldCheck size={18} />
        <strong>{service.auth}</strong>
        <span className={service.enabled ? 'ok' : 'warn'}>{service.authState}</span>
        <em>权限：{service.permissions}</em>
      </div>
      <div className="mcp-status">
        <span className={connected ? 'dot ok' : 'dot warn'} />
        <strong>{service.status}</strong>
        <em>{service.ping}</em>
        {service.error && <small>{service.error}</small>}
      </div>
      <div className="mcp-sync">
        <strong>{service.sync}</strong>
        <em>{service.source === 'api' ? '实时列表' : service.source === 'backend' ? '状态快照' : '预览模板'}</em>
      </div>
      <div className="mcp-actions">
        <button
          type="button"
          title="连接"
          onClick={() => onAction(service, 'connect')}
          disabled={actionDisabled || busy || connected}
        >
          {busy && actionKey.endsWith(':connect') ? <Loader2 size={16} className="spin-icon" /> : <Play size={16} />}
        </button>
        <button
          type="button"
          title={service.enabled ? '禁用' : '启用'}
          onClick={() => onAction(service, service.enabled ? 'disable' : 'enable')}
          disabled={actionDisabled || busy}
        >
          {service.enabled ? <Pause size={16} /> : <Check size={16} />}
        </button>
        <button
          type="button"
          title="配置"
          onClick={() => onAction(service, 'configure')}
          disabled={actionDisabled || busy}
        >
          {busy && actionKey.endsWith(':configure') ? <Loader2 size={16} className="spin-icon" /> : <Pencil size={16} />}
        </button>
      </div>
    </article>
  );
}

function formatPluginName(name) {
  const names = {
    'agent-sdk-dev': '助手开发套件',
    'claude-opus-4-5-migration': '模型迁移助手',
    'code-review': '代码审查',
    'commit-commands': '提交工作流',
    'explanatory-output-style': '解释型输出',
    'feature-dev': '功能开发',
    'frontend-design': '前端设计',
    hookify: '自动化编排',
    'learning-output-style': '学习模式',
    'plugin-dev': '能力扩展',
    'pr-review-toolkit': 'PR 审查工具箱',
    'ralph-wiggum': '迭代循环',
    'security-guidance': '安全指导'
  };
  return names[name] || name;
}

export default App;


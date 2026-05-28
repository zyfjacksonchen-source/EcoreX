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
  Star,
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
    description: '能力创建工具包，包含自动化、MCP、命令、助手和能力模板',
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
    name: 'campaign-analysis',
    title: '投放数据分析',
    category: '广告投放',
    description: '识别消耗、转化、线索质量与渠道异常波动。',
    commands: 2,
    agents: 1,
    skills: 3,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'creative-brief',
    title: '创意简报助手',
    category: '创意策略',
    description: '整理卖点、受众、脚本方向、素材需求与审核要点。',
    commands: 2,
    agents: 2,
    skills: 4,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'audience-diagnosis',
    title: '人群异常诊断',
    category: '人群洞察',
    description: '定位计划、定向、人群包与时段维度的效果异常原因。',
    commands: 1,
    agents: 1,
    skills: 2,
    hooks: 0,
    installed: false,
    enabled: false
  },
  {
    name: 'launch-workflow',
    title: '投放任务编排',
    category: '项目协同',
    description: '把投放建议拆解为负责人、节点、验收标准和风险提醒。',
    commands: 2,
    agents: 2,
    skills: 3,
    hooks: 0,
    installed: false,
    enabled: false
  }
];

const recentChats = [
  ['618 短视频投放复盘与预算优化', '10:24'],
  ['新品信息流素材点击率诊断', '昨天'],
  ['达人种草内容脚本批量生成', '昨天'],
  ['搜索广告关键词拓展与分组', '5月20日'],
  ['品牌直播间转化路径梳理', '5月19日'],
  ['客户月度投放周报自动生成', '5月18日'],
  ['竞品广告卖点与素材拆解', '5月16日'],
  ['线索质量回传异常排查', '5月15日']
];

const quickActions = [
  ['投放数据分析', '诊断消耗、转化、ROI 与异常波动', BarChart3],
  ['生成客户周报', '自动输出进展、风险与行动项', FileText],
  ['核查素材卖点', '比对行业口径、竞品打法与审核风险', BookOpen],
  ['创建投放任务', '拆解负责人、截止时间与验收标准', Activity]
];

const abilityCards = [
  ['投放分析', BarChart3],
  ['广告周报', FileText],
  ['任务管理', Check],
  ['风险预警', Zap],
  ['素材审核', ShieldCheck],
  ['知识问答', BookOpen]
];

const mcpServices = [
  {
    name: '广告知识库',
    url: 'https://data.ecorex.com/ad-knowledge',
    tags: ['行业案例', '投放口径', '智能检索'],
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
    name: '投放 MCP 连接器',
    url: 'https://data.ecorex.com/campaign-data',
    tags: ['计划指标', '转化数据', '同步'],
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
    name: '素材资产库',
    url: 'https://data.ecorex.com/creative-assets',
    tags: ['素材数据', '数据查询', '报表'],
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
    url: 'https://data.ecorex.com/campaign-task',
    tags: ['投放项目', '运营', '自动化'],
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
  ['准备广告投放与项目分析上下文', '进行中', '10:24:07', 'running'],
  ['等待用户确认下一步任务', '待确认', '--', 'pending']
];

const messageStates = {
  queued: { label: '排队中', icon: Clock3, tone: 'queued' },
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
const LEGACY_IMAGE_MODEL_ALIASES = new Map([
  ['image-2', DEFAULT_IMAGE_MODEL_NAME]
]);

function defaultImageModelName(value = '') {
  const model = String(value || '').trim();
  if (!model) return DEFAULT_IMAGE_MODEL_NAME;
  return LEGACY_IMAGE_MODEL_ALIASES.get(model.toLowerCase()) || model;
}
const DEFAULT_PERMISSION_MODE_KEY = 'ecorex-default-permission-mode';
const MAX_COMPOSER_ATTACHMENTS = 10;
const RECENT_CHAT_STORAGE_KEY = 'ecorex-recent-chats';
const CONVERSATION_STORAGE_KEY = 'ecorex-chat-conversations';
const MAX_RECENT_CHATS = 20;
const MAX_STORED_CONVERSATIONS = 30;
const MAX_STORED_MESSAGES_PER_CONVERSATION = 120;
const MAX_CONVERSATION_STORAGE_CHARS = 4 * 1024 * 1024;
const MAX_LIVE_MESSAGES_PER_CONVERSATION = 180;
const MAX_LIVE_ASSISTANT_TEXT_CHARS = 60000;
const FRONT_AGENT_EVENT_TEXT_CHARS = 20000;
const MAX_COMPOSER_PROMPT_CHARS = 80000;
const MAX_PENDING_FOLLOWUPS = 20;
const MAX_PENDING_FOLLOWUP_CHARS = 20000;
const MESSAGE_WINDOW_SIZE = 40;
const MESSAGE_WINDOW_STEP = 30;
const ASSISTANT_COLLAPSE_CHARS = 900;
const CONTEXT_RECENT_MESSAGE_LIMIT = 12;
const CONTEXT_RECENT_MESSAGE_CHARS = 760;
const CONTEXT_SUMMARY_MAX_CHARS = 5000;
const CONTEXT_COMPACT_TRIGGER_MESSAGES = 36;
const CONTEXT_COMPACT_RECENT_LIMIT = 8;
const CONTEXT_COMPACT_SOURCE_MESSAGES = 28;
const NATIVE_SESSION_ROTATE_TRIGGER_MESSAGES = 18;
const NATIVE_SESSION_ROTATE_STEP_MESSAGES = 14;
const STARTUP_FRONTEND_TIMEOUT_MS = 9000;
const ARTIFACT_PREVIEW_MAX_ITEMS = 8;
const ARTIFACT_PREVIEW_MAX_CHARS = 140000;
const ARTIFACT_PREVIEW_CACHE_MAX_ITEMS = 120;
const DEFAULT_AGENT_MODEL_NAME = 'gpt-5.5';
const ARTIFACT_PREVIEW_EXTENSIONS = [
  'html', 'htm', 'md', 'markdown', 'txt', 'json', 'csv', 'log', 'yaml', 'yml', 'xml', 'css', 'js', 'jsx', 'ts', 'tsx',
  'png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'avif',
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'
];
const ARTIFACT_TEXT_EXTENSIONS = ['md', 'markdown', 'txt', 'json', 'csv', 'log', 'yaml', 'yml', 'xml', 'css', 'js', 'jsx', 'ts', 'tsx'];
const ARTIFACT_HTML_EXTENSIONS = ['html', 'htm'];
const ARTIFACT_IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'avif'];
const ARTIFACT_OFFICE_EXTENSIONS = ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'];
const ATTACHMENT_PREVIEW_URL_MAX_CHARS = 1600000;
const MAX_STORED_LEDGER_ITEMS = 80;
const MAX_STORED_INGEST_ITEMS = 40;
const MAX_COMPOSER_REFERENCES = 6;
const PROJECT_FILE_REFRESH_INTERVAL_MS = 5000;
const COMPOSER_REFERENCE_SNIPPET_CHARS = 220;
const AGENT_EVENT_QUEUE_LIMIT = 1600;
const AGENT_EVENT_FLUSH_BATCH = 120;
const AGENT_EVENT_FLUSH_DELAY_MS = 40;
const AGENT_EVENT_PENDING_DELAY_MS = 140;
const PENDING_AGENT_EVENT_TTL_MS = 7000;
const AGENT_TIMELINE_BATCH_LIMIT = 24;
const AGENT_EVENT_TERMINAL_KINDS = new Set(['result', 'done', 'error', 'cancelled', 'timeout']);
const AGENT_DISCLOSURE_DELAYS_MS = [1800, 6500, 14000, 28000];
const CONVERSATION_SAVE_DEBOUNCE_MS = 220;
const MANAGED_SECRET_DEFINITIONS = [
  { key: 'ANTHROPIC_API_KEY', label: '模型服务密钥', hint: '用于亦芯调用模型服务' },
  { key: 'ANTHROPIC_AUTH_TOKEN', label: '模型授权令牌', hint: '用于企业授权会话' },
  { key: 'ECOREX_LICENSE_KEY', label: 'EcoreX 授权码', hint: '用于工作台授权校验' }
];

const DEFAULT_PERMISSION_OPTION = {
  value: 'default',
  label: '默认权限',
  description: '联网搜索、网页读取和常规工具自动执行；文件读写、命令执行和系统目录访问继续请求确认。',
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

function displayUserNameFromAuth(status = {}) {
  const user = status.user || status.profile || status.account || {};
  const raw = user.name || user.displayName || user.nickname || status.name || status.displayName || user.email || status.email || '';
  const value = String(raw || '').trim();
  if (value.includes('@')) return value.split('@')[0] || '张晓明';
  return value || '张晓明';
}

function authUserFromStatus(status = {}) {
  return status.user || status.profile || status.account || {};
}

function displayUserEmailFromAuth(status = {}) {
  const user = authUserFromStatus(status);
  return String(user.email || status.email || '').trim();
}

function roleLabelFromAuthUser(user = {}) {
  const role = String(user.role || '').trim();
  if (user.roleLabel) return user.roleLabel;
  if (role === 'super_admin') return '超级管理员';
  if (role === 'admin') return '管理员';
  return '成员';
}

function authHasPermission(status = {}, permission = '') {
  const user = authUserFromStatus(status);
  const permissions = Array.isArray(user.permissions)
    ? user.permissions
    : Array.isArray(status.permissions)
      ? status.permissions
      : [];
  return permissions.includes(permission);
}

function canManageSkillsFromAuth(status = {}) {
  const role = String(authUserFromStatus(status).role || status.role || '').trim();
  return role === 'super_admin' || role === 'admin' || authHasPermission(status, 'skills:manage');
}

function avatarInitialsFromUser(user = {}, fallbackName = '') {
  const explicit = String(user.avatarInitials || '').trim();
  if (explicit) return explicit.slice(0, 2).toUpperCase();
  const source = String(user.displayName || user.name || fallbackName || user.email || 'E').trim();
  if (!source) return 'E';
  if (/^[A-Za-z]/.test(source)) return source.slice(0, 2).toUpperCase();
  return source.slice(0, 1);
}

let activeConversationStorageOwner = '';

function storageOwnerFromAuthStatus(status = {}) {
  return displayUserEmailFromAuth(status).toLowerCase();
}

function setConversationStorageOwner(status = {}) {
  activeConversationStorageOwner = normalizeAuthStatus(status).loggedIn ? storageOwnerFromAuthStatus(status) : '';
}

function currentConversationStorageOwner() {
  return activeConversationStorageOwner;
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

function resolveAfter(ms, value = null) {
  return new Promise((resolve) => {
    window.setTimeout(() => resolve(value), ms);
  });
}

function withStartupTimeout(promise, ms = STARTUP_FRONTEND_TIMEOUT_MS) {
  return Promise.race([
    promise,
    resolveAfter(ms, { timedOut: true })
  ]);
}

async function preloadStartupState() {
  if (!window.ecorex) {
    const skipped = { status: 'skipped', label: '预览模式', detail: '桌面预加载不可用', total: 0, fulfilled: 0 };
    window.__ecorexStartupState = skipped;
    return skipped;
  }
  const startedAt = Date.now();
  const entries = [
    ['settings', window.ecorex.getSettings],
    ['agentSessions', window.ecorex.getAgentSessions],
    ['modelProfiles', window.ecorex.listModelProfiles],
    ['projects', window.ecorex.listProjects || window.ecorex.getProjects]
  ].filter(([, fn]) => typeof fn === 'function');
  if (!entries.length) {
    const skipped = { status: 'skipped', label: '未接入', detail: '预加载接口不可用', total: 0, fulfilled: 0 };
    window.__ecorexStartupState = skipped;
    return skipped;
  }
  const results = await Promise.allSettled(entries.map(([, fn]) => Promise.resolve().then(() => fn())));
  const fulfilledKeys = [];
  window.__ecorexStartupCache = entries.reduce((cache, [key], index) => {
    const result = results[index];
    if (result?.status === 'fulfilled' && result.value && result.value.ok !== false && !result.value.unauthorized) {
      cache[key] = result.value;
      fulfilledKeys.push(key);
    }
    return cache;
  }, window.__ecorexStartupCache || {});
  const summary = {
    status: fulfilledKeys.length === entries.length ? 'ready' : (fulfilledKeys.length ? 'partial' : 'failed'),
    label: fulfilledKeys.length === entries.length ? '已完成' : (fulfilledKeys.length ? '部分完成' : '未完成'),
    detail: `${fulfilledKeys.length}/${entries.length} 项已预加载`,
    keys: fulfilledKeys,
    total: entries.length,
    fulfilled: fulfilledKeys.length,
    durationMs: Date.now() - startedAt
  };
  window.__ecorexStartupState = summary;
  return summary;
}

function formatFileSize(bytes = 0) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(value >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function isImageAttachment(attachment = {}) {
  return String(attachment.type || '').startsWith('image/') || /\.(png|jpe?g|webp|gif|svg)$/i.test(attachment.name || '');
}

function isVideoAttachment(attachment = {}) {
  return String(attachment.type || '').startsWith('video/') || /\.(mp4|webm|ogg|ogv|mov|m4v)$/i.test(attachment.name || '');
}

const CHAT_IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.avif']);
const CHAT_VIDEO_EXTENSIONS = new Set(['.mp4', '.webm', '.ogg', '.ogv', '.mov', '.m4v']);

function cleanChatUrlToken(value = '') {
  let token = String(value || '')
    .trim()
    .replace(/^<|>$/g, '')
    .replace(/\s+["'][\s\S]*$/, '');
  while (/[)\]}.,;!?，。；：！？]$/.test(token)) {
    token = token.slice(0, -1);
  }
  return token.trim();
}

function safeChatExternalUrl(value = '', { mediaOnly = false } = {}) {
  const raw = cleanChatUrlToken(value);
  if (!raw || raw.length > 2048 || /[\u0000-\u001F\u007F]/.test(raw)) return '';
  const normalized = /^www\./i.test(raw) ? `https://${raw}` : raw;
  try {
    const parsed = new URL(normalized);
    if (mediaOnly && !['http:', 'https:'].includes(parsed.protocol)) return '';
    if (!mediaOnly && !['http:', 'https:', 'mailto:'].includes(parsed.protocol)) return '';
    if ((parsed.protocol === 'http:' || parsed.protocol === 'https:') && (parsed.username || parsed.password)) return '';
    return parsed.toString();
  } catch {
    return '';
  }
}

function chatMediaKind(url = '') {
  const safeUrl = safeChatExternalUrl(url, { mediaOnly: true });
  if (!safeUrl) return '';
  try {
    const parsed = new URL(safeUrl);
    const pathname = decodeURIComponent(parsed.pathname || '').toLowerCase();
    const ext = pathname.match(/\.[a-z0-9]+$/)?.[0] || '';
    if (CHAT_IMAGE_EXTENSIONS.has(ext)) return 'image';
    if (CHAT_VIDEO_EXTENSIONS.has(ext)) return 'video';
  } catch {
    return '';
  }
  return '';
}

function attachmentPromptSection(attachments = []) {
  if (!attachments.length) return '';
  const lines = attachments.map((attachment, index) => {
    const location = attachment.source === 'project'
      ? `，项目文件：${attachment.relativePath || attachment.pathLabel || attachment.name || '项目资料'}`
      : attachment.path ? `，本地路径：${attachment.path}` : '，来源：剪贴板或浏览器上传';
    return `${index + 1}. ${attachment.name || `附件 ${index + 1}`}（${attachment.type || 'unknown'}，${formatFileSize(attachment.sizeBytes)}${location}）`;
  });
  return `已附加文件：\n${lines.join('\n')}`;
}

function attachmentStableKey(attachment = {}, index = 0) {
  return String(
    attachment.id
    || attachment.path
    || attachment.filePath
    || `${attachment.name || 'attachment'}:${attachment.sizeBytes || attachment.size || 0}:${attachment.type || attachment.mimeType || ''}:${index}`
  );
}

function safeAttachmentPreviewUrl(value = '', { persist = false } = {}) {
  const previewUrl = String(value || '').trim();
  if (!previewUrl || previewUrl.length > ATTACHMENT_PREVIEW_URL_MAX_CHARS) return '';
  if (persist && !previewUrl.startsWith('data:')) return '';
  return previewUrl;
}

function clampLongText(value = '', maxChars = MAX_LIVE_ASSISTANT_TEXT_CHARS) {
  const text = String(value || '');
  if (text.length <= maxChars) return text;
  const marker = '\n\n[前文已自动压缩，保留首尾内容以保持会话流畅]\n\n';
  const headChars = Math.min(2400, Math.floor(maxChars * 0.18));
  const tailChars = Math.max(0, maxChars - headChars - marker.length);
  return `${text.slice(0, headChars)}${marker}${text.slice(-tailChars)}`;
}

function clampComposerPromptText(value = '') {
  return clampLongText(value, MAX_COMPOSER_PROMPT_CHARS);
}

function serializeAgentAttachment(attachment = {}, index = 0) {
  const path = String(attachment.path || attachment.filePath || '').trim();
  const previewUrl = safeAttachmentPreviewUrl(attachment.previewUrl || attachment.previewDataUrl || attachment.thumbnail || '');
  const id = attachmentStableKey(attachment, index);
  return {
    id,
    name: String(attachment.name || attachment.fileName || `attachment-${index + 1}`).slice(0, 240),
    path,
    filePath: path,
    type: String(attachment.type || attachment.mimeType || '').slice(0, 120),
    mimeType: String(attachment.mimeType || attachment.type || '').slice(0, 120),
    sizeBytes: Number(attachment.sizeBytes || attachment.size) || 0,
    source: String(attachment.source || 'upload').slice(0, 40),
    pathLabel: String(attachment.pathLabel || '').slice(0, 600),
    relativePath: String(attachment.relativePath || '').slice(0, 600),
    projectId: String(attachment.projectId || '').slice(0, 120),
    projectName: String(attachment.projectName || '').slice(0, 120),
    previewUrl,
    previewDataUrl: previewUrl.startsWith('data:') ? previewUrl : '',
    status: String(attachment.status || 'ready').slice(0, 40),
    progress: Number.isFinite(Number(attachment.progress)) ? Number(attachment.progress) : 100,
    lastModified: attachment.lastModified || attachment.updatedAt || null
  };
}

function serializeAgentAttachments(attachments = []) {
  return (Array.isArray(attachments) ? attachments : [])
    .filter(Boolean)
    .slice(0, MAX_COMPOSER_ATTACHMENTS)
    .map((attachment, index) => serializeAgentAttachment(attachment, index));
}

function filterAttachmentsForProjectScope(attachments = [], projectId = '') {
  const safeProjectId = String(projectId || '').trim();
  return (Array.isArray(attachments) ? attachments : []).filter((attachment) => {
    const attachmentProjectId = String(attachment?.projectId || '').trim();
    const projectScoped = attachment?.source === 'project' || Boolean(attachmentProjectId);
    if (!projectScoped) return true;
    return Boolean(safeProjectId && attachmentProjectId && attachmentProjectId === safeProjectId);
  });
}

function sanitizeStoredAttachment(attachment = {}, index = 0) {
  const serialized = serializeAgentAttachment(attachment, index);
  return {
    ...serialized,
    previewUrl: safeAttachmentPreviewUrl(serialized.previewUrl, { persist: true }),
    previewDataUrl: safeAttachmentPreviewUrl(serialized.previewDataUrl, { persist: true }),
    ingest: attachment.ingest ? sanitizeAttachmentIngestItem(attachment.ingest, index) : undefined
  };
}

function compactEventDetail(value, maxLength = 1100) {
  if (value == null || value === '') return '';
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim().slice(0, maxLength);
  try {
    return JSON.stringify(value, null, 2).replace(/\s+$/g, '').slice(0, maxLength);
  } catch {
    return String(value).replace(/\s+/g, ' ').trim().slice(0, maxLength);
  }
}

function looksLikeNoisyLocalPath(value = '') {
  const text = String(value || '').trim();
  if (!text) return false;
  if (text.includes('\uFFFD')) return true;
  if (/(^|[^A-Za-z])[A-Za-z]:[\\/]|\\\\|\/(?:Users|home|tmp|var|mnt|private)\//.test(text)) return true;
  if (/\b(?:path|filePath|cwd|installPath|projectPath|raw)\b/i.test(text)) return true;
  return false;
}

function isInternalAgentOutputLine(value = '') {
  const text = String(value || '').trim();
  if (!text) return false;
  if (/^Launching skill:/i.test(text)) return true;
  if (/\bbridge:[a-z0-9_.:-]+\b/i.test(text) && /^Launching/i.test(text)) return true;
  if (/^[\d\s|:._=\-#\u2580-\u259f\u25a0-\u25a1\u25aa-\u25ab]+$/u.test(text) && text.length >= 16) return true;
  if (/^(?:\d+\s*)?[\u2580-\u259f\u25a0-\u25a1\u25aa-\u25ab#=_-]{8,}/u.test(text)) return true;
  return false;
}

function cleanPublicAgentText(value = '', { dropPathLines = true } = {}) {
  return String(value || '')
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => !(dropPathLines && looksLikeNoisyLocalPath(line)))
    .filter((line) => !isInternalAgentOutputLine(line))
    .join('\n')
    .replace(/\[EcoreX capability running\]/gi, 'EcoreX 正在调用原生能力')
    .replace(/\bClaude\s*Code\s*CLI\b/gi, 'EcoreX')
    .replace(/\bClaude\s*Code\b/gi, 'EcoreX')
    .replace(/\bClaude\s*CLI\b/gi, 'EcoreX')
    .replace(/\bAnthropic\s*CLI\b/gi, 'EcoreX')
    .replace(/\bclaude\s+mcp\b/gi, 'EcoreX MCP')
    .replace(/\bclaude\s+(plugin|plugins)\b/gi, 'EcoreX SKILLS')
    .replace(/\bClaude\b/gi, 'EcoreX')
    .trim();
}

function toolToneFromStatus(status = '') {
  const normalized = String(status || '').toLowerCase();
  if (/error|fail|failed|timeout|denied|rejected|exception|异常|失败|错误|超时|拒绝/.test(normalized)) return 'danger';
  if (/done|complete|completed|success|ok|finished|已完成|成功/.test(normalized)) return 'success';
  if (/pending|queued|waiting|confirm|等待|确认|排队/.test(normalized)) return 'pending';
  return 'running';
}

function readableToolStatus(status = '', fallback = '进行中') {
  return formatAgentEventStatus(status || fallback, fallback);
}

function normalizeToolLedgerItem(raw = {}, event = {}, index = 0) {
  const source = raw && typeof raw === 'object' ? raw : { text: raw };
  const toolName = source.toolName
    || source.name
    || source.tool?.name
    || source.tool
    || event.toolName
    || event.task?.name
    || event.taskName
    || event.name
    || '工具调用';
  const status = source.status || source.state || event.status || event.state || event.kind || 'running';
  const action = source.action
    || source.operation
    || source.summary
    || source.description
    || source.text
    || source.message
    || event.text
    || event.message
    || agentDisclosureLabel(event)
    || '执行工具';
  const cleanAction = cleanPublicAgentText(action, { dropPathLines: true }) || agentDisclosureLabel(event) || '执行工具';
  const errorDetail = cleanPublicAgentText(source.error || source.stderr || '', { dropPathLines: true });
  const detail = errorDetail ? `error: ${compactEventDetail(errorDetail, 500)}` : '';
  const id = String(source.id || source.callId || source.toolUseId || event.id || event.__seq || `${toolName}-${index}-${Date.now()}`);
  return {
    id,
    toolName: sanitizeDisplayText(toolName, '工具调用').slice(0, 80),
    action: sanitizeDisplayText(cleanAction, '执行工具').slice(0, 140),
    path: String(source.path || source.filePath || source.cwd || '').slice(0, 600),
    output: compactEventDetail(cleanPublicAgentText(source.output || source.stdout || source.stderr || '', { dropPathLines: true }), 800),
    status: readableToolStatus(status, '进行中'),
    tone: toolToneFromStatus(status),
    detail,
    time: formatAgentEventTime(event),
    kind: event.kind || 'tool'
  };
}

function isNoisyToolLedgerItem(item = {}) {
  const body = cleanPublicAgentText([item.action, item.output, item.detail].filter(Boolean).join('\n'), { dropPathLines: true }).trim();
  if (!body && /^(?:工具返回结果|工具调用)$/i.test(String(item.toolName || '').trim())) return true;
  return [item.action, item.output, item.detail].some(isInternalAgentOutputLine);
}

function normalizeToolLedgerItemsFromEvent(event = {}) {
  const sources = [];
  if (Array.isArray(event.ledger)) sources.push(...event.ledger);
  else if (event.ledger && typeof event.ledger === 'object') sources.push(event.ledger);
  if (Array.isArray(event.tools)) sources.push(...event.tools);
  else if (event.tool && typeof event.tool === 'object') sources.push(event.tool);
  if (
    ['tool', 'ledger'].includes(event.kind)
    || event.toolName
    || event.command
    || event.input
    || event.output
    || event.stdout
    || event.stderr
  ) {
    sources.push(event);
  }
  const seen = new Set();
  return sources
    .filter(Boolean)
    .map((source, index) => normalizeToolLedgerItem(source, event, index))
    .filter((item) => !isNoisyToolLedgerItem(item))
    .filter((item) => {
      const key = `${item.id}:${item.toolName}:${item.action}:${item.status}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 12);
}

function appendToolLedgerItems(current = [], incoming = []) {
  if (!incoming.length) return current;
  const byKey = new Map((Array.isArray(current) ? current : []).map((item) => [
    `${item.id}:${item.toolName}:${item.action}`,
    item
  ]));
  for (const item of incoming) {
    const key = `${item.id}:${item.toolName}:${item.action}`;
    byKey.set(key, { ...(byKey.get(key) || {}), ...item });
  }
  return Array.from(byKey.values()).slice(-MAX_STORED_LEDGER_ITEMS);
}

function ingestToneFromStatus(status = '', ok = true) {
  const normalized = String(status || '').toLowerCase();
  if (!ok || /error|fail|failed|unsupported|denied|too-large|blocked|异常|失败|不可|过大|拒绝/.test(normalized)) return 'warn';
  if (/done|complete|completed|success|ok|ready|已完成|成功|已摄取/.test(normalized)) return 'success';
  if (/pending|queued|waiting|uploading|等待|排队|上传/.test(normalized)) return 'pending';
  return 'running';
}

function sanitizeAttachmentIngestItem(raw = {}, index = 0) {
  const source = raw && typeof raw === 'object' ? raw : { summary: raw };
  const name = source.name || source.fileName || source.attachmentName || source.path || source.filePath || `附件 ${index + 1}`;
  const status = source.status || source.state || (source.ok === false ? 'failed' : 'ready');
  const metadata = source.metadata || source.image || source.imageInfo || source.info || {};
  const summary = source.summary
    || source.textSummary
    || source.preview
    || source.description
    || source.message
    || '';
  const reason = source.reason || source.error || source.unavailableReason || source.blockedReason || '';
  return {
    id: String(source.id || source.attachmentId || source.path || source.filePath || `${name}-${index}`).slice(0, 160),
    attachmentId: String(source.attachmentId || source.id || '').slice(0, 120),
    name: sanitizeDisplayText(name, `附件 ${index + 1}`).slice(0, 180),
    path: String(source.path || source.filePath || '').slice(0, 600),
    type: String(source.type || source.mimeType || '').slice(0, 120),
    sizeBytes: Number(source.sizeBytes || source.size) || 0,
    status: readableToolStatus(status, '已处理'),
    tone: ingestToneFromStatus(status, source.ok !== false),
    summary: sanitizeDisplayText(summary, reason ? '无法预览' : '已完成附件读取').slice(0, 280),
    reason: sanitizeDisplayText(reason, '').slice(0, 260),
    metadata: compactEventDetail(metadata, 420),
    previewUrl: safeAttachmentPreviewUrl(source.previewUrl || source.previewDataUrl || source.thumbnail || ''),
    time: source.time || source.updatedAt || null
  };
}

function normalizeAttachmentIngestItemsFromEvent(event = {}) {
  const sources = [];
  if (Array.isArray(event.attachments)) sources.push(...event.attachments);
  else if (event.attachment && typeof event.attachment === 'object') sources.push(event.attachment);
  if (Array.isArray(event.ingestion)) sources.push(...event.ingestion);
  else if (event.ingestion && typeof event.ingestion === 'object') sources.push(event.ingestion);
  if (Array.isArray(event.attachmentIngest)) sources.push(...event.attachmentIngest);
  else if (event.attachmentIngest && typeof event.attachmentIngest === 'object') sources.push(event.attachmentIngest);
  if (['attachment', 'ingest'].includes(event.kind)) sources.push(event);
  return sources
    .filter(Boolean)
    .map((source, index) => sanitizeAttachmentIngestItem(source, index))
    .slice(0, 12);
}

function mergeAttachmentIngestItems(current = [], incoming = []) {
  if (!incoming.length) return current;
  const byKey = new Map((Array.isArray(current) ? current : []).map((item) => [
    item.attachmentId || item.path || item.name || item.id,
    item
  ]));
  for (const item of incoming) {
    const key = item.attachmentId || item.path || item.name || item.id;
    byKey.set(key, { ...(byKey.get(key) || {}), ...item });
  }
  return Array.from(byKey.values()).slice(-MAX_STORED_INGEST_ITEMS);
}

function recoveryStateFromStatus(status = '', fallback = 'recoverable') {
  const normalized = String(status || fallback).toLowerCase();
  if (/stop|stopped|cancel|cancelled|canceled|killed|已停止|已取消/.test(normalized)) {
    return { state: 'stopped', label: '已停止', tone: 'pending' };
  }
  if (/error|fail|failed|crash|timeout|异常|失败|崩溃|超时/.test(normalized)) {
    return { state: 'retryable', label: '可重试', tone: 'danger' };
  }
  if (/running|active|resume|recover|restored|进行|恢复/.test(normalized)) {
    return { state: 'recoverable', label: '可恢复', tone: 'running' };
  }
  return { state: 'recoverable', label: '可恢复', tone: 'running' };
}

function recoveryStateFromSession(row = {}) {
  const base = recoveryStateFromStatus(row.recoveryStatus || row.status || row.state, 'recoverable');
  return {
    ...base,
    sessionId: row.sessionId || row.id || '',
    prompt: sanitizeDisplayText(row.promptPreview || row.prompt || row.title || '', ''),
    detail: sanitizeDisplayText(row.recoveryHint || row.detail || row.message, '窗口恢复后可继续查看这轮任务的输出。')
  };
}

function recoveryStateFromAgentEvent(event = {}) {
  if (!event || (event.kind !== 'recovery' && !event.recovery && !event.recoveryStatus)) return null;
  const raw = event.recovery && typeof event.recovery === 'object' ? event.recovery : event;
  const base = recoveryStateFromStatus(raw.status || raw.state || raw.recoveryStatus || event.status || event.state, 'recoverable');
  return {
    ...base,
    sessionId: raw.sessionId || event.sessionId || '',
    prompt: sanitizeDisplayText(raw.promptPreview || raw.prompt || event.promptPreview || event.prompt, ''),
    detail: sanitizeDisplayText(raw.detail || raw.message || event.text || event.message, '任务状态已从本地运行记录恢复。')
  };
}

function isContinuationPrompt(value = '') {
  return /^(是|对|好|好的|可以|继续|继续吧|同意|确认|允许|允许一次|执行|开始|行|嗯|ok|yes|y|go|继续执行)[。！!,.，\s]*$/i.test(String(value || '').trim());
}

function permissionActionFromValue(value = {}) {
  const raw = typeof value === 'string' ? value : value?.action;
  const text = String(raw || '').trim();
  if (text === 'allow' || /允许|确认|继续|approve|yes/i.test(text)) {
    return {
      action: 'allow',
      label: '允许一次',
      tone: 'pending',
      accessMode: 'fullAccess',
      prompt: '用户已在权限确认卡中选择「允许一次」。请继续上一轮正在等待确认的本地操作；该授权只对刚才那一步生效。不要把这条确认当作新的用户任务，不要重新询问，执行后直接返回结果。'
    };
  }
  if (text === 'plan' || /计划|只读|只做/i.test(text)) {
    return {
      action: 'plan',
      label: '只做计划',
      tone: 'warn',
      accessMode: 'default',
      prompt: '用户已在权限确认卡中选择「只做计划」。不要执行本地文件、命令或系统目录操作；请基于上一轮任务输出只读计划、风险和需要用户手动执行的步骤。'
    };
  }
  return {
    action: 'deny',
    label: '拒绝',
    tone: 'danger',
    accessMode: 'default',
    prompt: '用户已在权限确认卡中选择「拒绝」。不要执行上一轮等待确认的本地操作；请给出无需该操作的替代方案或说明无法继续的原因。'
  };
}

function permissionContinuationPrompt(decision = {}) {
  return [
    '权限确认回执：',
    decision.prompt || permissionActionFromValue(decision).prompt,
    '',
    '交互规则：这是一条来自权限确认卡的后台控制消息，不要在回复中复述它，也不要把它当成新的业务需求。'
  ].join('\n');
}

function readableMessageText(message = {}) {
  const text = String(message.text || '').replace(/\s+/g, ' ').trim();
  if (text) return text;
  if (Array.isArray(message.attachments) && message.attachments.length) {
    return `发送了 ${message.attachments.length} 个附件：${message.attachments.map((item) => item.name).filter(Boolean).slice(0, 4).join('、')}`;
  }
  return '';
}

function retryPromptFromMessage(message = {}) {
  const text = String(message.originalPrompt || '').trim();
  if (!text) return '';
  const marker = '用户当前输入：';
  const markerIndex = text.lastIndexOf(marker);
  if (markerIndex < 0) return text;
  const trailing = text.slice(markerIndex + marker.length).trim();
  const nextSectionIndex = trailing.search(/\n{2,}/);
  return (nextSectionIndex >= 0 ? trailing.slice(0, nextSectionIndex) : trailing).trim() || text;
}

function stripArtifactPathToken(value = '') {
  return String(value || '')
    .trim()
    .replace(/^['"`<([{]+|['"`>)\]}.,;，。；：:]+$/g, '');
}

function normalizeArtifactPathToken(value = '') {
  let next = stripArtifactPathToken(value);
  if (!next) return '';
  if (/^file:\/\//i.test(next)) {
    try {
      const url = new URL(next);
      next = decodeURIComponent(url.pathname || '');
      if (/^\/[A-Za-z]:\//.test(next)) next = next.slice(1);
      if (/^[A-Za-z]:\//.test(next)) next = next.replace(/\//g, '\\');
    } catch {
      next = next.replace(/^file:\/\/\/?/i, '');
    }
  }
  next = stripArtifactPathToken(next);
  if (/^Users[\\/]/i.test(next)) next = `C:\\${next}`;
  return next;
}

function isExplicitLocalArtifactPathToken(value = '') {
  const raw = String(value || '').trim();
  if (!raw) return false;
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:\/\//.test(raw) && !/^file:\/\//i.test(raw)) return false;
  return /^file:\/\//i.test(raw)
    || /^workspace:\//i.test(raw)
    || /^[A-Za-z]:[\\/]/.test(raw)
    || /^\\\\[^\\]/.test(raw)
    || /^\.{1,2}[\\/]/.test(raw)
    || /^~[\\/]/.test(raw)
    || /^\//.test(raw)
    || /[\\/]/.test(raw);
}

function artifactExtension(path = '') {
  const match = String(path || '').match(/\.([a-z0-9]+)$/i);
  return match ? match[1].toLowerCase() : '';
}

function artifactExtensionPattern() {
  return [...ARTIFACT_PREVIEW_EXTENSIONS]
    .sort((left, right) => right.length - left.length)
    .map((ext) => ext.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
}

function extensionFromMimeType(type = '') {
  const mime = String(type || '').toLowerCase();
  if (mime.includes('png')) return 'png';
  if (mime.includes('jpeg') || mime.includes('jpg')) return 'jpg';
  if (mime.includes('webp')) return 'webp';
  if (mime.includes('gif')) return 'gif';
  if (mime.includes('svg')) return 'svg';
  if (mime.includes('pdf')) return 'pdf';
  if (mime.includes('csv')) return 'csv';
  if (mime.includes('json')) return 'json';
  if (mime.includes('html')) return 'html';
  if (mime.includes('markdown')) return 'md';
  if (mime.includes('spreadsheet') || mime.includes('ms-excel')) return 'xlsx';
  if (mime.includes('presentation') || mime.includes('ms-powerpoint')) return 'pptx';
  if (mime.includes('wordprocessing') || mime.includes('msword')) return 'docx';
  if (mime.startsWith('text/')) return 'txt';
  return '';
}

function artifactPreviewKind(ext = '', type = '') {
  const normalizedExt = String(ext || extensionFromMimeType(type)).toLowerCase();
  const normalizedType = String(type || '').toLowerCase();
  if (ARTIFACT_HTML_EXTENSIONS.includes(normalizedExt)) return 'html';
  if (ARTIFACT_TEXT_EXTENSIONS.includes(normalizedExt) || normalizedType.startsWith('text/')) return 'text';
  if (ARTIFACT_IMAGE_EXTENSIONS.includes(normalizedExt) || normalizedType.startsWith('image/')) return 'image';
  if (normalizedExt === 'pdf' || normalizedType.includes('pdf')) return 'pdf';
  if (ARTIFACT_OFFICE_EXTENSIONS.includes(normalizedExt) || /officedocument|msword|ms-excel|ms-powerpoint/.test(normalizedType)) return 'office';
  return 'binary';
}

function artifactCanUseTextPreview(artifact = {}) {
  const kind = artifactPreviewKind(artifact.ext, artifact.type || artifact.mimeType);
  return kind === 'text' || kind === 'html';
}

function fileNameFromArtifactPath(path = '') {
  return String(path || '').split(/[\\/]/).filter(Boolean).at(-1) || path;
}

function artifactLanguageFromPath(path = '') {
  const ext = artifactExtension(path);
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (ext === 'htm' || ext === 'html') return 'html';
  if (['js', 'jsx'].includes(ext)) return 'javascript';
  if (['ts', 'tsx'].includes(ext)) return 'typescript';
  if (['yml', 'yaml'].includes(ext)) return 'yaml';
  return ext || 'text';
}

function parseArtifactPathToken(rawValue = '') {
  let value = stripArtifactPathToken(rawValue);
  if (!value || /^https?:\/\//i.test(value) || !isExplicitLocalArtifactPathToken(value)) return null;
  try {
    value = decodeURIComponent(value);
  } catch {
    // Keep the original value when the token is not URI encoded.
  }
  value = normalizeArtifactPathToken(value);
  const extPattern = artifactExtensionPattern();
  const match = value.match(new RegExp(`^(.+?\\.(${extPattern}))(?:#L?(\\d+)(?:-L?(\\d+))?)?(?::(\\d+))?(?::(\\d+))?$`, 'i'));
  if (!match) return null;
  const path = normalizeArtifactPathToken(match[1]);
  const ext = String(match[2] || '').toLowerCase();
  if (!ARTIFACT_PREVIEW_EXTENSIONS.includes(ext)) return null;
  const hashLine = Number(match[3]) || null;
  const hashEndLine = Number(match[4]) || null;
  const suffixLine = Number(match[5]) || null;
  const suffixColumn = Number(match[6]) || null;
  const line = hashLine || suffixLine || null;
  return {
    id: `${path}:${line || ''}:${suffixColumn || ''}`,
    path,
    name: fileNameFromArtifactPath(path),
    ext,
    line,
    endLine: hashEndLine || null,
    column: suffixColumn || null,
    raw: rawValue
  };
}

function looksLikeRemoteArtifactPathSubstring(token = '', source = '', start = 0) {
  const raw = String(token || '').trim();
  if (!raw || /^(?:file:|workspace:|[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/]|~[\\/]|\/)/.test(raw)) return false;
  const prefix = String(source || '').slice(Math.max(0, Number(start) - 24), Number(start)).toLowerCase();
  if (/(?:https?:\/\/|www\.)[^ \r\n<>"'`]*$/.test(prefix)) return true;
  return /^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[\\/]|$)/i.test(raw);
}

function extractArtifactReferences(text = '') {
  const source = String(text || '');
  if (!source) return [];
  const found = new Map();
  const add = (token) => {
    const artifact = parseArtifactPathToken(token);
    if (!artifact) return;
    const key = artifact.id.toLowerCase();
    if (!found.has(key)) found.set(key, artifact);
  };

  const markdownLinkPattern = /\[[^\]]{1,120}\]\(([^)\n]+)\)/g;
  for (const match of source.matchAll(markdownLinkPattern)) add(match[1]);

  const extPattern = artifactExtensionPattern();
  const pathPatterns = [
    new RegExp(
      `(?:[A-Za-z]:[\\\\/]|workspace:/|\\.{1,2}[\\\\/]|~[\\\\/]|/|[A-Za-z0-9_.-]+[\\\\/])(?:[^\\s<>"'\`|]+[\\\\/])*[^\\s<>"'\`|]+?\\.(?:${extPattern})(?:#L?\\d+(?:-L?\\d+)?|:\\d+(?::\\d+)?)?`,
      'gi'
    ),
    new RegExp(
      `(?:[A-Za-z]:[\\\\/]|workspace:/|\\.{1,2}[\\\\/]|~[\\\\/]|/)[^\\r\\n<>"'\`|]{0,900}?\\.(?:${extPattern})(?:#L?\\d+(?:-L?\\d+)?|:\\d+(?::\\d+)?)?`,
      'gi'
    )
  ];
  for (const pattern of pathPatterns) {
    for (const match of source.matchAll(pattern)) {
      if (looksLikeRemoteArtifactPathSubstring(match[0], source, match.index || 0)) continue;
      add(match[0]);
    }
  }

  return [...found.values()].slice(0, ARTIFACT_PREVIEW_MAX_ITEMS);
}

function finalArtifactsFromText(text = '') {
  return mergeArtifactReferences([
    ...extractArtifactReferences(text),
    ...inferArtifactsFromDirectorySummary(text)
  ]).map((artifact) => ({ ...artifact, source: 'assistant-final' }));
}

function artifactFromAttachment(attachment = {}, index = 0) {
  const path = String(attachment.path || attachment.filePath || '').trim();
  const name = String(attachment.name || attachment.fileName || fileNameFromArtifactPath(path) || `附件 ${index + 1}`).trim();
  const ext = artifactExtension(path || name) || extensionFromMimeType(attachment.type || attachment.mimeType);
  if (!ext || !ARTIFACT_PREVIEW_EXTENSIONS.includes(ext)) return null;
  return {
    id: `attachment:${attachmentStableKey(attachment, index)}:${ext}`,
    path: path || name,
    name,
    ext,
    type: attachment.type || attachment.mimeType || '',
    sizeBytes: Number(attachment.sizeBytes || attachment.size) || 0,
    previewUrl: safeAttachmentPreviewUrl(attachment.previewUrl || attachment.previewDataUrl || attachment.thumbnail || ''),
    source: attachment.source || 'attachment',
    raw: path || name
  };
}

function attachmentKindFromName(name = '', type = '') {
  const ext = artifactExtension(name);
  const mime = String(type || '').toLowerCase();
  if (mime.startsWith('image/') || ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg'].includes(ext)) return 'image';
  if (mime.startsWith('video/') || ['mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v'].includes(ext)) return 'video';
  if (['xls', 'xlsx', 'xlsm', 'csv'].includes(ext)) return 'sheet';
  if (['ppt', 'pptx', 'pptm'].includes(ext)) return 'slide';
  if (['doc', 'docx', 'md', 'txt', 'pdf'].includes(ext)) return 'document';
  if (['js', 'jsx', 'ts', 'tsx', 'json', 'css', 'html', 'htm', 'py', 'java', 'sql'].includes(ext)) return 'code';
  return 'file';
}

function projectFileRelativeLabel(file = {}) {
  const direct = String(file.relativePath || file.projectRelativePath || file.workspaceRelativePath || '').replace(/\\/g, '/').trim();
  if (direct) return direct;
  const label = String(file.pathLabel || '').replace(/\\/g, '/').trim();
  const markerIndex = label.toLowerCase().lastIndexOf('/files/');
  if (markerIndex >= 0) return label.slice(markerIndex + 7) || file.name || 'project-file';
  return sanitizeDisplayText(file.name || file.path || file.filePath || 'project-file', 'project-file');
}

function normalizeProjectFileItem(file = {}, index = 0) {
  const name = sanitizeDisplayText(file.name || file.fileName || fileNameFromArtifactPath(file.path || file.filePath || file.pathLabel || '') || `项目文件 ${index + 1}`, `项目文件 ${index + 1}`);
  const pathValue = String(file.path || file.filePath || file.pathLabel || '').trim();
  const type = String(file.type || file.mimeType || '').trim();
  const relativePath = projectFileRelativeLabel({ ...file, name });
  const kind = String(file.kind || attachmentKindFromName(name || relativePath, type)).trim() || 'file';
  const previewUrl = safeAttachmentPreviewUrl(file.previewUrl || file.previewDataUrl || file.thumbnail || '');
  return {
    ...file,
    id: String(file.id || `${relativePath}:${name}:${index}`).slice(0, 220),
    name,
    path: pathValue,
    filePath: pathValue,
    type,
    mimeType: String(file.mimeType || file.type || '').trim(),
    relativePath,
    kind,
    previewUrl,
    previewDataUrl: previewUrl,
    thumbnail: previewUrl,
    sizeBytes: Number(file.sizeBytes || file.size) || 0,
    modifiedAt: file.modifiedAt || file.updatedAt || null
  };
}

function normalizeProjectFileItems(files = []) {
  return (Array.isArray(files) ? files : []).map((file, index) => normalizeProjectFileItem(file, index));
}

function isVisibleProjectFile(file = {}) {
  const normalized = normalizeProjectFileItem(file);
  const name = String(normalized.name || '').trim().toLowerCase();
  const relativePath = String(normalized.relativePath || normalized.pathLabel || normalized.path || '').replace(/\\/g, '/').toLowerCase();
  if (!name) return false;
  if (name === 'project-memory.md') return false;
  if (name.startsWith('.') || name.startsWith('~$')) return false;
  if (/\.(tmp|temp|part|crdownload|lock|bak)$/i.test(name)) return false;
  if (/(^|\/)(project-memory\.md|\.ecorex-project\.json|project-context\.json)$/i.test(relativePath)) return false;
  if (/(^|\/)(tmp|temp|cache|intermediate|drafts?|\.tmp|\.temp|node_modules|__pycache__)(\/|$)/i.test(relativePath)) return false;
  return true;
}

function filterVisibleProjectFiles(files = []) {
  return normalizeProjectFileItems(files).filter(isVisibleProjectFile);
}

function projectFileIdentity(file = {}) {
  const normalized = normalizeProjectFileItem(file);
  return String(
    normalized.relativePath
    || normalized.path
    || normalized.filePath
    || normalized.pathLabel
    || normalized.name
    || ''
  ).replace(/\\/g, '/').toLowerCase();
}

function projectFileIdentitySet(files = []) {
  return new Set(filterVisibleProjectFiles(files).map(projectFileIdentity).filter(Boolean));
}

function projectFileToAttachment(file = {}, project = {}) {
  const normalized = normalizeProjectFileItem(file);
  return {
    id: `project-file:${normalized.id}`,
    name: normalized.name,
    path: normalized.path,
    filePath: normalized.filePath || normalized.path,
    type: normalized.type,
    mimeType: normalized.mimeType || normalized.type,
    sizeBytes: normalized.sizeBytes,
    source: 'project',
    projectId: project?.id || '',
    projectName: project?.name || '',
    pathLabel: normalized.pathLabel || normalized.relativePath,
    relativePath: normalized.relativePath,
    previewUrl: normalized.previewUrl,
    previewDataUrl: normalized.previewDataUrl,
    status: 'ready',
    progress: 100,
    lastModified: normalized.modifiedAt
  };
}

function projectFileToArtifact(file = {}) {
  const normalized = normalizeProjectFileItem(file);
  return {
    id: `project-file-preview:${normalized.id}`,
    name: normalized.name,
    path: normalized.path || normalized.pathLabel || normalized.relativePath,
    filePath: normalized.filePath || normalized.path,
    ext: artifactExtension(normalized.name || normalized.path || normalized.pathLabel),
    type: normalized.type,
    mimeType: normalized.mimeType || normalized.type,
    sizeBytes: normalized.sizeBytes,
    source: 'project-file',
    previewUrl: normalized.previewUrl
  };
}

function filterProjectFileCandidates(files = [], query = '', limit = 8) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  return normalizeProjectFileItems(files)
    .filter((file) => {
      if (!normalizedQuery) return true;
      return [file.name, file.relativePath, file.pathLabel, file.type]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedQuery);
    })
    .slice(0, limit);
}

function findProjectFileMention(value = '', cursor = 0) {
  const text = String(value || '');
  const safeCursor = Math.max(0, Math.min(Number(cursor) || 0, text.length));
  const before = text.slice(0, safeCursor);
  const match = before.match(/(^|[\s([{（【])@([^\s@]{0,80})$/u);
  if (!match) return null;
  const query = match[2] || '';
  const start = before.length - query.length - 1;
  return { start, end: safeCursor, query };
}

function buildProjectFileTree(files = []) {
  const root = { id: 'root', name: 'files', type: 'folder', children: [] };
  const ensureChild = (parent, segment, pathKey) => {
    let child = parent.children.find((item) => item.type === 'folder' && item.name === segment);
    if (!child) {
      child = { id: `folder:${pathKey}`, name: segment, type: 'folder', children: [] };
      parent.children.push(child);
    }
    return child;
  };
  normalizeProjectFileItems(files).forEach((file, index) => {
    const parts = projectFileRelativeLabel(file).split(/[\\/]+/).map((part) => part.trim()).filter(Boolean);
    const pathParts = parts.length ? parts : [file.name || `项目文件 ${index + 1}`];
    let cursor = root;
    pathParts.slice(0, -1).forEach((part, partIndex) => {
      cursor = ensureChild(cursor, part, pathParts.slice(0, partIndex + 1).join('/'));
    });
    cursor.children.push({
      id: `file:${file.id || pathParts.join('/')}:${index}`,
      name: pathParts.at(-1) || file.name,
      type: 'file',
      file
    });
  });
  const sortNode = (node) => {
    node.children.sort((left, right) => {
      if (left.type !== right.type) return left.type === 'folder' ? -1 : 1;
      return String(left.name).localeCompare(String(right.name), 'zh-CN');
    });
    node.children.filter((child) => child.type === 'folder').forEach(sortNode);
    return node;
  };
  return sortNode(root);
}

function artifactsFromAttachments(attachments = []) {
  return (Array.isArray(attachments) ? attachments : [])
    .map((attachment, index) => artifactFromAttachment(attachment, index))
    .filter(Boolean);
}

function artifactsFromLedger(ledger = []) {
  return (Array.isArray(ledger) ? ledger : [])
    .flatMap((item) => extractArtifactReferences([
      item.path,
      item.filePath,
      item.action,
      item.detail,
      item.output
    ].filter(Boolean).join('\n')));
}

function inferArtifactsFromDirectorySummary(text = '') {
  const source = String(text || '');
  if (!source) return [];
  const extPattern = artifactExtensionPattern();
  const directoryPattern = /(?:[A-Za-z]:[\\/]|workspace:\/|Users[\\/]|~[\\/]|\/(?:Users|home|mnt|tmp|var|private)\/)[^\r\n<>*"|?]*[\\/]/gi;
  const fileNamePattern = new RegExp(`([^\\\\/\\s<>:"'\\\`|]+\\.(?:${extPattern}))`, 'gi');
  const directories = [...source.matchAll(directoryPattern)]
    .map((match) => stripArtifactPathToken(match[0]))
    .filter(Boolean);
  if (!directories.length) return [];
  const baseDir = normalizeArtifactPathToken(directories.at(-1));
  const found = new Map();
  for (const match of source.matchAll(fileNamePattern)) {
    const name = stripArtifactPathToken(match[1]);
    if (!name || /[\\/]/.test(name)) continue;
    const ext = artifactExtension(name);
    if (!ARTIFACT_PREVIEW_EXTENSIONS.includes(ext)) continue;
    const separator = baseDir.includes('\\') || /^[A-Za-z]:/.test(baseDir) ? '\\' : '/';
    const path = `${baseDir.replace(/[\\/]*$/, '')}${separator}${name}`;
    const key = path.toLowerCase();
    if (!found.has(key)) {
      found.set(key, {
        id: key,
        path,
        name,
        ext,
        raw: name,
        source: 'assistant-final'
      });
    }
  }
  return Array.from(found.values()).slice(0, ARTIFACT_PREVIEW_MAX_ITEMS);
}

function mergeArtifactReferences(artifacts = []) {
  const byKey = new Map();
  for (const artifact of artifacts.filter(Boolean)) {
    const normalizedPath = artifact.path || artifact.filePath
      ? normalizeArtifactPathToken(artifact.path || artifact.filePath)
      : '';
    const normalizedArtifact = normalizedPath
      ? {
          ...artifact,
          path: normalizedPath,
          name: artifact.name || fileNameFromArtifactPath(normalizedPath),
          ext: artifact.ext || artifactExtension(normalizedPath),
          id: String(artifact.id || normalizedPath)
        }
      : artifact;
    const key = String(normalizedArtifact.path || normalizedArtifact.filePath || normalizedArtifact.raw || normalizedArtifact.name || normalizedArtifact.id || '')
      .replace(/\\/g, '/')
      .toLowerCase();
    if (!key || byKey.has(key)) continue;
    byKey.set(key, normalizedArtifact);
  }
  return Array.from(byKey.values()).slice(0, ARTIFACT_PREVIEW_MAX_ITEMS);
}

function artifactLocationLabel(artifact = {}, fallback = '') {
  const linePart = artifact.line ? `:${artifact.line}${artifact.column ? `:${artifact.column}` : ''}` : '';
  const rangePart = artifact.endLine && artifact.endLine !== artifact.line ? `-${artifact.endLine}` : '';
  return `${artifact.path || fallback}${linePart}${rangePart}`;
}

function isImageArtifact(artifact = {}) {
  return artifactPreviewKind(artifact.ext, artifact.type || artifact.mimeType) === 'image';
}

function artifactMatchKey(value = '') {
  return normalizeArtifactPathToken(String(value || ''))
    .replace(/\\/g, '/')
    .toLowerCase();
}

function findRichImageArtifact(url = '', label = '', artifacts = []) {
  const parsed = parseArtifactPathToken(url);
  const candidates = [
    parsed?.path,
    stripArtifactPathToken(url),
    label
  ].filter(Boolean).map(artifactMatchKey);
  if (!candidates.length) return null;
  return artifacts.find((artifact) => {
    if (!isImageArtifact(artifact)) return false;
    const keys = [
      artifact.path,
      artifact.filePath,
      artifact.raw,
      artifact.name
    ].filter(Boolean).map(artifactMatchKey);
    return keys.some((key) => candidates.includes(key));
  }) || null;
}

function normalizeArtifactPreviewResult(result = {}, artifact = {}) {
  const file = result.file && typeof result.file === 'object' ? result.file : {};
  const metadata = result.metadata && typeof result.metadata === 'object' ? result.metadata : {};
  const path = String(result.path || result.filePath || file.path || file.pathLabel || artifact.path || '').trim();
  const mimeType = result.mimeType
    || result.type
    || file.mimeType
    || metadata.mimeType
    || artifact.type
    || artifact.mimeType
    || '';
  const ext = artifact.ext || artifactExtension(path || artifact.name) || extensionFromMimeType(mimeType);
  const kind = result.kind || artifactPreviewKind(ext, mimeType);
  const previewUrl = safeAttachmentPreviewUrl(result.previewUrl || result.previewDataUrl || result.dataUrl || result.thumbnail || artifact.previewUrl || '');
  const rawContent = Array.isArray(result.lines)
    ? result.lines.join('\n')
    : (result.content ?? result.text ?? result.body ?? result.data ?? '');
  const content = typeof rawContent === 'string' ? rawContent : JSON.stringify(rawContent ?? '', null, 2);
  const metadataDetail = Object.keys(metadata).length || Object.keys(file).length
    ? { ...file, ...metadata }
    : (result.info || result.fileInfo || {});
  return {
    ok: result.ok !== false,
    path,
    name: result.name || file.name || artifact.name || fileNameFromArtifactPath(path),
    kind,
    ext,
    language: result.language || file.language || artifactLanguageFromPath(path || artifact.name),
    mimeType,
    previewUrl,
    sizeBytes: Number(result.sizeBytes || result.size || file.sizeBytes || metadata.sizeBytes || artifact.sizeBytes) || 0,
    reason: result.reason || result.unavailableReason || result.error || '',
    metadata: compactEventDetail(metadataDetail, 520),
    content: content.slice(0, ARTIFACT_PREVIEW_MAX_CHARS),
    previewId: String(result.previewId || metadata.previewId || '').slice(0, 120),
    truncated: Boolean(result.truncated) || content.length > ARTIFACT_PREVIEW_MAX_CHARS,
    startLine: Number(result.startLine || result.lineStart || artifact.line || 1) || 1,
    column: Number(result.column || artifact.column || 0) || null,
    previewable: result.previewable !== false,
    renderMode: result.renderMode || '',
    error: result.error || ''
  };
}

const artifactPreviewRequestCache = new Map();

function artifactPreviewCacheKey(artifact = {}) {
  const pathKey = String(artifact.path || artifact.filePath || artifact.raw || '').trim();
  const idKey = String(artifact.id || '').trim();
  const sessionKey = String(artifact.sessionId || artifact.agentSessionId || artifact.claudeSessionId || '').trim();
  const locationKey = `${artifact.line || ''}:${artifact.column || ''}`;
  return [idKey, pathKey, sessionKey, locationKey].filter(Boolean).join('|');
}

function cacheArtifactPreviewRequest(cacheKey, request) {
  if (!cacheKey) return request;
  if (artifactPreviewRequestCache.size >= ARTIFACT_PREVIEW_CACHE_MAX_ITEMS) {
    const oldestKey = artifactPreviewRequestCache.keys().next().value;
    if (oldestKey) artifactPreviewRequestCache.delete(oldestKey);
  }
  artifactPreviewRequestCache.set(cacheKey, request);
  return request;
}

async function previewArtifactWithBridge(artifact = {}) {
  const cacheKey = artifactPreviewCacheKey(artifact);
  if (cacheKey && artifactPreviewRequestCache.has(cacheKey)) {
    return artifactPreviewRequestCache.get(cacheKey);
  }
  const request = previewArtifactWithBridgeUncached(artifact).catch((error) => {
    if (cacheKey) artifactPreviewRequestCache.delete(cacheKey);
    throw error;
  });
  return cacheArtifactPreviewRequest(cacheKey, request);
}

async function previewArtifactWithBridgeUncached(artifact = {}) {
  const kind = artifactPreviewKind(artifact.ext, artifact.type || artifact.mimeType);
  if (kind === 'image' && artifact.previewUrl) {
    return {
      ok: true,
      kind,
      path: artifact.path,
      name: artifact.name,
      mimeType: artifact.type || artifact.mimeType || '',
      previewUrl: artifact.previewUrl,
      sizeBytes: artifact.sizeBytes || 0
    };
  }
  if (!artifact.path) {
    return {
      ok: false,
      unsupported: true,
      kind,
      path: artifact.path,
      name: artifact.name,
      mimeType: artifact.type || artifact.mimeType || '',
      sizeBytes: artifact.sizeBytes || 0,
      reason: kind === 'pdf'
        ? 'PDF 当前仅显示文件元信息，EcoreX 不会跳转系统应用。'
        : kind === 'office'
          ? 'Office 文件当前仅显示文件元信息，EcoreX 不会跳转系统应用。'
          : '该二进制格式当前仅显示文件元信息，EcoreX 不会跳转系统应用。'
    };
  }
  const payload = {
    name: artifact.name,
    path: artifact.path,
    filePath: artifact.path,
    mimeType: artifact.type || artifact.mimeType || '',
    type: artifact.type || artifact.mimeType || '',
    sizeBytes: artifact.sizeBytes || 0,
    line: artifact.line,
    column: artifact.column,
    sessionId: artifact.sessionId || artifact.agentSessionId || '',
    agentSessionId: artifact.agentSessionId || artifact.sessionId || '',
    claudeSessionId: artifact.claudeSessionId || '',
    projectId: artifact.projectId || '',
    projectName: artifact.projectName || '',
    source: artifact.source || 'assistant-artifact',
    maxChars: ARTIFACT_PREVIEW_MAX_CHARS,
    maxBytes: ARTIFACT_PREVIEW_MAX_CHARS
  };
  const candidates = [
    'previewFile',
    'files.preview',
    'workspace.previewFile',
    'getFilePreview',
    'filePreview',
    'readFilePreview'
  ];
  let lastError = null;
  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(payload);
    } catch (firstError) {
      try {
        return await fn(artifact.path, payload);
      } catch (secondError) {
        lastError = secondError || firstError;
      }
    }
  }
  return {
    ok: false,
    missing: true,
    unauthorized: isUnauthorizedError(lastError),
    error: lastError?.message || 'previewFile bridge is not available'
  };
}

async function validateArtifactAvailabilityWithBridge(artifact = {}) {
  if (!artifact.path) return false;
  const bridge = window.ecorex;
  if (!bridge) return true;
  const isNotFoundPreview = (result = {}) => (
    result?.reason === 'not-found'
    || result?.status === 'not-found'
    || /not[- ]found|file was not found|outside allowed roots/i.test(String(result?.error || result?.message || ''))
  );
  const hasPreviewLocation = (result = {}) => Boolean(
    result?.file
    || result?.path
    || result?.filePath
    || result?.content
    || result?.previewUrl
    || result?.name
    || result?.metadata
  );
  const isPreviewMetadata = (result = {}) => (
    result?.ok !== false && (
      result?.unsupported === true
      || result?.previewable === false
      || result?.renderMode === 'metadata'
      || hasPreviewLocation(result)
    )
  );
  const payload = {
    name: artifact.name,
    path: artifact.path,
    filePath: artifact.path,
    mimeType: artifact.type || artifact.mimeType || '',
    type: artifact.type || artifact.mimeType || '',
    sessionId: artifact.sessionId || artifact.agentSessionId || '',
    agentSessionId: artifact.agentSessionId || artifact.sessionId || '',
    claudeSessionId: artifact.claudeSessionId || '',
    projectId: artifact.projectId || '',
    projectName: artifact.projectName || '',
    source: artifact.source || 'assistant-artifact',
    validateOnly: true
  };
  const candidates = [
    'previewFile',
    'files.preview',
    'workspace.previewFile',
    'workspace.preview'
  ];
  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      const result = await fn(payload);
      if (isNotFoundPreview(result)) return false;
      if (isPreviewMetadata(result) && hasPreviewLocation(result)) return true;
    } catch (firstError) {
      try {
        const result = await fn(artifact.path, payload);
        if (isNotFoundPreview(result)) return false;
        if (isPreviewMetadata(result) && hasPreviewLocation(result)) return true;
      } catch {
        // Try the next bridge shape.
      }
      if (/not found|outside allowed roots|only supports local files/i.test(firstError?.message || '')) return false;
    }
  }
  return false;
}

async function openAttachmentFileWithBridge(attachment = {}) {
  const payload = {
    id: attachment.id,
    attachmentId: attachment.id,
    name: attachment.name,
    path: attachment.path || attachment.filePath,
    filePath: attachment.filePath || attachment.path,
    sizeBytes: attachment.sizeBytes || attachment.size || 0,
    mimeType: attachment.mimeType || attachment.type || '',
    type: attachment.type || attachment.mimeType || ''
  };
  const candidates = [
    'openAttachmentFile',
    'attachments.openFile',
    'attachment.openFile'
  ];
  let lastError = null;
  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(payload);
    } catch (firstError) {
      try {
        return await fn(payload.path, payload);
      } catch (secondError) {
        lastError = secondError || firstError;
      }
    }
  }
  return {
    ok: false,
    missing: true,
    unauthorized: isUnauthorizedError(lastError),
    error: lastError?.message || 'openAttachmentFile bridge is not available'
  };
}

async function openArtifactFileWithBridge(artifact = {}) {
  const payload = {
    id: artifact.id,
    name: artifact.name || fileNameFromArtifactPath(artifact.path),
    path: artifact.path || artifact.filePath,
    filePath: artifact.filePath || artifact.path,
    mimeType: artifact.mimeType || artifact.type || '',
    type: artifact.type || artifact.mimeType || '',
    sessionId: artifact.sessionId || artifact.agentSessionId || '',
    agentSessionId: artifact.agentSessionId || artifact.sessionId || '',
    claudeSessionId: artifact.claudeSessionId || '',
    projectId: artifact.projectId || '',
    projectName: artifact.projectName || '',
    source: artifact.source || 'assistant-artifact'
  };
  const candidates = [
    'openArtifactFile',
    'openGeneratedFile',
    'artifacts.openFile',
    'artifact.openFile'
  ];
  let lastError = null;
  for (const candidate of candidates) {
    const fn = getBridgeFunction(candidate);
    if (typeof fn !== 'function') continue;
    try {
      return await fn(payload);
    } catch (firstError) {
      try {
        return await fn(payload.path, payload);
      } catch (secondError) {
        lastError = secondError || firstError;
      }
    }
  }
  return {
    ok: false,
    missing: true,
    unauthorized: isUnauthorizedError(lastError),
    error: lastError?.message || 'openArtifactFile bridge is not available'
  };
}

async function openExternalUrlWithBridge(url = '') {
  const safeUrl = safeChatExternalUrl(url);
  if (!safeUrl) return { ok: false, error: 'Invalid link.' };
  if (!window.ecorex) {
    window.open(safeUrl, '_blank', 'noopener,noreferrer');
    return { ok: true, opened: true, fallback: 'window.open' };
  }
  return callEcorexAction(['openExternalUrl', 'shell.openExternal', 'shell.openExternalUrl'], { url: safeUrl });
}

function buildArtifactPromptReference(artifact = {}, selection = {}) {
  const selectedLine = Number(selection.line || artifact.line || 0) || null;
  const location = selectedLine
    ? `${artifact.path || artifact.name}:${selectedLine}${artifact.column ? `:${artifact.column}` : ''}`
    : artifactLocationLabel(artifact);
  const snippet = String(selection.text || '').trim().slice(0, 900);
  return [
    `文件：${location}`,
    snippet ? `片段：\n${snippet}` : '',
    '修改意图：[在这里填写你希望如何修改这段产物]'
  ].filter(Boolean).join('\n');
}

function sanitizeComposerReferenceItem(reference = {}, index = 0) {
  const artifact = reference.artifact && typeof reference.artifact === 'object' ? reference.artifact : reference;
  const selectedLine = Number(reference.line || artifact.line || 0) || null;
  const existingLocation = String(reference.location || '').trim();
  const location = existingLocation
    ? (selectedLine && !/(?:#L?\d+|:\d+(?::\d+)?)$/i.test(existingLocation) ? `${existingLocation}:${selectedLine}` : existingLocation)
    : selectedLine
    ? `${artifact.path || artifact.name}:${selectedLine}${artifact.column ? `:${artifact.column}` : ''}`
    : artifactLocationLabel(artifact, artifact.name || `文件 ${index + 1}`);
  const snippet = String(reference.text || reference.snippet || '').trim().slice(0, COMPOSER_REFERENCE_SNIPPET_CHARS);
  const name = sanitizeDisplayText(reference.name || artifact.name || fileNameFromArtifactPath(location), `文件 ${index + 1}`).slice(0, 160);
  const createdTime = sanitizeDisplayText(reference.createdTime || reference.time || formatAgentEventTime(), '').slice(0, 16);
  return {
    id: String(reference.id || `${location}:${snippet}:${index}`).slice(0, 220),
    name,
    location: sanitizeDisplayText(location, name).slice(0, 600),
    path: String(reference.path || artifact.path || '').slice(0, 600),
    line: selectedLine,
    text: snippet,
    createdTime
  };
}

function sanitizeComposerReferences(references = []) {
  return (Array.isArray(references) ? references : [])
    .filter(Boolean)
    .slice(0, MAX_COMPOSER_REFERENCES)
    .map((reference, index) => sanitizeComposerReferenceItem(reference, index));
}

function createComposerReferenceFromArtifact(artifact = {}, selection = {}) {
  const selectedLine = Number(selection.line || artifact.line || 0) || null;
  const location = selectedLine
    ? `${artifact.path || artifact.name}:${selectedLine}${artifact.column ? `:${artifact.column}` : ''}`
    : artifactLocationLabel(artifact, artifact.name);
  const text = String(selection.text || '').trim().slice(0, COMPOSER_REFERENCE_SNIPPET_CHARS);
  return sanitizeComposerReferenceItem({
    id: `ref:${location}:${text}:${Date.now()}`,
    artifact,
    name: artifact.name || fileNameFromArtifactPath(location),
    path: artifact.path || '',
    line: selectedLine,
    text,
    createdTime: formatAgentEventTime()
  });
}

function buildComposerReferenceSection(references = []) {
  const items = sanitizeComposerReferences(references);
  if (!items.length) return '';
  return [
    '[EcoreX selected file references]',
    'The user selected the following generated/local file locations inside the desktop preview. Treat them as precise edit targets for the next answer.',
    ...items.flatMap((item, index) => [
      `Reference ${index + 1}: ${item.name}`,
      `- location: ${item.location}`,
      item.text ? `- selected text:\n${item.text}` : ''
    ]).filter(Boolean),
    '[/EcoreX selected file references]'
  ].join('\n');
}

function sanitizeContextSummary(value = '') {
  return String(value || '').replace(/\s+\n/g, '\n').trim().slice(0, CONTEXT_SUMMARY_MAX_CHARS);
}

function conversationContextSection(messages = [], currentPrompt = '', contextSummary = '') {
  const summary = sanitizeContextSummary(contextSummary);
  const recentLimit = summary ? CONTEXT_COMPACT_RECENT_LIMIT : CONTEXT_RECENT_MESSAGE_LIMIT;
  const candidates = (Array.isArray(messages) ? messages : [])
    .filter((message) => ['user', 'assistant'].includes(message.role) && readableMessageText(message))
    .slice(-recentLimit);
  const recent = candidates.map((message, index) => {
    const role = message.role === 'user' ? '用户' : 'EcoreX';
    const text = readableMessageText(message).slice(0, CONTEXT_RECENT_MESSAGE_CHARS);
    return `${index + 1}. ${role}：${text}`;
  });
  if (!recent.length && !summary) return '';
  const lastUser = [...candidates].reverse().find((message) => message.role === 'user');
  const lastAssistant = [...candidates].reverse().find((message) => message.role === 'assistant');
  const continuationHint = isContinuationPrompt(currentPrompt)
    ? [
        '',
        '当前用户输入是短确认或继续指令，必须承接上一轮 EcoreX 的问题、计划或待确认动作继续推进；不要要求用户重复完整任务。',
        lastUser ? `上一条用户任务：${readableMessageText(lastUser).slice(0, CONTEXT_RECENT_MESSAGE_CHARS)}` : '',
        lastAssistant ? `上一条 EcoreX 回复：${readableMessageText(lastAssistant).slice(0, CONTEXT_RECENT_MESSAGE_CHARS)}` : ''
      ].filter(Boolean)
    : [];
  return [
    summary ? '当前会话已做上下文压缩。以下摘要承接 CLI microcompact/autocompact 或前端长会话压缩结果，优先用于理解长期上下文：' : '',
    summary,
    summary && recent.length ? '最近未压缩对话片段：' : '',
    '当前会话上下文摘要如下，仅用于理解代词、省略表达、短确认和继续上一轮任务，不要逐字复述：',
    ...recent,
    ...continuationHint
  ].filter(Boolean).join('\n');
}

function eventContextSummary(event = {}) {
  const candidates = [
    event.contextSummary,
    event.summary,
    event.compactSummary,
    event.contextManagement?.summary,
    event.contextManagement?.compact_summary,
    event.contextManagement?.message,
    event.context_management?.summary,
    event.raw?.context_management?.summary,
    event.raw?.context_management?.compact_summary,
    event.raw?.context_management?.message
  ];
  return sanitizeContextSummary(candidates.find((value) => String(value || '').trim()) || '');
}

function isContextCompactEvent(event = {}) {
  if (!event) return false;
  if (event.contextManagement || event.context_management || event.raw?.context_management) return true;
  const text = `${event.kind || ''} ${event.status || ''} ${event.text || ''}`;
  return /(compact|microcompact|autocompact|context[_ -]?management|上下文压缩|压缩上下文)/i.test(text);
}

function buildConversationContextSummary(messages = [], previousSummary = '', reason = 'front-auto', compactEvent = null) {
  const eventSummary = eventContextSummary(compactEvent || {});
  if (eventSummary) {
    return sanitizeContextSummary([
      `压缩来源：${reason}`,
      eventSummary
    ].join('\n'));
  }

  const useful = (Array.isArray(messages) ? messages : [])
    .filter((message) => ['user', 'assistant'].includes(message.role) && readableMessageText(message))
    .slice(-CONTEXT_COMPACT_SOURCE_MESSAGES);
  if (!useful.length) return sanitizeContextSummary(previousSummary);

  const previous = sanitizeContextSummary(previousSummary);
  const lines = useful.map((message, index) => {
    const role = message.role === 'user' ? '用户' : 'EcoreX';
    const text = readableMessageText(message).slice(0, 520);
    return `${index + 1}. ${role}：${text}`;
  });
  return sanitizeContextSummary([
    previous ? `上一版压缩摘要：\n${previous}` : '',
    `压缩来源：${reason}`,
    '关键历史轮次：',
    ...lines
  ].filter(Boolean).join('\n'));
}

function shouldRotateNativeClaudeSession(messages = [], lastRotateCount = 0) {
  const count = (Array.isArray(messages) ? messages : []).filter((message) =>
    ['user', 'assistant'].includes(message.role) && readableMessageText(message)
  ).length;
  return (
    count >= NATIVE_SESSION_ROTATE_TRIGGER_MESSAGES &&
    count - Number(lastRotateCount || 0) >= NATIVE_SESSION_ROTATE_STEP_MESSAGES
  );
}

function realtimePlatformSearchPolicySection(prompt = '') {
  const text = String(prompt || '');
  const platformPattern = /(小红书|抖音|微博|快手|B站|哔哩哔哩|社媒|社交平台|热榜|热门话题|趋势)/i;
  const freshPattern = /(近\s*\d+\s*天|近三天|近两天|今天|昨日|昨天|最新|实时|热门|热榜|趋势|话题)/i;
  if (!platformPattern.test(text) || !freshPattern.test(text)) return '';
  return [
    '实时平台趋势检索边界：',
    '1. 优先使用 1 次最聚焦的 WebSearch，必要时最多追加 1 次校验搜索；不要反复扩大到过多第三方榜单或历史报告。',
    '2. 如果公开网页搜索超过约 60 秒、或无法稳定获取站内实时热榜，立即基于已拿到的公开来源给出“公开资料推断”，并明确说明可信度与限制。',
    '3. 优先输出可行动结论、话题方向、内容建议和来源链接，不要为了追求完整站内榜单长时间等待。'
  ].join('\n');
}

function agentRunPolicySection(prompt = '') {
  return [
    '执行策略：',
    '1. 联网搜索、网页读取、MCP 调用、SKILLS 调用、只读信息检索和常规分析工具由 EcoreX 自主判断并直接执行，不要先询问用户是否允许。',
    '2. 涉及本地文件写入/修改/删除、命令执行、系统目录访问或不可逆变更时，触发 EcoreX 桌面端权限确认弹框，让用户点击按钮确认；不要要求用户在聊天里回复“继续”或“确认”。',
    '3. 简单问答只返回答案；复杂任务只展示当前动作、关键计划和必要风险，不输出完整调试日志或冗长状态树。',
    realtimePlatformSearchPolicySection(prompt)
  ].join('\n');
}

function recentChatTimeLabel(date = new Date()) {
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function createLocalId(prefix = 'id') {
  return window.crypto?.randomUUID?.() || `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizeRecentChatItem(item, index = 0) {
  const fromTuple = Array.isArray(item);
  const title = String(fromTuple ? item[0] : item?.title || '').trim();
  if (!title) return null;
  const projectId = String(fromTuple ? '' : item?.projectId || '').trim().slice(0, 120);
  const projectName = String(fromTuple ? '' : item?.projectName || '').trim().slice(0, 120);
  const ownerEmail = String(fromTuple ? '' : item?.ownerEmail || item?.accountEmail || '').trim().toLowerCase().slice(0, 160);
  return {
    id: String((fromTuple ? '' : item?.id) || `recent-${index}-${title}`).slice(0, 120),
    claudeSessionId: String((fromTuple ? '' : item?.claudeSessionId || item?.sessionId) || item?.id || '').slice(0, 120),
    title: title.slice(0, 80),
    time: String((fromTuple ? item[1] : item?.time) || '').trim() || recentChatTimeLabel(),
    updatedAt: Number(fromTuple ? 0 : item?.updatedAt) || Date.now() - index,
    projectId,
    projectName,
    ownerEmail
  };
}

function loadRawRecentChatItems() {
  try {
    const stored = localStorage.getItem(RECENT_CHAT_STORAGE_KEY);
    if (stored === null) return [];
    const parsed = JSON.parse(stored || '[]');
    return Array.isArray(parsed) ? parsed.map(normalizeRecentChatItem).filter(Boolean) : [];
  } catch {
    // Keep an empty production state instead of restoring design samples.
  }
  return [];
}

function loadRecentChatItems() {
  const owner = currentConversationStorageOwner();
  const conversations = loadRawConversationMap();
  return loadRawRecentChatItems()
    .filter((item) => !owner || !item.ownerEmail || item.ownerEmail === owner)
    .map((item) => {
      const conversation = conversations[item.id];
      if (!conversation || typeof conversation !== 'object') return item;
      if (owner && conversation.ownerEmail && String(conversation.ownerEmail).toLowerCase() !== owner) return item;
      const projectId = String(item.projectId || conversation.projectId || '').trim().slice(0, 120);
      const projectName = String(item.projectName || conversation.projectName || '').trim().slice(0, 120);
      return normalizeRecentChatItem({ ...item, projectId, projectName });
    })
    .filter(Boolean)
    .slice(0, MAX_RECENT_CHATS);
}

function storeRecentChatItems(items = []) {
  try {
    const owner = currentConversationStorageOwner();
    const retainedOtherOwnerItems = owner
      ? loadRawRecentChatItems().filter((item) => item.ownerEmail && item.ownerEmail !== owner)
      : [];
    const normalized = [...retainedOtherOwnerItems, ...items]
      .map((item, index) => normalizeRecentChatItem({
        ...item,
        ownerEmail: item?.ownerEmail || owner
      }, index))
      .filter(Boolean)
      .sort((left, right) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0))
      .slice(0, MAX_RECENT_CHATS);
    localStorage.setItem(RECENT_CHAT_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Recent chat sync is a UI convenience and should not break the agent.
  }
}

function updateStoredRecentChatItem(id, patch = {}) {
  const safeId = String(id || '').trim();
  if (!safeId) return [];
  const items = loadRecentChatItems().map((item) => (
    item.id === safeId ? normalizeRecentChatItem({ ...item, ...patch, id: safeId }) : item
  )).filter(Boolean);
  storeRecentChatItems(items);
  return items;
}

function deleteStoredRecentChatItem(id) {
  const safeId = String(id || '').trim();
  if (!safeId) return [];
  const items = loadRecentChatItems().filter((item) => item.id !== safeId);
  storeRecentChatItems(items);
  deleteConversationState(safeId);
  return items;
}

function updateStoredProjectChatReferences(projectId, projectName) {
  const safeProjectId = String(projectId || '').trim();
  if (!safeProjectId) return;
  const safeProjectName = String(projectName || '').trim().slice(0, 120);
  const items = loadRecentChatItems().map((item) => (
    item.projectId === safeProjectId ? { ...item, projectName: safeProjectName } : item
  ));
  storeRecentChatItems(items);
  const conversations = loadConversationMap();
  let changed = false;
  for (const [id, conversation] of Object.entries(conversations)) {
    if (conversation?.projectId !== safeProjectId) continue;
    conversations[id] = { ...conversation, projectName: safeProjectName };
    changed = true;
  }
  if (changed) storeConversationMap(conversations);
  window.dispatchEvent?.(new CustomEvent('ecorex:recent-chats-changed'));
}

function deleteStoredProjectChatReferences(projectId) {
  const safeProjectId = String(projectId || '').trim();
  if (!safeProjectId) return;
  const removedIds = new Set(loadRecentChatItems().filter((item) => item.projectId === safeProjectId).map((item) => item.id));
  storeRecentChatItems(loadRecentChatItems().filter((item) => item.projectId !== safeProjectId));
  const conversations = loadConversationMap();
  for (const id of Object.keys(conversations)) {
    if (removedIds.has(id) || conversations[id]?.projectId === safeProjectId) delete conversations[id];
  }
  storeConversationMap(conversations);
  window.dispatchEvent?.(new CustomEvent('ecorex:recent-chats-changed'));
}

function upsertRecentChatItem(items = [], item = {}) {
  const normalized = normalizeRecentChatItem({
    ...item,
    updatedAt: item.updatedAt || Date.now()
  });
  if (!normalized) return items;
  const deduped = items.filter((entry) => (
    entry.id !== normalized.id
    && !(entry.title === normalized.title && (entry.projectId || '') === (normalized.projectId || ''))
  ));
  return [normalized, ...deduped].slice(0, MAX_RECENT_CHATS);
}

function loadRawConversationMap() {
  try {
    const parsed = JSON.parse(localStorage.getItem(CONVERSATION_STORAGE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return parsed;
  } catch {
    return {};
  }
}

function loadConversationMap() {
  try {
    const parsed = loadRawConversationMap();
    const owner = currentConversationStorageOwner();
    if (!owner) return parsed;
    return Object.fromEntries(Object.entries(parsed).filter(([, value]) => (
      !value?.ownerEmail || String(value.ownerEmail).toLowerCase() === owner
    )));
  } catch {
    return {};
  }
}

function storeConversationMap(map = {}) {
  try {
    const owner = currentConversationStorageOwner();
    const retainedOtherOwnerEntries = owner
      ? Object.entries(loadRawConversationMap()).filter(([, value]) => (
          value?.ownerEmail && String(value.ownerEmail).toLowerCase() !== owner
        ))
      : [];
    const entries = [...retainedOtherOwnerEntries, ...Object.entries(map)]
      .filter(([, value]) => value && typeof value === 'object')
      .sort(([, left], [, right]) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0))
      .slice(0, MAX_STORED_CONVERSATIONS);
    while (entries.length > 1) {
      const serialized = JSON.stringify(Object.fromEntries(entries));
      if (serialized.length <= MAX_CONVERSATION_STORAGE_CHARS) {
        localStorage.setItem(CONVERSATION_STORAGE_KEY, serialized);
        return;
      }
      entries.pop();
    }
    localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch {
    // Conversation persistence should never block chat interaction.
  }
}

function sanitizeStoredTimeline(timeline = []) {
  return (Array.isArray(timeline) ? timeline : [])
    .filter((item) => Array.isArray(item))
    .slice(-80)
    .map((item) => item.slice(0, 5).map((value) => String(value || '').slice(0, 240)));
}

function sanitizeStoredMessages(messages = [], scope = {}) {
  const conversationProjectId = String(scope.projectId || '').trim();
  const conversationProjectName = String(scope.projectName || '').trim().slice(0, 120);
  return (Array.isArray(messages) ? messages : [])
    .slice(-MAX_STORED_MESSAGES_PER_CONVERSATION)
    .map((message) => {
      const messageProjectId = String(message.projectId || conversationProjectId || '').slice(0, 120);
      const messageProjectName = String(message.projectName || conversationProjectName || '').slice(0, 120);
      const scopedAttachments = filterAttachmentsForProjectScope(message.attachments || [], conversationProjectId);
      const wasStreaming = Boolean(message.streaming);
      return {
        id: String(message.id || createLocalId('message')).slice(0, 120),
        role: message.role === 'user' ? 'user' : 'assistant',
        text: String(message.text || '').slice(0, 12000),
        time: String(message.time || '').slice(0, 16),
        status: wasStreaming ? 'interrupted' : String(message.status || '').slice(0, 40),
        error: Boolean(message.error),
        streaming: false,
        sessionId: message.sessionId ? String(message.sessionId).slice(0, 120) : undefined,
        claudeSessionId: message.claudeSessionId ? String(message.claudeSessionId).slice(0, 120) : undefined,
        projectId: messageProjectId || undefined,
        projectName: messageProjectName || undefined,
        originalPrompt: message.originalPrompt ? String(message.originalPrompt).slice(0, 12000) : undefined,
        permissionDecision: message.permissionDecision && typeof message.permissionDecision === 'object'
          ? {
              action: String(message.permissionDecision.action || '').slice(0, 40),
              label: String(message.permissionDecision.label || '').slice(0, 80),
              status: String(message.permissionDecision.status || '').slice(0, 40),
              at: Number(message.permissionDecision.at) || null
            }
          : undefined,
        timeline: sanitizeStoredTimeline(message.timeline || []),
        ledger: Array.isArray(message.ledger)
          ? message.ledger.slice(-MAX_STORED_LEDGER_ITEMS).map((item, index) => normalizeToolLedgerItem(item, {}, index))
          : [],
        attachmentIngest: Array.isArray(message.attachmentIngest)
          ? message.attachmentIngest.slice(-MAX_STORED_INGEST_ITEMS).map((item, index) => sanitizeAttachmentIngestItem(item, index))
          : [],
        finalArtifacts: Array.isArray(message.finalArtifacts)
          ? mergeArtifactReferences(message.finalArtifacts).map((artifact) => ({
              ...artifact,
              id: String(artifact.id || artifact.path || artifact.name || '').slice(0, 220),
              path: String(artifact.path || '').slice(0, 1000),
              name: String(artifact.name || fileNameFromArtifactPath(artifact.path || '') || 'artifact').slice(0, 240),
              ext: String(artifact.ext || artifactExtension(artifact.path || artifact.name || '')).slice(0, 20),
              source: String(artifact.source || 'assistant-final').slice(0, 40),
              projectId: artifact.projectId ? String(artifact.projectId).slice(0, 120) : undefined
            }))
          : [],
        recovery: message.recovery && typeof message.recovery === 'object'
          ? {
              state: String(message.recovery.state || '').slice(0, 40),
              label: String(message.recovery.label || '').slice(0, 80),
              tone: String(message.recovery.tone || '').slice(0, 40),
              sessionId: String(message.recovery.sessionId || '').slice(0, 120),
              prompt: String(message.recovery.prompt || '').slice(0, 240),
              detail: String(message.recovery.detail || '').slice(0, 360)
            }
          : wasStreaming
            ? {
                state: 'recoverable',
                label: '可恢复',
                tone: 'running',
                sessionId: message.sessionId ? String(message.sessionId).slice(0, 120) : '',
                prompt: String(message.originalPrompt || '').slice(0, 240),
                detail: '上次关闭或重启时任务被中断，可在原会话继续或重试；已生成的产物会保留在原项目/会话内。'
              }
          : undefined,
        attachments: scopedAttachments.length
          ? scopedAttachments.slice(0, MAX_COMPOSER_ATTACHMENTS).map((attachment, index) => sanitizeStoredAttachment(attachment, index))
          : [],
        references: Array.isArray(message.references)
          ? sanitizeComposerReferences(message.references)
          : [],
        hiddenArtifactIds: Array.isArray(message.hiddenArtifactIds)
          ? message.hiddenArtifactIds.map((id) => String(id).slice(0, 220)).slice(0, ARTIFACT_PREVIEW_MAX_ITEMS)
          : []
      };
    });
}

function sanitizeLiveMessages(messages = []) {
  return (Array.isArray(messages) ? messages : [])
    .slice(-MAX_LIVE_MESSAGES_PER_CONVERSATION)
    .map((message) => {
      if (!message || typeof message !== 'object') return message;
      const maxText = message.role === 'assistant'
        ? MAX_LIVE_ASSISTANT_TEXT_CHARS
        : MAX_COMPOSER_PROMPT_CHARS;
      return {
        ...message,
        text: clampLongText(message.text || '', maxText),
        originalPrompt: message.originalPrompt ? clampLongText(message.originalPrompt, MAX_COMPOSER_PROMPT_CHARS) : message.originalPrompt,
        ledger: Array.isArray(message.ledger) ? message.ledger.slice(-MAX_STORED_LEDGER_ITEMS) : message.ledger
      };
    });
}

function saveConversationState(id, patch = {}) {
  if (!id) return;
  const map = loadConversationMap();
  const previous = map[id] || {};
  const projectId = String(Object.prototype.hasOwnProperty.call(patch, 'projectId') ? patch.projectId || '' : previous.projectId || '').slice(0, 120);
  const projectName = String(Object.prototype.hasOwnProperty.call(patch, 'projectName') ? patch.projectName || '' : previous.projectName || '').slice(0, 120);
  map[id] = {
    ...previous,
    ...patch,
    id,
    claudeSessionId: String(patch.claudeSessionId || previous.claudeSessionId || id).slice(0, 120),
    projectId,
    projectName,
    ownerEmail: String(patch.ownerEmail || previous.ownerEmail || currentConversationStorageOwner() || '').slice(0, 160),
    contextSummary: sanitizeContextSummary(patch.contextSummary || previous.contextSummary || ''),
    contextCompactedAt: patch.contextCompactedAt || previous.contextCompactedAt || null,
    messages: sanitizeStoredMessages(patch.messages || previous.messages || [], { projectId, projectName }),
    timeline: sanitizeStoredTimeline(patch.timeline || previous.timeline || []),
    updatedAt: patch.updatedAt || Date.now()
  };
  storeConversationMap(map);
}

function loadConversationState(id) {
  if (!id) return null;
  const item = loadConversationMap()[id];
  if (!item || typeof item !== 'object') return null;
  const messages = sanitizeStoredMessages(item.messages || [], { projectId: item.projectId || '', projectName: item.projectName || '' });
  return {
    ...item,
    contextSummary: sanitizeContextSummary(item.contextSummary || ''),
    contextCompactedAt: item.contextCompactedAt || null,
    messages,
    timeline: sanitizeStoredTimeline(item.timeline || [])
  };
}

function deleteConversationState(id) {
  if (!id) return;
  const map = loadConversationMap();
  delete map[id];
  storeConversationMap(map);
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
  const [startupState, setStartupState] = useState(() => window.__ecorexStartupState || {
    status: window.ecorex ? 'loading' : 'skipped',
    label: window.ecorex ? '预加载中' : '预览模式',
    detail: window.ecorex ? '正在预加载本地能力' : '桌面预加载不可用'
  });
  const startupReadyRef = useRef(false);

  useEffect(() => {
    document.documentElement.dataset.theme = 'dark';
    localStorage.setItem('ecorex-theme', 'dark');
  }, []);

  useEffect(() => {
    const listener = () => {
      refreshAuthStatus();
    };
    window.addEventListener?.('ecorex:auth-updated', listener);
    return () => window.removeEventListener?.('ecorex:auth-updated', listener);
  }, []);

  useEffect(() => {
    if (startupReadyRef.current) return;
    startupReadyRef.current = true;
    setStartupState({
      status: window.ecorex ? 'loading' : 'skipped',
      label: window.ecorex ? '预加载中' : '预览模式',
      detail: window.ecorex ? '正在预加载本地能力' : '桌面预加载不可用'
    });
    const startupWork = refreshAuthStatus()
      .then(() => Promise.allSettled([
        refreshBackend(),
        preloadStartupState()
      ]));
    withStartupTimeout(startupWork).finally(() => {
      window.__ecorexFinishStartup?.();
    })
      .then((result) => {
        const nextState = result?.timedOut
          ? {
              status: 'timeout',
              label: '已进入',
              detail: '预加载仍在后台继续',
              timedOut: true,
              timeoutMs: STARTUP_FRONTEND_TIMEOUT_MS
            }
          : (Array.isArray(result) ? result.find((item) => item.status === 'fulfilled' && item.value?.status)?.value : null) || window.__ecorexStartupState || {
              status: 'ready',
              label: '已完成',
              detail: '启动检查已完成'
            };
        window.__ecorexStartupState = nextState;
        setStartupState(nextState);
      })
      .catch(() => {
        const failedState = {
          status: 'failed',
          label: '未完成',
          detail: '启动检查未完成'
        };
        window.__ecorexStartupState = failedState;
        setStartupState(failedState);
      });
  }, []);

  async function refreshAuthStatus() {
    if (!window.ecorex?.getAuthStatus) {
      const previewStatus = normalizeAuthStatus(null, localStorage.getItem(PREVIEW_SESSION_KEY) === '1');
      setConversationStorageOwner(previewStatus);
      setAuthStatus(previewStatus);
      setLoggedIn(previewStatus.loggedIn);
      return previewStatus;
    }

    try {
      const status = normalizeAuthStatus(await window.ecorex.getAuthStatus());
      setConversationStorageOwner(status);
      setAuthStatus(status);
      setLoggedIn(status.loggedIn);
      if (status.loggedIn) setAuthNotice('');
      return status;
    } catch (error) {
      const status = normalizeAuthStatus({ ok: false, error: error?.message || 'Auth status failed' });
      setConversationStorageOwner(status);
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
        setConversationStorageOwner(nextAuth);
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
    setConversationStorageOwner({ loggedIn: false });
    setAuthStatus((current) => normalizeAuthStatus({ ...current, loggedIn: false, ok: false, error: 'Unauthorized' }));
    setLoggedIn(false);
    setAuthNotice('登录状态已过期，请重新登录。');
  }

  async function handleLogin(credentials = {}) {
    setAuthNotice('');
    if (!window.ecorex?.authLogin) {
      localStorage.setItem(PREVIEW_SESSION_KEY, '1');
      const previewStatus = normalizeAuthStatus({ loggedIn: true, user: { email: credentials.email || 'preview@ecorex.local' } }, true);
      setConversationStorageOwner(previewStatus);
      setAuthStatus(previewStatus);
      setLoggedIn(true);
      refreshBackend();
      return;
    }

    try {
      const result = await window.ecorex.authLogin(credentials);
      if (result?.ok === false) throw new Error(result.error || '登录失败');
      const status = normalizeAuthStatus(result?.auth || result?.status || result, true);
      setConversationStorageOwner(status);
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
    const loggedOutStatus = normalizeAuthStatus({ loggedIn: false });
    setConversationStorageOwner(loggedOutStatus);
    setAuthStatus(loggedOutStatus);
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
        startupState={startupState}
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
        <div className="mac-window-controls" aria-label="窗口控制">
          <button className="close" type="button" aria-label="关闭窗口" title="关闭" onClick={() => control('close')} />
          <button className="minimize" type="button" aria-label="最小化窗口" title="最小化" onClick={() => control('minimize')} />
          <button className="maximize" type="button" aria-label="最大化窗口" title="最大化" onClick={() => control('maximize')} />
        </div>
      )}
      <div className="titlebar-title">EcoreX 亦芯</div>
      {!isMac && (
        <div className="win-window-controls" aria-label="窗口控制">
          <button type="button" aria-label="最小化窗口" title="最小化" onClick={() => control('minimize')}>
            <span className="win-control-glyph minimize">－</span>
          </button>
          <button type="button" aria-label="最大化窗口" title="最大化" onClick={() => control('maximize')}>
            <span className="win-control-glyph maximize">□</span>
          </button>
          <button className="close" type="button" aria-label="关闭窗口" title="关闭" onClick={() => control('close')}>
            <span className="win-control-glyph close">×</span>
          </button>
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
            autoFocus
            data-testid="login-email-input"
            onChange={(event) => setEmail(event.target.value)}
            placeholder="请输入企业邮箱"
            tabIndex={0}
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
            tabIndex={0}
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

function systemSettingsTabFromPage(page) {
  if (page === 'mcp') return 'mcp';
  if (page === 'skills') return 'skills';
  if (page === 'users') return 'users';
  if (page === 'evaluations') return 'evaluations';
  if (page === 'diagnostics' || page === 'settings') return 'diagnostics';
  return '';
}

function pageFromSystemSettingsTab(tab) {
  if (tab === 'mcp') return 'mcp';
  if (tab === 'skills') return 'skills';
  if (tab === 'users') return 'users';
  if (tab === 'evaluations') return 'evaluations';
  return 'settings';
}

function MainShell({
  page,
  setPage,
  backendStatus,
  backendError,
  capabilities,
  authStatus,
  startupState,
  refreshBackend,
  onUnauthorized,
  logout
}) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const systemSettingsTab = systemSettingsTabFromPage(page);
  const workspacePage = systemSettingsTab ? 'system-settings' : page;

  return (
    <div className={`app-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`} data-testid="app-shell">
      <Sidebar
        page={page}
        setPage={setPage}
        logout={logout}
        authStatus={authStatus}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
      />
      <main className={`workspace workspace-${workspacePage}`} data-testid="workspace">
        <div className="workspace-view" hidden={page !== 'chat'}>
          <ChatView
            backendStatus={backendStatus}
            backendError={backendError}
            capabilities={capabilities}
            authStatus={authStatus}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
            setPage={setPage}
          />
        </div>
        {systemSettingsTab && (
          <SystemSettingsView
            activeTab={systemSettingsTab}
            onTabChange={(tab) => setPage(pageFromSystemSettingsTab(tab))}
            onBack={() => setPage('chat')}
            backendStatus={backendStatus}
            backendError={backendError}
            capabilities={capabilities}
            authStatus={authStatus}
            startupState={startupState}
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
            onBack={() => setPage('chat')}
          />
        )}
      </main>
    </div>
  );
}

function Sidebar({ page, setPage, logout, authStatus, collapsed = false, onToggleCollapsed }) {
  const [profileOpen, setProfileOpen] = useState(false);
  const [modelConfigOpen, setModelConfigOpen] = useState(false);
  const [currentModelLabel, setCurrentModelLabel] = useState('默认模型');
  const [recentItems, setRecentItems] = useState(loadRecentChatItems);
  const [activeConversationId, setActiveConversationId] = useState(() => loadRecentChatItems()[0]?.id || '');
  const [chatSearch, setChatSearch] = useState('');
  const [projectState, setProjectState] = useState({ apiReady: false, loading: false, projects: [], currentProject: null, status: '项目服务未就绪', notice: '' });
  const [quickProjectName, setQuickProjectName] = useState('');
  const [projectBusy, setProjectBusy] = useState('');
  const [renamingProjectId, setRenamingProjectId] = useState('');
  const [renamingProjectName, setRenamingProjectName] = useState('');
  const [renamingProjectSessionId, setRenamingProjectSessionId] = useState('');
  const [renamingProjectSessionTitle, setRenamingProjectSessionTitle] = useState('');
  const profileRef = useRef(null);
  const currentAuthUser = authUserFromStatus(authStatus);
  const currentDisplayName = displayUserNameFromAuth(authStatus);
  const currentEmail = displayUserEmailFromAuth(authStatus) || 'local@ecorex.com';
  const currentRoleLabel = roleLabelFromAuthUser(currentAuthUser);
  const currentInitials = avatarInitialsFromUser(currentAuthUser, currentDisplayName);

  useEffect(() => {
    let cancelled = false;
    const refreshCurrentModel = () => loadModelProfiles(DEFAULT_AGENT_MODEL_NAME).then((result) => {
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

  async function refreshSidebarProjects({ silent = false } = {}) {
    if (!silent) setProjectState((current) => ({ ...current, loading: true, notice: '' }));
    const result = await loadProjectState();
    if (result.unauthorized) {
      setProjectState((current) => ({ ...current, loading: false, apiReady: true, notice: '请重新登录后查看项目。' }));
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

  useEffect(() => {
    refreshSidebarProjects({ silent: true });
    const listener = () => refreshSidebarProjects({ silent: true });
    window.addEventListener?.('ecorex:projects-changed', listener);
    return () => window.removeEventListener?.('ecorex:projects-changed', listener);
  }, []);

  function startNewChat(project = null) {
    const id = createLocalId('conversation');
    const projectId = project?.id || '';
    const projectName = project?.name || '';
    const item = {
      id,
      claudeSessionId: id,
      title: '新会话',
      time: recentChatTimeLabel(),
      updatedAt: Date.now(),
      projectId,
      projectName
    };
    setRecentItems((items) => {
      const nextItems = upsertRecentChatItem(items, item);
      storeRecentChatItems(nextItems);
      return nextItems;
    });
    setActiveConversationId(id);
    setPage('chat');
    window.dispatchEvent?.(new CustomEvent('ecorex:new-chat', { detail: item }));
  }

  async function switchProjectForChat(project = {}) {
    if (!project?.id || !projectState.apiReady) return false;
    setProjectBusy(project.id);
    const result = await switchManagedProject(project.id);
    setProjectBusy('');
    if (result?.ok === false || result?.unauthorized) {
      setProjectState((current) => ({ ...current, notice: result?.unauthorized ? '请重新登录后切换项目。' : `项目切换失败：${sanitizeDisplayText(result?.error, '请稍后重试')}` }));
      return false;
    }
    window.dispatchEvent?.(new CustomEvent('ecorex:projects-changed'));
    window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project } }));
    return true;
  }

  async function openRecentChat(item = {}) {
    if (!item?.id) return;
    setActiveConversationId(item.id);
    if (item.projectId) {
      const project = projectState.projects.find((row) => row.id === item.projectId) || { id: item.projectId, name: item.projectName || '项目会话' };
      await switchProjectForChat(project);
    }
    setPage('chat');
    window.dispatchEvent?.(new CustomEvent('ecorex:open-chat', {
      detail: {
        id: item.id,
        claudeSessionId: item.claudeSessionId,
        title: item.title,
        projectId: item.projectId || '',
        projectName: item.projectName || ''
      }
    }));
  }

  async function openProject(project = {}) {
    if (!project?.id || project.id === 'empty') return;
    const ok = await switchProjectForChat(project);
    if (!ok) return;
    const latestSession = recentItems
      .filter((item) => item.projectId === project.id)
      .sort((left, right) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0))[0];
    if (latestSession) {
      await openRecentChat(latestSession);
    } else {
      startNewChat(project);
    }
  }

  async function createQuickProject(event) {
    event?.preventDefault?.();
    const name = quickProjectName.trim() || `新项目 ${new Date().toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    }).replace(/[/:]/g, '-')}`;
    if (!name || !hasEcorexFunction(['createProject', 'projects.create'])) return;
    setProjectBusy('create');
    const result = await createManagedProject({ name });
    setProjectBusy('');
    if (result?.ok === false || result?.unauthorized) {
      setProjectState((current) => ({ ...current, notice: result?.unauthorized ? '请重新登录后创建项目。' : `项目创建失败：${sanitizeDisplayText(result?.error, '请稍后重试')}` }));
      return;
    }
    const created = normalizeProjectItem(result.project || result.currentProject || { name }, 0, result.project?.id);
    setQuickProjectName('');
    await refreshSidebarProjects({ silent: true });
    window.dispatchEvent?.(new CustomEvent('ecorex:projects-changed'));
    startNewChat(created);
  }

  async function revealProjectFolder(project = {}) {
    if (!project?.id) return;
    setProjectBusy(`open:${project.id}`);
    const result = await openManagedProjectFolder(project.id);
    setProjectBusy('');
    if (result?.ok === false || result?.unauthorized || result?.missing) {
      setProjectState((current) => ({ ...current, notice: result?.unauthorized ? '请重新登录后打开项目目录。' : `目录打开失败：${sanitizeDisplayText(result?.error, '请稍后重试')}` }));
    }
  }

  function startProjectRename(project = {}) {
    if (!project?.id || project.id === 'empty') return;
    setRenamingProjectId(project.id);
    setRenamingProjectName(project.name || '');
  }

  function cancelProjectRename() {
    setRenamingProjectId('');
    setRenamingProjectName('');
  }

  async function saveProjectRename(project = {}) {
    if (!project?.id || project.id === 'empty' || !hasEcorexFunction(['updateProject', 'projects.update'])) return;
    const cleanName = sanitizeDisplayText(renamingProjectName, '').trim();
    const currentName = sanitizeDisplayText(project.name, '').trim();
    if (!cleanName) return;
    if (cleanName === currentName) {
      cancelProjectRename();
      return;
    }
    setProjectBusy(`rename:${project.id}`);
    const result = await updateManagedProject(project.id, { name: cleanName });
    setProjectBusy('');
    if (result?.ok === false || result?.unauthorized) {
      setProjectState((current) => ({
        ...current,
        notice: result?.unauthorized
          ? '请重新登录后重命名项目。'
          : `项目重命名失败：${sanitizeDisplayText(result?.error, '请稍后重试')}`
      }));
      return;
    }
    const updatedProject = normalizeProjectItem(result.project || { ...project, name: cleanName }, 0, project.id);
    updateStoredProjectChatReferences(project.id, updatedProject.name);
    cancelProjectRename();
    setProjectState((current) => ({ ...current, notice: `项目已重命名为「${updatedProject.name}」。` }));
    await refreshSidebarProjects({ silent: true });
    window.dispatchEvent?.(new CustomEvent('ecorex:projects-changed'));
    if (project.current) {
      window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project: updatedProject } }));
    }
  }

  function openBlankChatAfterDelete() {
    const id = createLocalId('conversation');
    setActiveConversationId(id);
    setPage('chat');
    window.dispatchEvent?.(new CustomEvent('ecorex:new-chat', {
      detail: {
        id,
        claudeSessionId: id,
        title: '新会话',
        updatedAt: Date.now(),
        emptyAfterDelete: true
      }
    }));
  }

  useEffect(() => {
    if (!profileOpen) return undefined;
    const closeOnOutside = (event) => {
      if (profileRef.current && !profileRef.current.contains(event.target)) {
        setProfileOpen(false);
      }
    };
    document.addEventListener('pointerdown', closeOnOutside);
    return () => document.removeEventListener('pointerdown', closeOnOutside);
  }, [profileOpen]);

  useEffect(() => {
    const upsert = (event) => {
      if (event.detail?.id) setActiveConversationId(event.detail.id);
      setRecentItems((items) => {
        const nextItems = upsertRecentChatItem(items, event.detail || {});
        storeRecentChatItems(nextItems);
        return nextItems;
      });
    };
    const reload = () => setRecentItems(loadRecentChatItems());
    window.addEventListener?.('ecorex:recent-chat-upsert', upsert);
    window.addEventListener?.('ecorex:recent-chats-changed', reload);
    return () => {
      window.removeEventListener?.('ecorex:recent-chat-upsert', upsert);
      window.removeEventListener?.('ecorex:recent-chats-changed', reload);
    };
  }, []);

  function renameProjectSession(session = {}) {
    if (!session?.id) return;
    setRenamingProjectSessionId(session.id);
    setRenamingProjectSessionTitle(session.title || '项目会话');
  }

  function saveProjectSessionRename(session = {}) {
    if (!session?.id) return;
    const currentTitle = session.title || '项目会话';
    const cleanTitle = sanitizeDisplayText(renamingProjectSessionTitle, '').trim();
    setRenamingProjectSessionId('');
    setRenamingProjectSessionTitle('');
    if (!cleanTitle || cleanTitle === currentTitle) return;
    setRecentItems(() => updateStoredRecentChatItem(session.id, {
      title: cleanTitle,
      updatedAt: Date.now()
    }));
    saveConversationState(session.id, {
      title: cleanTitle,
      updatedAt: Date.now()
    });
  }

  function deleteProjectSession(session = {}, project = {}) {
    if (!session?.id) return;
    setRecentItems(() => deleteStoredRecentChatItem(session.id));
    if (session.id !== activeConversationId) return;
    window.setTimeout(() => {
      const nextSession = loadRecentChatItems()
        .filter((item) => item.projectId === project.id)
        .sort((left, right) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0))[0];
      if (nextSession) openRecentChat(nextSession);
      else if (project?.id && project.id !== 'empty') startNewChat(project);
      else openBlankChatAfterDelete();
    }, 0);
  }

  const projectConversationMap = useMemo(() => {
    const map = new Map();
    for (const item of recentItems) {
      if (!item.projectId) continue;
      const list = map.get(item.projectId) || [];
      list.push(item);
      map.set(item.projectId, list);
    }
    for (const [projectId, items] of map.entries()) {
      map.set(projectId, items.sort((left, right) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0)));
    }
    return map;
  }, [recentItems]);

  const historyItems = useMemo(() => recentItems.filter((item) => !item.projectId), [recentItems]);
  const sidebarSearchQuery = chatSearch.trim().toLowerCase();
  const matchesSidebarSearch = (record = {}) => {
    if (!sidebarSearchQuery) return true;
    return [record.title, record.name, record.time, record.projectName, record.status, record.statusLabel, record.description]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(sidebarSearchQuery);
  };
  const visibleProjectEntries = useMemo(() => {
    const sourceProjects = projectState.projects.length
      ? projectState.projects
      : (sidebarSearchQuery ? [] : [{ id: 'empty', name: projectState.apiReady ? '暂无项目' : projectState.status }]);
    return sourceProjects
      .map((project) => {
        const sessions = projectConversationMap.get(project.id) || [];
        const projectMatches = matchesSidebarSearch(project);
        const visibleSessions = sidebarSearchQuery && !projectMatches
          ? sessions.filter((session) => matchesSidebarSearch(session))
          : sessions;
        return { project, sessions: visibleSessions, projectMatches };
      })
      .filter(({ sessions, projectMatches }) => !sidebarSearchQuery || projectMatches || sessions.length);
  }, [projectState.projects, projectState.apiReady, projectState.status, projectConversationMap, sidebarSearchQuery]);
  const visibleHistoryItems = useMemo(() => historyItems.filter((item) => matchesSidebarSearch(item)), [historyItems, sidebarSearchQuery]);
  const canQuickCreateProject = hasEcorexFunction(['createProject', 'projects.create']);
  const canQuickRenameProject = projectState.apiReady && hasEcorexFunction(['updateProject', 'projects.update']);

  return (
    <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-top">
        <Logo compact={collapsed} />
        <button
          className="icon-button small sidebar-collapse-button"
          type="button"
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
          title={collapsed ? '展开侧栏' : '收起侧栏'}
          onClick={onToggleCollapsed}
        >
          {collapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
        </button>
      </div>
      <button className="new-chat" type="button" title="新会话" onClick={() => startNewChat()}>
        <Plus size={22} />
        新会话
      </button>
      <nav className="side-nav">
        <button className={page === 'projects' ? 'active' : ''} type="button" title="项目管理" data-testid="sidebar-projects-nav" onClick={() => setPage('projects')}>
          <LayoutDashboard size={25} />
          项目管理
        </button>
      </nav>
      <label className="sidebar-search">
        <Search size={14} />
        <input
          value={chatSearch}
          onChange={(event) => setChatSearch(event.target.value)}
          placeholder="搜索项目或对话"
          aria-label="搜索项目或对话"
        />
        {chatSearch && (
          <button type="button" title="清空搜索" aria-label="清空搜索" onClick={() => setChatSearch('')}>
            <X size={13} />
          </button>
        )}
      </label>
      <section className="sidebar-projects">
        <header>
          <h3>项目</h3>
          <button type="button" title="刷新项目" onClick={() => refreshSidebarProjects()} disabled={projectState.loading}>
            <Loader2 size={13} className={projectState.loading ? 'spin-icon' : ''} />
          </button>
        </header>
        <form className="sidebar-project-create" onSubmit={createQuickProject}>
          <input
            data-testid="sidebar-project-create-input"
            value={quickProjectName}
            onChange={(event) => setQuickProjectName(event.target.value)}
            placeholder={canQuickCreateProject ? '快速创建项目' : '项目未连接'}
            disabled={!canQuickCreateProject || projectBusy === 'create'}
          />
          <button type="button" data-testid="sidebar-project-create-button" title="创建项目" onClick={createQuickProject} disabled={!canQuickCreateProject || projectBusy === 'create'}>
            <Plus size={14} />
          </button>
        </form>
        <div className="sidebar-project-list">
          {visibleProjectEntries.length === 0 && (
            <div className="sidebar-empty">没有匹配的项目或对话</div>
          )}
          {visibleProjectEntries.slice(0, sidebarSearchQuery ? 20 : 8).map(({ project, sessions }) => {
            const isProjectRenaming = renamingProjectId === project.id;
            const projectRenameBusy = projectBusy === `rename:${project.id}`;
            return (
              <div className={`sidebar-project ${project.current ? 'active' : ''}`} key={project.id}>
                <div className={`sidebar-project-row ${isProjectRenaming ? 'editing' : ''}`}>
                  {isProjectRenaming ? (
                    <form className="sidebar-project-rename-form" onSubmit={(event) => {
                      event.preventDefault();
                      saveProjectRename(project);
                    }}>
                      <input
                        data-testid="sidebar-project-rename-input"
                        value={renamingProjectName}
                        onChange={(event) => setRenamingProjectName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Escape') cancelProjectRename();
                        }}
                        disabled={projectRenameBusy}
                        autoFocus
                      />
                      <button type="submit" data-testid="sidebar-project-rename-save" title="保存项目名称" disabled={!renamingProjectName.trim() || projectRenameBusy}>
                        {projectRenameBusy ? <Loader2 size={12} className="spin-icon" /> : <Check size={12} />}
                      </button>
                      <button type="button" title="取消" onClick={cancelProjectRename} disabled={projectRenameBusy}>
                        <X size={12} />
                      </button>
                    </form>
                  ) : (
                    <>
                      <button type="button" data-testid="sidebar-project-open" title={project.name} onClick={() => openProject(project)} disabled={project.id === 'empty' || projectBusy === project.id}>
                        <FolderOpen size={15} />
                        <span>{project.name}</span>
                        <em>{sessions.length || project.sessionCount || 0}</em>
                      </button>
                      {project.id !== 'empty' && (
                        <div className="sidebar-project-actions">
                          <button type="button" data-testid="sidebar-project-rename" title="快速改名" aria-label={`快速改名 ${project.name}`} onClick={() => startProjectRename(project)} disabled={!canQuickRenameProject || projectRenameBusy}>
                            <Pencil size={12} />
                          </button>
                          <button type="button" data-testid="sidebar-project-open-folder" title="在资源管理器中打开" onClick={() => revealProjectFolder(project)} disabled={projectBusy === `open:${project.id}`}>
                            <FolderOpen size={13} />
                          </button>
                        </div>
                      )}
                    </>
                  )}
                </div>
                {sessions.slice(0, sidebarSearchQuery ? 8 : 4).map((session) => (
                  <div className={`sidebar-project-session-row ${session.id === activeConversationId ? 'active' : ''}`} key={session.id}>
                    {renamingProjectSessionId === session.id ? (
                      <form className="sidebar-project-session-rename-form" onSubmit={(event) => {
                        event.preventDefault();
                        saveProjectSessionRename(session);
                      }}>
                        <input
                          data-testid="sidebar-project-session-rename-input"
                          value={renamingProjectSessionTitle}
                          onChange={(event) => setRenamingProjectSessionTitle(event.target.value)}
                          autoFocus
                        />
                        <button type="submit" data-testid="sidebar-project-session-rename-save" title="保存">
                          <Check size={12} />
                        </button>
                        <button type="button" title="取消" onClick={() => {
                          setRenamingProjectSessionId('');
                          setRenamingProjectSessionTitle('');
                        }}>
                          <X size={12} />
                        </button>
                      </form>
                    ) : (
                      <>
                        <button className={`sidebar-project-session ${session.id === activeConversationId ? 'active' : ''}`} type="button" title={session.title} onClick={() => openRecentChat(session)}>
                          <Bot size={13} />
                          <span>{session.title}</span>
                          <em>{session.time}</em>
                        </button>
                        <button className="sidebar-project-session-action" type="button" data-testid="sidebar-project-session-rename" title="重命名会话" aria-label="重命名会话" onClick={(event) => {
                          event.stopPropagation();
                          renameProjectSession(session);
                        }}>
                          <Pencil size={12} />
                        </button>
                        <button className="sidebar-project-session-action danger" type="button" data-testid="sidebar-project-session-delete" title="删除会话" aria-label="删除会话" onClick={(event) => {
                          event.stopPropagation();
                          deleteProjectSession(session, project);
                        }}>
                          <X size={12} />
                        </button>
                      </>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        {projectState.notice && <p>{projectState.notice}</p>}
      </section>
      <div className="recent">
        <h3>历史对话</h3>
        {visibleHistoryItems.length === 0 && (
          <div className="recent-empty">
            <Bot size={17} />
            <strong>{sidebarSearchQuery ? '没有匹配对话' : '暂无历史对话'}</strong>
            <span>{sidebarSearchQuery ? '换个关键词再试试' : '未关联项目的会话会出现在这里'}</span>
          </div>
        )}
        {visibleHistoryItems.map(({ id, claudeSessionId, title, time }, index) => (
          <div className={`recent-row ${id === activeConversationId || (!activeConversationId && index === 0) ? 'active' : ''}`} key={id || title}>
            <button
              className="recent-open"
              type="button"
              onClick={() => {
                openRecentChat({ id, claudeSessionId, title });
              }}
              title={title}
            >
              <Bot size={16} />
              <span>{title}</span>
              <em>{time}</em>
            </button>
            <button
              className="recent-delete"
              type="button"
              title="删除最近对话"
              aria-label={`删除 ${title}`}
              onClick={(event) => {
                event.stopPropagation();
                setRecentItems((items) => {
                  const nextItems = items.filter((item) => item.id !== id);
                  storeRecentChatItems(nextItems);
                  deleteConversationState(id);
                  if (index === 0) {
                    window.setTimeout(() => {
                      const nextHistory = nextItems.find((item) => !item.projectId);
                      if (nextHistory) openRecentChat(nextHistory);
                      else openBlankChatAfterDelete();
                    }, 0);
                  }
                  return nextItems;
                });
              }}
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
      <div className="user-card" ref={profileRef}>
        <button className="profile-trigger" type="button" onClick={() => setProfileOpen((value) => !value)}>
          <div className="avatar avatar-photo">{currentInitials}</div>
          <div>
            <strong>{currentDisplayName}</strong>
            <span>{currentEmail}</span>
            <em className="profile-mini-status"><i />{currentRoleLabel} · {currentModelLabel}</em>
          </div>
          <ChevronDown size={18} />
        </button>
        {profileOpen && (
          <div className="profile-popover">
            <header>
              <div className="avatar avatar-photo">{currentInitials}</div>
              <div>
                <strong>{currentDisplayName}</strong>
                <span>{currentEmail}</span>
                <em><i />{currentRoleLabel}</em>
              </div>
            </header>
            <button className="profile-team" type="button">
              <UsersRound size={24} />
              <span>
                <strong>{currentAuthUser.team || 'EcoreX 本地工作区'}</strong>
                <em>{currentAuthUser.title || currentRoleLabel}</em>
              </span>
              <ChevronRight size={18} />
            </button>
            <div className="profile-menu-grid">
              <button type="button"><User size={20} />个人资料</button>
              <button
                type="button"
                onClick={() => {
                  setProfileOpen(false);
                  setPage('settings');
                }}
              >
                <Settings size={20} />系统设置
              </button>
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

function UserRoleSettingsView({ authStatus, onUnauthorized }) {
  const currentUser = authUserFromStatus(authStatus);
  const [state, setState] = useState('loading');
  const [notice, setNotice] = useState('');
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [canManageUsers, setCanManageUsers] = useState(false);
  const [canManageEnterprise, setCanManageEnterprise] = useState(false);
  const [busy, setBusy] = useState('');
  const [profileDraft, setProfileDraft] = useState({
    displayName: currentUser.displayName || currentUser.name || '',
    title: currentUser.title || '',
    team: currentUser.team || ''
  });
  const [createDraft, setCreateDraft] = useState({
    email: '',
    displayName: '',
    role: 'user',
    password: ''
  });

  async function loadUsers({ silent = false } = {}) {
    if (!silent) setState('loading');
    const result = await callEcorex(['listUsers'], {});
    if (result?.unauthorized || /unauthorized/i.test(result?.error || '')) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录。');
      return;
    }
    if (result?.ok === false) {
      setState('error');
      setNotice(sanitizeDisplayText(result.error, '用户与角色加载失败。'));
      return;
    }
    const nextUsers = Array.isArray(result.users) ? result.users : [];
    const self = result.currentUser || nextUsers.find((user) => user.email === currentUser.email) || currentUser;
    setUsers(nextUsers);
    setRoles(Array.isArray(result.roles) ? result.roles : []);
    setCanManageUsers(Boolean(result.canManageUsers));
    setCanManageEnterprise(Boolean(result.canManageEnterprise));
    setProfileDraft({
      displayName: self.displayName || self.name || '',
      title: self.title || '',
      team: self.team || ''
    });
    setState('ready');
    if (!silent) setNotice('');
  }

  useEffect(() => {
    loadUsers();
  }, []);

  async function saveProfile() {
    setBusy('profile');
    const result = await callEcorex(['updateProfile'], profileDraft);
    setBusy('');
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '个人资料保存失败。'));
      return;
    }
    setNotice('个人资料已保存。');
    await loadUsers({ silent: true });
  }

  async function createUser() {
    if (!createDraft.email.trim() || !createDraft.password.trim()) {
      setNotice('请填写邮箱和初始密码。');
      return;
    }
    setBusy('create');
    const result = await callEcorex(['createUser'], createDraft);
    setBusy('');
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '账户创建失败。'));
      return;
    }
    setCreateDraft({ email: '', displayName: '', role: 'user', password: '' });
    setNotice('账户已创建，可使用该账号登录。');
    await loadUsers({ silent: true });
  }

  async function updateUser(user, patch) {
    setBusy(user.id || user.email);
    const result = await callEcorex(['updateUser'], { id: user.id, email: user.email, ...patch });
    setBusy('');
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '账户更新失败。'));
      return;
    }
    setNotice('账户已更新。');
    await loadUsers({ silent: true });
  }

  async function deleteUser(user) {
    setBusy(`delete:${user.id || user.email}`);
    const result = await callEcorex(['deleteUser'], { id: user.id, email: user.email });
    setBusy('');
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '账户删除失败。'));
      return;
    }
    setNotice('账户已删除。');
    await loadUsers({ silent: true });
  }

  async function runEnterpriseAction(action, summary) {
    setBusy(action);
    const result = await callEcorex(['runEnterpriseAction'], { action, summary });
    setBusy('');
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '企业动作执行失败。'));
      return;
    }
    setNotice('企业动作已记录。');
  }

  const roleOptions = roles.length ? roles : [
    { value: 'super_admin', label: '超级管理员' },
    { value: 'admin', label: '管理员' },
    { value: 'user', label: '成员' }
  ];

  return (
    <section className="enterprise-users-page" data-testid="enterprise-users-page">
      {notice && <ManagementBanner tone={state === 'error' || state === 'unauthorized' ? 'error' : 'warn'} text={notice} />}
      <div className="enterprise-settings-grid">
        <article className="enterprise-panel">
          <header>
            <div className="project-chat-actions">
              <h3>个人资料</h3>
              <p>当前登录账号、团队和职位信息。</p>
            </div>
            <span className={`role-pill ${currentUser.role || 'user'}`}>{roleLabelFromAuthUser(currentUser)}</span>
          </header>
          <div className="profile-form-grid">
            <label>
              <span>显示名称</span>
              <input value={profileDraft.displayName} onChange={(event) => setProfileDraft((draft) => ({ ...draft, displayName: event.target.value }))} />
            </label>
            <label>
              <span>职位</span>
              <input value={profileDraft.title} onChange={(event) => setProfileDraft((draft) => ({ ...draft, title: event.target.value }))} />
            </label>
            <label>
              <span>团队</span>
              <input value={profileDraft.team} onChange={(event) => setProfileDraft((draft) => ({ ...draft, team: event.target.value }))} />
            </label>
            <label>
              <span>邮箱</span>
              <input value={displayUserEmailFromAuth(authStatus)} disabled />
            </label>
          </div>
          <button className="primary" type="button" onClick={saveProfile} disabled={busy === 'profile'}>
            {busy === 'profile' ? <Loader2 size={16} className="spin-icon" /> : <Check size={16} />}
            保存资料
          </button>
        </article>

        <article className="enterprise-panel" data-testid="users-admin-panel">
          <header>
            <div>
              <h3>用户与角色</h3>
              <p>{canManageUsers ? '创建本地账号、调整角色并停用离职账号。' : '当前账号只能查看和更新自己的资料。'}</p>
            </div>
            <button type="button" onClick={() => loadUsers()} disabled={state === 'loading'}>
              {state === 'loading' ? <Loader2 size={16} className="spin-icon" /> : <RotateCcw size={16} />}
              刷新
            </button>
          </header>

          {canManageUsers && (
            <div className="user-create-row">
              <input placeholder="邮箱" value={createDraft.email} onChange={(event) => setCreateDraft((draft) => ({ ...draft, email: event.target.value }))} />
              <input placeholder="显示名称" value={createDraft.displayName} onChange={(event) => setCreateDraft((draft) => ({ ...draft, displayName: event.target.value }))} />
              <select value={createDraft.role} onChange={(event) => setCreateDraft((draft) => ({ ...draft, role: event.target.value }))}>
                {roleOptions.map((role) => <option value={role.value} key={role.value}>{role.label}</option>)}
              </select>
              <input placeholder="初始密码" type="password" value={createDraft.password} onChange={(event) => setCreateDraft((draft) => ({ ...draft, password: event.target.value }))} />
              <button type="button" onClick={createUser} disabled={busy === 'create'}>
                <Plus size={15} />
                创建
              </button>
            </div>
          )}

          <div className="users-table">
            {users.map((user) => {
              const isSelf = user.email === currentUser.email;
              const rowBusy = busy === (user.id || user.email) || busy === `delete:${user.id || user.email}`;
              return (
                <div className={`user-row ${user.active === false ? 'disabled' : ''}`} key={user.id || user.email}>
                  <div className="user-row-main">
                    <div className="avatar mini">{avatarInitialsFromUser(user)}</div>
                    <div>
                      <strong>{user.displayName || user.name || user.email}</strong>
                      <em>{user.email}</em>
                    </div>
                  </div>
                  <select value={user.role || 'user'} onChange={(event) => updateUser(user, { role: event.target.value })} disabled={!canManageUsers || rowBusy}>
                    {roleOptions.map((role) => <option value={role.value} key={role.value}>{role.label}</option>)}
                  </select>
                  <button type="button" onClick={() => updateUser(user, { active: user.active === false })} disabled={!canManageUsers || isSelf || rowBusy}>
                    {user.active === false ? '启用' : '停用'}
                  </button>
                  <button className="danger" type="button" title="删除账号" onClick={() => deleteUser(user)} disabled={!canManageUsers || isSelf || rowBusy}>
                    <X size={15} />
                  </button>
                </div>
              );
            })}
            {!users.length && <p className="enterprise-empty">暂无可显示账号。</p>}
          </div>
        </article>
      </div>

      {canManageEnterprise && (
        <article className="enterprise-panel">
          <header>
            <div>
              <h3>企业动作</h3>
              <p>记录管理员级同步操作，便于审计本地配置变更。</p>
            </div>
          </header>
          <div className="enterprise-action-row">
            <button type="button" onClick={() => runEnterpriseAction('syncMcp', '管理员同步 MCP 配置')} disabled={busy === 'syncMcp'}>
              <Network size={16} />
              同步 MCP
            </button>
            <button type="button" onClick={() => runEnterpriseAction('pushSkill', '管理员同步 SKILLS 配置')} disabled={busy === 'pushSkill'}>
              <Layers3 size={16} />
              同步 SKILLS
            </button>
            <button type="button" onClick={() => runEnterpriseAction('pushAgentUpdate', '管理员同步 Agent 更新策略')} disabled={busy === 'pushAgentUpdate'}>
              <Archive size={16} />
              同步更新
            </button>
          </div>
        </article>
      )}
    </section>
  );
}

function EvaluationView({ onUnauthorized, embedded = false }) {
  const [state, setState] = useState('loading');
  const [notice, setNotice] = useState('');
  const [framework, setFramework] = useState(null);
  const [runningEval, setRunningEval] = useState(false);

  async function loadEvaluations() {
    setState('loading');
    setNotice('');
    const result = await callEcorex(['listEvaluations', 'evaluation.list', 'evaluations.list']);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('请重新登录后查看企业评估框架。');
      return;
    }
    if (result?.ok === false && !result.missing) {
      setState('error');
      setNotice(sanitizeDisplayText(result.error, '评估框架加载失败。'));
      return;
    }
    setFramework(result?.ok === false ? null : result);
    setState(result?.ok === false ? 'unsupported' : 'ready');
  }

  async function runEvaluationSuite() {
    setRunningEval(true);
    setNotice('');
    const result = await callEcorex(['runEvaluations', 'evaluation.run', 'evaluations.run'], { mode: 'definition-ready' });
    setRunningEval(false);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('请重新登录后运行评估。');
      return;
    }
    if (result?.ok === false) {
      setNotice(sanitizeDisplayText(result.error, '评估运行失败。'));
      return;
    }
    setFramework((current) => ({ ...(current || {}), lastReport: result }));
    setNotice(`评估已完成：${result.results?.length || result.sampleCount || 0} 条样本，耗时 ${result.durationMs || 0} ms。`);
  }

  useEffect(() => {
    loadEvaluations();
  }, []);

  const dimensions = Array.isArray(framework?.dimensions) ? framework.dimensions : [];
  const samples = Array.isArray(framework?.samples) ? framework.samples : [];
  const retryPolicy = framework?.retryPolicy || {};
  const memoryTaxonomy = framework?.memoryTaxonomy || {};
  const lastReport = framework?.lastReport || null;

  return (
    <section className={`evaluation-page management-page ${embedded ? 'embedded' : ''}`} data-testid="evaluation-page">
      <div className="management-toolbar">
        <div>
          <h2>评估</h2>
          <p>50 条样本、评分与计时，仅用于更新前后回归评估，不进入项目记忆。</p>
        </div>
        <button type="button" onClick={loadEvaluations} disabled={state === 'loading' || runningEval}>
          <Loader2 size={16} className={state === 'loading' ? 'spin-icon' : ''} />
          刷新
        </button>
        <button className="primary" type="button" onClick={runEvaluationSuite} disabled={runningEval || state === 'loading' || state === 'unsupported'}>
          {runningEval ? <Loader2 size={16} className="spin-icon" /> : <Play size={16} />}
          运行评估
        </button>
      </div>

      {notice && <ManagementBanner tone={state === 'error' || state === 'unauthorized' ? 'error' : 'warn'} text={notice} />}

      {state === 'loading' && <ManagementState icon={Loader2} spin title="正在加载评估框架" text="读取本机评估样本和上一次报告。" />}
      {state === 'unsupported' && <ManagementState title="评估框架未就绪" text="当前桌面端暂未返回评估样本。" />}
      {state === 'ready' && (
        <>
          <div className="stats-row compact">
            {[
              ['样本', framework?.sampleCount || samples.length, ClipboardList, '仅用于评估'],
              ['维度', dimensions.length, Target, '事实/结构/工具/计时'],
              ['重试', retryPolicy.maxAttempts || 0, RotateCcw, retryPolicy.strategy || 'exponential-backoff-with-jitter'],
              ['上次报告', lastReport?.sampleCount || 0, CircleCheck, lastReport?.finishedAt ? formatDateTime(lastReport.finishedAt) : '尚未运行']
            ].map(([label, value, Icon, detail]) => (
              <div className="stat-card" key={label}>
                <span><Icon size={22} /></span>
                <div>
                  <em>{label}</em>
                  <strong>{value}<small> 项</small></strong>
                  <p>{detail}</p>
                </div>
              </div>
            ))}
          </div>

          <div className="management-grid two">
            <article className="settings-panel">
              <h3>评分维度</h3>
              <div className="settings-list compact">
                {dimensions.map((item) => (
                  <div className="setting-row" key={item.key}>
                    <strong>{item.label || item.key}</strong>
                    <em>{Math.round(Number(item.weight || 0) * 100)}%</em>
                  </div>
                ))}
              </div>
            </article>
            <article className="settings-panel">
              <h3>记忆边界</h3>
              <p>{memoryTaxonomy.currentImplementation || '评估样本不进入聊天历史、项目记忆或向量记忆。'}</p>
              <small>结构化记忆 {memoryTaxonomy.structuredMemory?.length || 0} 项 · 向量记忆 {memoryTaxonomy.vectorMemory?.length || 0} 项</small>
            </article>
          </div>

          <div className="table-head evaluation-samples-head">
            <span>样本</span>
            <span>分类</span>
            <span>预期结果</span>
            <span>工具</span>
          </div>
          <div className="evaluation-sample-list">
            {samples.slice(0, 50).map((sample) => (
              <article className="evaluation-sample-row" key={sample.id}>
                <strong>{sample.id}</strong>
                <span>{sample.category}</span>
                <em>{sanitizeDisplayText(sample.expectedResult, '待评估').slice(0, 160)}</em>
                <small>{Array.isArray(sample.expectedTools) && sample.expectedTools.length ? sample.expectedTools.join(', ') : '按需'}</small>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function SystemSettingsView({
  activeTab = 'diagnostics',
  onTabChange,
  onBack,
  backendStatus,
  backendError,
  capabilities,
  authStatus,
  startupState,
  refreshBackend,
  onUnauthorized
}) {
  const tabs = [
    ['mcp', 'MCP', Box, '服务、授权与连接状态'],
    ['skills', 'SKILLS', Layers3, '安装、启用与更新'],
    ['users', '用户与角色', UsersRound, '本地账号、角色与企业权限'],
    ['diagnostics', '诊断 / 设置', Settings, '健康检查与默认参数']
  ];
  tabs.splice(3, 0, ['evaluations', '评估', ClipboardList, '50 条样本、评分与计时']);

  return (
    <section className="system-settings-page panel" data-testid="system-settings-page">
      <HeaderBar
        title="系统设置"
        badge="个人"
        subtitle="集中管理 MCP、SKILLS、运行诊断与默认偏好"
        backendStatus={backendStatus}
        onRefresh={() => refreshBackend?.({ refresh: true })}
        onBack={onBack}
      />
      <div className="system-settings-tabs" role="tablist" aria-label="系统设置">
        {tabs.map(([value, label, Icon, desc]) => (
          <button
            className={activeTab === value ? 'active' : ''}
            type="button"
            role="tab"
            aria-selected={activeTab === value}
            data-testid={`system-settings-tab-${value}`}
            key={value}
            onClick={() => onTabChange?.(value)}
          >
            <Icon size={18} />
            <span>{label}</span>
            <em>{desc}</em>
          </button>
        ))}
      </div>
      <div className="system-settings-body">
        {activeTab === 'mcp' && (
          <McpView
            embedded
            backendStatus={backendStatus}
            capabilities={capabilities}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
          />
        )}
        {activeTab === 'skills' && (
          <SkillsView
            embedded
            authStatus={authStatus}
            backendStatus={backendStatus}
            capabilities={capabilities}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
          />
        )}
        {activeTab === 'evaluations' && (
          <EvaluationView onUnauthorized={onUnauthorized} />
        )}
        {activeTab === 'users' && (
          <UserRoleSettingsView authStatus={authStatus} onUnauthorized={onUnauthorized} />
        )}
        {activeTab === 'diagnostics' && (
          <DiagnosticsView
            embedded
            backendStatus={backendStatus}
            backendError={backendError}
            capabilities={capabilities}
            authStatus={authStatus}
            startupState={startupState}
            refreshBackend={refreshBackend}
            onUnauthorized={onUnauthorized}
          />
        )}
      </div>
    </section>
  );
}

function LegacyEvaluationViewDisabled({ onUnauthorized }) {
  const [framework, setFramework] = useState(null);
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState('');

  async function loadEvaluationFramework(silent = false) {
    if (!silent) setLoading(true);
    const result = await callEcorex(['listEvaluations', 'evaluation.list']);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setLoading(false);
      return;
    }
    if (result?.ok === false) {
      setNotice(result.error || '评估框架加载失败。');
      setLoading(false);
      return;
    }
    setFramework(result.framework || result);
    setLoading(false);
  }

  async function runEvaluation() {
    setRunning(true);
    setNotice('正在运行 50 条评估样本。');
    const result = await callEcorex(['runEvaluations', 'evaluation.run'], {});
    if (result?.unauthorized) {
      onUnauthorized?.();
      setRunning(false);
      return;
    }
    if (result?.ok === false) {
      setNotice(result.error || '评估运行失败。');
      setRunning(false);
      return;
    }
    setReport(result);
    setRunning(false);
    setNotice('评估完成，结果不会写入会话记忆。');
  }

  useEffect(() => {
    loadEvaluationFramework(true);
  }, []);

  const aggregate = report?.aggregate || framework?.lastReport?.aggregate || {};
  const dimensions = [
    ['事实正确性', aggregate.factuality],
    ['结构完整度', aggregate.structure],
    ['工具调用合理性', aggregate.toolUse],
    ['计时', aggregate.latency]
  ];
  const sampleCount = Number(framework?.sampleCount || framework?.samples?.length || report?.sampleCount || 50);

  return (
    <section className="evaluation-page embedded-settings-section">
      <div className="settings-panel">
        <header>
          <div>
            <h3>评估框架</h3>
            <p>50 条样本仅用于上线前评估，不进入项目记忆或向量记忆。</p>
          </div>
          <button type="button" onClick={runEvaluation} disabled={running || loading}>
            {running ? <Loader2 size={15} className="spin-icon" /> : <Play size={15} />}
            运行评估
          </button>
        </header>
        {notice && <ManagementBanner text={notice} tone={report?.ok === false ? 'error' : 'warn'} />}
        <div className="health-check-grid">
          <div className="health-check-item ok">
            <ClipboardList size={18} />
            <span>样本</span>
            <strong>{sampleCount}</strong>
            <em>输入与预期结果</em>
          </div>
          {dimensions.map(([label, value]) => (
            <div className="health-check-item running" key={label}>
              <Target size={18} />
              <span>{label}</span>
              <strong>{value == null ? '-' : `${Math.round(Number(value) * 100)}%`}</strong>
              <em>上线前回归</em>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function normalizeModelProfile(raw = {}, index = 0, currentId = '') {
  const id = String(raw.id || raw.profileId || raw.key || raw.name || `profile-${index + 1}`);
  const modelName = String(raw.modelName || raw.model || raw.defaultModel || '').trim();
  const imageModelName = defaultImageModelName(raw.imageModelName || raw.imageModel || raw.visionModel || DEFAULT_IMAGE_MODEL_NAME);
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
    imageModelName: defaultImageModelName(profile.imageModelName || profile.imageModel || DEFAULT_IMAGE_MODEL_NAME)
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

async function loadModelProfiles(defaultModelName = DEFAULT_AGENT_MODEL_NAME) {
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
    imageModelName: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME),
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
    imageModel: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME),
    imageModelName: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME)
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
    imageModel: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME),
    imageModelName: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME)
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
    imageModel: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME),
    imageModelName: defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME)
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
  const imageModelName = defaultImageModelName(draft.imageModelName || DEFAULT_IMAGE_MODEL_NAME);
  const payload = {
    id: draft.id || undefined,
    profileId: draft.id || undefined,
    name: draft.name,
    baseUrl: draft.baseUrl,
    model: imageModelName,
    imageModel: imageModelName,
    imageModelName,
    prompt: 'A compact desktop AI agent interface for advertising project analysis, dark mode, crisp UI screenshot style.',
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
    const result = await loadModelProfiles(initialModelName || DEFAULT_AGENT_MODEL_NAME);
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
      imageModelName: defaultImageModelName(draft.imageModelName)
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
        imageModelName: defaultImageModelName(draft.imageModelName)
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
        imageModelName: defaultImageModelName(draft.imageModelName)
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
    <div
      className="modal-backdrop model-config-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <section className="model-config-modal" role="dialog" aria-modal="true" aria-label="模型配置">
        <header className="model-config-head">
          <div>
            <span>系统设置 / 模型配置</span>
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
                  <span>{imageTest?.latencyMs ? `${imageTest.latencyMs} ms · ${defaultImageModelName(draft.imageModelName)}` : defaultImageModelName(draft.imageModelName)}</span>
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

function formatAgentEventStatus(value, fallback = '进行中') {
  const normalized = String(value || '').trim().toLowerCase();
  const labels = {
    running: '进行中',
    active: '进行中',
    progress: '进行中',
    pending: '等待中',
    queued: '排队中',
    starting: '启动中',
    started: '已启动',
    completed: '已完成',
    complete: '已完成',
    done: '已完成',
    success: '已完成',
    failed: '异常',
    fail: '异常',
    error: '异常',
    timeout: '已超时',
    cancelled: '已取消',
    canceled: '已取消',
    debug: '调试',
    system: '系统',
    tool: '能力调用'
  };
  return labels[normalized] || sanitizeDisplayText(value, fallback);
}

function timelineItemFromAgentEvent(event) {
  const taskLabel = agentDisclosureLabel(event);
  const labelMap = {
    status: taskLabel || '准备执行任务',
    tool: taskLabel || '调用本地工具',
    ledger: taskLabel || '记录工具调用',
    attachment: '读取附件内容',
    recovery: '恢复运行任务',
    stderr: '记录执行日志',
    debug: '同步任务进度',
    assistant: '生成回复内容',
    result: '整理最终结果',
    done: '任务执行完成',
    cancelled: '任务已取消',
    timeout: '任务已超时',
    error: '执行遇到异常'
  };
  const statusMap = {
    status: formatAgentEventStatus(event.status || event.state, '进行中'),
    tool: formatAgentEventStatus(event.status || event.state, '能力调用'),
    ledger: formatAgentEventStatus(event.status || event.state, '能力调用'),
    attachment: formatAgentEventStatus(event.status || event.state, '附件读取'),
    recovery: formatAgentEventStatus(event.status || event.state, '可恢复'),
    stderr: '日志',
    debug: formatAgentEventStatus(event.status || event.state, '同步中'),
    assistant: '生成中',
    result: '生成结果',
    done: '已完成',
    cancelled: '已取消',
    timeout: '已超时',
    error: '失败'
  };
  const toneMap = {
    status: 'running',
    tool: 'running',
    ledger: toolToneFromStatus(event.status || event.state),
    attachment: ingestToneFromStatus(event.status || event.state, event.ok !== false),
    recovery: recoveryStateFromStatus(event.status || event.state).tone,
    stderr: 'warn',
    debug: 'pending',
    assistant: 'running',
    result: 'success',
    done: 'success',
    cancelled: 'pending',
    timeout: 'danger',
    error: 'danger'
  };

  return [
    sanitizeDisplayText(labelMap[event.kind] || taskLabel || '执行步骤', '执行步骤').slice(0, 120),
    sanitizeDisplayText(statusMap[event.kind] || formatAgentEventStatus(event.status || event.state, '进行中'), '进行中').slice(0, 40),
    formatAgentEventTime(event),
    toneMap[event.kind] || 'running',
    event.kind || 'status'
  ];
}

function agentDisclosureLabel(event = {}) {
  const rawName = String(event?.task?.name || event?.taskName || event?.toolName || '').trim();
  const toolNames = Array.isArray(event.tools)
    ? event.tools.map((tool) => String(tool?.name || '').trim()).filter(Boolean)
    : [];
  const firstTool = toolNames[0] || rawName;
  if (/^(EcoreX capability|Agent session|EcoreX 原生能力)$/i.test(firstTool)) {
    const text = String(event.text || '').trim();
    if (/联网|检索|搜索/i.test(text)) return '联网检索';
    if (/网页|读取/i.test(text)) return '读取网页';
    if (/工具|能力/i.test(text)) return '调用原生能力';
    return '';
  }
  if (/^ToolSearch$/i.test(firstTool)) return rawName || '准备调用工具';
  if (/^WebSearch$/i.test(firstTool)) return '联网检索';
  if (/^WebFetch$/i.test(firstTool)) return '读取网页';
  if (/^TodoWrite$/i.test(firstTool)) return '更新任务清单';
  if (/^TodoRead$/i.test(firstTool)) return '读取任务清单';
  if (/^Task$/i.test(firstTool)) return '调度子 Agent';
  if (/^Read$|^Grep$|^Glob$|^LS$|^NotebookRead$/i.test(firstTool)) return '查看文件';
  if (/^Write$|^Edit$|^MultiEdit$|^NotebookEdit$/i.test(firstTool)) return '准备修改文件';
  if (/^Bash$/i.test(firstTool)) return '执行本地命令';
  if (/^mcp__|^MCP$/i.test(firstTool)) return '调用 MCP';
  if (/^Skill$|^SKILLS$/i.test(firstTool)) return '调用 SKILLS';
  if (toolNames.length > 1) return `调用 ${toolNames.length} 个工具`;
  return sanitizeDisplayText(rawName, '');
}

function appendTimeline(timeline = [], item, limit = 80) {
  return appendTimelineItems(timeline, [item], limit);
}

function isSuccessfulTerminalTimelineItem(item = []) {
  const tone = String(item?.[3] || '');
  const kind = String(item?.[4] || '');
  return tone === 'success' && (kind === 'result' || kind === 'done');
}

function isTerminalTimelineItem(item = []) {
  const tone = String(item?.[3] || '');
  const kind = String(item?.[4] || '');
  return ['result', 'done', 'cancelled', 'timeout', 'error'].includes(kind) || tone === 'danger';
}

function closeOpenTimelineItemsBeforeTerminal(items = []) {
  const terminalIndex = items.reduce((latest, item, index) => (
    isSuccessfulTerminalTimelineItem(item) ? index : latest
  ), -1);
  if (terminalIndex < 0) return items;
  return items.map((item, index) => {
    if (!Array.isArray(item) || index >= terminalIndex) return item;
    if (!['running', 'pending'].includes(item[3])) return item;
    return [item[0], '已完成', item[2], 'success', item[4] || 'status'];
  });
}

function appendTimelineItems(timeline = [], items = [], limit = 80) {
  if (!items.length) return timeline;
  return closeOpenTimelineItemsBeforeTerminal([...timeline, ...items]).slice(-limit);
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
    ledger_event: 'ledger',
    tool_call: 'tool',
    tool_use: 'tool',
    tool_result: 'tool',
    command: 'tool',
    attachment_ingest: 'attachment',
    attachment_ingestion: 'attachment',
    file_ingest: 'attachment',
    file_preview: 'attachment',
    restore: 'recovery',
    restored: 'recovery',
    session_recovery: 'recovery',
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
  const kind = normalizeAgentEventKind(raw.kind || raw.type || raw.event);
  const rawText = raw.text || raw.content || raw.message || raw.delta || '';
  return {
    ...raw,
    sessionId,
    kind,
    text: clampLongText(rawText, kind === 'assistant' || kind === 'result' ? MAX_LIVE_ASSISTANT_TEXT_CHARS : FRONT_AGENT_EVENT_TEXT_CHARS),
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

function normalizeDuplicateAssistantSegment(value = '') {
  return String(value || '')
    .toLowerCase()
    .replace(/[\s`*_~"'“”‘’()[\]{}<>《》【】,，.。!！?？:：;；、-]+/g, '');
}

function dedupeAdjacentRepeatedSentences(value = '') {
  const parts = String(value || '').match(/\s+|[^。！？!?\n]+[。！？!?]?/g) || [];
  const output = [];
  let lastComparable = '';
  for (const part of parts) {
    if (/^\s+$/.test(part)) {
      output.push(part);
      continue;
    }
    const comparable = normalizeDuplicateAssistantSegment(part);
    if (comparable && comparable.length >= 16 && comparable === lastComparable) {
      continue;
    }
    output.push(part);
    if (comparable) lastComparable = comparable;
  }
  return output.join('');
}

function dedupeAdjacentRepeatedLines(value = '') {
  const lines = String(value || '').split(/\r?\n/);
  const output = [];
  let lastComparable = '';
  for (const line of lines) {
    const comparable = normalizeDuplicateAssistantSegment(line);
    if (comparable && comparable.length >= 16 && comparable === lastComparable) {
      continue;
    }
    output.push(line);
    if (comparable) lastComparable = comparable;
  }
  return output.join('\n');
}

function dedupeAssistantOutputText(value = '') {
  return dedupeAdjacentRepeatedLines(dedupeAdjacentRepeatedSentences(value));
}

function isMeaningfulAssistantOverlap(value = '') {
  const normalized = normalizeDuplicateAssistantSegment(value);
  if (normalized.length >= 6) return true;
  const cjkCount = (normalized.match(/[\u4e00-\u9fff]/g) || []).length;
  return normalized.length >= 4 && cjkCount >= 3;
}

function assistantTextOverlapLength(existing = '', incoming = '') {
  const left = String(existing || '');
  const right = String(incoming || '');
  const max = Math.min(left.length, right.length, 1600);
  for (let length = max; length >= 4; length -= 1) {
    const segment = left.slice(-length);
    if (right.startsWith(segment) && isMeaningfulAssistantOverlap(segment)) {
      return length;
    }
  }
  return 0;
}

function cleanAssistantOutputText(value = '') {
  return dedupeAssistantOutputText(cleanPublicAgentText(String(value || '')
    .replace(/^\s*\*\s+/gm, '- ')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '$1')
    .replace(/\*/g, '')));
}

function redactArtifactPathsInResultLine(line = '') {
  const text = String(line || '').trimEnd();
  if (!looksLikeNoisyLocalPath(text)) return text;
  const artifacts = extractArtifactReferences(text);
  if (!artifacts.length) return '';
  let next = text;
  for (const artifact of artifacts) {
    const replacement = artifact.name || fileNameFromArtifactPath(artifact.path || artifact.raw);
    if (!replacement) continue;
    for (const token of [artifact.raw, artifact.path]) {
      if (token) next = next.split(token).join(replacement);
    }
  }
  return looksLikeNoisyLocalPath(next) ? '' : next;
}

function cleanResultOutputText(value = '') {
  const text = String(value || '')
    .split(/\r?\n/)
    .map((line) => redactArtifactPathsInResultLine(line))
    .filter(Boolean)
    .join('\n');
  return cleanAssistantOutputText(text);
}

function isGenericTerminalText(value = '') {
  const text = cleanAssistantOutputText(value).trim().toLowerCase();
  return /^(done|complete|completed|success|ok|finished|agent task completed\.?|已完成|完成|任务完成|任务执行完成)$/.test(text);
}

function mergeAssistantOutputText(existing = '', incoming = '') {
  const existingText = cleanAssistantOutputText(existing);
  const incomingText = cleanAssistantOutputText(incoming);
  const normalizedExisting = normalizeDuplicateAssistantSegment(existingText);
  const normalizedIncoming = normalizeDuplicateAssistantSegment(incomingText);
  if (!incomingText) return existingText;
  if (!existingText) return incomingText;
  if (normalizedExisting && normalizedIncoming) {
    if (normalizedIncoming === normalizedExisting) return incomingText.length >= existingText.length ? incomingText : existingText;
    if (normalizedIncoming.startsWith(normalizedExisting) && normalizedExisting.length >= 16) return incomingText;
    if (normalizedExisting.endsWith(normalizedIncoming) && normalizedIncoming.length >= 16) return existingText;
  }
  const overlap = assistantTextOverlapLength(existingText, incomingText);
  if (overlap > 0) return cleanAssistantOutputText(`${existingText}${incomingText.slice(overlap)}`);
  return cleanAssistantOutputText(mergeAssistantText(existingText, incomingText));
}

function finalizeAssistantOutputText(existing = '', incoming = '', options = {}) {
  const existingText = cleanAssistantOutputText(existing);
  const incomingText = options.preserveArtifactLabels
    ? cleanResultOutputText(incoming)
    : cleanAssistantOutputText(incoming);
  if (!incomingText) return existingText;
  if (!existingText) return incomingText;
  return mergeAssistantOutputText(existingText, incomingText);
}

function promptDisclosureProfile(prompt = '', attachments = []) {
  const text = String(prompt || '').trim();
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
  const wantsFreshInfo = /(联网|搜索|检索|查询|查下|查一下|最新|今天|昨天|近\s*\d+\s*天|近三天|近两天|天气|热点|热门|趋势|小红书|抖音|微博|新闻|官网|网页|链接|报告)/i.test(text);
  const touchesLocalWork = /(文件|附件|目录|本地|工作区|读取|写入|修改|删除|命令|终端|项目|代码|表格|图片|PDF|文档)/i.test(text) || hasAttachments;
  const complex = text.length > 48 || /(分析|对比|梳理|整理|生成|诊断|排查|方案|计划|复盘|总结|报告|自动化|批量)/i.test(text);
  if (wantsFreshInfo) return 'search';
  if (touchesLocalWork) return 'local';
  if (complex) return 'analysis';
  return 'simple';
}

function buildImmediateTimeline(prompt = '', attachments = [], time = formatAgentEventTime()) {
  const profile = promptDisclosureProfile(prompt, attachments);
  const items = [['接收用户任务', '已提交', time, 'success', 'status']];
  if (profile === 'search') {
    items.push(['准备联网检索', '进行中', time, 'running', 'tool']);
  } else if (profile === 'local') {
    items.push(['准备读取任务上下文', '进行中', time, 'running', 'tool']);
  } else if (profile === 'analysis') {
    items.push(['分析任务目标', '进行中', time, 'running', 'status']);
  }
  return items;
}

function buildDisclosureProgressSteps(prompt = '', attachments = []) {
  const profile = promptDisclosureProfile(prompt, attachments);
  if (profile === 'search') {
    return [
      ['连接联网检索能力', '进行中', 'tool'],
      ['检索公开网页来源', '进行中', 'tool'],
      ['筛选近期待验证信息', '进行中', 'tool'],
      ['整理可引用结果', '进行中', 'result']
    ];
  }
  if (profile === 'local') {
    return [
      ['读取任务上下文', '进行中', 'tool'],
      ['检查可用本地能力', '进行中', 'tool'],
      ['等待必要的本地权限确认', '按需确认', 'status'],
      ['整理执行结果', '进行中', 'result']
    ];
  }
  if (profile === 'analysis') {
    return [
      ['分析任务目标', '进行中', 'status'],
      ['拆分关键问题', '进行中', 'status'],
      ['组织回答结构', '进行中', 'assistant']
    ];
  }
  return [
    ['整理回答', '进行中', 'assistant']
  ];
}

function createInitialMessages(userName = '张晓明') {
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  const name = String(userName || '').trim() || '张晓明';
  return [
    {
      id: 'assistant-welcome',
      role: 'assistant',
      text: `Hi ${name}，接下来我们做些什么？`,
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

function ChatView({ backendStatus, backendError, capabilities, authStatus, refreshBackend, onUnauthorized, setPage }) {
  const authDisplayName = useMemo(() => displayUserNameFromAuth(authStatus), [authStatus]);
  const [prompt, setPrompt] = useState('');
  const [running, setRunning] = useState(false);
  const [permissionMode, setPermissionMode] = useState(() => readStoredDefaultAccessMode());
  const [model, setModel] = useState(DEFAULT_AGENT_MODEL_NAME);
  const [modelProfiles, setModelProfiles] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [visibleMessageCount, setVisibleMessageCount] = useState(MESSAGE_WINDOW_SIZE);
  const [messages, setMessages] = useState(() => createInitialMessages(authDisplayName));
  const [timeline, setTimeline] = useState(initialTimeline);
  const [contextSummary, setContextSummary] = useState('');
  const [contextCompactedAt, setContextCompactedAt] = useState(null);
  const [runningSessions, setRunningSessions] = useState([]);
  const [attachments, setAttachments] = useState([]);
  const [composerReferences, setComposerReferences] = useState([]);
  const [focusArtifact, setFocusArtifact] = useState(null);
  const [chatProject, setChatProject] = useState(null);
  const [projectFiles, setProjectFiles] = useState([]);
  const [conversationId, setConversationId] = useState(() => createLocalId('conversation'));
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const fileInputRef = useRef(null);
  const messageListRef = useRef(null);
  const autoScrollPinnedRef = useRef(true);
  const sessionMap = useRef(new Map());
  const runningRef = useRef(false);
  const runningSessionsRef = useRef(new Set());
  const runningSessionRowsRef = useRef([]);
  const currentSessionIdRef = useRef(null);
  const eventQueueRef = useRef([]);
  const eventSeqRef = useRef(0);
  const pendingFollowUpsRef = useRef([]);
  const followUpFlushInFlightRef = useRef(false);
  const messagesRef = useRef(messages);
  const contextSummaryRef = useRef('');
  const contextCompactedMessageCountRef = useRef(0);
  const nativeSessionRotatedMessageCountRef = useRef(0);
  const flushTimerRef = useRef(null);
  const conversationSaveTimerRef = useRef(null);
  const conversationSaveSnapshotRef = useRef(null);
  const pendingCancelsRef = useRef(new Set());
  const permissionContinuationLocksRef = useRef(new Set());
  const statusTimers = useRef([]);
  const attachmentObjectUrlsRef = useRef(new Set());
  const conversationIdRef = useRef(conversationId);
  const conversationSessionIdRef = useRef(conversationId);
  const conversationProjectRef = useRef(null);
  const sessionOwnersRef = useRef(new Map());
  const storedEventsByConversation = useRef(new Map());
  const activeProject = chatProject || conversationProjectRef.current || null;

  useEffect(() => {
    let cancelled = false;
    if (!activeProject?.id || !hasEcorexFunction(['listProjectFiles', 'projects.listFiles'])) {
      setProjectFiles([]);
      return () => {
        cancelled = true;
      };
    }
    const loadFiles = () => listManagedProjectFiles(activeProject.id).then((result) => {
      if (cancelled) return;
      if (result?.ok === false || result?.unauthorized) {
        setProjectFiles([]);
        return;
      }
      setProjectFiles(filterVisibleProjectFiles(result?.files || []));
    }).catch(() => {
      if (!cancelled) setProjectFiles([]);
    });
    const onFilesChanged = (event) => {
      if (!event?.detail?.projectId || event.detail.projectId === activeProject.id) loadFiles();
    };
    const onFocus = () => loadFiles();
    loadFiles();
    const interval = window.setInterval(loadFiles, PROJECT_FILE_REFRESH_INTERVAL_MS);
    window.addEventListener?.('ecorex:project-files-changed', onFilesChanged);
    window.addEventListener?.('focus', onFocus);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener?.('ecorex:project-files-changed', onFilesChanged);
      window.removeEventListener?.('focus', onFocus);
    };
  }, [activeProject?.id]);

  useEffect(() => {
    if (!activeProject?.id) {
      setAttachments((items) => items.filter((item) => item.source !== 'project' && !item.projectId));
      return;
    }
    const available = projectFileIdentitySet(projectFiles);
    setAttachments((items) => items.filter((item) => {
      if (item.source !== 'project' && !item.projectId) return true;
      if (item.projectId !== activeProject.id) return false;
      const key = projectFileIdentity(item);
      return key && available.has(key);
    }));
  }, [activeProject?.id, projectFiles]);

  const selectedPlugins = useMemo(() => {
    const managedPlugins = Array.isArray(capabilities?.capabilityPacks)
      ? capabilities.capabilityPacks
          .map((plugin) => plugin?.name)
          .filter((name) => /^[a-zA-Z0-9_.-]{1,80}$/.test(String(name || '')))
      : [];
    return [...new Set(['feature-dev', 'code-review', 'security-guidance', 'plugin-dev', ...managedPlugins])];
  }, [capabilities]);

  const permissionOptions = useMemo(() => {
    return permissionOptionsForUi();
  }, [capabilities]);

  const modelOptions = useMemo(() => {
    const activeProfile = modelProfiles.find((profile) => profile?.active && profile?.modelName);
    const currentModel = activeProfile?.modelName || model || DEFAULT_AGENT_MODEL_NAME;
    return [[currentModel, currentModel]];
  }, [model, modelProfiles]);

  const visibleMessages = useMemo(() => messages.slice(-visibleMessageCount), [messages, visibleMessageCount]);
  const activeConversationRunning = useMemo(() => (
    runningSessions.some((row) => {
      if (!isRunningSessionActive(row)) return false;
      const owner = sessionOwnersRef.current.get(row.sessionId || row.id);
      return String(row.conversationId || owner?.conversationId || '') === String(conversationId);
    })
  ), [runningSessions, conversationId]);
  const hiddenMessageCount = Math.max(messages.length - visibleMessages.length, 0);

  function isMessageListNearBottom(node = messageListRef.current) {
    if (!node) return true;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    return distance <= 96;
  }

  function updateMessageAutoScrollPin() {
    const pinned = isMessageListNearBottom();
    autoScrollPinnedRef.current = pinned;
    setShowJumpLatest(!pinned);
  }

  function scrollMessagesToLatest(behavior = 'smooth') {
    const scroll = () => {
      const node = messageListRef.current;
      if (!node) return;
      autoScrollPinnedRef.current = true;
      setShowJumpLatest(false);
      node.scrollTo({ top: node.scrollHeight, behavior });
    };
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(scroll);
    } else {
      scroll();
    }
  }

  function scheduleStatusTimer(callback, delay) {
    const timer = window.setTimeout(() => {
      statusTimers.current = statusTimers.current.filter((item) => item !== timer);
      callback();
    }, delay);
    statusTimers.current.push(timer);
    return timer;
  }

  function flushConversationSave() {
    const snapshot = conversationSaveSnapshotRef.current;
    if (!snapshot) return;
    conversationSaveSnapshotRef.current = null;
    saveConversationState(snapshot.id, snapshot.state);
  }

  function rememberAttachmentObjectUrl(url) {
    if (url) attachmentObjectUrlsRef.current.add(url);
    return url;
  }

  function revokeAttachmentObjectUrl(url) {
    if (!url || !attachmentObjectUrlsRef.current.has(url)) return;
    URL.revokeObjectURL(url);
    attachmentObjectUrlsRef.current.delete(url);
  }

  function attachmentFromBrowserFile(file, source = 'upload') {
    const previewUrl = file && source !== 'paste' && isImageAttachment({ name: file.name, type: file.type })
      ? rememberAttachmentObjectUrl(URL.createObjectURL(file))
      : '';
    return {
      id: `${source}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      name: file?.name || (source === 'paste' ? 'pasted-image.png' : '未命名文件'),
      path: file?.path || '',
      type: file?.type || '',
      sizeBytes: file?.size || 0,
      previewUrl,
      source,
      lastModified: file?.lastModified || null
    };
  }

  function readImageFileAsDataUrl(file) {
    if (!file || !isImageAttachment({ name: file.name, type: file.type }) || file.size > 5 * 1024 * 1024) {
      return Promise.resolve('');
    }
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
      reader.onerror = () => resolve('');
      reader.readAsDataURL(file);
    });
  }

  function appendAttachments(nextItems = []) {
    const normalized = nextItems
      .filter(Boolean)
      .slice(0, MAX_COMPOSER_ATTACHMENTS)
      .map((item) => ({
        ...item,
        status: item.status || 'uploading',
        progress: Number.isFinite(Number(item.progress)) ? Number(item.progress) : 0
      }));
    if (!normalized.length) return;
    setAttachments((items) => {
      const byKey = new Map(items.map((item) => [item.path || `${item.name}:${item.sizeBytes}:${item.type}`, item]));
      for (const item of normalized) {
        byKey.set(item.path || `${item.name}:${item.sizeBytes}:${item.type}`, item);
      }
      return [...byKey.values()].slice(0, MAX_COMPOSER_ATTACHMENTS);
    });
    for (const item of normalized) {
      if (item.status !== 'uploading') continue;
      scheduleStatusTimer(() => {
        setAttachments((items) => items.map((current) => (
          current.id === item.id ? { ...current, status: 'ready', progress: 100 } : current
        )));
      }, 500);
    }
  }

  function removeAttachment(id) {
    setAttachments((items) => {
      const removed = items.find((item) => item.id === id);
      revokeAttachmentObjectUrl(removed?.previewUrl);
      return items.filter((item) => item.id !== id);
    });
  }

  function addProjectFileAttachment(file = {}) {
    if (!activeProject?.id) return;
    appendAttachments([projectFileToAttachment(file, activeProject)]);
  }

  async function removeProjectFileFromChat(file = {}) {
    if (!activeProject?.id || !hasEcorexFunction(['removeProjectFile', 'projects.removeFile'])) return;
    const confirmed = typeof window === 'undefined' || typeof window.confirm !== 'function'
      ? true
      : window.confirm(`从 EcoreX 中删除「${file.name || '项目文件'}」？`);
    if (!confirmed) return;
    const result = await removeManagedProjectFile(activeProject.id, file);
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false) return;
    setProjectFiles((items) => filterVisibleProjectFiles(items).filter((item) => projectFileIdentity(item) !== projectFileIdentity(file)));
    setAttachments((items) => items.filter((item) => projectFileIdentity(item) !== projectFileIdentity(file)));
    window.dispatchEvent?.(new CustomEvent('ecorex:project-files-changed', { detail: { projectId: activeProject.id } }));
  }

  function clearAttachments({ revoke = true } = {}) {
    setAttachments((items) => {
      if (revoke) {
        for (const item of items) revokeAttachmentObjectUrl(item.previewUrl);
      }
      return [];
    });
  }

  async function selectAttachmentFiles() {
    if (window.ecorex?.selectAttachmentFiles) {
      const result = await window.ecorex.selectAttachmentFiles({ limit: MAX_COMPOSER_ATTACHMENTS });
      if (result?.unauthorized) {
        onUnauthorized?.();
        return;
      }
      if (result?.ok !== false && Array.isArray(result?.files)) {
        appendAttachments(result.files.map((file) => ({
          id: file.id || `file-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          name: file.name,
          path: file.path || '',
          type: file.type || '',
          sizeBytes: file.sizeBytes || file.size || 0,
          previewUrl: file.previewDataUrl || '',
          source: 'upload'
        })));
        return;
      }
    }
    fileInputRef.current?.click();
  }

  function handleBrowserFileSelection(files, source = 'upload') {
    appendAttachments(Array.from(files || []).map((file) => attachmentFromBrowserFile(file, source)));
  }

  function handlePastedFiles(files) {
    const entries = Array.from(files || []).map((file) => ({
      file,
      attachment: attachmentFromBrowserFile(file, 'paste')
    }));
    appendAttachments(entries.map((entry) => entry.attachment));
    for (const { file, attachment } of entries) {
      if (!isImageAttachment(attachment)) continue;
      readImageFileAsDataUrl(file).then((previewUrl) => {
        if (!previewUrl) return;
        setAttachments((items) => items.map((item) => (
          item.id === attachment.id ? { ...item, previewUrl } : item
        )));
      });
    }
  }

  function usefulMessageCount(items = messagesRef.current) {
    return (Array.isArray(items) ? items : []).filter((message) =>
      ['user', 'assistant'].includes(message.role) && readableMessageText(message)
    ).length;
  }

  function updateContextSummaryFromCompact(reason, compactEvent = null) {
    const count = usefulMessageCount();
    contextCompactedMessageCountRef.current = count;
    setContextSummary((previous) => buildConversationContextSummary(messagesRef.current, previous, reason, compactEvent));
    setContextCompactedAt(Date.now());
  }

  function mirrorCliContextCompaction(events = []) {
    const compactEvent = [...events].reverse().find(isContextCompactEvent);
    if (compactEvent) updateContextSummaryFromCompact('claude-cli-compact', compactEvent);
  }

  function startFreshConversation(item = {}) {
    conversationIdRef.current = item?.id || createLocalId('conversation');
    conversationSessionIdRef.current = item?.claudeSessionId || item?.sessionId || conversationIdRef.current;
    const nextProject = item?.projectId
      ? { id: item.projectId, name: item.projectName || '项目会话' }
      : null;
    conversationProjectRef.current = nextProject;
    setChatProject(nextProject);
    setConversationId(conversationIdRef.current);
    clearAttachments();
    attachmentObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    attachmentObjectUrlsRef.current.clear();
    const initialAttachments = filterAttachmentsForProjectScope(
      serializeAgentAttachments(item?.initialAttachments || item?.attachments || []),
      nextProject?.id || ''
    );
    if (initialAttachments.length) appendAttachments(initialAttachments);
    setPrompt(item?.initialPrompt || '');
    setComposerReferences([]);
    setFocusArtifact(null);
    setVisibleMessageCount(MESSAGE_WINDOW_SIZE);
    contextSummaryRef.current = '';
    contextCompactedMessageCountRef.current = 0;
    nativeSessionRotatedMessageCountRef.current = 0;
    setContextSummary('');
    setContextCompactedAt(null);
    setMessages(createInitialMessages(authDisplayName));
    setTimeline(initialTimeline);
    syncCurrentSessionForVisibleConversation(conversationIdRef.current);
  }

  function openStoredConversation(item = {}) {
    const nextId = item?.id || item?.conversationId;
    if (!nextId) return;
    const stored = loadConversationState(nextId);
    conversationIdRef.current = nextId;
    conversationSessionIdRef.current = stored?.claudeSessionId || item?.claudeSessionId || item?.sessionId || nextId;
    const nextProjectId = stored?.projectId || item?.projectId || '';
    const nextProject = nextProjectId
      ? { id: nextProjectId, name: stored?.projectName || item?.projectName || '项目会话' }
      : null;
    conversationProjectRef.current = nextProject;
    setChatProject(nextProject);
    setConversationId(nextId);
    clearAttachments();
    attachmentObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    attachmentObjectUrlsRef.current.clear();
    setPrompt('');
    setComposerReferences([]);
    setFocusArtifact(null);
    setVisibleMessageCount(MESSAGE_WINDOW_SIZE);
    const storedSummary = sanitizeContextSummary(stored?.contextSummary || '');
    contextSummaryRef.current = storedSummary;
    setContextSummary(storedSummary);
    setContextCompactedAt(stored?.contextCompactedAt || null);
    nativeSessionRotatedMessageCountRef.current = 0;
    if (stored?.messages?.length) {
      contextCompactedMessageCountRef.current = usefulMessageCount(stored.messages);
      setMessages(stored.messages);
      setTimeline(stored.timeline?.length ? stored.timeline : initialTimeline);
    } else {
      contextCompactedMessageCountRef.current = 0;
      setMessages(createInitialMessages(authDisplayName));
      setTimeline(initialTimeline);
    }
    syncCurrentSessionForVisibleConversation(conversationIdRef.current);
  }

  function syncRecentChatFromPrompt(conversationId, text, projectOverride = null, claudeSessionIdOverride = '') {
    const targetConversationId = String(conversationId || conversationIdRef.current || createLocalId('conversation'));
    const title = sanitizeDisplayText(text, '新会话').replace(/\s+/g, ' ').slice(0, 34);
    const isCurrentConversation = targetConversationId === String(conversationIdRef.current || '');
    const stored = targetConversationId && !isCurrentConversation
      ? loadConversationState(targetConversationId)
      : null;
    const activeProject = projectOverride
      || (stored?.projectId ? { id: stored.projectId, name: stored.projectName || '' } : null)
      || conversationProjectRef.current;
    const targetClaudeSessionId = String(claudeSessionIdOverride || '').trim()
      || stored?.claudeSessionId
      || (isCurrentConversation ? conversationSessionIdRef.current : '')
      || targetConversationId;
    window.dispatchEvent?.(new CustomEvent('ecorex:recent-chat-upsert', {
      detail: {
        id: targetConversationId,
        claudeSessionId: targetClaudeSessionId,
        title: title || '新会话',
        time: recentChatTimeLabel(),
        updatedAt: Date.now(),
        projectId: activeProject?.id || '',
        projectName: activeProject?.name || ''
      }
    }));
  }

  function commitRunningSessionRows(updater) {
    const currentRows = runningSessionRowsRef.current;
    const nextRows = typeof updater === 'function' ? updater(currentRows) : updater;
    const activeRows = (Array.isArray(nextRows) ? nextRows : []).filter(isRunningSessionActive);
    runningSessionRowsRef.current = activeRows;
    setRunningSessions(activeRows);
    syncRunningSessions(activeRows);
    syncCurrentSessionForVisibleConversation(conversationIdRef.current, activeRows);
    return activeRows;
  }

  function syncRunningSessions(rows = runningSessionRowsRef.current) {
    const hasRunningSessions = runningSessionsRef.current.size > 0 || rows.some(isRunningSessionActive);
    runningRef.current = hasRunningSessions;
    setRunning(hasRunningSessions);
  }

  function sessionOwnerForSession(sessionId, fallback = {}) {
    const key = String(sessionId || '');
    if (!key) return null;
    const storedOwner = sessionOwnersRef.current.get(key);
    if (storedOwner) return storedOwner;
    const row = runningSessionRowsRef.current.find((item) => item.sessionId === key || item.id === key);
    const messageId = sessionMap.current.get(key) || row?.messageId || fallback.messageId || '';
    const owner = {
      sessionId: key,
      messageId,
      conversationId: String(row?.conversationId || fallback.conversationId || conversationIdRef.current || ''),
      claudeSessionId: String(row?.claudeSessionId || fallback.claudeSessionId || conversationSessionIdRef.current || ''),
      projectId: String(row?.projectId || fallback.projectId || conversationProjectRef.current?.id || ''),
      projectName: String(row?.projectName || fallback.projectName || conversationProjectRef.current?.name || '')
    };
    sessionOwnersRef.current.set(key, owner);
    return owner;
  }

  function hasRunningSessionForConversation(activeConversationId) {
    const targetId = String(activeConversationId || '');
    if (!targetId) return runningSessionsRef.current.size > 0 || runningSessionRowsRef.current.some(isRunningSessionActive);
    if (runningSessionRowsRef.current.some((row) => {
      if (!isRunningSessionActive(row)) return false;
      const owner = sessionOwnerForSession(row.sessionId || row.id, row);
      return String(owner?.conversationId || '') === targetId;
    })) {
      return true;
    }
    return Array.from(runningSessionsRef.current).some((sessionId) => {
      const owner = sessionOwnerForSession(sessionId);
      return String(owner?.conversationId || '') === targetId;
    });
  }

  function runningSessionIdForConversation(activeConversationId, rows = runningSessionRowsRef.current) {
    const targetId = String(activeConversationId || '');
    if (!targetId) return null;
    const row = (Array.isArray(rows) ? rows : []).find((item) => {
      if (!isRunningSessionActive(item)) return false;
      const owner = sessionOwnerForSession(item.sessionId || item.id, item);
      return String(owner?.conversationId || '') === targetId;
    });
    if (row?.sessionId || row?.id) return row.sessionId || row.id;
    for (const sessionId of Array.from(runningSessionsRef.current)) {
      const owner = sessionOwnersRef.current.get(sessionId);
      if (String(owner?.conversationId || '') === targetId) return sessionId;
    }
    return null;
  }

  function syncCurrentSessionForVisibleConversation(activeConversationId = conversationIdRef.current, rows = runningSessionRowsRef.current) {
    const nextSessionId = runningSessionIdForConversation(activeConversationId, rows);
    currentSessionIdRef.current = nextSessionId;
    setCurrentSessionId(nextSessionId);
    return nextSessionId;
  }

  function updateMessagesForConversation(ownerConversationId, updater) {
    if (!ownerConversationId) return;
    if (String(ownerConversationId) === String(conversationIdRef.current)) {
      setMessages((previousMessages) => {
        const nextMessages = typeof updater === 'function' ? updater(previousMessages) : updater;
        return sanitizeLiveMessages(Array.isArray(nextMessages) ? nextMessages : previousMessages);
      });
      return;
    }
    const stored = loadConversationState(ownerConversationId) || storedEventsByConversation.current.get(ownerConversationId) || {};
    const previousMessages = Array.isArray(stored.messages) ? stored.messages : [];
    const nextMessages = typeof updater === 'function' ? updater(previousMessages) : updater;
    const projectId = String(stored.projectId || '').trim();
    const projectName = String(stored.projectName || '').trim();
    const nextState = {
      ...stored,
      messages: sanitizeStoredMessages(Array.isArray(nextMessages) ? nextMessages : previousMessages, { projectId, projectName }),
      timeline: sanitizeStoredTimeline(stored.timeline || []),
      updatedAt: Date.now()
    };
    storedEventsByConversation.current.set(ownerConversationId, nextState);
    saveConversationState(ownerConversationId, nextState);
  }

  function updateTimelineForConversation(ownerConversationId, updater) {
    if (!ownerConversationId) return;
    if (String(ownerConversationId) === String(conversationIdRef.current)) {
      setTimeline(updater);
      return;
    }
    const stored = loadConversationState(ownerConversationId) || storedEventsByConversation.current.get(ownerConversationId) || {};
    const previousTimeline = Array.isArray(stored.timeline) ? stored.timeline : [];
    const nextTimeline = typeof updater === 'function' ? updater(previousTimeline) : updater;
    const projectId = String(stored.projectId || '').trim();
    const projectName = String(stored.projectName || '').trim();
    const nextState = {
      ...stored,
      messages: sanitizeStoredMessages(stored.messages || [], { projectId, projectName }),
      timeline: sanitizeStoredTimeline(Array.isArray(nextTimeline) ? nextTimeline : previousTimeline),
      updatedAt: Date.now()
    };
    storedEventsByConversation.current.set(ownerConversationId, nextState);
    saveConversationState(ownerConversationId, nextState);
  }

  function trackSession(sessionId, meta = {}) {
    if (!sessionId) return;
    const owner = {
      sessionId,
      messageId: meta.messageId || sessionMap.current.get(sessionId) || '',
      conversationId: String(meta.conversationId || conversationIdRef.current || ''),
      claudeSessionId: String(meta.claudeSessionId || conversationSessionIdRef.current || conversationIdRef.current || ''),
      projectId: String(meta.projectId || conversationProjectRef.current?.id || ''),
      projectName: String(meta.projectName || conversationProjectRef.current?.name || '')
    };
    sessionOwnersRef.current.set(sessionId, owner);
    runningSessionsRef.current.add(sessionId);
    if (String(owner.conversationId || '') === String(conversationIdRef.current || '')) {
      currentSessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);
    }
    commitRunningSessionRows((rows) => mergeRunningSessionRows(rows, [
      normalizeRunningSession({
        sessionId,
        id: sessionId,
        conversationId: owner.conversationId,
        claudeSessionId: owner.claudeSessionId,
        projectId: owner.projectId,
        projectName: owner.projectName,
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
    const previousOwner = sessionOwnersRef.current.get(previousSessionId);
    if (previousOwner) {
      sessionOwnersRef.current.delete(previousSessionId);
      sessionOwnersRef.current.set(nextSessionId, { ...previousOwner, sessionId: nextSessionId });
    }
    runningSessionsRef.current.delete(previousSessionId);
    runningSessionsRef.current.add(nextSessionId);
    const messageId = sessionMap.current.get(previousSessionId);
    if (messageId) {
      sessionMap.current.delete(previousSessionId);
      sessionMap.current.set(nextSessionId, messageId);
    }
    if (
      currentSessionIdRef.current === previousSessionId
      || String(previousOwner?.conversationId || '') === String(conversationIdRef.current || '')
    ) {
      currentSessionIdRef.current = nextSessionId;
      setCurrentSessionId(nextSessionId);
    }
    commitRunningSessionRows((rows) => rows.map((row) => (
      row.sessionId === previousSessionId
        ? {
            ...row,
            sessionId: nextSessionId,
            id: nextSessionId,
            conversationId: previousOwner?.conversationId || row.conversationId,
            claudeSessionId: previousOwner?.claudeSessionId || row.claudeSessionId,
            messageId: row.messageId || messageId,
            updatedAt: Date.now()
          }
        : row
    )));
  }

  function finishSession(sessionId) {
    const owner = sessionOwnerForSession(sessionId);
    runningSessionsRef.current.delete(sessionId);
    sessionMap.current.delete(sessionId);
    sessionOwnersRef.current.delete(sessionId);
    const nextRows = commitRunningSessionRows((rows) => rows.filter((row) => row.sessionId !== sessionId));
    if (currentSessionIdRef.current === sessionId) {
      syncCurrentSessionForVisibleConversation(conversationIdRef.current, nextRows);
    }
    if (owner?.conversationId && !hasRunningSessionForConversation(owner.conversationId) && pendingFollowUpsRef.current.some((item) => item.conversationId === owner.conversationId)) {
      scheduleStatusTimer(() => flushQueuedFollowUps(owner.conversationId), 80);
    }
    if (!runningSessionsRef.current.size && !nextRows.length && pendingFollowUpsRef.current.length) {
      scheduleStatusTimer(() => flushQueuedFollowUps(), 80);
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
      } else if (now - (event.__queuedAt || now) < PENDING_AGENT_EVENT_TTL_MS) {
        pendingEvents.push({ ...event, __queuedAt: event.__queuedAt || now });
      }
    }

    if (pendingEvents.length) {
      eventQueueRef.current = compactAgentEventQueue([...eventQueueRef.current, ...pendingEvents]);
    }

    if (!relevantEvents.length) return pendingEvents.length > 0;
    mirrorCliContextCompaction(relevantEvents);

    const eventsByConversation = new Map();
    const deferredEvents = [];
    for (const event of relevantEvents) {
      const messageId = sessionMap.current.get(event.sessionId);
      if (!messageId) continue;
      const owner = sessionOwnerForSession(event.sessionId, { messageId });
      const ownerConversationId = owner?.conversationId || conversationIdRef.current;
      const ownerMessages = String(ownerConversationId) === String(conversationIdRef.current)
        ? messagesRef.current
        : (loadConversationState(ownerConversationId)?.messages || storedEventsByConversation.current.get(ownerConversationId)?.messages || []);
      if (!ownerMessages.some((message) => message.id === messageId)) {
        if (now - (event.__queuedAt || now) < PENDING_AGENT_EVENT_TTL_MS) {
          deferredEvents.push({ ...event, __queuedAt: event.__queuedAt || now });
        }
        continue;
      }
      const conversationEvents = eventsByConversation.get(ownerConversationId) || [];
      conversationEvents.push(event);
      eventsByConversation.set(ownerConversationId, conversationEvents);
    }

    if (deferredEvents.length) {
      eventQueueRef.current = compactAgentEventQueue([...eventQueueRef.current, ...deferredEvents]);
    }

    for (const [ownerConversationId, ownerEvents] of eventsByConversation.entries()) {
      const eventsByMessage = new Map();
      for (const event of ownerEvents) {
        const messageId = sessionMap.current.get(event.sessionId);
        if (!messageId) continue;
      const items = eventsByMessage.get(messageId) || [];
      items.push(event);
      eventsByMessage.set(messageId, items);
      }

    commitRunningSessionRows((rows) => mergeRunningSessionRows(
      rows,
        ownerEvents
        .filter((event) => !AGENT_EVENT_TERMINAL_KINDS.has(event.kind))
        .map((event, index) => normalizeRunningSession({
          sessionId: event.sessionId,
          status: event.kind === 'error' ? 'error' : 'running',
          state: event.kind,
          prompt: rows.find((row) => row.sessionId === event.sessionId)?.prompt,
          conversationId: ownerConversationId,
          messageId: sessionMap.current.get(event.sessionId),
          updatedAt: Date.now()
        }, index, 'local'))
    ));

      updateMessagesForConversation(ownerConversationId, (items) => {
      return items.map((item) => {
        const messageEvents = eventsByMessage.get(item.id);
        if (!messageEvents?.length) return item;
        const terminalEvent = [...messageEvents].reverse().find((event) =>
          event.kind === 'result' || AGENT_EVENT_TERMINAL_KINDS.has(event.kind)
        );
        const timelineItems = compactTimelineEvents(messageEvents).map((event) => timelineItemFromAgentEvent(event));
        const ledgerItems = messageEvents.flatMap((event) => normalizeToolLedgerItemsFromEvent(event));
        const attachmentIngestItems = messageEvents.flatMap((event) => normalizeAttachmentIngestItemsFromEvent(event));
        const recoveryEvent = [...messageEvents].reverse().map(recoveryStateFromAgentEvent).find(Boolean);
        const hasNormalProgressEvent = messageEvents.some((event) => (
          ['status', 'tool', 'ledger', 'attachment', 'assistant', 'result', 'done'].includes(event.kind)
          && !event.recovery
          && !event.recoveryStatus
        ));
        let nextItem = {
          ...item,
          timeline: appendTimelineItems(item.timeline || [], timelineItems),
          ledger: appendToolLedgerItems(item.ledger || [], ledgerItems),
          attachmentIngest: mergeAttachmentIngestItems(item.attachmentIngest || [], attachmentIngestItems),
          recovery: recoveryEvent || (hasNormalProgressEvent ? null : item.recovery),
          silentRecovery: hasNormalProgressEvent ? false : item.silentRecovery
        };

        for (const event of messageEvents) {
          if (['status', 'tool', 'ledger', 'attachment', 'recovery', 'stderr', 'debug'].includes(event.kind)) {
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
              text: mergeAssistantOutputText(nextItem.text, event.text),
              streaming: true,
              status: 'generating'
            };
            continue;
          }

          if (event.kind === 'result') {
            const finalArtifacts = finalArtifactsFromText([nextItem.text, event.text].filter(Boolean).join('\n'));
            nextItem = {
              ...nextItem,
              text: finalizeAssistantOutputText(nextItem.text, event.text, { preserveArtifactLabels: true }),
              finalArtifacts: mergeArtifactReferences([...(nextItem.finalArtifacts || []), ...finalArtifacts]),
              streaming: false,
              status: 'complete',
              meta: '',
              recovery: null,
              silentRecovery: false
            };
            continue;
          }

          if (event.kind === 'done') {
            const finalArtifacts = finalArtifactsFromText([nextItem.text, event.text].filter(Boolean).join('\n'));
            const terminalText = isGenericTerminalText(event.text) ? '' : cleanResultOutputText(event.text);
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'complete',
              error: false,
              text: cleanAssistantOutputText(nextItem.text) || terminalText,
              finalArtifacts: finalArtifacts.length
                ? mergeArtifactReferences([...(nextItem.finalArtifacts || []), ...finalArtifacts])
                : nextItem.finalArtifacts,
              recovery: null,
              silentRecovery: false
            };
            continue;
          }

          if (event.kind === 'cancelled') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'cancelled',
              error: false,
              text: cleanAssistantOutputText(nextItem.text || event.text || agentRecoveryText(event))
            };
            continue;
          }

          if (event.kind === 'timeout') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'timeout',
              error: true,
              text: cleanAssistantOutputText(nextItem.text || event.text || agentRecoveryText(event))
            };
            continue;
          }

          if (event.kind === 'error') {
            nextItem = {
              ...nextItem,
              streaming: false,
              status: 'error',
              error: true,
              text: cleanAssistantOutputText(nextItem.text || event.text || agentRecoveryText(event))
            };
          }
        }

        if (terminalEvent && !cleanAssistantOutputText(nextItem.text || '').trim()) {
          const terminalText = terminalEvent.kind === 'result'
            ? finalizeAssistantOutputText('', terminalEvent.text, { preserveArtifactLabels: true })
            : isGenericTerminalText(terminalEvent.text)
              ? ''
              : cleanResultOutputText(terminalEvent.text);
          if (terminalText.trim()) {
            const finalArtifacts = finalArtifactsFromText(terminalText);
            nextItem = {
              ...nextItem,
              text: terminalText,
              finalArtifacts: finalArtifacts.length
                ? mergeArtifactReferences([...(nextItem.finalArtifacts || []), ...finalArtifacts])
                : nextItem.finalArtifacts,
              streaming: false,
              status: terminalEvent.kind === 'error' ? 'error' : 'complete',
              error: terminalEvent.kind === 'error',
              recovery: terminalEvent.kind === 'error' ? nextItem.recovery : null,
              silentRecovery: terminalEvent.kind === 'error' ? nextItem.silentRecovery : false
            };
          }
        }

        if (nextItem.permissionDecision?.status === 'running' && terminalEvent) {
          permissionContinuationLocksRef.current.delete(item.id);
          nextItem = {
            ...nextItem,
            permissionDecision: {
              ...nextItem.permissionDecision,
              status: ['done', 'result'].includes(terminalEvent.kind) ? 'complete' : terminalEvent.kind,
              at: Date.now()
            }
          };
        }

        return nextItem;
      });
    });

      updateTimelineForConversation(ownerConversationId, (items) => appendTimelineItems(
        items,
        compactTimelineEvents(ownerEvents).map((event) => timelineItemFromAgentEvent(event))
      ));
    }

    relevantEvents
      .filter((event) => AGENT_EVENT_TERMINAL_KINDS.has(event.kind))
      .filter((event) => !deferredEvents.some((deferred) => deferred.__seq === event.__seq))
      .forEach((event) => finishSession(event.sessionId));

    return pendingEvents.length > 0 || deferredEvents.length > 0;
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

  function findStoredSessionOwner(row = {}) {
    const sessionId = String(row.sessionId || row.id || '').trim();
    const claudeSessionId = String(row.claudeSessionId || '').trim();
    const conversationIdHint = String(row.conversationId || '').trim();
    const messageIdHint = String(row.messageId || '').trim();
    const matchesMessage = (message = {}) => (
      Boolean(messageIdHint && message.id === messageIdHint)
      || Boolean(sessionId && message.sessionId === sessionId)
      || Boolean(claudeSessionId && message.claudeSessionId === claudeSessionId)
    );
    const currentMessage = messagesRef.current.find(matchesMessage);
    if (currentMessage) {
      return {
        conversationId: conversationIdRef.current,
        messageId: currentMessage.id,
        projectId: conversationProjectRef.current?.id || currentMessage.projectId || row.projectId || '',
        projectName: conversationProjectRef.current?.name || currentMessage.projectName || row.projectName || ''
      };
    }

    const conversations = loadConversationMap();
    const orderedIds = conversationIdHint
      ? [conversationIdHint, ...Object.keys(conversations).filter((id) => id !== conversationIdHint)]
      : Object.keys(conversations);
    for (const id of orderedIds) {
      const conversation = conversations[id];
      if (!conversation || typeof conversation !== 'object') continue;
      const message = (conversation.messages || []).find(matchesMessage);
      if (!message && id !== conversationIdHint) continue;
      return {
        conversationId: id,
        messageId: message?.id || messageIdHint,
        projectId: conversation.projectId || message?.projectId || row.projectId || '',
        projectName: conversation.projectName || message?.projectName || row.projectName || ''
      };
    }
    return null;
  }

  function appendRecoveredSessionMessage(ownerConversationId, message = {}, owner = {}) {
    if (!ownerConversationId || !message?.id) return;
    const projectId = String(owner.projectId || '').trim();
    const projectName = String(owner.projectName || '').trim();
    if (String(ownerConversationId) === String(conversationIdRef.current)) {
      setMessages((items) => {
        if (items.some((item) => item.id === message.id)) return items;
        return sanitizeLiveMessages([...items, message]);
      });
      return;
    }
    const stored = loadConversationState(ownerConversationId) || {};
    saveConversationState(ownerConversationId, {
      ...stored,
      projectId: projectId || stored.projectId || '',
      projectName: projectName || stored.projectName || ''
    });
    updateMessagesForConversation(ownerConversationId, (items) => (
      items.some((item) => item.id === message.id) ? items : [...items, message]
    ));
  }

  async function refreshAgentSessions() {
    if (!window.ecorex?.getAgentSessions) return;
    const cached = window.__ecorexStartupCache?.agentSessions;
    const result = cached || await callEcorex(['getAgentSessions', 'agent.getSessions']);
    if (cached) delete window.__ecorexStartupCache.agentSessions;
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false && result.missing) return;
    if (result?.ok === false) return;

    const backendRows = extractAgentSessionRows(result).map((row, index) => {
      const sessionId = row.sessionId || row.id;
      const storedOwner = findStoredSessionOwner(row);
      const ownerConversationId = row.conversationId || storedOwner?.conversationId || conversationIdRef.current;
      const ownerProjectId = row.projectId || storedOwner?.projectId || '';
      const ownerProjectName = row.projectName || storedOwner?.projectName || '';
      let messageId = row.messageId || storedOwner?.messageId || sessionMap.current.get(sessionId);
      const ownerMessages = String(ownerConversationId) === String(conversationIdRef.current)
        ? messagesRef.current
        : (loadConversationState(ownerConversationId)?.messages || []);
      const ownerHasMessage = Boolean(messageId && ownerMessages.some((message) => message.id === messageId));
      if (sessionId && (!messageId || !ownerHasMessage)) {
        const active = isRunningSessionActive(row);
        const recovery = recoveryStateFromSession(row);
        messageId = messageId || `assistant-resumed-${sessionId}`;
        sessionMap.current.set(sessionId, messageId);
        appendRecoveredSessionMessage(ownerConversationId, {
          id: messageId,
          role: 'assistant',
          text: '',
          time: formatAgentEventTime(row),
          streaming: active,
          status: active ? (row.status === 'error' ? 'error' : 'thinking') : (recovery.state === 'retryable' ? 'error' : 'cancelled'),
          sessionId,
          claudeSessionId: row.claudeSessionId || '',
          projectId: ownerProjectId || '',
          projectName: ownerProjectName || '',
          silentRecovery: true,
          originalPrompt: row.prompt || row.promptPreview || '',
          recovery,
          timeline: [[
            `恢复运行会话 ${index + 1}`,
            formatSessionStatus(row.status || row.state || 'running'),
            formatAgentEventTime(row),
            recovery.tone || (row.status === 'error' ? 'danger' : 'running')
          ]]
        }, { projectId: ownerProjectId, projectName: ownerProjectName });
      } else if (sessionId && messageId) {
        sessionMap.current.set(sessionId, messageId);
      }
      if (sessionId) {
        sessionOwnersRef.current.set(sessionId, {
          sessionId,
          messageId: messageId || '',
          conversationId: ownerConversationId,
          claudeSessionId: row.claudeSessionId || '',
          projectId: ownerProjectId || '',
          projectName: ownerProjectName || ''
        });
      }
      if (sessionId && messageId && ownerHasMessage && !isRunningSessionActive(row) && row.recoverable) {
        const recovery = recoveryStateFromSession(row);
        updateMessagesForConversation(ownerConversationId, (items) => items.map((message) => (
          message.id === messageId
            ? {
                ...message,
                streaming: false,
                status: message.status === 'complete' ? 'interrupted' : (message.status || 'interrupted'),
                recovery: message.recovery || recovery,
                originalPrompt: message.originalPrompt || row.prompt || row.promptPreview || ''
              }
            : message
        )));
      }
      return {
        ...row,
        source: 'api',
        conversationId: ownerConversationId,
        messageId,
        projectId: ownerProjectId,
        projectName: ownerProjectName
      };
    });
    const nextRows = commitRunningSessionRows((rows) => mergeRunningSessionRows(rows, backendRows, { replaceSources: ['api'] }));
    syncCurrentSessionForVisibleConversation(conversationIdRef.current, nextRows);
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
      const cached = window.__ecorexStartupCache?.settings;
      if (cached && !cancelled) {
        const settings = cached.settings || cached;
        const nextAccessMode = normalizeAccessMode(settings?.defaultPermissionMode || settings?.permissionMode || settings?.accessMode);
        storeDefaultAccessMode(nextAccessMode);
        setPermissionMode(nextAccessMode);
      }
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
      const cached = window.__ecorexStartupCache?.modelProfiles;
      if (cached && !cancelled) {
        const normalized = normalizeModelProfileStore({
          profiles: extractCollection(cached, ['profiles', 'modelProfiles', 'items']),
          activeId: cached.activeId || cached.currentId || cached.activeProfileId || cached.currentProfile?.id || cached.current?.id
        }, model || 'sonnet');
        const currentCached = getCurrentModelProfile(normalized.profiles);
        setModelProfiles(normalized.profiles);
        if (currentCached?.modelName) setModel(currentCached.modelName);
        delete window.__ecorexStartupCache.modelProfiles;
      }
      const result = await loadModelProfiles(model || DEFAULT_AGENT_MODEL_NAME);
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
    clearTimeout(conversationSaveTimerRef.current);
    conversationSaveTimerRef.current = null;
    flushConversationSave();
    attachmentObjectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    attachmentObjectUrlsRef.current.clear();
  }, []);

  useEffect(() => {
    conversationIdRef.current = conversationId;
    if (!conversationSessionIdRef.current) conversationSessionIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    messagesRef.current = messages;
    const count = usefulMessageCount(messages);
    if (
      count >= CONTEXT_COMPACT_TRIGGER_MESSAGES &&
      count - contextCompactedMessageCountRef.current >= MESSAGE_WINDOW_STEP
    ) {
      contextCompactedMessageCountRef.current = count;
      setContextSummary((previous) => buildConversationContextSummary(messages, previous, 'ecorex-front-auto'));
      setContextCompactedAt(Date.now());
    }
  }, [messages]);

  useEffect(() => {
    contextSummaryRef.current = sanitizeContextSummary(contextSummary);
  }, [contextSummary]);

  useEffect(() => {
    if (conversationSaveSnapshotRef.current?.id && conversationSaveSnapshotRef.current.id !== conversationId) {
      clearTimeout(conversationSaveTimerRef.current);
      conversationSaveTimerRef.current = null;
      flushConversationSave();
    }
    conversationSaveSnapshotRef.current = {
      id: conversationId,
      state: {
        claudeSessionId: conversationSessionIdRef.current || conversationId,
        projectId: conversationProjectRef.current?.id || '',
        projectName: conversationProjectRef.current?.name || '',
        contextSummary,
        contextCompactedAt,
        messages,
        timeline,
        updatedAt: Date.now()
      }
    };
    if (!conversationSaveTimerRef.current) {
      conversationSaveTimerRef.current = window.setTimeout(() => {
        conversationSaveTimerRef.current = null;
        flushConversationSave();
      }, CONVERSATION_SAVE_DEBOUNCE_MS);
    }
  }, [conversationId, activeProject, contextSummary, contextCompactedAt, messages, timeline]);

  useEffect(() => {
    const node = messageListRef.current;
    if (!node) return undefined;
    updateMessageAutoScrollPin();
    node.addEventListener('scroll', updateMessageAutoScrollPin, { passive: true });
    return () => node.removeEventListener('scroll', updateMessageAutoScrollPin);
  }, []);

  useEffect(() => {
    if (messages.length <= 2 || autoScrollPinnedRef.current) {
      scrollMessagesToLatest(messages.length <= 2 ? 'auto' : 'smooth');
    } else {
      setShowJumpLatest(true);
    }
  }, [messages, running]);

  useEffect(() => {
    const listener = (event) => startFreshConversation(event.detail || {});
    window.addEventListener?.('ecorex:new-chat', listener);
    return () => window.removeEventListener?.('ecorex:new-chat', listener);
  }, [authDisplayName]);

  useEffect(() => {
    const listener = (event) => openStoredConversation(event.detail || {});
    window.addEventListener?.('ecorex:open-chat', listener);
    return () => window.removeEventListener?.('ecorex:open-chat', listener);
  }, [authDisplayName]);

  useEffect(() => {
    const listener = (event) => {
      const project = event.detail?.project;
      if (!project?.id) return;
      const nextProject = { id: project.id, name: project.name || '项目会话' };
      conversationProjectRef.current = nextProject;
      setChatProject(nextProject);
    };
    window.addEventListener?.('ecorex:project-context', listener);
    return () => window.removeEventListener?.('ecorex:project-context', listener);
  }, []);

  function scheduleMessageStatus(id, status, delay, ownerConversationId = conversationIdRef.current) {
    scheduleStatusTimer(() => {
      updateMessagesForConversation(ownerConversationId, (items) =>
        items.map((item) => (item.id === id ? { ...item, status } : item))
      );
    }, delay);
  }

  function appendAssistantTimelineItem(assistantId, item, { revealTrace = true } = {}) {
    const current = messagesRef.current.find((message) => message.id === assistantId);
    if (!current?.streaming) return;
    const exists = (current.timeline || []).some((entry) => entry?.[0] === item?.[0] && entry?.[4] === item?.[4]);
    if (exists) return;
    setMessages((items) =>
      items.map((message) =>
        message.id === assistantId && message.streaming
          ? {
              ...message,
              showTrace: revealTrace || message.showTrace,
              status: message.status === 'generating' ? 'generating' : 'thinking',
              timeline: appendTimeline(message.timeline || [], item)
            }
          : message
      )
    );
    setTimeline((items) => appendTimeline(items, item));
  }

  function scheduleAssistantProgressDisclosure(assistantId, promptText, attachmentList = []) {
    const profile = promptDisclosureProfile(promptText, attachmentList);
    const steps = buildDisclosureProgressSteps(promptText, attachmentList);
    steps.forEach(([label, status, kind], index) => {
      const delay = AGENT_DISCLOSURE_DELAYS_MS[index] || (AGENT_DISCLOSURE_DELAYS_MS.at(-1) + index * 12000);
      scheduleStatusTimer(() => {
        const current = messagesRef.current.find((message) => message.id === assistantId);
        if (!current?.streaming) return;
        const hasVisibleAnswer = cleanAssistantOutputText(current.text || '').trim().length > 24;
        if (profile === 'simple' && hasVisibleAnswer) return;
        appendAssistantTimelineItem(
          assistantId,
          [label, status, formatAgentEventTime(), 'running', kind || 'status'],
          { revealTrace: profile !== 'simple' }
        );
      }, delay);
    });
  }

  function enqueueFollowUpPrompt(cleanPrompt, cleanAttachments = []) {
    const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const userId = `user-queued-${Date.now()}`;
    const safePrompt = clampLongText(cleanPrompt, MAX_PENDING_FOLLOWUP_CHARS);
    const cleanReferences = sanitizeComposerReferences(composerReferences);
    const item = {
      id: userId,
      conversationId: conversationIdRef.current,
      claudeSessionId: conversationSessionIdRef.current || conversationIdRef.current,
      prompt: safePrompt,
      attachments: cleanAttachments,
      references: cleanReferences,
      createdAt: Date.now(),
      time: now
    };
    pendingFollowUpsRef.current = [...pendingFollowUpsRef.current, item].slice(-MAX_PENDING_FOLLOWUPS);
    syncRecentChatFromPrompt(item.conversationId, safePrompt || cleanReferences[0]?.name || cleanAttachments[0]?.name || '追加消息');
    updateMessagesForConversation(item.conversationId, (items) => [
      ...items,
      {
        id: userId,
        role: 'user',
        text: safePrompt || (cleanReferences.length ? '已选中文件片段，请继续修改。' : '已添加附件，请继续分析。'),
        time: now,
        status: 'queued',
        attachments: cleanAttachments,
        references: cleanReferences
      }
    ]);
    setTimeline((items) => appendTimeline(items, ['已追加用户消息', '当前任务结束后继续处理', formatAgentEventTime(), 'pending']));
    setPrompt('');
    setComposerReferences([]);
    clearAttachments({ revoke: false });
  }

  function buildQueuedFollowUpPrompt(items = []) {
    if (items.length === 1) {
      const item = items[0];
      return item.prompt || (item.references?.length ? '请继续处理刚才追加的文件定位。' : '请继续处理刚才追加的附件。');
    }
    return [
      '以下是用户在上一轮运行中追加的消息，请在同一会话中合并处理：',
      ...items.map((item, index) => [
        '',
        `追加 ${index + 1}（${item.time || formatAgentEventTime()}）：`,
        item.prompt || (item.references?.length ? '请继续处理这条追加消息中的文件定位。' : item.attachments?.length ? '请继续处理这条追加消息中的附件。' : '')
      ].filter(Boolean).join('\n'))
    ].join('\n');
  }

  async function flushQueuedFollowUps(targetConversationId = conversationIdRef.current) {
    if (followUpFlushInFlightRef.current) return;
    const activeConversationId = targetConversationId || conversationIdRef.current;
    if (hasRunningSessionForConversation(activeConversationId)) return;
    const currentConversationId = activeConversationId;
    const readyItems = pendingFollowUpsRef.current.filter((item) => item.conversationId === currentConversationId);
    if (!readyItems.length) return;
    pendingFollowUpsRef.current = pendingFollowUpsRef.current.filter((item) => item.conversationId !== currentConversationId);
    followUpFlushInFlightRef.current = true;
    const queuedMessageIds = readyItems.map((item) => item.id);
    const queuedClaudeSessionId = readyItems[0]?.claudeSessionId || conversationSessionIdRef.current || currentConversationId;
    const mergedPrompt = buildQueuedFollowUpPrompt(readyItems);
    const mergedAttachments = readyItems.flatMap((item) => item.attachments || []);
    const mergedReferences = readyItems.flatMap((item) => item.references || []);
    try {
      await sendPrompt(mergedPrompt, mergedAttachments, {
        forceRun: true,
        fromQueue: true,
        queuedMessageIds,
        queuedConversationId: currentConversationId,
        queuedClaudeSessionId,
        queuedReferences: mergedReferences
      });
    } finally {
      followUpFlushInFlightRef.current = false;
      if (!runningRef.current && pendingFollowUpsRef.current.some((item) => item.conversationId === conversationIdRef.current)) {
        scheduleStatusTimer(() => flushQueuedFollowUps(), 80);
      }
    }
  }

  async function sendPrompt(text = prompt, attachmentList = attachments, options = {}) {
    const {
      forceRun = false,
      fromQueue = false,
      queuedMessageIds = [],
      queuedClaudeSessionId = null,
      queuedConversationId = null,
      queuedReferences = null
    } = options || {};
    const activeConversationId = String(fromQueue && queuedConversationId ? queuedConversationId : conversationIdRef.current);
    const storedConversationForRun = activeConversationId !== String(conversationIdRef.current)
      ? (loadConversationState(activeConversationId) || storedEventsByConversation.current.get(activeConversationId) || {})
      : null;
    const activeProject = storedConversationForRun?.projectId
      ? { id: storedConversationForRun.projectId, name: storedConversationForRun.projectName || '' }
      : conversationProjectRef.current;
    const cleanPrompt = clampComposerPromptText(text).trim();
    const cleanAttachments = filterAttachmentsForProjectScope(serializeAgentAttachments(attachmentList), activeProject?.id || '');
    const cleanReferences = sanitizeComposerReferences(Array.isArray(queuedReferences) ? queuedReferences : composerReferences);
    if (!cleanPrompt && !cleanAttachments.length && !cleanReferences.length) return;
    if (hasRunningSessionForConversation(activeConversationId) && !forceRun) {
      enqueueFollowUpPrompt(cleanPrompt, cleanAttachments);
      return;
    }
    const attachmentSection = attachmentPromptSection(cleanAttachments);
    const referenceSection = buildComposerReferenceSection(cleanReferences);
    const nativeDesktop = Boolean(window.ecorex);
    const queuedMessageIdSet = new Set(queuedMessageIds);
    const useQueuedMessages = queuedMessageIdSet.size > 0;
    let nextClaudeSessionId = queuedClaudeSessionId
      || storedConversationForRun?.claudeSessionId
      || (activeConversationId === String(conversationIdRef.current) ? conversationSessionIdRef.current : activeConversationId)
      || activeConversationId;
    let contextSection = '';
    let compactedForNativeSession = false;
    if (nativeDesktop && !fromQueue && shouldRotateNativeClaudeSession(messagesRef.current, nativeSessionRotatedMessageCountRef.current)) {
      const compactSummary = sanitizeContextSummary(contextSummaryRef.current)
        || buildConversationContextSummary(messagesRef.current, '', 'ecorex-fast-compact');
      contextSummaryRef.current = compactSummary;
      contextCompactedMessageCountRef.current = usefulMessageCount(messagesRef.current);
      nativeSessionRotatedMessageCountRef.current = contextCompactedMessageCountRef.current;
      nextClaudeSessionId = createLocalId('claude-compact');
      conversationSessionIdRef.current = nextClaudeSessionId;
      contextSection = conversationContextSection(messagesRef.current, cleanPrompt, compactSummary);
      compactedForNativeSession = true;
      setContextSummary(compactSummary);
      setContextCompactedAt(Date.now());
    } else if (queuedClaudeSessionId && activeConversationId === String(conversationIdRef.current)) {
      conversationSessionIdRef.current = queuedClaudeSessionId;
    } else if (!nativeDesktop) {
      contextSection = conversationContextSection(messagesRef.current, cleanPrompt, contextSummaryRef.current);
    }
    const currentUserTask = `用户当前输入：${cleanPrompt || (cleanReferences.length ? '请根据我选中的文件片段继续修改。' : '请分析这些附件，并给出可执行建议。')}`;
    const promptForAgent = [contextSection, agentRunPolicySection(cleanPrompt), currentUserTask, referenceSection, attachmentSection]
      .filter(Boolean)
      .join('\n\n');

    const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const userId = `user-${Date.now()}`;
    const assistantId = `assistant-${Date.now()}`;
    const requestedSessionId = createLocalId('session');
    const initialDisclosureTimeline = buildImmediateTimeline(cleanPrompt, cleanAttachments, now);
    const accessMode = normalizeAccessMode(permissionMode);
    const requestedPermissionMode = permissionModeFromAccessMode(accessMode);
    autoScrollPinnedRef.current = true;
    setShowJumpLatest(false);
    sessionMap.current.set(requestedSessionId, assistantId);
    syncRecentChatFromPrompt(activeConversationId, cleanPrompt || cleanReferences[0]?.name || cleanAttachments[0]?.name || '新会话', activeProject, nextClaudeSessionId);
    trackSession(requestedSessionId, {
      messageId: assistantId,
      conversationId: activeConversationId,
      claudeSessionId: nextClaudeSessionId,
      projectId: activeProject?.id || '',
      projectName: activeProject?.name || '',
      prompt: promptForAgent,
      accessMode,
      source: 'local',
      compactedForNativeSession
    });

    updateMessagesForConversation(activeConversationId, (items) => [
      ...items.map((item) => (queuedMessageIdSet.has(item.id) ? { ...item, status: 'sending' } : item)),
      ...(!useQueuedMessages ? [
      {
        id: userId,
        role: 'user',
        text: cleanPrompt || (cleanReferences.length ? '已选中文件片段，请继续修改。' : '已添加附件，请分析。'),
        time: now,
        status: 'sending',
        attachments: cleanAttachments,
        references: cleanReferences
      }] : []),
      {
        id: assistantId,
        role: 'assistant',
        text: '',
        time: now,
        streaming: true,
        status: 'thinking',
        sessionId: requestedSessionId,
        claudeSessionId: nextClaudeSessionId,
        projectId: activeProject?.id || '',
        projectName: activeProject?.name || '',
        originalPrompt: cleanPrompt,
        showTrace: promptDisclosureProfile(cleanPrompt, cleanAttachments) !== 'simple',
        timeline: initialDisclosureTimeline
      }
    ]);
    if (!fromQueue) {
      setPrompt('');
      setComposerReferences([]);
      clearAttachments({ revoke: false });
    }
    updateTimelineForConversation(activeConversationId, (items) => appendTimelineItems(items, initialDisclosureTimeline));
    scheduleAssistantProgressDisclosure(assistantId, cleanPrompt, cleanAttachments);
    const statusMessageIds = useQueuedMessages ? queuedMessageIds : [userId];
    statusMessageIds.forEach((id) => {
      scheduleMessageStatus(id, 'sent', 280, activeConversationId);
      scheduleMessageStatus(id, 'read', 760, activeConversationId);
    });

    if (!window.ecorex) {
      scheduleMessageStatus(assistantId, 'generating', 420, activeConversationId);
      scheduleStatusTimer(() => {
        updateMessagesForConversation(activeConversationId, (items) =>
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
      return;
    }

    try {
      const result = await window.ecorex.runPrompt({
        sessionId: requestedSessionId,
        conversationId: activeConversationId,
        claudeSessionId: nextClaudeSessionId,
        messageId: assistantId,
        assistantMessageId: assistantId,
        prompt: promptForAgent,
        rawPrompt: cleanPrompt,
        userPrompt: cleanPrompt,
        attachments: cleanAttachments,
        attachmentMetadata: cleanAttachments,
        selectedReferences: cleanReferences,
        projectId: activeProject?.id || null,
        projectName: activeProject?.name || '',
        disableProjectContext: !activeProject?.id,
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
        updateMessagesForConversation(activeConversationId, (items) =>
          items.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                streaming: false,
                error: true,
                status: result.status === 'timeout' ? 'timeout' : result.status === 'cancelled' ? 'cancelled' : 'error',
                  text: cleanAssistantOutputText(unauthorized ? '登录状态已过期，请重新登录后继续。' : agentRunFailureMessage(result)),
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
      if (activeConversationId === String(conversationIdRef.current || '')) {
        currentSessionIdRef.current = sessionId;
        setCurrentSessionId(sessionId);
      } else {
        syncCurrentSessionForVisibleConversation(conversationIdRef.current);
      }
      updateMessagesForConversation(activeConversationId, (items) =>
        items.map((item) => (item.id === assistantId ? {
          ...item,
          sessionId,
          claudeSessionId: result.claudeSessionId || nextClaudeSessionId || item.claudeSessionId,
          projectId: result.projectId || activeProject?.id || item.projectId || '',
          projectName: result.projectName || activeProject?.name || item.projectName || ''
        } : item))
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
      updateMessagesForConversation(activeConversationId, (items) =>
        items.map((item) =>
          item.id === assistantId
            ? {
                ...item,
                streaming: false,
                error: true,
                status: 'error',
                text: cleanAssistantOutputText(unauthorized ? '登录状态已过期，请重新登录后继续。' : agentRunFailureMessage({ error: error?.message })),
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
    const owner = sessionOwnerForSession(sessionId);
    const ownerConversationId = owner?.conversationId || conversationIdRef.current;
    const messageId = sessionMap.current.get(sessionId) || runningSessionRowsRef.current.find((row) => row.sessionId === sessionId)?.messageId;
    updateMessagesForConversation(ownerConversationId, (items) =>
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
    updateTimelineForConversation(ownerConversationId, (items) => appendTimeline(items, ['用户取消当前任务', '已取消', formatAgentEventTime(), 'pending']));
    if (window.ecorex?.stopPrompt) {
      try {
        const result = await window.ecorex.stopPrompt(sessionId);
        if (result?.ok === false && result.reason === 'not-found') {
          finishSession(sessionId);
        } else {
          scheduleStatusTimer(() => {
            if (sessionMap.current.has(sessionId)) finishSession(sessionId);
          }, 5000);
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

  function clearMessageRecoveryState(message = {}) {
    if (!message?.id) return;
    const ownerConversationId = message.conversationId || conversationIdRef.current;
    updateMessagesForConversation(ownerConversationId, (items) => items.map((item) => (
      item.id === message.id
        ? {
            ...item,
            recovery: null,
            silentRecovery: false,
            status: ['interrupted', 'cancelled', 'timeout', 'error'].includes(String(item.status || '').toLowerCase())
              ? 'sending'
              : item.status
          }
        : item
    )));
  }

  function retryMessage(message) {
    const retryPrompt = retryPromptFromMessage(message);
    if (retryPrompt) {
      clearMessageRecoveryState(message);
      sendPrompt(retryPrompt);
    }
  }

  function isDuplicateAgentStartResult(result = {}) {
    const code = String(result?.code || result?.status || '').toLowerCase();
    return code === 'duplicate-session' || code === 'duplicate-start';
  }

  function attachPermissionContinuationToRunningSession(message, result = {}, requestedSessionId = '') {
    const sessionId = String(result.sessionId || result.duplicateOf || requestedSessionId || '').trim();
    if (!message?.id || !sessionId) return false;
    if (requestedSessionId && requestedSessionId !== sessionId) {
      runningSessionsRef.current.delete(requestedSessionId);
      sessionMap.current.delete(requestedSessionId);
      sessionOwnersRef.current.delete(requestedSessionId);
    }
    const owner = {
      sessionId,
      messageId: message.id,
      conversationId: String(result.conversationId || conversationIdRef.current || ''),
      claudeSessionId: String(result.claudeSessionId || conversationSessionIdRef.current || conversationIdRef.current || ''),
      projectId: String(result.projectId || conversationProjectRef.current?.id || ''),
      projectName: String(result.projectName || conversationProjectRef.current?.name || '')
    };
    sessionMap.current.set(sessionId, message.id);
    sessionOwnersRef.current.set(sessionId, owner);
    runningSessionsRef.current.add(sessionId);
    if (String(owner.conversationId || '') === String(conversationIdRef.current || '')) {
      currentSessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);
    }
    commitRunningSessionRows((rows) => mergeRunningSessionRows(
      rows.filter((row) => row.sessionId !== requestedSessionId),
      [normalizeRunningSession({
        sessionId,
        id: sessionId,
        conversationId: owner.conversationId,
        claudeSessionId: owner.claudeSessionId,
        projectId: owner.projectId,
        projectName: owner.projectName,
        messageId: message.id,
        status: 'running',
        state: 'running',
        prompt: message.originalPrompt || '',
        updatedAt: Date.now()
      }, rows.length, 'local')]
    ));
    setMessages((items) =>
      items.map((item) => (item.id === message.id ? {
        ...item,
        streaming: true,
        error: false,
        status: 'thinking',
        sessionId,
        claudeSessionId: owner.claudeSessionId || item.claudeSessionId,
        projectId: owner.projectId || item.projectId || '',
        projectName: owner.projectName || item.projectName || '',
        timeline: appendTimeline(item.timeline || [], ['权限已确认，继续等待当前执行', '已接入运行会话', formatAgentEventTime(), 'pending', 'status'])
      } : item))
    );
    return true;
  }

  async function continueFromPermission(message = {}, actionValue = 'allow') {
    if (!message?.id || permissionContinuationLocksRef.current.has(message.id)) return;
    permissionContinuationLocksRef.current.add(message.id);
    const decision = permissionActionFromValue(actionValue);
    const requestedSessionId = createLocalId('session');
    const continuationPrompt = permissionContinuationPrompt(decision);
    const now = formatAgentEventTime();
    const accessMode = decision.accessMode || normalizeAccessMode(permissionMode);
    const requestedPermissionMode = permissionModeFromAccessMode(accessMode);

    sessionMap.current.set(requestedSessionId, message.id);
    trackSession(requestedSessionId, {
      messageId: message.id,
      prompt: continuationPrompt,
      accessMode,
      source: 'local'
    });

    setMessages((items) =>
      items.map((item) =>
        item.id === message.id
          ? {
              ...item,
              streaming: true,
              status: 'thinking',
              text: '',
              permissionPromptText: item.permissionPromptText || item.text || '',
              error: false,
              sessionId: requestedSessionId,
              claudeSessionId: conversationSessionIdRef.current || conversationIdRef.current,
              projectId: conversationProjectRef.current?.id || item.projectId || '',
              projectName: conversationProjectRef.current?.name || item.projectName || '',
              permissionDecision: {
                action: decision.action,
                label: decision.label,
                status: 'running',
                at: Date.now()
              },
              showTrace: true,
              timeline: appendTimeline(item.timeline || [], [
                decision.action === 'allow' ? '权限已确认，继续执行' : decision.action === 'plan' ? '权限选择为只做计划' : '权限已拒绝',
                decision.label,
                now,
                decision.tone || 'pending',
                'status'
              ])
            }
          : item
      )
    );
    setTimeline((items) => appendTimeline(items, [
      decision.action === 'allow' ? '权限已确认，继续执行' : decision.action === 'plan' ? '权限选择为只做计划' : '权限已拒绝',
      decision.label,
      now,
      decision.tone || 'pending',
      'status'
    ]));

    if (!window.ecorex?.runPrompt) {
      finishSession(requestedSessionId);
      permissionContinuationLocksRef.current.delete(message.id);
      setMessages((items) =>
        items.map((item) =>
          item.id === message.id
            ? {
                ...item,
                streaming: false,
                status: 'complete',
                permissionDecision: {
                  action: decision.action,
                  label: decision.label,
                  status: 'unavailable',
                  at: Date.now()
                }
              }
            : item
        )
      );
      return;
    }

    try {
      const result = await window.ecorex.runPrompt({
        sessionId: requestedSessionId,
        conversationId: conversationIdRef.current,
        claudeSessionId: conversationSessionIdRef.current || conversationIdRef.current,
        messageId: message.id,
        assistantMessageId: message.id,
        prompt: continuationPrompt,
        accessMode,
        permissionMode: requestedPermissionMode,
        defaultPermissionMode: requestedPermissionMode,
        projectId: conversationProjectRef.current?.id || null,
        projectName: conversationProjectRef.current?.name || '',
        disableProjectContext: !conversationProjectRef.current?.id,
        ...fullAccessConfirmationFields(accessMode),
        model,
        plugins: selectedPlugins,
        permissionContinuation: true
      });

      if (!result.ok) {
        if (isDuplicateAgentStartResult(result) && attachPermissionContinuationToRunningSession(message, result, requestedSessionId)) {
          permissionContinuationLocksRef.current.delete(message.id);
          return;
        }
        finishSession(requestedSessionId);
        permissionContinuationLocksRef.current.delete(message.id);
        if (result.unauthorized) onUnauthorized?.();
        setMessages((items) =>
          items.map((item) =>
            item.id === message.id
              ? {
                  ...item,
                  streaming: false,
                  error: true,
                  status: result.status === 'timeout' ? 'timeout' : 'error',
                  permissionDecision: {
                    action: decision.action,
                    label: decision.label,
                    status: 'failed',
                    at: Date.now()
                  },
                  timeline: appendTimeline(item.timeline || [], ['权限续跑启动失败', formatSessionStatus(result.status || result.code || 'failed'), formatAgentEventTime(), 'danger'])
                }
              : item
          )
        );
        return;
      }

      const sessionId = result.sessionId || requestedSessionId;
      if (sessionId !== requestedSessionId) transferSession(requestedSessionId, sessionId);
      if (String(conversationIdRef.current || '') === String(sessionOwnerForSession(sessionId)?.conversationId || conversationIdRef.current || '')) {
        currentSessionIdRef.current = sessionId;
        setCurrentSessionId(sessionId);
      }
      setMessages((items) =>
        items.map((item) => (item.id === message.id ? {
          ...item,
          sessionId,
          claudeSessionId: result.claudeSessionId || conversationSessionIdRef.current || conversationIdRef.current || item.claudeSessionId,
          projectId: result.projectId || conversationProjectRef.current?.id || item.projectId || '',
          projectName: result.projectName || conversationProjectRef.current?.name || item.projectName || ''
        } : item))
      );
      if (result.initialEvent) queueAgentEvents(result.initialEvent);
    } catch (error) {
      finishSession(requestedSessionId);
      permissionContinuationLocksRef.current.delete(message.id);
      if (isUnauthorizedError(error)) onUnauthorized?.();
      setMessages((items) =>
        items.map((item) =>
          item.id === message.id
            ? {
                ...item,
                streaming: false,
                error: true,
                status: 'error',
                permissionDecision: {
                  action: decision.action,
                  label: decision.label,
                  status: 'failed',
                  at: Date.now()
                },
                timeline: appendTimeline(item.timeline || [], ['权限续跑启动失败', '失败', formatAgentEventTime(), 'danger'])
              }
            : item
        )
      );
    }
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
    const safeSessionId = String(sessionId || '').trim();
    if (!safeSessionId) return;
    currentSessionIdRef.current = safeSessionId;
    setCurrentSessionId(safeSessionId);
    const row = runningSessionRowsRef.current.find((item) => item.sessionId === safeSessionId || item.id === safeSessionId);
    const owner = sessionOwnerForSession(safeSessionId, row || {});
    const ownerConversationId = String(owner?.conversationId || row?.conversationId || '').trim();
    const messageId = row?.messageId || sessionMap.current.get(safeSessionId) || owner?.messageId;
    if (ownerConversationId && ownerConversationId !== String(conversationIdRef.current || '')) {
      const recent = loadRecentChatItems().find((item) => item.id === ownerConversationId) || {};
      const detail = {
        ...recent,
        id: ownerConversationId,
        claudeSessionId: owner?.claudeSessionId || row?.claudeSessionId || recent.claudeSessionId || ownerConversationId,
        sessionId: safeSessionId,
        title: recent.title || row?.prompt || row?.title || '运行会话',
        projectId: owner?.projectId || row?.projectId || recent.projectId || '',
        projectName: owner?.projectName || row?.projectName || recent.projectName || '',
        updatedAt: recent.updatedAt || row?.updatedAt || Date.now(),
        time: recent.time || recentChatTimeLabel()
      };
      openStoredConversation(detail);
      window.dispatchEvent?.(new CustomEvent('ecorex:recent-chat-upsert', { detail }));
      if (messageId) {
        window.setTimeout(() => {
          document.getElementById(`message-${messageId}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }, 180);
      }
      return;
    }
    if (!messageId) return;
    const messageIndex = messages.findIndex((message) => message.id === messageId);
    if (messageIndex >= 0) {
      setVisibleMessageCount((count) => Math.max(count, messages.length - messageIndex));
    }
    window.setTimeout(() => {
      document.getElementById(`message-${messageId}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 80);
  }

  function insertArtifactReference(artifact, selection = {}) {
    const nextReference = createComposerReferenceFromArtifact(artifact, selection);
    setComposerReferences((current) => {
      const byLocation = new Map(sanitizeComposerReferences(current).map((item) => [item.location, item]));
      byLocation.set(nextReference.location, nextReference);
      return Array.from(byLocation.values()).slice(-MAX_COMPOSER_REFERENCES);
    });
    window.setTimeout(() => {
      const input = document.querySelector('[data-testid="chat-input"]');
      input?.focus?.();
      input?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
    }, 0);
  }

  function removeComposerReference(id) {
    setComposerReferences((items) => items.filter((item) => item.id !== id));
  }

  function hideMessageArtifact(messageId, artifactId) {
    setMessages((items) => items.map((message) => {
      if (message.id !== messageId) return message;
      const hidden = new Set(message.hiddenArtifactIds || []);
      hidden.add(String(artifactId || ''));
      return { ...message, hiddenArtifactIds: Array.from(hidden).filter(Boolean).slice(0, ARTIFACT_PREVIEW_MAX_ITEMS) };
    }));
    if (focusArtifact?.id === artifactId) setFocusArtifact(null);
  }

  async function openUserAttachment(attachment = {}) {
    if (!attachment.path && !attachment.filePath) return;
    const result = await openAttachmentFileWithBridge(attachment);
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false && !result?.missing) {
      console.warn('Attachment open failed', result.error || result);
    }
  }

  async function openGeneratedArtifact(artifact = {}) {
    const result = await openArtifactFileWithBridge(artifact);
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false && !result?.missing) {
      console.warn('Artifact open failed', result.error || result);
    }
  }

  const pendingPermissionRequest = useMemo(() => {
    for (const message of [...messages].reverse()) {
      const request = permissionRequestFromMessage(message, message.timeline || timeline);
      if (request) return { message, request };
    }
    return null;
  }, [messages, timeline]);

  return (
    <div className={`chat-layout ${focusArtifact ? 'preview-focus' : 'chat-only'}`}>
      <RunningSessionStrip
        sessions={runningSessions}
        currentSessionId={currentSessionId}
        onSelect={selectRunningSession}
        onStop={cancelPrompt}
        onOpenSessions={() => setPage?.('settings')}
      />
      <section className="chat-main panel">
        <HeaderBar
          title="EcoreX"
          badge="亦芯助手"
          subtitle="面向广告投放、素材创意、预算优化、归因分析与客户项目协同的自主思考型助手"
          backendStatus={backendStatus}
          onRefresh={refreshBackend}
        />

        <div className="messages" ref={messageListRef}>
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
          {hiddenMessageCount > 0 && (
            <button
              className="show-history"
              type="button"
              onClick={() => setVisibleMessageCount((count) => count + MESSAGE_WINDOW_STEP)}
            >
              显示更多历史（还有 {hiddenMessageCount} 条）
            </button>
          )}
          {visibleMessages.map((message) => {
            const hasAnswerPreview = Boolean(
              cleanAssistantOutputText(message.text || '').trim()
              || message.finalArtifacts?.length
              || message.rich
            );
            const hasPublicTrace = (message.timeline || timeline || []).some(isPublicTraceItem);
            const shouldShowTrace = Boolean(
              message.showTrace
              || hasPublicTrace
              || message.error
              || message.status === 'timeout'
              || message.status === 'cancelled'
              || (message.streaming && !hasAnswerPreview)
            );
            return (
              <ChatMessage
                key={message.id}
                message={message}
                timeline={message.timeline || timeline}
                sourceMap={backendStatus?.sourceMap}
                showTrace={shouldShowTrace}
                onRetry={retryMessage}
                onPermissionReply={continueFromPermission}
                onInsertArtifactReference={insertArtifactReference}
                onOpenArtifact={(artifact) => setFocusArtifact(artifact)}
                onOpenArtifactLocal={openGeneratedArtifact}
                onHideArtifact={hideMessageArtifact}
                onOpenAttachment={openUserAttachment}
              />
            );
          })}
          {showJumpLatest && (
            <button className="jump-latest" type="button" onClick={() => scrollMessagesToLatest('smooth')}>
              <ChevronDown size={14} />
              回到最新
            </button>
          )}
        </div>

        <Composer
          prompt={prompt}
          setPrompt={setPrompt}
          attachments={attachments}
          onSelectFiles={selectAttachmentFiles}
          onPasteFiles={handlePastedFiles}
          onRemoveAttachment={removeAttachment}
          references={composerReferences}
          onRemoveReference={removeComposerReference}
          activeProject={activeProject}
          projectFiles={projectFiles}
          onSelectProjectFile={addProjectFileAttachment}
          onRemoveProjectFile={removeProjectFileFromChat}
          running={activeConversationRunning}
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
        <PermissionConfirmationModal
          request={pendingPermissionRequest?.request}
          onReply={(action) => {
            if (pendingPermissionRequest?.message) continueFromPermission(pendingPermissionRequest.message, action);
          }}
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            handleBrowserFileSelection(event.target.files, 'upload');
            event.target.value = '';
          }}
        />
      </section>

      {focusArtifact ? (
        <ArtifactFocusPanel
          artifact={focusArtifact}
          onClose={() => setFocusArtifact(null)}
          onInsertReference={insertArtifactReference}
          onOpenLocal={openGeneratedArtifact}
        />
      ) : null}
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
    <div className="running-session-strip" data-testid="running-session-strip">
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
            <div className={`running-session-pill ${active ? 'active' : ''}`} data-testid="running-session-pill" key={session.sessionId}>
              <button type="button" data-testid="running-session-open" onClick={() => onSelect?.(session.sessionId)} title="切换查看">
                <span className={`dot ${session.tone === 'danger' ? 'warn' : session.tone === 'running' ? 'running-dot' : 'ok'}`} />
                <strong>{session.title || `会话 ${index + 1}`}</strong>
                <em>{session.prompt || '正在执行任务'}</em>
              </button>
              <button
                className="session-stop"
                data-testid="running-session-stop"
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

function HeaderBar({ title, badge, subtitle, backendStatus, onRefresh, onBack, backLabel = '返回' }) {
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
        {onBack && (
          <button className="view-back-button" type="button" onClick={onBack}>
            <ChevronLeft size={16} />
            {backLabel}
          </button>
        )}
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

function ChatExternalLink({ url, label }) {
  const safeUrl = safeChatExternalUrl(url);
  if (!safeUrl) return <>{label || url}</>;
  return (
    <a
      className="chat-rich-link"
      href={safeUrl}
      rel="noreferrer"
      onClick={(event) => {
        event.preventDefault();
        openExternalUrlWithBridge(safeUrl);
      }}
    >
      {label || safeUrl}
    </a>
  );
}

function ChatInlineMedia({ url, kind, label }) {
  const safeUrl = safeChatExternalUrl(url, { mediaOnly: true });
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
  }, [safeUrl]);
  if (!safeUrl || failed) return <ChatExternalLink url={url} label={label || url} />;
  const caption = label && label !== safeUrl ? label : kind === 'video' ? '视频' : '图片';
  return (
    <figure className={`chat-rich-media ${kind}`}>
      {kind === 'video' ? (
        <video src={safeUrl} controls playsInline preload="none" onError={() => setFailed(true)} />
      ) : (
        <img src={safeUrl} alt={caption} loading="lazy" onError={() => setFailed(true)} />
      )}
      <figcaption>
        <span>{caption}</span>
        <button type="button" onClick={() => openExternalUrlWithBridge(safeUrl)}>打开</button>
      </figcaption>
    </figure>
  );
}

function ChatInlineArtifactImage({ artifact, label }) {
  const initialPreviewUrl = safeAttachmentPreviewUrl(artifact.previewUrl || '');
  const [preview, setPreview] = useState({ status: initialPreviewUrl ? 'ready' : 'loading', url: initialPreviewUrl });
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const fallbackUrl = safeAttachmentPreviewUrl(artifact.previewUrl || '');
    setFailed(false);
    setPreview({ status: fallbackUrl ? 'ready' : 'loading', url: fallbackUrl });
    previewArtifactWithBridge(artifact).then((result) => {
      if (cancelled) return;
      const normalized = normalizeArtifactPreviewResult(result, artifact);
      setPreview({
        status: (normalized.previewUrl || fallbackUrl) ? 'ready' : 'unavailable',
        url: normalized.previewUrl || fallbackUrl,
        name: normalized.name || artifact.name,
        path: normalized.path || artifact.path
      });
    }).catch(() => {
      if (!cancelled) setPreview({ status: fallbackUrl ? 'ready' : 'unavailable', url: fallbackUrl });
    });
    return () => {
      cancelled = true;
    };
  }, [artifact.id, artifact.path, artifact.previewUrl]);

  const caption = label || artifact.name || fileNameFromArtifactPath(artifact.path) || '图片';
  if (preview.status !== 'ready' || !preview.url || failed) {
    return <span className="chat-rich-artifact-pending">{caption}</span>;
  }
  return (
    <figure className="chat-rich-media image artifact-inline-image" data-testid="chat-inline-artifact-image">
      <img src={preview.url} alt={caption} loading="lazy" onError={() => setFailed(true)} />
      <figcaption>
        <span>{caption}</span>
      </figcaption>
    </figure>
  );
}

function RichMessageText({ text = '', className = '', imageArtifacts = [] }) {
  const richImageArtifacts = useMemo(
    () => (Array.isArray(imageArtifacts) ? imageArtifacts.filter(isImageArtifact) : []),
    [imageArtifacts]
  );
  const nodes = useMemo(() => {
    const source = String(text || '');
    if (!source && !richImageArtifacts.length) return null;
    const tokenPattern = /!\[([^\]\n]{0,180})\]\(([^)\n]{1,2000})\)|\[([^\]\n]{1,180})\]\(([^)\n]{1,2000})\)|((?:https?:\/\/|www\.)[^\s<>"'`]+)/gi;
    const output = [];
    const embeddedImageIds = new Set();
    let cursor = 0;
    let index = 0;
    const pushText = (value) => {
      if (value) output.push(value);
    };
    for (const match of source.matchAll(tokenPattern)) {
      const full = match[0] || '';
      const start = match.index || 0;
      pushText(source.slice(cursor, start));
      cursor = start + full.length;

      const markdownImage = match[1] !== undefined;
      const label = match[1] || match[3] || '';
      const cleanUrl = cleanChatUrlToken(match[2] || match[4] || match[5] || '');
      const safeUrl = safeChatExternalUrl(cleanUrl);
      if (!safeUrl) {
        const imageArtifact = findRichImageArtifact(cleanUrl, label, richImageArtifacts);
        if (imageArtifact) {
          embeddedImageIds.add(imageArtifact.id);
          output.push(
            <ChatInlineArtifactImage
              key={`artifact-image-${index += 1}`}
              artifact={imageArtifact}
              label={label || imageArtifact.name}
            />
          );
          continue;
        }
        pushText(full);
        continue;
      }

      const mediaKind = chatMediaKind(safeUrl);
      if ((markdownImage || mediaKind) && mediaKind) {
        output.push(<ChatInlineMedia key={`media-${index += 1}`} url={safeUrl} kind={mediaKind} label={label} />);
      } else {
        output.push(<ChatExternalLink key={`link-${index += 1}`} url={safeUrl} label={label || cleanUrl} />);
      }
    }
    pushText(source.slice(cursor));
    for (const imageArtifact of richImageArtifacts) {
      if (embeddedImageIds.has(imageArtifact.id)) continue;
      const autoKey = imageArtifact.id || imageArtifact.path || `auto-${index += 1}`;
      output.push(
        <ChatInlineArtifactImage
          key={`artifact-image-auto-${autoKey}`}
          artifact={imageArtifact}
          label={imageArtifact.name}
        />
      );
    }
    return output;
  }, [text, richImageArtifacts]);

  return <div className={`rich-message-text ${className}`}>{nodes}</div>;
}

function ChatMessage({
  message,
  timeline,
  sourceMap,
  showTrace = false,
  onRetry,
  onPermissionReply,
  onInsertArtifactReference,
  onOpenArtifact,
  onOpenArtifactLocal,
  onHideArtifact,
  onOpenAttachment
}) {
  const [expanded, setExpanded] = useState(false);
  const [availableArtifactIds, setAvailableArtifactIds] = useState(() => new Set());
  const permissionPromptRequest = message.role === 'assistant'
    ? permissionRequestFromMessage(message, timeline, { includeResolved: true })
    : null;
  const rawText = message.role === 'assistant' && permissionPromptRequest
    ? ''
    : message.role === 'assistant'
      ? cleanAssistantOutputText(message.text || '')
      : (message.text || '');
  const hasVisibleAssistantContent = Boolean(
    rawText.trim()
    || message.error
    || message.finalArtifacts?.length
    || message.ledger?.length
    || message.attachmentIngest?.length
    || message.rich
  );
  const hiddenArtifactIds = useMemo(() => new Set(message.hiddenArtifactIds || []), [message.hiddenArtifactIds]);
  const artifactContext = useMemo(() => ({
    sessionId: message.sessionId || message.agentSessionId || '',
    claudeSessionId: message.claudeSessionId || '',
    projectId: message.projectId || message.project?.id || '',
    projectName: message.projectName || message.project?.name || ''
  }), [message.sessionId, message.agentSessionId, message.claudeSessionId, message.projectId, message.project, message.projectName]);
  const candidateArtifactReferences = useMemo(
    () => mergeArtifactReferences([
      ...(message.role === 'assistant' ? (message.finalArtifacts || []) : [])
    ])
      .map((artifact) => ({ ...artifact, ...artifactContext }))
      .filter((artifact) => !hiddenArtifactIds.has(artifact.id)),
    [message.role, message.finalArtifacts, hiddenArtifactIds, artifactContext]
  );
  useEffect(() => {
    let cancelled = false;
    if (!candidateArtifactReferences.length) {
      setAvailableArtifactIds(new Set());
      return () => {
        cancelled = true;
      };
    }
    Promise.all(candidateArtifactReferences.map(async (artifact) => ({
      id: artifact.id,
      available: await validateArtifactAvailabilityWithBridge(artifact)
    }))).then((results) => {
      if (cancelled) return;
      setAvailableArtifactIds(new Set(results.filter((item) => item.available).map((item) => item.id)));
    });
    return () => {
      cancelled = true;
    };
  }, [candidateArtifactReferences]);
  const artifactReferences = useMemo(
    () => candidateArtifactReferences.filter((artifact) => availableArtifactIds.has(artifact.id)),
    [candidateArtifactReferences, availableArtifactIds]
  );
  const userAttachmentIngest = useMemo(() => (
    Array.isArray(message.attachments)
      ? message.attachments.map((attachment) => attachment.ingest).filter(Boolean)
      : []
  ), [message.attachments]);
  if (message.role === 'user') {
    return (
      <div className="user-row" id={`message-${message.id}`}>
        <div className="user-bubble">
          <RichMessageText text={message.text} className="user-text" />
          {Boolean(message.attachments?.length) && (
            <AttachmentPreviewList attachments={message.attachments} compact onOpen={onOpenAttachment} />
          )}
          <MessageReferenceList references={message.references || []} compact />
          <AttachmentIngestionSummary items={userAttachmentIngest} compact />
          <MessageStatus status={message.status || 'read'} time={message.time} compact />
        </div>
        <div className="avatar user-avatar">张</div>
      </div>
    );
  }

  if (permissionPromptRequest && !hasVisibleAssistantContent) return null;
  if (message.silentRecovery && !hasVisibleAssistantContent) return null;

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
        <RichMessageText
          text={displayText}
          className={shouldCollapse && !expanded ? 'assistant-text collapsed' : 'assistant-text'}
          imageArtifacts={artifactReferences}
        />
        {shouldCollapse && (
          <button className="text-expand" type="button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起长回复' : `展开全文（${rawText.length.toLocaleString('zh-CN')} 字符）`}
          </button>
        )}
        <ArtifactPreviewShelf
          artifacts={artifactReferences}
          createdTime={message.time}
          onOpenArtifact={onOpenArtifact}
          onOpenArtifactLocal={onOpenArtifactLocal}
          onHideArtifact={(artifact) => onHideArtifact?.(message.id, artifact.id)}
        />
        <AttachmentIngestionSummary items={message.attachmentIngest || []} />
        <ToolResultInline items={message.ledger || []} />
        <ToolLedgerDisclosure items={message.ledger || []} />
        <RecoveryStateNotice recovery={message.recovery} status={message.status} onRetry={() => onRetry?.(message)} />
        {message.rich && <CampaignPerformanceReport />}
        {message.streaming && <ThinkingIndicator phase={message.status} compact={hasVisibleAssistantContent} />}
        {showTrace && <InlineAgentTrace timeline={timeline} sourceMap={sourceMap} priority="low" />}
        {!message.streaming && message.error && (
          <button className="message-retry-link" type="button" onClick={() => onRetry?.(message)}>
            <Loader2 size={15} />
            重试
          </button>
        )}
      </div>
    </div>
  );
}

function AttachmentIngestionSummary({ items = [], compact = false }) {
  const visibleItems = useMemo(() => (
    (Array.isArray(items) ? items : []).filter(Boolean).slice(0, compact ? 2 : 5)
  ), [items, compact]);
  if (!visibleItems.length) return null;
  return (
    <div className={`attachment-ingest-summary ${compact ? 'compact' : ''}`}>
      {visibleItems.map((item, index) => (
        <div className={`attachment-ingest-item ${item.tone || 'running'}`} key={item.id || `${item.name}-${index}`}>
          <span className="attachment-ingest-icon">
            {isImageAttachment(item) ? <Eye size={14} /> : <FileText size={14} />}
          </span>
          <div>
            <strong>{item.name || `附件 ${index + 1}`}</strong>
            <span>{item.reason || item.summary || item.metadata || item.status}</span>
            {!compact && item.metadata && !item.reason && <em>{item.metadata}</em>}
          </div>
          <small>{item.status}</small>
        </div>
      ))}
    </div>
  );
}

function inlineToolResultText(item = {}) {
  const raw = item.output || item.detail || item.action || '';
  const text = compactEventDetail(cleanPublicAgentText(raw, { dropPathLines: true }), 520);
  if (!text || text.length < 18) return '';
  if (isInternalAgentOutputLine(text)) return '';
  if (/^(?:WebSearch|WebFetch|Read|Bash|PowerShell|TodoWrite|TodoRead|Grep|Glob|LS)\s+(?:completed|running|done)$/i.test(text)) return '';
  if (/^large ledger tool \d+/i.test(text)) return '';
  if (looksLikeNoisyLocalPath(text)) return '';
  return text;
}

function ToolResultInline({ items = [] }) {
  const visible = useMemo(() => (
    (Array.isArray(items) ? items : [])
      .filter((item) => ['success', 'completed'].includes(String(item.tone || item.status || '').toLowerCase()))
      .map(inlineToolResultText)
      .filter(Boolean)
      .slice(-2)
  ), [items]);
  if (!visible.length) return null;
  return (
    <div className="tool-result-inline" data-testid="tool-result-inline">
      {visible.map((text, index) => <span key={`${index}-${text.slice(0, 30)}`}>{text}</span>)}
    </div>
  );
}

function ToolLedgerDisclosure({ items = [] }) {
  const [expanded, setExpanded] = useState(false);
  const ledgerItems = useMemo(() => (
    (Array.isArray(items) ? items : [])
      .filter(Boolean)
      .filter((item) => !isNoisyToolLedgerItem(item))
      .slice(-MAX_STORED_LEDGER_ITEMS)
  ), [items]);
  const latest = ledgerItems.at(-1);
  const visibleItems = useMemo(() => (expanded ? ledgerItems.slice(-10) : []), [expanded, ledgerItems]);
  if (!latest) return null;
  const hasDetails = ledgerItems.some((item) => item.detail);
  return (
    <div className={`tool-ledger ${expanded ? 'expanded' : 'compact'}`}>
      <button className="tool-ledger-summary" type="button" onClick={() => setExpanded((value) => !value)}>
        <span className={`agent-trace-node ${latest.tone || 'running'}`} />
        <strong>{latest.toolName}</strong>
        <em>{latest.action}</em>
        <small>{latest.status}</small>
        <b>{expanded ? '收起' : '展开'}</b>
      </button>
      {expanded && (
        <div className="tool-ledger-list">
          {visibleItems.map((item, index) => (
            <div className={`tool-ledger-row ${item.tone || 'running'}`} key={`${item.id}-${index}`}>
              <span className={`agent-trace-node ${item.tone || 'running'}`} />
              <div>
                <strong>{item.toolName}</strong>
                <em>{item.action}</em>
              </div>
              <small>{item.status}</small>
              {item.detail && (
                <details>
                  <summary>{hasDetails ? '详情' : '查看'}</summary>
                  <pre>{item.detail}</pre>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function RecoveryStateNotice({ recovery, status, onRetry }) {
  if (!recovery?.state) return null;
  const completed = ['complete', 'completed'].includes(String(status || '').toLowerCase());
  const active = ['thinking', 'generating', 'sending'].includes(String(status || '').toLowerCase());
  if (completed && recovery.state === 'recoverable') return null;
  if (active && recovery.state === 'recoverable') return null;
  const canRetry = ['recoverable', 'retryable'].includes(recovery.state) || ['error', 'timeout', 'interrupted'].includes(String(status || '').toLowerCase());
  return (
    <div className={`message-recovery-state ${recovery.tone || 'running'}`}>
      <Clock3 size={15} />
      <div>
        <strong>{recovery.label || '可恢复'}</strong>
        <span>{recovery.detail || recovery.prompt || '这轮任务来自本地运行记录，可在当前会话继续处理。'}</span>
      </div>
      {canRetry && (
        <button type="button" onClick={onRetry}>
          <RotateCcw size={14} />
          重试
        </button>
      )}
    </div>
  );
}

function ArtifactPreviewShelf({ artifacts = [], createdTime = '', onOpenArtifact, onOpenArtifactLocal, onHideArtifact, compact = false }) {
  if (!artifacts.length) return null;

  return (
    <div className={`artifact-preview-shelf ${compact ? 'compact' : ''}`}>
      <div className="artifact-card-list">
        {artifacts.map((artifact) => (
          <ArtifactThumbnailCard
            artifact={artifact}
            createdTime={createdTime}
            key={artifact.id}
            onOpen={() => onOpenArtifact?.(artifact)}
            onOpenLocal={() => onOpenArtifactLocal?.(artifact)}
            onHide={() => onHideArtifact?.(artifact)}
          />
        ))}
      </div>
    </div>
  );
}

function ArtifactThumbnailCard({ artifact, createdTime = '', onOpen, onOpenLocal, onHide }) {
  const kind = artifactPreviewKind(artifact.ext, artifact.type || artifact.mimeType);
  const ext = artifactExtension(artifact.path || artifact.name || artifact.ext);
  const Icon = kind === 'image'
    ? Eye
    : kind === 'html'
      ? Code2
      : ['xls', 'xlsx', 'csv'].includes(ext)
        ? BarChart3
        : ['ppt', 'pptx'].includes(ext)
          ? LayoutDashboard
          : FileText;
  const title = artifact.name || fileNameFromArtifactPath(artifact.path);
  const timeLabel = createdTime || formatAgentEventTime();
  return (
    <div className={`artifact-thumb-card artifact-file-card ${kind}`} data-testid="artifact-file-card">
      <button
        className="artifact-thumb-open artifact-file-open"
        data-testid="artifact-file-open"
        type="button"
        title={artifactLocationLabel(artifact)}
        aria-label={`打开 ${title}`}
        onClick={onOpen}
      >
        <span className="artifact-thumb-icon artifact-file-icon" data-testid="artifact-file-icon">
          <Icon size={18} />
        </span>
        <span className="artifact-thumb-main">
          <strong>{title}</strong>
          <time className="artifact-file-time" data-testid="artifact-file-produced-at">创建时间：{timeLabel}</time>
        </span>
        <ChevronRight size={18} />
      </button>
      <button
        className="artifact-thumb-remove artifact-file-hide"
        data-testid="artifact-file-hide"
        type="button"
        title="隐藏这张预览卡片"
        aria-label={`隐藏 ${title}`}
        onClick={onHide}
      >
        <X size={14} />
      </button>
      <button
        className="artifact-thumb-local-open"
        data-testid="artifact-file-local-open"
        type="button"
        title="在本地打开"
        aria-label={`本地打开 ${title}`}
        onClick={onOpenLocal}
      >
        <FolderOpen size={14} />
      </button>
    </div>
  );
}

function MessageReferenceList({ references = [], compact = false, onRemove }) {
  const items = sanitizeComposerReferences(references);
  if (!items.length) return null;
  return (
    <div className={`composer-reference-tray ${compact ? 'compact' : ''}`}>
      {items.map((item) => (
        <div className="composer-reference-chip" key={item.id}>
          <span />
          <strong>{item.text || item.location}</strong>
          <em>{item.location || item.name}</em>
          {onRemove && (
            <button type="button" title="移除引用" onClick={() => onRemove(item.id)}>
              <X size={13} />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function ComposerReferenceTray({ references = [], onRemove }) {
  return <MessageReferenceList references={references} onRemove={onRemove} />;
}

function ArtifactFocusPanel({ artifact, onClose, onInsertReference, onOpenLocal }) {
  if (!artifact) return null;
  return (
    <aside className="artifact-focus-panel">
      <div className="artifact-focus-head">
        <div>
          <span>文件预览</span>
          <strong>{artifact.name || fileNameFromArtifactPath(artifact.path)}</strong>
        </div>
        <button type="button" title="关闭预览" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <ArtifactPreviewCard
        artifact={artifact}
        focus
        onClose={onClose}
        onInsertReference={onInsertReference}
        onOpenLocal={onOpenLocal}
      />
    </aside>
  );
}

function ArtifactPreviewCard({ artifact, onClose, onInsertReference, onOpenLocal, focus = false }) {
  const [preview, setPreview] = useState({ status: 'idle' });
  const [selectedLine, setSelectedLine] = useState(artifact.line || null);
  const [selectedText, setSelectedText] = useState('');
  const [copyNotice, setCopyNotice] = useState('');
  const previewRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    setPreview({ status: 'loading' });
    setSelectedLine(artifact.line || null);
    setSelectedText('');
    previewArtifactWithBridge(artifact).then((result) => {
      if (cancelled) return;
      const normalizedResult = typeof result === 'string' ? { ok: true, content: result } : (result || {});
      if (normalizedResult.ok === false) {
        setPreview({
          status: normalizedResult.unsupported ? 'unsupported' : normalizedResult.missing ? 'missing' : 'error',
          ...normalizeArtifactPreviewResult(normalizedResult, artifact),
          error: normalizedResult.unauthorized
            ? '登录状态已过期，重新登录后可继续预览。'
            : (normalizedResult.reason || normalizedResult.error || '暂时无法预览这个文件。')
        });
        return;
      }
      setPreview({ status: 'ready', ...normalizeArtifactPreviewResult(normalizedResult, artifact) });
    }).catch((error) => {
      if (!cancelled) {
        setPreview({ status: 'error', error: error?.message || String(error) });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [artifact.id]);

  useEffect(() => {
    function handlePreviewSelection(event) {
      const payload = event?.data && typeof event.data === 'object' ? event.data : null;
      if (!payload || payload.type !== 'ecorex-preview-selection') return;
      if (payload.previewId && preview.previewId && payload.previewId !== preview.previewId) return;
      const text = String(payload.text || '').trim().slice(0, 900);
      if (!text) return;
      const page = Number(payload.page || payload.pageNumber || 0) || null;
      const range = String(payload.range || payload.cellRange || '').trim();
      const locationText = range ? `${range}` : page ? `第 ${page} 页` : '';
      const nextText = locationText ? `${locationText}\n${text}` : text;
      setSelectedText(nextText);
      syncSelectionToComposer({ line: selectedLine || artifact.line, text: nextText });
    }
    window.addEventListener('message', handlePreviewSelection);
    return () => window.removeEventListener('message', handlePreviewSelection);
  }, [artifact.id, artifact.line, preview.previewId, selectedLine]);

  function syncSelectionToComposer(selection = {}) {
    onInsertReference?.(artifact, selection);
  }

  function captureSelectedText() {
    const selection = window.getSelection?.();
    if (!selection || selection.isCollapsed || !previewRef.current) return;
    if (!previewRef.current.contains(selection.anchorNode) || !previewRef.current.contains(selection.focusNode)) return;
    const text = selection.toString().trim();
    if (text) {
      const nextText = text.slice(0, 900);
      setSelectedText(nextText);
      syncSelectionToComposer({ line: selectedLine || artifact.line, text: nextText });
    }
  }

  async function copyPath() {
    const copied = await copyTextToClipboard(artifactLocationLabel(artifact));
    setCopyNotice(copied ? '已复制' : '复制失败');
    window.setTimeout(() => setCopyNotice(''), 1400);
  }

  function insertReference() {
    onInsertReference?.(artifact, {
      line: selectedLine || artifact.line,
      text: selectedText
    });
  }

  const content = preview.status === 'ready' ? preview.content || '' : '';
  const language = preview.status === 'ready' ? preview.language : artifactLanguageFromPath(artifact.path || artifact.name);
  const metadataOnly = preview.status === 'ready'
    && (preview.previewable === false || preview.renderMode === 'metadata' || (preview.renderMode !== 'vue-office' && ['pdf', 'office', 'binary'].includes(preview.kind)));
  const canRenderVueOffice = preview.status === 'ready' && !metadataOnly && preview.renderMode === 'vue-office' && preview.previewUrl;
  const canRenderHtml = preview.status === 'ready' && !metadataOnly && preview.kind === 'html';
  const canRenderImage = preview.status === 'ready' && !metadataOnly && preview.kind === 'image' && preview.previewUrl;
  const canRenderText = preview.status === 'ready' && !metadataOnly && !canRenderImage && !canRenderVueOffice;

  return (
    <div className={`artifact-preview-card ${focus ? 'focus' : ''}`}>
      {!focus && (
        <header>
          <div>
            <strong>{artifact.name}</strong>
            <span>{artifactLocationLabel(artifact)}</span>
          </div>
          <button type="button" title="关闭预览" onClick={onClose}>
            <X size={15} />
          </button>
        </header>
      )}

      {preview.status === 'loading' && (
        <div className="artifact-preview-state">
          <Loader2 size={16} className="spin-icon" />
          正在打开预览
        </div>
      )}

      {preview.status === 'missing' && (
        <div className="artifact-preview-state warn">
          <AlertTriangle size={16} />
          桌面端暂未提供 previewFile 接口，已保留路径定位，可直接回填到输入框。
        </div>
      )}

      {preview.status === 'error' && (
        <div className="artifact-preview-state error">
          <AlertTriangle size={16} />
          {preview.error}
        </div>
      )}

      {preview.status === 'unsupported' && (
        <ArtifactMetaPreview preview={preview} artifact={artifact} />
      )}

      {preview.status === 'ready' && (
        <>
          {metadataOnly && (
            <ArtifactMetaPreview preview={preview} artifact={artifact} />
          )}
          {canRenderImage && (
            <div className="artifact-image-preview">
              <img src={preview.previewUrl} alt={artifact.name} />
            </div>
          )}
          {canRenderHtml && (
            <iframe
              className="artifact-html-frame"
              title={`${artifact.name} preview`}
              sandbox=""
              srcDoc={content}
            />
          )}
          {canRenderVueOffice && (
            <iframe
              className="artifact-html-frame artifact-vue-office-frame"
              title={`${artifact.name} preview`}
              sandbox="allow-scripts allow-same-origin allow-forms"
              referrerPolicy="no-referrer"
              src={preview.previewUrl}
            />
          )}
          {canRenderText && (
            <ArtifactTextPreview
              content={content}
              language={language}
              startLine={preview.startLine}
              selectedLine={selectedLine}
              onSelectLine={(line, text) => {
                const nextText = text.trim().slice(0, 900);
                setSelectedLine(line);
                setSelectedText(nextText);
                syncSelectionToComposer({ line, text: nextText });
              }}
              onMouseUp={captureSelectedText}
              previewRef={previewRef}
            />
          )}
          {preview.truncated && <div className="artifact-preview-note">内容较长，当前只显示前部预览。</div>}
        </>
      )}

      <footer>
        <span>{selectedText ? '已选片段' : selectedLine ? `定位到第 ${selectedLine} 行` : '未选择片段'}</span>
        {copyNotice && <em>{copyNotice}</em>}
        <button type="button" onClick={copyPath}>
          <Copy size={14} />
          复制路径
        </button>
        <button type="button" data-testid="artifact-preview-local-open" onClick={() => onOpenLocal?.(artifact)}>
          <FolderOpen size={14} />
          本地打开
        </button>
        <button className="primary-inline" type="button" onClick={insertReference}>
          <Plus size={14} />
          选中文件
        </button>
      </footer>
    </div>
  );
}

function ArtifactMetaPreview({ preview = {}, artifact = {} }) {
  const kindLabels = {
    pdf: 'PDF 文件',
    office: 'Office 文件',
    image: '图片文件',
    binary: '二进制文件'
  };
  const kind = preview.kind || artifactPreviewKind(artifact.ext, artifact.type || artifact.mimeType);
  const rows = [
    ['类型', kindLabels[kind] || artifact.ext || '未知格式'],
    ['大小', formatFileSize(preview.sizeBytes || artifact.sizeBytes || 0)],
    ['MIME', preview.mimeType || artifact.type || artifact.mimeType || '未知'],
    ['路径', preview.path || artifact.path || artifact.name],
    preview.metadata ? ['元信息', preview.metadata] : null,
    ['说明', preview.reason || preview.error || '当前格式不在 EcoreX 内直接渲染，已禁止跳转系统应用。']
  ].filter(Boolean);
  return (
    <div className="artifact-meta-preview">
      <div className="artifact-meta-icon">
        {kind === 'pdf' ? <FileText size={24} /> : kind === 'office' ? <ClipboardList size={24} /> : <Archive size={24} />}
      </div>
      <div className="artifact-meta-grid">
        {rows.map(([label, value]) => (
          <div key={label}>
            <strong>{label}</strong>
            <span>{value || '无'}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ArtifactTextPreview({ content, language, startLine = 1, selectedLine, onSelectLine, onMouseUp, previewRef }) {
  const visibleLines = useMemo(() => {
    const lines = String(content || '').split(/\r?\n/);
    return lines.length ? lines : [''];
  }, [content]);
  return (
    <div className="artifact-text-preview" data-language={language} onMouseUp={onMouseUp} ref={previewRef}>
      {visibleLines.map((line, index) => {
        const lineNumber = startLine + index;
        return (
          <button
            className={lineNumber === selectedLine ? 'artifact-line selected' : 'artifact-line'}
            key={`${lineNumber}-${index}`}
            type="button"
            onClick={() => onSelectLine?.(lineNumber, line)}
          >
            <span>{lineNumber}</span>
            <code>{line || ' '}</code>
          </button>
        );
      })}
    </div>
  );
}

function AttachmentPreviewList({ attachments = [], onRemove, onOpen, compact = false }) {
  if (!attachments.length) return null;
  return (
    <div className={`attachment-tray ${compact ? 'compact' : ''}`}>
      {attachments.map((attachment) => {
        const image = isImageAttachment(attachment);
        const video = isVideoAttachment(attachment);
        const kindClass = image ? 'image' : video ? 'video' : 'file';
        const clickable = typeof onOpen === 'function' && Boolean(attachment.path || attachment.filePath);
        const progress = Math.max(0, Math.min(100, Math.round(Number(attachment.progress) || 0)));
        const statusText = attachment.status === 'uploading'
          ? `上传中... ${progress}%`
          : clickable ? '单击打开' : '已添加';
        return (
          <div
            className={`attachment-chip ${kindClass} ${clickable ? 'clickable' : ''}`}
            key={attachment.id || attachment.name}
            role={clickable ? 'button' : undefined}
            tabIndex={clickable ? 0 : undefined}
            title={clickable ? '用本机默认应用打开' : attachment.name}
            onClick={clickable ? () => onOpen(attachment) : undefined}
            onKeyDown={clickable ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onOpen(attachment);
              }
            } : undefined}
          >
            <AttachmentThumb attachment={attachment} compact={compact} />
            <div>
              <strong>{attachment.name || '未命名附件'}</strong>
              <span>{statusText} · {formatFileSize(attachment.sizeBytes)}</span>
            </div>
            {onRemove && (
              <button type="button" onClick={(event) => { event.stopPropagation(); onRemove(attachment.id); }} title="移除附件">
                <X size={13} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AttachmentThumb({ attachment, compact = false }) {
  const [failed, setFailed] = useState(false);
  const image = isImageAttachment(attachment);
  const kind = attachmentKindFromName(attachment.name || attachment.path || '', attachment.type || attachment.mimeType);
  const Icon = kind === 'sheet'
    ? BarChart3
    : kind === 'slide'
      ? LayoutDashboard
      : kind === 'code'
        ? Code2
        : FileText;

  useEffect(() => {
    setFailed(false);
  }, [attachment.previewUrl]);

  return (
    <div className={`attachment-thumb ${kind}`}>
      {image && attachment.previewUrl && !failed
        ? <img src={attachment.previewUrl} alt="" onError={() => setFailed(true)} />
        : <Icon size={compact ? 14 : 17} />}
    </div>
  );
}

function ProjectFileThumb({ file, compact = false }) {
  return <AttachmentThumb attachment={projectFileToAttachment(file)} compact={compact} />;
}

function ProjectFileMentionMenu({ open, files = [], query = '', selectedIndex = 0, onPick, onClose }) {
  const candidates = filterProjectFileCandidates(filterVisibleProjectFiles(files), query, 8);
  if (!open) return null;
  return (
    <div className="project-file-mention-menu" data-testid="project-file-mention-menu">
      <header>
        <span>@ 项目文件</span>
        <em>{candidates.length ? `${candidates.length} 个匹配` : '无匹配'}</em>
        {onClose && (
          <button
            className="project-file-popover-close"
            type="button"
            title="关闭"
            onMouseDown={(event) => {
              event.preventDefault();
              onClose?.();
            }}
          >
            <X size={14} />
          </button>
        )}
      </header>
      {candidates.length ? candidates.map((file, index) => (
        <button
          className={index === selectedIndex ? 'active' : ''}
          data-testid="project-file-mention-option"
          key={file.id || file.relativePath || file.name}
          type="button"
          onMouseDown={(event) => {
            event.preventDefault();
            onPick?.(file);
          }}
        >
          <ProjectFileThumb file={file} compact />
          <span>
            <strong>{file.name}</strong>
            <em>{file.relativePath || file.pathLabel || formatFileSize(file.sizeBytes)}</em>
          </span>
        </button>
      )) : (
        <div className="project-file-mention-empty">当前项目没有匹配文件</div>
      )}
    </div>
  );
}

function ProjectFilePickerPopover({ open, files = [], title = '项目文件', onPick, onRemove, onClose }) {
  const [query, setQuery] = useState('');
  const visibleFiles = useMemo(() => filterVisibleProjectFiles(files), [files]);
  const candidates = useMemo(
    () => filterProjectFileCandidates(visibleFiles, query, 60),
    [visibleFiles, query]
  );

  useEffect(() => {
    if (!open) setQuery('');
  }, [open]);

  if (!open) return null;
  return (
    <div className="project-file-picker-popover" data-testid="project-file-picker-popover">
      <header>
        <div>
          <strong>{title}</strong>
          <span>{visibleFiles.length ? `${visibleFiles.length} 个可引用文件` : '暂无可引用文件'}</span>
        </div>
        <button type="button" title="关闭" onClick={onClose}>
          <X size={16} />
        </button>
      </header>
      <label className="project-file-picker-search">
        <Search size={15} />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索项目文件" />
      </label>
      <div className="project-file-picker-list">
        {candidates.length ? candidates.map((file) => (
          <div className={`project-file-picker-row ${file.kind || 'file'}`} key={file.id || file.relativePath || file.name}>
            <button type="button" data-testid="project-file-picker-option" onClick={() => onPick?.(file)}>
              <ProjectFileThumb file={file} compact />
              <span>
                <strong>{file.name}</strong>
                <em>{[file.relativePath || file.pathLabel, formatFileSize(file.sizeBytes)].filter(Boolean).join(' · ')}</em>
              </span>
            </button>
            {onRemove && (
              <button className="project-file-picker-remove" type="button" title="从 EcoreX 中删除" onClick={() => onRemove?.(file)}>
                <X size={14} />
              </button>
            )}
          </div>
        )) : (
          <div className="project-file-picker-empty">
            <FileText size={18} />
            <span>{visibleFiles.length ? '没有匹配的项目文件' : '当前项目没有可引用文件'}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ProjectFileTreeNode({ node, depth = 0, onOpen, onReference, onRemove, fileBusy = '' }) {
  if (!node) return null;
  if (node.type === 'folder') {
    return (
      <div className="project-file-tree-folder" data-depth={depth}>
        {depth > 0 && (
          <div className="project-file-tree-folder-label">
            <ChevronRight size={14} />
            <FolderOpen size={15} />
            <span>{node.name}</span>
          </div>
        )}
        <div className="project-file-tree-children">
          {(node.children || []).map((child) => (
            <ProjectFileTreeNode
              key={child.id}
              node={child}
              depth={depth + 1}
              onOpen={onOpen}
              onReference={onReference}
              onRemove={onRemove}
              fileBusy={fileBusy}
            />
          ))}
        </div>
      </div>
    );
  }
  const file = node.file || {};
  const kind = file.kind || attachmentKindFromName(file.name || file.relativePath || '', file.type || file.mimeType);
  const busyKey = file.id || file.pathLabel || file.relativePath;
  return (
    <div className={`project-reference-file ${kind}`} data-testid="project-file-entry">
      <ProjectFileThumb file={file} compact />
      <button type="button" data-testid="project-file-open" onClick={() => onOpen?.(file)}>
        <strong>{file.name}</strong>
        <span>{[file.relativePath, formatFileSize(file.sizeBytes), file.modifiedAt ? formatDateTime(file.modifiedAt) : '项目文件'].filter(Boolean).join(' · ')}</span>
      </button>
      <div className="project-file-actions">
        <button type="button" title="@到项目对话" data-testid="project-file-reference" onClick={() => onReference?.(file)}>
          <FileText size={14} />
        </button>
        <button type="button" title="从 EcoreX 中删除" data-testid="project-file-remove" onClick={() => onRemove?.(file)} disabled={fileBusy === busyKey}>
          <X size={14} />
        </button>
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

function ThinkingIndicator({ phase = 'thinking', compact = false }) {
  const label = phase === 'generating' ? 'AI 正在生成' : 'AI 思考中';
  return (
    <div className={`thinking-indicator ${phase === 'generating' ? 'generating' : 'thinking'} ${compact ? 'compact' : ''}`}>
      <span className="thinking-orbit"><i /><i /><i /></span>
      <strong>{label}</strong>
      {!compact && <span className="thinking-wave"><i /><i /><i /></span>}
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

function CampaignPerformanceReport() {
  return (
    <div className="ad-performance-report">
      <div className="insights">
        <h4>关键洞察</h4>
        <ul>
          <li>本周总消耗 23.54 万，较上周下降 8.7%，线索成本同步下降 11.2%。</li>
          <li>转化主要来自信息流 62%、搜索广告 21% 和达人种草 15%。</li>
          <li>周四晚间点击率环比上升 6%，主要受新品短视频素材拉动。</li>
        </ul>
      </div>
      <div className="chart-card">
        <h4>转化结构（按渠道）</h4>
        <div className="chart-wrap">
          <div className="donut">
            <strong>23,541</strong>
            <span>Leads</span>
          </div>
          <div className="legend">
            <span><i className="c1" />信息流 62%</span>
            <span><i className="c2" />搜索 21%</span>
            <span><i className="c3" />达人 15%</span>
            <span><i className="c4" />其他 2%</span>
          </div>
        </div>
      </div>
      <div className="report-bottom">
        <div className="suggestions">
          {[
            ['优化预算结构', '将高意向人群预算提升至 40%，预计线索成本下降 12%。', Zap, '高'],
            ['提升素材效率', '复用高点击视频脚本并扩展 6 个开头版本，预计点击率提升 8%。', Activity, '中'],
            ['补齐归因链路', '统一落地页、表单和 CRM 回传字段，减少无效线索误判。', Box, '中']
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
  attachments = [],
  onSelectFiles,
  onPasteFiles,
  onRemoveAttachment,
  references = [],
  onRemoveReference,
  activeProject = null,
  projectFiles = [],
  onSelectProjectFile,
  onRemoveProjectFile,
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
  const composerRef = useRef(null);
  const textareaRef = useRef(null);
  const [fileMention, setFileMention] = useState(null);
  const [fileMentionIndex, setFileMentionIndex] = useState(0);
  const [filePickerOpen, setFilePickerOpen] = useState(false);
  const visibleProjectFiles = useMemo(() => filterVisibleProjectFiles(projectFiles), [projectFiles]);
  const projectFileCandidates = useMemo(
    () => filterProjectFileCandidates(visibleProjectFiles, fileMention?.query || '', 8),
    [visibleProjectFiles, fileMention?.query]
  );

  useEffect(() => {
    if (!fileMention && !filePickerOpen) return undefined;
    const closeOnOutside = (event) => {
      if (composerRef.current && !composerRef.current.contains(event.target)) {
        setFileMention(null);
        setFilePickerOpen(false);
      }
    };
    document.addEventListener('pointerdown', closeOnOutside);
    return () => document.removeEventListener('pointerdown', closeOnOutside);
  }, [fileMention, filePickerOpen]);

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

  function insertPromptText(text = '') {
    const nextText = String(text || '').trim();
    if (!nextText) return;
    const textarea = textareaRef.current;
    const currentValue = String(prompt || '');
    if (!textarea) {
      setPrompt(clampComposerPromptText(currentValue ? `${currentValue}\n${nextText}` : nextText));
      return;
    }
    const start = Number.isFinite(textarea.selectionStart) ? textarea.selectionStart : currentValue.length;
    const end = Number.isFinite(textarea.selectionEnd) ? textarea.selectionEnd : start;
    const needsLeadingBreak = start > 0 && currentValue[start - 1] && !/\s/.test(currentValue[start - 1]);
    const needsTrailingBreak = end < currentValue.length && currentValue[end] && !/\s/.test(currentValue[end]);
    const insertion = `${needsLeadingBreak ? '\n' : ''}${nextText}${needsTrailingBreak ? '\n' : ''}`;
    const updated = clampComposerPromptText(`${currentValue.slice(0, start)}${insertion}${currentValue.slice(end)}`);
    const cursor = Math.min(start + insertion.length, updated.length);
    setPrompt(updated);
    window.setTimeout(() => {
      textarea.focus();
      textarea.setSelectionRange(cursor, cursor);
    }, 0);
  }

  function updateProjectFileMention(value = prompt, cursor = textareaRef.current?.selectionStart ?? String(value || '').length) {
    if (!activeProject?.id || !visibleProjectFiles.length) {
      setFileMention(null);
      return;
    }
    const mention = findProjectFileMention(value, cursor);
    setFileMention(mention);
    setFileMentionIndex(0);
  }

  function insertProjectFileReference(file = {}, range = fileMention) {
    if (!file?.name) return;
    const currentValue = String(prompt || '');
    const replacement = `@${file.name} `;
    const textarea = textareaRef.current;
    const fallbackStart = Number.isFinite(textarea?.selectionStart) ? textarea.selectionStart : currentValue.length;
    const fallbackEnd = Number.isFinite(textarea?.selectionEnd) ? textarea.selectionEnd : fallbackStart;
    const start = Number.isFinite(range?.start) ? range.start : fallbackStart;
    const end = Number.isFinite(range?.end) ? range.end : fallbackEnd;
    const prefix = start > 0 && currentValue[start - 1] && !/\s/.test(currentValue[start - 1]) ? ' ' : '';
    const suffix = end < currentValue.length && currentValue[end] && !/\s/.test(currentValue[end]) ? ' ' : '';
    const insertion = `${prefix}${replacement}${suffix}`;
    const nextValue = clampComposerPromptText(`${currentValue.slice(0, start)}${insertion}${currentValue.slice(end)}`);
    const cursor = Math.min(start + insertion.length, nextValue.length);
    setPrompt(nextValue);
    setFileMention(null);
    setFileMentionIndex(0);
    setFilePickerOpen(false);
    onSelectProjectFile?.(file);
    window.setTimeout(() => {
      const textarea = textareaRef.current;
      textarea?.focus?.();
      textarea?.setSelectionRange?.(cursor, cursor);
    }, 0);
  }

  function pickProjectFileMention(file = projectFileCandidates[fileMentionIndex]) {
    if (!file || !fileMention) return;
    insertProjectFileReference(file, fileMention);
  }

  function toggleProjectFilePicker() {
    if (!activeProject?.id || !visibleProjectFiles.length) return;
    setFileMention(null);
    setFilePickerOpen((open) => !open);
  }

  function filesFromDataTransfer(dataTransfer) {
    const files = Array.from(dataTransfer?.items || [])
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter(Boolean);
    return files.length ? files : Array.from(dataTransfer?.files || []);
  }

  function transferText(dataTransfer) {
    const uriList = String(dataTransfer?.getData?.('text/uri-list') || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#'))
      .join('\n');
    return uriList || String(dataTransfer?.getData?.('text/plain') || '').trim();
  }

  function stageTransferredInput(event, dataTransfer) {
    const types = Array.from(dataTransfer?.types || []);
    const files = filesFromDataTransfer(dataTransfer);
    if (files.length) {
      event.preventDefault();
      onPasteFiles?.(files);
      return true;
    }
    const text = transferText(dataTransfer);
    if (text && event.type === 'drop') {
      event.preventDefault();
      insertPromptText(text);
      return true;
    }
    if (text && types.includes('text/uri-list')) {
      event.preventDefault();
      insertPromptText(text);
      return true;
    }
    return false;
  }

  return (
    <div
      ref={composerRef}
      className="composer"
      data-testid="chat-composer"
      onDragOver={(event) => {
        const types = Array.from(event.dataTransfer?.types || []);
        if (types.some((type) => ['Files', 'text/uri-list', 'text/plain'].includes(type))) {
          event.preventDefault();
        }
      }}
      onDrop={(event) => stageTransferredInput(event, event.dataTransfer)}
    >
      <AttachmentPreviewList attachments={attachments} onRemove={onRemoveAttachment} />
      <ComposerReferenceTray references={references} onRemove={onRemoveReference} />
      <textarea
        data-testid="chat-input"
        ref={textareaRef}
        value={prompt}
        onChange={(event) => {
          const nextValue = clampComposerPromptText(event.target.value);
          setPrompt(nextValue);
          updateProjectFileMention(nextValue, event.target.selectionStart);
        }}
        onClick={(event) => {
          setFilePickerOpen(false);
          updateProjectFileMention(prompt, event.currentTarget.selectionStart);
        }}
        onKeyUp={(event) => {
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            updateProjectFileMention(prompt, event.currentTarget.selectionStart);
          }
        }}
        onKeyDown={(event) => {
          if (fileMention) {
            if (event.key === 'ArrowDown') {
              event.preventDefault();
              setFileMentionIndex((index) => Math.min(Math.max(0, projectFileCandidates.length - 1), index + 1));
              return;
            }
            if (event.key === 'ArrowUp') {
              event.preventDefault();
              setFileMentionIndex((index) => Math.max(0, index - 1));
              return;
            }
            if ((event.key === 'Enter' || event.key === 'Tab') && projectFileCandidates.length) {
              event.preventDefault();
              pickProjectFileMention(projectFileCandidates[fileMentionIndex] || projectFileCandidates[0]);
              return;
            }
            if (event.key === 'Escape') {
              event.preventDefault();
              setFileMention(null);
              return;
            }
          }
          if (event.key === 'Enter' && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.isComposing) {
            event.preventDefault();
            sendPrompt(prompt, attachments);
          }
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
            event.preventDefault();
            sendPrompt(prompt, attachments);
          }
        }}
        onPaste={(event) => {
          stageTransferredInput(event, event.clipboardData);
        }}
        placeholder="你可以问我任何问题"
      />
      <ProjectFileMentionMenu
        open={Boolean(fileMention)}
        files={projectFiles}
        query={fileMention?.query || ''}
        selectedIndex={fileMentionIndex}
        onPick={pickProjectFileMention}
        onClose={() => setFileMention(null)}
      />
      <ProjectFilePickerPopover
        open={filePickerOpen}
        files={visibleProjectFiles}
        title="项目文件"
        onPick={(file) => insertProjectFileReference(file, null)}
        onRemove={onRemoveProjectFile}
        onClose={() => setFilePickerOpen(false)}
      />
      <div className="composer-bottom">
        <div className="tool-row">
          <button className="composer-file-button" type="button" onClick={onSelectFiles} title="添加文件">
            <Plus size={18} />
            <span>添加文件</span>
          </button>
          {activeProject?.id && (
            <button className="composer-file-button project-file-toggle" type="button" onClick={toggleProjectFilePicker} title="引用项目文件" disabled={!visibleProjectFiles.length} aria-expanded={filePickerOpen}>
              <FileText size={17} />
              <span>@ 项目文件</span>
            </button>
          )}
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
            title={running ? '追加发送' : '发送'}
            type="button"
            onClick={() => sendPrompt(prompt, attachments)}
          >
            <Send size={24} />
          </button>
          {running && currentSessionId && (
            <button
              className="stop-current"
              data-testid="chat-stop-button"
              title="停止当前任务"
              type="button"
              onClick={() => cancelPrompt()}
            >
              <Pause size={20} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function PermissionSelect({ value, onChange, options }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);
  const selected = options.find((option) => option.value === normalizeAccessMode(value)) || DEFAULT_PERMISSION_OPTION;

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutside);
    return () => document.removeEventListener('pointerdown', closeOnOutside);
  }, [open]);

  return (
    <div
      ref={menuRef}
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
  const menuRef = useRef(null);
  const selected = options.find(([optionValue]) => optionValue === value) || options[0];

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutside);
    return () => document.removeEventListener('pointerdown', closeOnOutside);
  }, [open]);

  return (
    <div
      ref={menuRef}
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

  useEffect(() => {
    const reload = () => refreshProjects({ silent: true });
    window.addEventListener?.('ecorex:projects-changed', reload);
    return () => window.removeEventListener?.('ecorex:projects-changed', reload);
  }, []);

  const currentProject = projectState.currentProject;
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
    </section>
  );
}

function ProjectsView({ backendStatus, refreshBackend, onUnauthorized, setPage, onBack }) {
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
  const [projectSearch, setProjectSearch] = useState('');
  const [sortMode, setSortMode] = useState('activity');
  const [menuProjectId, setMenuProjectId] = useState('');
  const [starredProjectIds, setStarredProjectIds] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem('ecorex-starred-projects') || '[]'));
    } catch {
      return new Set();
    }
  });
  const [recentItems, setRecentItems] = useState(loadRecentChatItems);
  const [projectFiles, setProjectFiles] = useState([]);
  const [projectFilesExpanded, setProjectFilesExpanded] = useState(false);
  const [projectPrompt, setProjectPrompt] = useState('');
  const [projectPromptAttachments, setProjectPromptAttachments] = useState([]);
  const [projectPromptMention, setProjectPromptMention] = useState(null);
  const [projectPromptMentionIndex, setProjectPromptMentionIndex] = useState(0);
  const [projectFilePickerOpen, setProjectFilePickerOpen] = useState(false);
  const [busy, setBusy] = useState('');
  const [fileBusy, setFileBusy] = useState('');
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [projectDetailsOpen, setProjectDetailsOpen] = useState(false);
  const projectChatComposerRef = useRef(null);
  const projectPromptRef = useRef(null);

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
      return current ? (nextCurrent?.id || nextProjects[0]?.id || '') : '';
    });
  }

  useEffect(() => {
    refreshProjects({ silent: true });
  }, [backendStatus?.ok]);

  useEffect(() => {
    const reload = () => refreshProjects({ silent: true });
    window.addEventListener?.('ecorex:projects-changed', reload);
    return () => window.removeEventListener?.('ecorex:projects-changed', reload);
  }, []);

  useEffect(() => {
    const reload = () => setRecentItems(loadRecentChatItems());
    window.addEventListener?.('ecorex:recent-chats-changed', reload);
    window.addEventListener?.('ecorex:recent-chat-upsert', reload);
    return () => {
      window.removeEventListener?.('ecorex:recent-chats-changed', reload);
      window.removeEventListener?.('ecorex:recent-chat-upsert', reload);
    };
  }, []);

  const selectedProject = selectedProjectId
    ? (projectState.projects.find((project) => project.id === selectedProjectId) || projectState.currentProject || null)
    : null;
  const visibleProjectFiles = useMemo(() => filterVisibleProjectFiles(projectFiles), [projectFiles]);

  useEffect(() => {
    setEditDraft(projectDraftFromProject(selectedProject || {}));
    setProjectDetailsOpen(false);
    setProjectPromptAttachments([]);
    setProjectPromptMention(null);
  }, [selectedProject?.id, selectedProject?.updatedAt]);

  async function refreshProjectFiles(project = selectedProject) {
    if (!project?.id || !hasEcorexFunction(['listProjectFiles', 'projects.listFiles'])) {
      setProjectFiles([]);
      return;
    }
    const result = await listManagedProjectFiles(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      return;
    }
    if (result?.ok === false) {
      setProjectFiles([]);
      return;
    }
    setProjectFiles(filterVisibleProjectFiles(result?.files || []));
  }

  useEffect(() => {
    refreshProjectFiles(selectedProject);
  }, [selectedProject?.id, selectedProject?.updatedAt]);

  useEffect(() => {
    if (!selectedProject?.id) return undefined;
    const onFocus = () => refreshProjectFiles(selectedProject);
    const interval = window.setInterval(() => refreshProjectFiles(selectedProject), PROJECT_FILE_REFRESH_INTERVAL_MS);
    window.addEventListener?.('focus', onFocus);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener?.('focus', onFocus);
    };
  }, [selectedProject?.id]);

  useEffect(() => {
    if (!selectedProject?.id) {
      setProjectPromptAttachments((items) => items.filter((item) => item.source !== 'project' && !item.projectId));
      return;
    }
    const available = projectFileIdentitySet(projectFiles);
    setProjectPromptAttachments((items) => items.filter((item) => {
      if (item.source !== 'project' && !item.projectId) return true;
      if (item.projectId !== selectedProject.id) return false;
      const key = projectFileIdentity(item);
      return key && available.has(key);
    }));
  }, [projectFiles, selectedProject?.id]);

  useEffect(() => {
    setProjectFilesExpanded(visibleProjectFiles.length > 0);
  }, [visibleProjectFiles.length]);

  useEffect(() => {
    if (!projectPromptMention && !projectFilePickerOpen) return undefined;
    const closeOnOutside = (event) => {
      if (projectChatComposerRef.current && !projectChatComposerRef.current.contains(event.target)) {
        setProjectPromptMention(null);
        setProjectFilePickerOpen(false);
      }
    };
    document.addEventListener('pointerdown', closeOnOutside);
    return () => document.removeEventListener('pointerdown', closeOnOutside);
  }, [projectPromptMention, projectFilePickerOpen]);

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
      setCreateDialogOpen(false);
      setProjectState((current) => ({ ...current, notice: '项目已创建，并切换为当前项目。' }));
      await refreshProjects({ silent: true, preferId: result.project?.id });
      setSelectedProjectId(result.project?.id || '');
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
      setProjectDetailsOpen(false);
      const updatedProject = normalizeProjectItem(result.project || { ...selectedProject, ...payload }, 0, selectedProject.id);
      updateStoredProjectChatReferences(selectedProject.id, updatedProject.name);
      window.dispatchEvent?.(new CustomEvent('ecorex:projects-changed'));
      window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project: updatedProject } }));
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
      window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project } }));
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

  async function deleteProject(project = selectedProject) {
    if (!project?.id || !projectState.apiReady || !hasEcorexFunction(['deleteProject', 'projects.delete', 'removeProject', 'projects.remove'])) return;
    const confirmed = typeof window === 'undefined' || typeof window.confirm !== 'function'
      ? true
      : window.confirm([
          `删除项目「${project.name}」？`,
          '',
          '这会同步删除该项目在本机工作区中的全部项目文件、项目记忆和会话资源引用。此操作不可撤销。'
        ].join('\n'));
    if (!confirmed) return;
    setBusy(`delete:${project.id}`);
    const result = await deleteManagedProject(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后删除项目。' }));
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: result.missing ? '项目服务未就绪' : `项目删除失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: `项目「${project.name}」及本地文件已删除。` }));
      deleteStoredProjectChatReferences(project.id);
      setSelectedProjectId('');
      window.dispatchEvent?.(new CustomEvent('ecorex:projects-changed'));
      await refreshProjects({ silent: true });
      refreshBackend?.({ refresh: true });
    }
    setBusy('');
  }

  async function openProjectFolder(project = selectedProject) {
    if (!project?.id || !projectState.apiReady || !hasEcorexFunction(['openProjectFolder', 'projects.openFolder', 'projects.open'])) return;
    setBusy(`open:${project.id}`);
    const result = await openManagedProjectFolder(project.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
      setProjectState((current) => ({ ...current, notice: '请重新登录后打开项目目录。' }));
    } else if (result?.ok === false || result?.missing) {
      setProjectState((current) => ({ ...current, notice: `项目目录打开失败：${sanitizeDisplayText(result?.error, '请稍后重试')}` }));
    } else {
      setProjectState((current) => ({ ...current, notice: '' }));
    }
    setBusy('');
  }

  async function openProject(project = {}) {
    if (!project?.id || project.id === 'empty') return;
    setSelectedProjectId(project.id);
    if (!project.current && !project.archived) await switchProject(project);
    else window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project } }));
  }

  function toggleProjectStar(project = {}) {
    if (!project?.id) return;
    setStarredProjectIds((current) => {
      const next = new Set(current);
      if (next.has(project.id)) next.delete(project.id);
      else next.add(project.id);
      try {
        localStorage.setItem('ecorex-starred-projects', JSON.stringify([...next]));
      } catch {
        // Starred state is a local UI preference.
      }
      return next;
    });
  }

  async function addFilesToProject() {
    if (!selectedProject?.id || !hasEcorexFunction(['addProjectFiles', 'projects.addFiles'])) return;
    setFileBusy('add');
    const result = await addManagedProjectFiles(selectedProject.id);
    if (result?.unauthorized) {
      onUnauthorized?.();
    } else if (result?.ok === false) {
      setProjectState((current) => ({ ...current, notice: `项目文件添加失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    } else if (!result?.canceled) {
      setProjectState((current) => ({ ...current, notice: `已加入 ${result.files?.length || 0} 个项目文件。` }));
      await refreshProjectFiles(selectedProject);
      setProjectFilesExpanded(true);
      await refreshProjects({ silent: true, preferId: selectedProject.id });
      window.dispatchEvent?.(new CustomEvent('ecorex:project-files-changed', { detail: { projectId: selectedProject.id } }));
    }
    setFileBusy('');
  }

  async function openProjectFile(file = {}) {
    if (!selectedProject?.id) return;
    setFileBusy(file.id || file.pathLabel || 'open');
    const result = await openManagedProjectFile(selectedProject.id, file);
    if (result?.unauthorized) onUnauthorized?.();
    else if (result?.ok === false) setProjectState((current) => ({ ...current, notice: `项目文件打开失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    setFileBusy('');
  }

  async function removeProjectFile(file = {}) {
    if (!selectedProject?.id || !hasEcorexFunction(['removeProjectFile', 'projects.removeFile'])) return;
    const confirmed = typeof window === 'undefined' || typeof window.confirm !== 'function'
      ? true
      : window.confirm(`从项目中移除「${file.name || '文件'}」？`);
    if (!confirmed) return;
    setFileBusy(file.id || file.pathLabel || 'remove');
    const result = await removeManagedProjectFile(selectedProject.id, file);
    if (result?.unauthorized) onUnauthorized?.();
    else if (result?.ok === false) setProjectState((current) => ({ ...current, notice: `项目文件移除失败：${sanitizeDisplayText(result.error, '请稍后重试')}` }));
    else {
      await refreshProjectFiles(selectedProject);
      await refreshProjects({ silent: true, preferId: selectedProject.id });
      window.dispatchEvent?.(new CustomEvent('ecorex:project-files-changed', { detail: { projectId: selectedProject.id } }));
    }
    setFileBusy('');
  }

  const projectPromptFileCandidates = useMemo(
    () => filterProjectFileCandidates(visibleProjectFiles, projectPromptMention?.query || '', 8),
    [visibleProjectFiles, projectPromptMention?.query]
  );

  function updateProjectPromptMention(value = projectPrompt, cursor = String(value || '').length) {
    if (!selectedProject?.id || !visibleProjectFiles.length) {
      setProjectPromptMention(null);
      return;
    }
    const mention = findProjectFileMention(value, cursor);
    setProjectPromptMention(mention);
    setProjectPromptMentionIndex(0);
  }

  function pickProjectPromptFile(file = projectPromptFileCandidates[projectPromptMentionIndex]) {
    if (!file || !projectPromptMention || !selectedProject?.id) return;
    const replacement = `@${file.name} `;
    const nextValue = clampComposerPromptText(`${projectPrompt.slice(0, projectPromptMention.start)}${replacement}${projectPrompt.slice(projectPromptMention.end)}`);
    setProjectPrompt(nextValue);
    setProjectPromptMention(null);
    setProjectPromptMentionIndex(0);
    addProjectPromptAttachmentFromFile(file);
  }

  function addProjectPromptAttachmentFromFile(file = {}) {
    if (!selectedProject?.id) return;
    setProjectPromptAttachments((items) => {
      const next = projectFileToAttachment(file, selectedProject);
      const byId = new Map(items.map((item) => [item.id || item.path || item.name, item]));
      byId.set(next.id || next.path || next.name, next);
      return [...byId.values()].slice(0, MAX_COMPOSER_ATTACHMENTS);
    });
  }

  function referenceProjectFileInPrompt(file = {}) {
    if (!file?.name) return;
    addProjectPromptAttachmentFromFile(file);
    setProjectPrompt((current) => {
      const token = `@${file.name}`;
      if (String(current || '').includes(token)) return current;
      const separator = current && !/\s$/.test(current) ? ' ' : '';
      return clampComposerPromptText(`${current || ''}${separator}${token} `);
    });
    setProjectPromptMention(null);
    setProjectFilePickerOpen(false);
  }

  function openProjectPromptMentionPicker() {
    if (!selectedProject?.id || !visibleProjectFiles.length) return;
    setProjectPromptMention(null);
    setProjectFilePickerOpen((open) => !open);
  }

  function removeProjectPromptAttachment(id) {
    setProjectPromptAttachments((items) => items.filter((item) => item.id !== id));
  }

  function routeProjectSession(session = {}) {
    if (!session?.id) return;
    window.dispatchEvent?.(new CustomEvent('ecorex:open-chat', { detail: session }));
    setPage?.('chat');
  }

  async function routeProjectPrompt() {
    const cleanPrompt = projectPrompt.trim();
    if (!selectedProject?.id || !cleanPrompt) return;
    if (!selectedProject.current && !selectedProject.archived) await switchProject(selectedProject);
    const id = createLocalId('conversation');
    const item = {
      id,
      claudeSessionId: id,
      title: cleanPrompt.slice(0, 44),
      time: recentChatTimeLabel(),
      updatedAt: Date.now(),
      projectId: selectedProject.id,
      projectName: selectedProject.name
    };
    const nextItems = upsertRecentChatItem(loadRecentChatItems(), item);
    storeRecentChatItems(nextItems);
    setRecentItems(nextItems);
    window.dispatchEvent?.(new CustomEvent('ecorex:new-chat', { detail: { ...item, initialPrompt: cleanPrompt, initialAttachments: projectPromptAttachments } }));
    window.dispatchEvent?.(new CustomEvent('ecorex:project-context', { detail: { project: selectedProject } }));
    window.dispatchEvent?.(new CustomEvent('ecorex:recent-chats-changed'));
    setProjectPrompt('');
    setProjectPromptAttachments([]);
    setProjectPromptMention(null);
    setPage?.('chat');
  }

  const activeProjects = projectState.projects.filter((project) => !project.archived);
  const archivedProjects = projectState.projects.filter((project) => project.archived);
  const canCreateProject = projectState.apiReady && hasEcorexFunction(['createProject', 'projects.create']);
  const canUpdateProject = projectState.apiReady && hasEcorexFunction(['updateProject', 'projects.update']);
  const canArchiveProject = projectState.apiReady && hasEcorexFunction(['archiveProject', 'projects.archive', 'updateProject', 'projects.update']);
  const canSwitchProject = projectState.apiReady && hasEcorexFunction(['switchProject', 'projects.switch']);
  const canDeleteProject = projectState.apiReady && hasEcorexFunction(['deleteProject', 'projects.delete', 'removeProject', 'projects.remove']);
  const projectSessions = selectedProject
    ? recentItems.filter((item) => item.projectId === selectedProject.id)
    : [];
  const filteredProjects = projectState.projects
    .filter((project) => {
      const query = projectSearch.trim().toLowerCase();
      if (!query) return true;
      return [project.name, project.description, project.client, project.goal, project.statusLabel]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    })
    .sort((left, right) => {
      if (starredProjectIds.has(left.id) !== starredProjectIds.has(right.id)) {
        return starredProjectIds.has(left.id) ? -1 : 1;
      }
      if (sortMode === 'name') return String(left.name).localeCompare(String(right.name), 'zh-CN');
      if (sortMode === 'created') return String(right.id).localeCompare(String(left.id));
      return (new Date(right.updatedAt || 0).getTime() || 0) - (new Date(left.updatedAt || 0).getTime() || 0);
    });
  const listProjects = filteredProjects.length
    ? filteredProjects
    : (projectState.projects.length ? [] : [{ id: 'empty', name: projectState.apiReady ? '暂无项目' : '项目服务未就绪', statusLabel: projectState.status }]);

  return (
    <section className={`projects-page panel ${selectedProject ? 'project-detail-mode' : 'project-list-mode'}`} title="项目">
      {!selectedProject ? (
        <div className="projects-home-shell">
          <header className="projects-home-head">
            <div className="projects-page-title">
              {onBack && (
                <button className="view-back-button" type="button" onClick={onBack}>
                  <ChevronLeft size={16} />
                  返回
                </button>
              )}
              <h1>项目管理</h1>
            </div>
            <div className="projects-home-actions">
              <label>
                排序
                <select value={sortMode} onChange={(event) => setSortMode(event.target.value)}>
                  <option value="activity">最近活动</option>
                  <option value="name">名称</option>
                  <option value="created">创建时间</option>
                </select>
              </label>
              <button type="button" data-testid="projects-new-button" onClick={() => setCreateDialogOpen(true)}>
                <Plus size={16} />
                新建项目
              </button>
            </div>
          </header>

          <div className="projects-search">
            <Search size={18} />
            <input value={projectSearch} onChange={(event) => setProjectSearch(event.target.value)} placeholder="搜索项目..." />
          </div>

          <section className="projects-list-panel visual-hidden-panel" data-testid="projects-list-panel" aria-label="项目列表" />
          <div className="projects-card-grid">
            {listProjects.map((project) => (
              <article
                className={`project-home-card project-list-entry ${project.current ? 'active' : ''} ${project.archived ? 'archived' : ''}`}
                data-testid="projects-list-entry"
                key={project.id}
                role="button"
                tabIndex={project.id === 'empty' ? -1 : 0}
                onClick={() => openProject(project)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') openProject(project);
                }}
              >
                <div className="project-card-actions">
                  <button type="button" title="更多" onClick={(event) => { event.stopPropagation(); setMenuProjectId((current) => current === project.id ? '' : project.id); }}>
                    <MoreHorizontal size={17} />
                  </button>
                </div>
                <strong>{project.name}</strong>
                <span>{project.updatedAt ? `更新于 ${formatDateTime(project.updatedAt)}` : project.statusLabel || '最近更新'}</span>
                <em>{projectBusinessSummary(project)}</em>
                <span>{project.sessionCount || 0} 个会话 · {project.fileCount || 0} 个文件</span>
                {menuProjectId === project.id && project.id !== 'empty' && (
                  <div className="project-card-menu" onClick={(event) => event.stopPropagation()}>
                    <button type="button" onClick={() => { toggleProjectStar(project); setMenuProjectId(''); }}>
                      <Star size={17} />
                      {starredProjectIds.has(project.id) ? '取消收藏' : '收藏'}
                    </button>
                    <button type="button" onClick={() => { setSelectedProjectId(project.id); setMenuProjectId(''); }}>
                      <Pencil size={17} />
                      编辑详情
                    </button>
                    <span />
                    <button type="button" onClick={() => { toggleArchive(project); setMenuProjectId(''); }} disabled={!canArchiveProject}>
                      <Archive size={17} />
                      {project.archived ? '恢复' : '归档'}
                    </button>
                    <button className="danger" type="button" onClick={() => { deleteProject(project); setMenuProjectId(''); }} disabled={!canDeleteProject}>
                      <X size={17} />
                      删除
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </div>
      ) : (
        <div className="project-detail-shell">
          <header className="project-detail-hero">
            <div className="projects-page-title">
              <button className="project-back-button" type="button" onClick={() => setSelectedProjectId('')}>
                <ChevronLeft size={17} />
                全部项目
              </button>
              <h1>项目管理</h1>
            </div>
            <div className="project-detail-actions compact">
              <button type="button" title={starredProjectIds.has(selectedProject.id) ? '取消收藏' : '收藏'} onClick={() => toggleProjectStar(selectedProject)}>
                <Star size={17} />
              </button>
              <button type="button" data-testid="project-detail-open-folder" onClick={() => openProjectFolder(selectedProject)} disabled={busy === `open:${selectedProject.id}`}>
                {busy === `open:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <FolderOpen size={15} />}
                打开目录
              </button>
              <button type="button" data-testid="project-detail-edit-button" onClick={() => setProjectDetailsOpen(true)} disabled={!canUpdateProject}>
                <Pencil size={15} />
                编辑资料
              </button>
              <button type="button" data-testid="project-detail-save" onClick={saveProject} disabled={!canUpdateProject || !editDraft.name.trim() || busy === `save:${selectedProject.id}`}>
                {busy === `save:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <Check size={15} />}
                保存
              </button>
              <button type="button" data-testid="project-detail-archive" onClick={() => toggleArchive(selectedProject)} disabled={!canArchiveProject || busy === `archive:${selectedProject.id}`}>
                {busy === `archive:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <Archive size={15} />}
                {selectedProject.archived ? '恢复' : '归档'}
              </button>
              <button className="danger" type="button" data-testid="project-detail-delete" onClick={() => deleteProject(selectedProject)} disabled={!canDeleteProject || busy === `delete:${selectedProject.id}`}>
                {busy === `delete:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <X size={15} />}
                删除
              </button>
            </div>
          </header>

          <input className="project-title-input" data-testid="project-edit-name" value={editDraft.name} onChange={(event) => updateEditField('name', event.target.value)} disabled={!canUpdateProject} />

          <div className="project-chat-composer" ref={projectChatComposerRef}>
            <AttachmentPreviewList attachments={projectPromptAttachments} onRemove={removeProjectPromptAttachment} compact />
            <textarea
              data-testid="project-prompt-input"
              ref={projectPromptRef}
              value={projectPrompt}
              onChange={(event) => {
                const nextValue = clampComposerPromptText(event.target.value);
                setProjectPrompt(nextValue);
                updateProjectPromptMention(nextValue, event.target.selectionStart);
              }}
              onClick={(event) => {
                setProjectFilePickerOpen(false);
                updateProjectPromptMention(projectPrompt, event.currentTarget.selectionStart);
              }}
              onKeyDown={(event) => {
                if (projectPromptMention) {
                  if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    setProjectPromptMentionIndex((index) => Math.min(Math.max(0, projectPromptFileCandidates.length - 1), index + 1));
                    return;
                  }
                  if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    setProjectPromptMentionIndex((index) => Math.max(0, index - 1));
                    return;
                  }
                  if ((event.key === 'Enter' || event.key === 'Tab') && projectPromptFileCandidates.length) {
                    event.preventDefault();
                    pickProjectPromptFile(projectPromptFileCandidates[projectPromptMentionIndex] || projectPromptFileCandidates[0]);
                    return;
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault();
                    setProjectPromptMention(null);
                  }
                }
              }}
              placeholder="今天想推进什么项目任务？"
            />
            <ProjectFileMentionMenu
              open={Boolean(projectPromptMention)}
              files={projectFiles}
              query={projectPromptMention?.query || ''}
              selectedIndex={projectPromptMentionIndex}
              onPick={pickProjectPromptFile}
              onClose={() => setProjectPromptMention(null)}
            />
            <ProjectFilePickerPopover
              open={projectFilePickerOpen}
              files={visibleProjectFiles}
              title="项目文件"
              onPick={referenceProjectFileInPrompt}
              onRemove={removeProjectFile}
              onClose={() => setProjectFilePickerOpen(false)}
            />
            <div className="project-chat-actions">
              <button type="button" title="添加项目文件" onClick={addFilesToProject} disabled={fileBusy === 'add'}>
                {fileBusy === 'add' ? <Loader2 size={18} className="spin-icon" /> : <Plus size={20} />}
              </button>
              <button type="button" title="引用项目文件" onClick={openProjectPromptMentionPicker} disabled={!visibleProjectFiles.length} aria-expanded={projectFilePickerOpen}>
                <FileText size={18} />
              </button>
              <button className="project-file-summary-button" type="button" onClick={openProjectPromptMentionPicker} disabled={!visibleProjectFiles.length} aria-expanded={projectFilePickerOpen}>
                <FolderOpen size={15} />
                <span>
                  <strong>{visibleProjectFiles.length ? `${visibleProjectFiles.length} 个项目文件` : '暂无可引用文件'}</strong>
                  <em>{selectedProject.statusLabel} · 点击查看并 @ 到会话</em>
                </span>
              </button>
              <button className="project-send-button" type="button" title="进入项目会话" onClick={routeProjectPrompt} disabled={!projectPrompt.trim()}>
                <Send size={18} />
              </button>
            </div>
          </div>

          <section className="project-detail-panel" data-testid="project-detail-panel">
            <div className="project-detail-content">
              <section className="project-thread-panel">
                <header>
                  <div>
                    <h3>项目会话</h3>
                    <small>{projectSessions.length ? `${projectSessions.length} 个项目会话` : '暂无项目会话'}</small>
                  </div>
                </header>
                <div className="project-session-list">
                  {projectSessions.length ? projectSessions.map((session) => (
                    <div className="project-session-entry" key={session.id}>
                      <button type="button" onClick={() => routeProjectSession(session)}>
                        <strong>{session.title}</strong>
                        <span>最近消息 {session.time || '刚刚'}</span>
                      </button>
                    </div>
                  )) : (
                    <div className="project-empty-state compact">
                      <Bot size={28} />
                      <strong>从上方输入开始项目会话</strong>
                      <span>这里创建的新会话会自动路由到当前项目。</span>
                    </div>
                  )}
                </div>
              </section>

              <section className="project-context-panel">
                <button className="project-context-row" type="button" onClick={() => setProjectDetailsOpen(true)}>
                  <div>
                    <strong>项目资料与指令</strong>
                    <span>点击展开填写客户、目标、预算、周期、交付物和长期指令。</span>
                  </div>
                  <Pencil size={20} />
                </button>
                <div className="project-context-summary">
                  <div>
                    <span>客户 / 品牌</span>
                    <strong>{editDraft.client || '未填写'}</strong>
                  </div>
                  <div>
                    <span>目标</span>
                    <strong>{editDraft.goal || '未填写'}</strong>
                  </div>
                  <div>
                    <span>预算</span>
                    <strong>{editDraft.budget || '未填写'}</strong>
                  </div>
                  <div>
                    <span>周期</span>
                    <strong>{editDraft.period || '未填写'}</strong>
                  </div>
                  <div className="wide">
                    <span>项目指令</span>
                    <strong>{editDraft.instructions || '未填写'}</strong>
                  </div>
                </div>

                <div className="project-files-block">
                  <header>
                    <div>
                      <strong>项目文件</strong>
                      <span>添加 PDF、文档或其他资料，供当前项目引用；project-memory.md 用于保存长期记忆。</span>
                    </div>
                    <button type="button" data-testid="project-file-add" onClick={addFilesToProject} disabled={fileBusy === 'add'}>
                      {fileBusy === 'add' ? <Loader2 size={17} className="spin-icon" /> : <Plus size={20} />}
                    </button>
                  </header>
                  {visibleProjectFiles.length ? (
                    <div className={`project-file-tree ${projectFilesExpanded ? 'expanded' : ''}`} data-testid="project-file-tree">
                      <button className="project-file-tree-root" type="button" onClick={() => setProjectFilesExpanded((value) => !value)}>
                        {projectFilesExpanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
                        <FolderOpen size={18} />
                        <span>
                          <strong>files</strong>
                          <em>{visibleProjectFiles.length} 个文件 · {projectFilesExpanded ? '点击收起' : '点击展开文件树'}</em>
                        </span>
                      </button>
                      {projectFilesExpanded && (
                        <ProjectFileTreeNode
                          node={buildProjectFileTree(visibleProjectFiles)}
                          onOpen={openProjectFile}
                          onReference={referenceProjectFileInPrompt}
                          onRemove={removeProjectFile}
                          fileBusy={fileBusy}
                        />
                      )}
                    </div>
                  ) : (
                    <button className="project-file-drop" type="button" onClick={addFilesToProject}>
                      <Upload size={30} />
                      <span>添加 PDF、文档或其他文本资料，供当前项目引用。</span>
                    </button>
                  )}
                </div>
              </section>
            </div>
          </section>
        </div>
      )}
      {createDialogOpen && (
        <div className="modal-backdrop project-modal-backdrop" role="presentation" onMouseDown={() => setCreateDialogOpen(false)}>
          <section className="project-editor-modal" role="dialog" aria-modal="true" aria-label="新建项目" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>新项目</span>
                <h3>创建项目</h3>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setCreateDialogOpen(false)}>
                <X size={18} />
              </button>
            </header>
            <form className="project-edit-form project-modal-form" onSubmit={createProject}>
              <label className="wide">
                <span>项目名称</span>
                <input data-testid="projects-create-name" value={createDraft.name} onChange={(event) => updateCreateField('name', event.target.value)} placeholder="例如：Q2 搜索广告增长计划" disabled={!canCreateProject || busy === 'create'} autoFocus />
              </label>
              <label>
                <span>客户 / 品牌</span>
                <input value={createDraft.client} onChange={(event) => updateCreateField('client', event.target.value)} disabled={!canCreateProject || busy === 'create'} />
              </label>
              <label>
                <span>目标</span>
                <input value={createDraft.goal} onChange={(event) => updateCreateField('goal', event.target.value)} disabled={!canCreateProject || busy === 'create'} />
              </label>
              <label>
                <span>预算</span>
                <input value={createDraft.budget} onChange={(event) => updateCreateField('budget', event.target.value)} disabled={!canCreateProject || busy === 'create'} />
              </label>
              <label>
                <span>周期</span>
                <input value={createDraft.period} onChange={(event) => updateCreateField('period', event.target.value)} disabled={!canCreateProject || busy === 'create'} />
              </label>
              <label className="wide">
                <span>项目指令</span>
                <textarea value={createDraft.instructions} onChange={(event) => updateCreateField('instructions', event.target.value)} disabled={!canCreateProject || busy === 'create'} placeholder="例如：默认使用中文；输出面向广告投放负责人；复盘时优先关注 CTR/CVR/CPA。" />
              </label>
              <footer className="project-modal-footer">
                <button type="button" onClick={() => setCreateDialogOpen(false)}>取消</button>
                <button className="primary" type="submit" data-testid="projects-create-submit" disabled={!canCreateProject || !createDraft.name.trim() || busy === 'create'}>
                  {busy === 'create' ? <Loader2 size={15} className="spin-icon" /> : <Plus size={15} />}
                  创建
                </button>
              </footer>
            </form>
          </section>
        </div>
      )}
      {projectDetailsOpen && selectedProject && (
        <div className="modal-backdrop project-modal-backdrop" role="presentation" onMouseDown={() => setProjectDetailsOpen(false)}>
          <section className="project-editor-modal" role="dialog" aria-modal="true" aria-label="编辑项目资料" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>项目资料</span>
                <h3>编辑项目资料与指令</h3>
              </div>
              <button type="button" aria-label="关闭" onClick={() => setProjectDetailsOpen(false)}>
                <X size={18} />
              </button>
            </header>
            <div className="project-edit-form project-modal-form">
              <label className="wide">
                <span>项目名称</span>
                <input data-testid="project-modal-edit-name" value={editDraft.name} onChange={(event) => updateEditField('name', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label className="wide">
                <span>项目指令</span>
                <textarea data-testid="project-edit-instructions" value={editDraft.instructions} onChange={(event) => updateEditField('instructions', event.target.value)} disabled={!canUpdateProject} placeholder="例如：输出面向广告投放负责人；默认使用中文；复盘时优先关注 CTR/CVR/CPA。" />
              </label>
              <label>
                <span>客户 / 品牌</span>
                <input data-testid="project-edit-client" value={editDraft.client} onChange={(event) => updateEditField('client', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label>
                <span>目标</span>
                <input data-testid="project-edit-goal" value={editDraft.goal} onChange={(event) => updateEditField('goal', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label>
                <span>预算</span>
                <input data-testid="project-edit-budget" value={editDraft.budget} onChange={(event) => updateEditField('budget', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label>
                <span>周期</span>
                <input data-testid="project-edit-period" value={editDraft.period} onChange={(event) => updateEditField('period', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label>
                <span>行业</span>
                <input value={editDraft.industry} onChange={(event) => updateEditField('industry', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label>
                <span>场景</span>
                <input value={editDraft.scenario} onChange={(event) => updateEditField('scenario', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <label className="wide">
                <span>交付物</span>
                <textarea data-testid="project-edit-deliverables" value={editDraft.deliverablesText} onChange={(event) => updateEditField('deliverablesText', event.target.value)} disabled={!canUpdateProject} />
              </label>
              <footer className="project-modal-footer">
                <button type="button" onClick={() => setProjectDetailsOpen(false)}>取消</button>
                <button className="primary" type="button" data-testid="project-detail-modal-save" onClick={saveProject} disabled={!canUpdateProject || !editDraft.name.trim() || busy === `save:${selectedProject.id}`}>
                  {busy === `save:${selectedProject.id}` ? <Loader2 size={15} className="spin-icon" /> : <Check size={15} />}
                  保存
                </button>
              </footer>
            </div>
          </section>
        </div>
      )}
      {projectState.notice && <p className="diagnostics-notice compact project-page-notice">{projectState.notice}</p>}
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

function isPublicTraceItem(item = []) {
  const [label = '', status = '', , tone = '', kind = ''] = item;
  if (/能力索引|source map|57MB/i.test(`${label} ${status}`)) return false;
  const publicKinds = new Set(['tool', 'ledger', 'attachment', 'recovery', 'stderr', 'result', 'done', 'cancelled', 'error', 'timeout']);
  if (tone === 'danger') return true;
  if (publicKinds.has(kind)) return true;
  if (kind === 'debug' || kind === 'assistant') return false;
  if (kind === 'status') {
    const text = `${label} ${status}`.toLowerCase();
    return /权限|授权|确认|等待|工具|命令|文件|项目|联网|检索|读取|整理|错误|异常|失败|超时/.test(text);
  }
  return false;
}

function traceDisclosureSummary(items = []) {
  const doneCount = items.filter((item) => item[3] === 'success').length;
  const latestActiveIndex = items.reduce((latest, item, index) => (
    ['running', 'pending', 'warn'].includes(item[3]) ? index : latest
  ), -1);
  const latestTerminalIndex = items.reduce((latest, item, index) => (
    isTerminalTimelineItem(item) ? index : latest
  ), -1);
  if (latestTerminalIndex >= 0 && latestTerminalIndex >= latestActiveIndex) {
    const terminal = items[latestTerminalIndex];
    return { label: terminal[0], status: terminal[1], tone: terminal[3], doneCount };
  }
  const active = [...items].reverse().find((item) => ['running', 'pending', 'warn'].includes(item[3]));
  if (active) return { label: active[0], status: active[1], tone: active[3], doneCount };
  const last = items[items.length - 1];
  return last ? { label: last[0], status: last[1], tone: last[3], doneCount } : null;
}

function publicTraceItems(timeline = []) {
  const items = timeline.filter(isPublicTraceItem);
  const seen = new Set();
  return items.filter((item) => {
    const key = `${item[0]}-${item[1]}-${item[3]}-${item[4] || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isHighRiskPermissionText(value = '') {
  const text = String(value || '').toLowerCase();
  if (!text) return false;
  const localRisk = /(文件|写入|修改|删除|覆盖|移动|重命名|命令|终端|powershell|bash|shell|系统目录|本地目录|工作区|磁盘)/i.test(text);
  const genericPermission = /(权限|授权|确认|允许|继续执行)/i.test(text) && localRisk;
  return localRisk || genericPermission;
}

function summarizePermissionRequestText(value = '') {
  const text = cleanAssistantOutputText(value)
    .replace(/请\s*回复\s*[“"']?(继续|确认|同意|允许一次)[”"']?[^。！？!?\n]*(。|！|!|？|\?)?/gi, '')
    .replace(/用户确认[“"']?(是|继续|确认|允许一次)[”"']?后继续[。！？!?\s]*/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!text) return 'EcoreX 请求执行一步本地操作，请确认是否允许。';
  return text.length > 180 ? `${text.slice(0, 180)}...` : text;
}

function permissionRequestFromMessage(message = {}, timeline = [], options = {}) {
  if (message.role !== 'assistant') return null;
  if (!options.includeResolved && message.permissionDecision?.status) return null;
  const text = cleanAssistantOutputText(message.text || '');
  const traceText = publicTraceItems(timeline).slice(-5).map((item) => `${item[0]} ${item[1]}`).join(' ');
  const candidate = `${text} ${traceText}`;
  if (!isHighRiskPermissionText(candidate)) return null;
  if (!/(是否|要不要|请确认|允许|授权|等待确认|确认后|继续执行|可以执行|approve|permission)/i.test(candidate)) return null;
  return {
    title: '需要确认本地操作',
    description: '这一步可能涉及文件、命令或系统目录访问。请选择是否允许，EcoreX 会在当前会话继续执行，不会新开会话。',
    action: summarizePermissionRequestText(text || traceText)
  };
}

function InlineAgentTrace({ timeline = [], sourceMap, priority = 'normal' }) {
  const [expanded, setExpanded] = useState(false);
  const publicItems = useMemo(() => publicTraceItems(timeline), [timeline]);
  const summary = useMemo(() => traceDisclosureSummary(publicItems), [publicItems]);
  const visibleItems = useMemo(() => (expanded ? publicItems.slice(-6) : []), [expanded, publicItems]);
  const expandable = publicItems.length > 4 || publicItems.some((item) => ['danger', 'warn', 'pending'].includes(item[3]));

  if (!summary) return null;

  return (
    <div className={`agent-trace ${expanded ? 'expanded' : 'compact'} ${priority === 'low' ? 'low-priority' : ''}`}>
      <button className="agent-trace-summary" type="button" onClick={() => setExpanded((value) => !value)}>
        <span className={`agent-trace-node ${summary.tone}`} />
        <strong>{summary.label}</strong>
        <em>{summary.status}</em>
        {expandable && <b>{expanded ? '收起' : `展开 ${publicItems.length} 步`}</b>}
      </button>
      {expanded && (
        <div className="agent-trace-list">
          {visibleItems.map(([label, status, time, tone], index) => (
            <div className={`agent-trace-row ${tone}`} key={`${label}-${index}`}>
              <span className={`agent-trace-node ${tone}`} />
              <strong>{label}</strong>
              <em>{status}</em>
              <small>{time}</small>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function InlinePermissionRequest({ request, onReply }) {
  if (!request) return null;
  const options = [
    ['允许一次', '允许一次，继续执行上一轮请求的本地操作。'],
    ['拒绝', '拒绝本次本地操作，请给出无需该操作的替代方案。'],
    ['只做计划', '先不要执行本地操作，请改为输出只读计划和风险说明。']
  ];
  return (
    <div className="inline-permission-request">
      <ShieldCheck size={18} />
      <div>
        <strong>{request.title}</strong>
        <span>{request.description}</span>
      </div>
      <div className="inline-permission-actions">
        {options.map(([label, reply]) => (
          <button key={label} type="button" onClick={() => onReply?.(reply)}>
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

function PermissionConfirmationModal({ request, onReply }) {
  if (!request) return null;
  return (
    <div className="permission-confirmation-backdrop" role="presentation">
      <div
        className="permission-confirmation-dialog inline-permission-request"
        role="dialog"
        aria-modal="true"
        aria-labelledby="permission-confirmation-title"
        data-testid="permission-confirmation-modal"
      >
        <ShieldCheck size={18} />
        <div>
          <strong id="permission-confirmation-title">{request.title}</strong>
          {request.action && <span className="permission-confirmation-action">{request.action}</span>}
          <span>{request.description}</span>
        </div>
        <div className="inline-permission-actions">
          <button type="button" onClick={() => onReply?.('允许一次')}>允许一次</button>
          <button type="button" onClick={() => onReply?.('拒绝')}>拒绝</button>
          <button type="button" onClick={() => onReply?.('只做计划')}>只做计划</button>
        </div>
      </div>
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
  return !['complete', 'completed', 'done', 'idle', 'cancelled', 'canceled', 'stopped', 'interrupted', 'error', 'failed', 'timeout'].includes(normalized);
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
  const conversationId = String(raw.conversationId || raw.conversation_id || raw.chatId || raw.chat_id || '').trim();
  const claudeSessionId = String(raw.claudeSessionId || raw.claude_session_id || raw.nativeSessionId || raw.native_session_id || '').trim();
  const projectId = String(raw.projectId || raw.project_id || '').trim();
  const projectName = sanitizeDisplayText(raw.projectName || raw.project_name || raw.project?.name || '', '');
  return {
    id: sessionId,
    sessionId,
    conversationId,
    claudeSessionId,
    status,
    state: raw.state || status,
    title: sanitizeDisplayText(raw.title || raw.name || `会话 ${index + 1}`, `会话 ${index + 1}`),
    prompt,
    messageId: raw.messageId || raw.message_id || '',
    projectId,
    projectName,
    projectPath: raw.projectPath || raw.project_path || '',
    accessMode,
    permissionMode: permissionModeFromAccessMode(accessMode),
    accessLabel: permissionOptionByValue(accessMode).label,
    recoverable: boolFrom(raw.recoverable ?? raw.canResume ?? raw.resumeAvailable ?? raw.pendingRecovery, false),
    retryable: boolFrom(raw.retryable ?? raw.canRetry, false),
    recoveryStatus: raw.recoveryStatus || raw.recoveryState || '',
    recoveryHint: raw.recoveryHint || raw.detail || raw.message || '',
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
      conversationId: row.conversationId || existing.conversationId || '',
      claudeSessionId: row.claudeSessionId || existing.claudeSessionId || '',
      projectId: row.projectId || existing.projectId || '',
      projectName: row.projectName || existing.projectName || '',
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

function shouldSurfaceAgentSession(row = {}) {
  return isRunningSessionActive(row)
    || Boolean(row.recoverable || row.retryable || row.recoveryStatus || row.recoveryHint);
}

function extractAgentSessionRows(result) {
  const rows = extractCollection(result, ['sessions', 'runningSessions', 'activeSessions', 'items', 'data']);
  return rows.map((session, index) => normalizeRunningSession(session, index, 'api')).filter(shouldSurfaceAgentSession);
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
  if (status) return localizedStatusLabel(status);
  const labels = {
    success: '在线',
    running: '连接中',
    danger: '异常',
    pending: '待连接'
  };
  return labels[tone] || '未知';
}

function localizedStatusLabel(status, fallback = '未知') {
  const text = String(status || '').trim();
  if (!text) return fallback;
  const normalized = text.toLowerCase();
  const labels = {
    ok: '正常',
    success: '正常',
    ready: '已就绪',
    online: '在线',
    connected: '已连接',
    authenticated: '已认证',
    valid: '有效',
    present: '已检测',
    configured: '已配置',
    running: '运行中',
    active: '运行中',
    starting: '启动中',
    loading: '加载中',
    checking: '检测中',
    pending: '等待中',
    syncing: '同步中',
    timeout: '已超时',
    failed: '异常',
    error: '异常',
    missing: '缺失',
    absent: '缺失',
    invalid: '无效',
    disabled: '已禁用',
    inactive: '未启用',
    offline: '离线',
    disconnected: '已断开',
    incomplete: '未完成',
    skipped: '已跳过',
    partial: '部分完成',
    idle: '待命'
  };
  return labels[normalized] || sanitizeDisplayText(text, fallback);
}

function sanitizeDisplayText(value, fallback = '信息待返回') {
  const text = String(value || '').trim();
  if (!text) return fallback;
  if (/([A-Za-z]:\\|\\\\|\/\.|\/Users\/|\/home\/|node_modules|cache|projectPath|installPath|\braw\b|\bpath\b)/i.test(text)) {
    return fallback;
  }
  return text
    .replace(/\[EcoreX capability running\]/gi, 'EcoreX 正在调用原生能力')
    .replace(/--dangerously-skip-permissions/gi, '完全访问权限')
    .replace(/\bbypassPermissions\b/gi, '完全访问权限')
    .replace(/\bfullAccess\b/gi, '完全访问权限')
    .replace(/\bClaude MCP\b/gi, 'EcoreX MCP')
    .replace(/\bClaude\s*Code\b/gi, '本地能力')
    .replace(/\bClaude\b/gi, '本地')
    .replace(/\bAgent\b/gi, '亦芯助手')
    .replace(/\bCLI\b/g, '本地')
    .replace(/\bMCP\b/g, 'MCP')
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
  if (!text || /mcp\.|\/mcp|command|stdio/i.test(text)) return 'EcoreX MCP 端点';
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
    autoRefreshBackend: raw.autoRefreshBackend !== false,
    anonymousTelemetryEnabled: raw.anonymousTelemetryEnabled === true,
    telemetryEndpoint: raw.telemetryEndpoint || '',
    telemetryInstallId: raw.telemetryInstallId || ''
  };
}

function pickFirstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== '');
}

function formatHealthValue(value, fallback = '待返回') {
  if (typeof value === 'boolean') return value ? '正常' : '需检查';
  if (typeof value === 'number') return String(value);
  return localizedStatusLabel(value, fallback);
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
  const bridge = diagnostics?.agentBridge
    || diagnostics?.runtimeEngine
    || diagnostics?.runtime
    || backendStatus?.agentBridge
    || backendStatus?.runtimeEngine
    || backendStatus?.runtime
    || {};
  const version = pickFirstDefined(bridge.version, diagnostics?.claude?.version, backendStatus?.claude?.version);
  const rawStatus = pickFirstDefined(bridge.status, bridge.state, bridge.health, bridge.ready);
  const available = pickFirstDefined(bridge.available, bridge.ok, bridge.ready, bridge.path, bridge.command, Boolean(version));
  const tone = toneFromHealth(pickFirstDefined(rawStatus, available), available ? 'ok' : 'warn');
  return {
    value: version ? sanitizeDisplayText(version, '已就绪') : (available ? localizedStatusLabel(rawStatus, '已就绪') : localizedStatusLabel(rawStatus, '未检测')),
    detail: available ? '本地运行引擎可用' : '等待运行引擎',
    tone
  };
}

function startupPreloadSummary(startupState = {}) {
  const state = startupState || {};
  const normalized = String(state.status || '').toLowerCase();
  if (!window.ecorex && !normalized) {
    return { value: '预览模式', detail: '桌面预加载不可用', tone: 'running' };
  }
  const tone = ['ready', 'complete', 'completed', 'success'].includes(normalized)
    ? 'ok'
    : (['loading', 'running', 'partial', 'timeout', 'skipped'].includes(normalized) ? 'running' : 'warn');
  return {
    value: state.label || localizedStatusLabel(normalized, window.ecorex ? '预加载中' : '预览模式'),
    detail: sanitizeDisplayText(state.detail, tone === 'ok' ? '启动预加载已完成' : '预加载状态待更新'),
    tone
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
    instructions: '',
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
    instructions: project.instructions || '',
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
    instructions: String(draft.instructions || '').trim(),
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
    instructions: sanitizeDisplayText(raw.instructions || raw.systemInstructions || raw.projectInstructions, ''),
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

async function deleteManagedProject(projectId) {
  return callEcorexAction(['deleteProject', 'projects.delete', 'removeProject', 'projects.remove'], {
    id: projectId,
    projectId,
    confirmDelete: true,
    deleteFilesConfirmed: true
  });
}

async function openManagedProjectFolder(projectId) {
  return callEcorexAction(['openProjectFolder', 'projects.openFolder', 'projects.open'], { id: projectId, projectId });
}

async function listManagedProjectFiles(projectId) {
  return callEcorexAction(['listProjectFiles', 'projects.listFiles'], { id: projectId, projectId });
}

async function addManagedProjectFiles(projectId) {
  return callEcorexAction(['addProjectFiles', 'projects.addFiles'], { id: projectId, projectId });
}

async function openManagedProjectFile(projectId, file = {}) {
  return callEcorexAction(['openProjectFile', 'projects.openFile'], { ...file, id: projectId, projectId });
}

async function removeManagedProjectFile(projectId, file = {}) {
  return callEcorexAction(['removeProjectFile', 'projects.removeFile'], { ...file, id: projectId, projectId });
}

function DiagnosticsView({ backendStatus, backendError, capabilities, authStatus, startupState, refreshBackend, onUnauthorized, embedded = false }) {
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
    const result = await loadModelProfiles(settings.defaultModel || DEFAULT_AGENT_MODEL_NAME);
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
  const startupHealth = startupPreloadSummary(startupState || window.__ecorexStartupState);
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
    ['预加载', startupHealth.value, startupHealth.detail, Loader2, startupHealth.tone],
    ['工作区', workspace.workspaceRoot || settings.workspaceRoot || backendStatus?.workspaceRoot ? '已配置' : '未配置', `${workspace.entries.length} 个顶层条目`, LayoutDashboard, workspace.workspaceRoot || settings.workspaceRoot ? 'ok' : 'warn'],
    ['项目', currentProject?.name || (projectState.apiReady ? '暂无项目' : '未连接'), projectState.apiReady ? `${projectState.projects.length} 个项目 · ${projectFileCount} 个文件` : '项目服务未就绪', Box, currentProject ? 'ok' : 'warn'],
    ['模型配置', modelHealthSummary.value, modelHealthSummary.detail, Brain, modelHealthSummary.tone]
  ];

  const healthItems = [
    ['本地构建', localBuildHealth.value, localBuildHealth.detail, Upload, localBuildHealth.tone],
    ['Agent 引擎', runtimeHealth.value, runtimeHealth.detail, SquareTerminal, runtimeHealth.tone],
    ['预加载状态', startupHealth.value, startupHealth.detail, Loader2, startupHealth.tone],
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
    <section className={`diagnostics-page ${embedded ? 'embedded-settings-section' : 'panel'}`} data-testid="diagnostics-page">
      {!embedded && (
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
      )}

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
            <label className="switch-row">
              <span>匿名诊断上报</span>
              <button
                className={`toggle ${settings.anonymousTelemetryEnabled ? 'on' : ''}`}
                type="button"
                title="默认关闭；开启后仅上报匿名崩溃/性能摘要，不包含提示词、密钥或本地路径正文。"
                onClick={() => saveSettings({ anonymousTelemetryEnabled: !settings.anonymousTelemetryEnabled })}
              >
                <span />
              </button>
            </label>
            <label className="wide">
              <span>诊断上报端点</span>
              <input
                value={settings.telemetryEndpoint || ''}
                placeholder="https://telemetry.example.com/ecorex"
                onChange={(event) => setSettings((current) => ({ ...current, telemetryEndpoint: event.target.value }))}
                onBlur={() => saveSettings({ telemetryEndpoint: settings.telemetryEndpoint || '' })}
              />
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

function skillItemsFromCapabilities(capabilities = {}) {
  const items = [
    ...extractCollection({ capabilityPacks: capabilities?.capabilityPacks }, ['capabilityPacks']),
    ...extractCollection(capabilities, ['installedSkillPacks', 'skillPacks', 'capabilityPacks', 'plugins', 'skills']),
    ...extractCollection(capabilities?.capabilities, ['installedSkillPacks', 'skillPacks', 'capabilityPacks', 'plugins', 'skills'])
  ];
  const seen = new Set();
  return items
    .filter((item) => item && typeof item === 'object' && !isNativeSkillItem(item))
    .filter((item) => {
      const key = String(item.id || item.name || item.title || item.packageName || '').toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((item, index) => normalizeSkillItem({
      ...item,
      installed: item.installed ?? item.available ?? true,
      enabled: item.enabled ?? true,
      skills: item.skills ?? item.skillCount ?? 1,
      category: item.category || item.sourceKind || item.type || 'workflow'
    }, index, 'capabilities'));
}

async function loadCapabilitySkills(currentCapabilities = null) {
  const fromState = skillItemsFromCapabilities(currentCapabilities || {});
  if (fromState.length) return fromState;
  if (!window.ecorex?.getCapabilities) return [];
  const result = await window.ecorex.getCapabilities({ refresh: true });
  return skillItemsFromCapabilities(result?.capabilities || result || {});
}

function SkillsView({ backendStatus, capabilities, refreshBackend, onUnauthorized, authStatus, embedded = false }) {
  const [skills, setSkills] = useState([]);
  const [view, setView] = useState('all');
  const [category, setCategory] = useState('all');
  const [query, setQuery] = useState('');
  const [installSourcePath, setInstallSourcePath] = useState('');
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
      : extractCollection(result, [
        'installedSkillPacks',
        'skillPacks',
        'skills',
        'plugins',
        'capabilityPacks',
        'capabilities.installedSkillPacks',
        'capabilities.skillPacks',
        'capabilities.capabilityPacks',
        'capabilities.skills',
        'capabilities.plugins'
      ]);

    if (!items.length) {
      const fallbackItems = await loadCapabilitySkills(capabilities);
      if (fallbackItems.length) {
        setSkills(fallbackItems);
        setState('ready');
        setLastLoadedAt(formatDateTime(new Date().toISOString()));
        if (!silent) setNotice('');
        return;
      }
      if (result?.missing) {
        setSkills([]);
        setState('unsupported');
        setNotice('本地能力服务未就绪。');
        return;
      }
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

  async function installSkillFromSource() {
    const sourcePath = installSourcePath.trim();
    if (!sourcePath) return;
    setActionKey('__source-install__:install');
    setNotice('');
    const result = await callEcorexAction([
      'installSkill',
      'skills.install',
      'skill.install'
    ], { sourcePath });
    setActionKey('');

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后继续操作。');
      return;
    }
    if (result?.ok === false) {
      setNotice(`Skill 安装失败：${sanitizeDisplayText(result.error, '请检查路径后重试')}`);
      return;
    }
    setInstallSourcePath('');
    setNotice('Skill 已安装并加入 EcoreX 托管白名单。');
    refreshBackend?.();
    await loadSkills({ silent: true });
  }

  async function resetSkillState() {
    setActionKey('__skill-reset__:reset');
    setNotice('');
    const result = await callEcorexAction([
      'resetSkills',
      'skills.reset',
      'skill.reset'
    ], { confirmReset: true });
    setActionKey('');

    if (result?.unauthorized) {
      onUnauthorized?.();
      setState('unauthorized');
      setNotice('登录状态已过期，请重新登录后继续操作。');
      return;
    }
    if (result?.ok === false) {
      setNotice(`Skill 重置失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
      return;
    }
    setNotice('Skill 已恢复为内置初始状态。');
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

  const canManageSkills = canManageSkillsFromAuth(authStatus);
  const actionDisabled = !canManageSkills || state === 'offline' || state === 'unsupported' || state === 'unauthorized';

  return (
    <section className={`management ${embedded ? 'embedded-management' : 'panel'}`} data-testid="skills-page">
      {!embedded && (
        <HeaderBar
          title="SKILLS"
          badge={state === 'offline' ? '预览' : undefined}
          subtitle="管理本机可用能力、协同助手与工作流入口；安装、更新和启停动作都会等待确认"
          backendStatus={backendStatus}
          onRefresh={() => {
            refreshBackend?.({ refresh: true });
            loadSkills();
          }}
        />
      )}
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

      {canManageSkills && (
        <div className="skill-admin-panel">
          <label>
            <Code2 size={15} />
            <input
              value={installSourcePath}
              onChange={(event) => setInstallSourcePath(event.target.value)}
              placeholder="C:\\path\\to\\skill"
              data-testid="skill-source-path-input"
            />
          </label>
          <button
            type="button"
            className="primary"
            onClick={installSkillFromSource}
            disabled={!installSourcePath.trim() || actionKey === '__source-install__:install'}
            data-testid="skill-source-install-button"
          >
            {actionKey === '__source-install__:install' ? <Loader2 size={14} className="spin-icon" /> : <Upload size={14} />}
            安装 Skill
          </button>
          <button
            type="button"
            onClick={resetSkillState}
            disabled={actionKey === '__skill-reset__:reset'}
            data-testid="skill-reset-button"
          >
            {actionKey === '__skill-reset__:reset' ? <Loader2 size={14} className="spin-icon" /> : <RotateCcw size={14} />}
            重置初始状态
          </button>
        </div>
      )}

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

function mcpItemsFromCapabilities(capabilities = {}) {
  const items = [
    ...extractCollection(capabilities, ['services', 'servers', 'mcp.services', 'mcp.servers', 'capabilityPacks', 'plugins', 'skillPacks']),
    ...extractCollection(capabilities?.capabilities, ['services', 'servers', 'mcp.services', 'mcp.servers', 'capabilityPacks', 'plugins', 'skillPacks'])
  ];
  const seen = new Set();
  return items
    .filter((item) => item && typeof item === 'object')
    .filter((item) => {
      const raw = `${item.sourceKind || ''} ${item.type || ''} ${item.kind || ''} ${item.category || ''} ${item.name || ''}`.toLowerCase();
      return raw.includes('mcp') && !isNativeConnectorItem(item);
    })
    .filter((item) => {
      const key = String(item.id || item.name || item.title || item.packageName || '').toLowerCase();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((item, index) => normalizeMcpService({
      ...item,
      displayName: item.displayName || item.title || item.name,
      endpointLabel: item.endpointLabel || 'EcoreX MCP 端点',
      tags: item.tags || ['MCP', item.category || 'EcoreX'],
      auth: item.auth || '本地认证',
      authState: item.authState || (item.enabled === false ? '未启用' : '已配置'),
      status: item.status || (item.enabled === false ? 'disabled' : 'configured'),
      enabled: item.enabled ?? true,
      installed: item.installed ?? true,
      permissions: item.permissions || '读写'
    }, index, 'capabilities'));
}

async function loadCapabilityMcpServices(currentCapabilities = null) {
  const fromState = mcpItemsFromCapabilities(currentCapabilities || {});
  if (fromState.length) return fromState;
  if (!window.ecorex?.getCapabilities) return [];
  const result = await window.ecorex.getCapabilities({ refresh: true });
  return mcpItemsFromCapabilities(result?.capabilities || result || {});
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

function McpView({ backendStatus, capabilities, refreshBackend, onUnauthorized, embedded = false }) {
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
      setNotice('预览模式：EcoreX 默认不展示内置 MCP，后续可在应用内自行添加。');
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
      setNotice('登录状态已过期，请重新登录后管理 MCP。');
      return;
    }

    if (result?.ok === false && !result.missing) {
      setServices([]);
      setState('error');
      setNotice(sanitizeDisplayText(result.error, 'MCP 列表加载失败。'));
      return;
    }

    let source = 'api';
    let items = result?.ok === false && result.missing
      ? []
      : extractCollection(result, ['services', 'servers', 'mcp.services', 'mcp.servers']);

    if (!items.length) {
      const fallbackItems = await loadCapabilityMcpServices(capabilities);
      if (fallbackItems.length) {
        setServices(fallbackItems);
        setState('ready');
        setLastLoadedAt(formatDateTime(new Date().toISOString()));
        if (!silent) setNotice('');
        return;
      }
      if (result?.missing) {
        setServices([]);
        setState('unsupported');
        setNotice('本地能力服务未就绪。');
        return;
      }
    }

    const nextServices = items
      .filter((item) => !isNativeConnectorItem(item))
      .map((item, index) => normalizeMcpService(item, index, source));
    setServices(nextServices);
    setState(nextServices.length ? 'ready' : 'empty');
    setLastLoadedAt(formatDateTime(new Date().toISOString()));
    if (source === 'backend') {
      setNotice('本地能力服务未完全就绪，当前展示 MCP 状态快照；操作会继续等待确认。');
    } else if (!silent) {
      setNotice('');
    }
  }

  useEffect(() => {
    loadMcpServices();
  }, []);

  async function runMcpAction(service, action) {
    if (action === 'configure' && !service) {
      setNotice('请选择具体 MCP 后查看或调整配置。');
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
        : `MCP ${label}失败：${sanitizeDisplayText(result.error, '请稍后重试')}`);
      return;
    }

    setNotice(`MCP ${label}已确认。`);
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
    <section className={`management mcp-view ${embedded ? 'embedded-management' : 'panel'}`} data-testid="mcp-page">
      {!embedded && (
        <HeaderBar
          title="EcoreX MCP"
          badge={state === 'offline' ? '预览' : undefined}
          subtitle="读取本机真实 MCP，展示连接、启用、授权与错误状态；不把模板数据伪装成已接入"
          backendStatus={backendStatus}
          onRefresh={() => {
            refreshBackend?.({ refresh: true });
            loadMcpServices();
          }}
        />
      )}
      <div className="mcp-toolbar">
        <label>
          <Search size={16} />
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索 MCP"
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
          MCP 配置
        </button>
      </div>

      <div className="stats-row compact">
        {[
          ['全部 MCP', totals.total, Box, lastLoadedAt || '未加载'],
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
        <span>MCP 信息</span>
        <span>权限与认证</span>
        <span>连接状态</span>
        <span>最后同步</span>
        <span>操作</span>
      </div>
      <div className="mcp-list">
        {state === 'loading' && !services.length && (
          <ManagementState icon={Loader2} spin title="正在加载 MCP" text="正在读取本机真实 MCP 列表。" />
        )}
        {state === 'unsupported' && (
          <ManagementState title="本地能力服务未就绪" text="暂时无法读取 MCP 列表，因此不会展示模板为真实服务。" />
        )}
        {state === 'unauthorized' && (
          <ManagementState title="登录状态已过期" text="请重新登录后再查看和管理 MCP。" />
        )}
        {state === 'error' && (
          <ManagementState title="MCP 加载失败" text={notice || '请刷新或查看诊断页。'} />
        )}
        {state !== 'loading' && state !== 'unsupported' && state !== 'unauthorized' && state !== 'error' && !filteredServices.length && (
          <ManagementState
            icon={Box}
            title="暂无匹配 MCP"
            text={services.length ? '当前筛选条件下没有结果。' : '暂未返回可展示 MCP。'}
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


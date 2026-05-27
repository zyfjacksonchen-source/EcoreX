const { app, BrowserWindow, ipcMain, session, safeStorage, dialog, screen, shell, nativeImage } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const zlib = require('zlib');
const { pathToFileURL } = require('url');
const { fileURLToPath } = require('url');
const { createModelAdapter, DEFAULT_IMAGE_MODEL, normalizeImageModelName } = require('./model-adapter.cjs');
const {
  listEvaluationFramework,
  runEvaluationFramework,
  RETRY_POLICY,
  PARALLELISM_POLICY,
  MEMORY_TAXONOMY,
  EVALUATION_SAMPLE_COUNT
} = require('./evaluation-framework.cjs');

const ROOT_DIR = path.resolve(__dirname, '..');
const isWindows = process.platform === 'win32';
const runningAgents = new Map();
const pendingAgentStarts = new Map();
const recentAgentStartsByWindow = new Map();
const agentSessionActors = new Map();
const selectedAttachmentAccess = new Map();
const agentArtifactAccess = new Map();
const agentToolLedger = new Map();
const claudeSessionBindingCache = new Map();
const invalidClaudeSessionResumeCache = new Map();
let claudeSessionBindingCacheLoaded = false;
const vueOfficePreviewState = {
  server: null,
  port: 0,
  starting: null,
  resource: null,
  grants: new Map(),
  lastError: ''
};
let mainWindow = null;
let startupSplashUrl = '';
let cachedClaude = null;
let cachedClaudeCheckedAt = 0;
let locateClaudeInflight = null;
let cachedCapabilities = null;
let cachedCapabilitiesKey = '';
let cachedBackendStatus = null;
let backendStatusInflight = null;
const AGENT_TIMEOUT_MS = 30 * 60 * 1000;
const AGENT_IDLE_TIMEOUT_MS = 12 * 60 * 1000;
const AGENT_MIN_TIMEOUT_MS = 30 * 1000;
const MAX_RUNNING_AGENTS = 4;
const AGENT_RUNTIME_KIND = 'claude-cli-session-actor';
const AGENT_START_DEBOUNCE_MS = 1200;
const AGENT_START_PENDING_TTL_MS = 15 * 1000;
const BACKEND_STATUS_TTL_MS = 3 * 1000;
const CLAUDE_TRANSCRIPT_CACHE_TTL_MS = 60 * 1000;
const CLAUDE_INVALID_RESUME_TTL_MS = 10 * 60 * 1000;
const RENDERER_RECOVERY_DELAY_MS = 800;
const RENDERER_UNRESPONSIVE_RECOVERY_MS = 15 * 1000;
const RENDERER_RECOVERY_WINDOW_MS = 60 * 1000;
const MAX_RENDERER_RECOVERY_ATTEMPTS = 3;
const STARTUP_PRELOAD_TIMEOUT_MS = 12 * 1000;
const MAX_PROMPT_CHARS = 80_000;
const MIN_PROMPT_CHARS = 1_000;
const MAX_PROMPT_PREVIEW_CHARS = 240;
const SETTINGS_FILE_NAME = 'settings.json';
const AUTH_SESSION_FILE_NAME = 'auth-session.json';
const AUTH_IDENTITY_FILE_NAME = 'auth-identity.json';
const AUTH_USERS_FILE_NAME = 'auth-users.json';
const ENTERPRISE_ADMIN_JOURNAL_FILE_NAME = 'enterprise-admin-journal.jsonl';
const SECRETS_FILE_NAME = 'secrets.json';
const MODEL_PROFILES_FILE_NAME = 'model-profiles.json';
const SESSION_TRANSCRIPT_DIR_NAME = 'sessions';
const CLAUDE_SESSION_BINDINGS_FILE_NAME = 'session-bindings.json';
const MANAGED_SKILL_PACKS_FILE_NAME = 'skill-packs.json';
const MANAGED_SKILL_PACKS_DIR_NAME = 'skill-packs';
const RUN_JOURNAL_FILE_NAME = 'agent-run-journal.jsonl';
const CRASH_SUMMARY_FILE_NAME = 'crash-summary.json';
const TELEMETRY_QUEUE_FILE_NAME = 'telemetry-queue.json';
const EVALUATION_REPORT_FILE_NAME = 'evaluation-report.json';
const DIAGNOSTIC_EXPORT_DIR_NAME = 'EcoreX Diagnostics';
const PROJECT_STATE_FILE_NAME = '.ecorex-projects.json';
const PROJECT_METADATA_FILE_NAME = '.ecorex-project.json';
const PROJECT_MEMORY_DIR_NAME = '.ecorex-memory';
const PROJECT_MEMORY_FILE_NAME = 'project-memory.md';
const PROJECT_CONTEXT_FILE_NAME = 'project-context.json';
const PROJECT_FILES_DIR_NAME = 'files';
const LOG_FILE_NAME = 'ecorex-agent.log';
const MAX_LOG_LINES = 200;
const MAX_CRASH_EVENTS = 50;
const MAX_TELEMETRY_EVENTS = 100;
const MAX_DIAGNOSTIC_LOG_LINES = 80;
const MAX_DIAGNOSTIC_SESSIONS = 12;
const MAX_COMMAND_OUTPUT_CHARS = 2 * 1024 * 1024;
const MAX_AGENT_LINE_BUFFER_CHARS = 2 * 1024 * 1024;
const MAX_TRANSCRIPT_EVENTS = 160;
const MAX_TRANSCRIPT_HEAD_EVENTS = 40;
const MAX_TRANSCRIPT_TEXT_PREVIEW_CHARS = 320;
const MAX_RECENT_SESSION_FILES = 20;
const MAX_IPC_OUTPUT_CHARS = 200 * 1024;
const MAX_AGENT_EVENT_TEXT_CHARS = 20 * 1024;
const MAX_AGENT_EVENT_RAW_CHARS = 64 * 1024;
const MAX_AGENT_EVENT_QUEUE = 2000;
const HARD_MAX_AGENT_EVENT_QUEUE = 5000;
const MAX_AGENT_EVENT_BATCH = 40;
const AGENT_EVENT_FLUSH_MS = 75;
const AGENT_EVENT_PAUSE_HIGH_WATER = 180;
const AGENT_EVENT_RESUME_LOW_WATER = 80;
const MAX_TOOL_LEDGER_ACTIVE_ENTRIES = 200;
const MAX_MANAGED_ITEMS = 500;
const MAX_SKILLS_PER_PLUGIN = 200;
const MAX_SKILL_PACK_COPY_BYTES = 80 * 1024 * 1024;
const MAX_SECRET_VALUE_CHARS = 20_000;
const MAX_MODEL_PROFILES = 20;
const MAX_MODEL_PROFILE_TEXT_CHARS = 2048;
const MODEL_PROFILE_TEST_TIMEOUT_MS = 20 * 1000;
const IMAGE_GENERATION_TIMEOUT_MS = 2 * 60 * 1000;
const ATTACHMENT_PREVIEW_MAX_BYTES = 2 * 1024 * 1024;
const ECOREX_AGENT_CONFIG_DIR_NAME = 'agent-runtime-config';
const BLOCKED_LOCAL_SKILL_NAMES = new Set(['superpowers', 'huashu-design', 'huashu_design', 'huashu design']);
const ECOREX_BUILTIN_PLUGIN_ALLOWLIST = new Set(['feature-dev', 'code-review', 'security-guidance', 'plugin-dev']);
const ECOREX_GENERAL_WORKSPACE_DIR_NAME = 'general-workspace';
const ATTACHMENT_TEXT_MAX_BYTES = 512 * 1024;
const ATTACHMENT_IMAGE_MAX_BYTES = 768 * 1024;
const ATTACHMENT_DATA_URL_MAX_CHARS = 2 * 1024 * 1024;
const ATTACHMENT_INLINE_TEXT_CHARS = 16 * 1024;
const ATTACHMENT_IMAGE_BASE64_SAMPLE_CHARS = 4096;
const MAX_AGENT_ATTACHMENTS = 12;
const MAX_ATTACHMENT_PROMPT_CHARS = 32 * 1024;
const SELECTED_ATTACHMENT_ACCESS_TTL_MS = 4 * 60 * 60 * 1000;
const MAX_SELECTED_ATTACHMENT_ACCESS = 200;
const AGENT_ARTIFACT_ACCESS_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_AGENT_ARTIFACT_ACCESS = 500;
const MAX_RUN_JOURNAL_BYTES = 2 * 1024 * 1024;
const MAX_RUN_JOURNAL_ENTRIES = 240;
const MAX_TOOL_LEDGER_SUMMARY_CHARS = 1600;
const FILE_PREVIEW_MAX_BYTES = 512 * 1024;
const FILE_PREVIEW_IMAGE_MAX_BYTES = 768 * 1024;
const FILE_PREVIEW_OFFICE_MAX_BYTES = 12 * 1024 * 1024;
const FILE_PREVIEW_OFFICE_MAX_CHARS = 80 * 1024;
const VUE_OFFICE_PREVIEW_MAX_BYTES = 100 * 1024 * 1024;
const VUE_OFFICE_GRANT_TTL_MS = 30 * 60 * 1000;
const VUE_OFFICE_MAX_GRANTS = 200;
const VUE_OFFICE_VENDOR_DIR_NAME = 'vue-office';
const VUE_OFFICE_DOCUMENT_EXTENSIONS = new Set(['.pdf', '.docx', '.xls', '.xlsx', '.xlsm', '.pptx', '.pptm']);
const FILE_PREVIEW_TEXT_EXTENSIONS = new Set([
  '.html',
  '.htm',
  '.md',
  '.markdown',
  '.txt',
  '.json',
  '.jsonl',
  '.csv',
  '.log',
  '.css',
  '.js',
  '.mjs',
  '.cjs',
  '.jsx',
  '.ts',
  '.tsx',
  '.yaml',
  '.yml',
  '.xml'
]);
const FILE_PREVIEW_IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.bmp', '.avif']);
const FILE_PREVIEW_DOCUMENT_EXTENSIONS = new Set(['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx', '.pptm']);
const LOCAL_AUTH_HASH_ITERATIONS = 210_000;
const LOCAL_AUTH_MIN_PASSWORD_CHARS = 8;
const AUTH_SESSION_TTL_MS = 12 * 60 * 60 * 1000;
const AUTH_SESSION_REFRESH_THRESHOLD_MS = 30 * 60 * 1000;
const MAX_PROJECTS = 100;
const MAX_PROJECT_STATS_ITEMS = 2000;
const MAX_PROJECT_FILE_SELECTION = 20;
const MAX_PROJECT_FILE_BYTES = 100 * 1024 * 1024;
const FULL_ACCESS_PERMISSION_MODE = 'fullAccess';
const FULL_ACCESS_CLAUDE_FLAG = '--dangerously-skip-permissions';
const CLAUDE_AUTO_ALLOWED_TOOL_SET = 'WebFetch,WebSearch,Task,TodoRead,TodoWrite,mcp__*';
const PUBLIC_PERMISSION_MODES = ['default', FULL_ACCESS_PERMISSION_MODE];
const WINDOW_CONTROL_ACTIONS = new Set(['minimize', 'maximize', 'close']);
const USER_ROLE_PERMISSIONS = Object.freeze({
  super_admin: ['profile:update', 'users:manage', 'enterprise:manage', 'settings:manage', 'secrets:manage', 'models:manage', 'mcp:manage', 'skills:manage', 'agent:operate'],
  admin: ['profile:update', 'enterprise:manage', 'settings:manage', 'secrets:manage', 'models:manage', 'mcp:manage', 'skills:manage', 'agent:operate'],
  user: ['profile:update', 'agent:operate']
});
const USER_ROLES = new Set(Object.keys(USER_ROLE_PERMISSIONS));
const ENTERPRISE_ADMIN_ACTIONS = new Set(['pushAgentUpdate', 'pushSkill', 'syncMcp']);
const PERMISSION_POLICIES = Object.freeze({
  default: {
    value: 'default',
    accessMode: 'default',
    mode: 'default',
    permissionMode: 'auto',
    cliMode: 'auto',
    cliFlags: [],
    label: '默认权限',
    description: '联网搜索、网页读取和常规工具自动执行；文件读写、命令执行和系统目录访问继续按权限确认。',
    fullAccess: false
  },
  acceptEdits: {
    value: 'acceptEdits',
    accessMode: 'acceptEdits',
    mode: 'acceptEdits',
    permissionMode: 'acceptEdits',
    cliMode: 'acceptEdits',
    cliFlags: [],
    label: '接受编辑',
    description: '允许常规读写与编辑，敏感动作仍遵循底层权限策略。',
    fullAccess: false
  },
  auto: {
    value: 'auto',
    accessMode: 'auto',
    mode: 'auto',
    permissionMode: 'auto',
    cliMode: 'auto',
    cliFlags: [],
    label: '自动权限',
    description: '由 Claude Code 自动判断权限请求。',
    fullAccess: false
  },
  plan: {
    value: 'plan',
    accessMode: 'plan',
    mode: 'plan',
    permissionMode: 'plan',
    cliMode: 'plan',
    cliFlags: [],
    label: '计划模式',
    description: '优先产出计划，进入实现前保留更严格的控制。',
    fullAccess: false
  },
  [FULL_ACCESS_PERMISSION_MODE]: {
    value: FULL_ACCESS_PERMISSION_MODE,
    accessMode: FULL_ACCESS_PERMISSION_MODE,
    mode: FULL_ACCESS_PERMISSION_MODE,
    permissionMode: FULL_ACCESS_PERMISSION_MODE,
    cliMode: null,
    cliFlags: [FULL_ACCESS_CLAUDE_FLAG],
    label: '完全访问权限',
    description: '使用 Claude Code --dangerously-skip-permissions 跳过权限检查。',
    fullAccess: true,
    requiresConfirmation: true
  }
});
const PERMISSION_MODE_ALIASES = new Map([
  ...Object.keys(PERMISSION_POLICIES).map((mode) => [mode, mode])
]);
const ALLOWED_PERMISSION_MODES = new Set(PERMISSION_MODE_ALIASES.keys());
const BUILTIN_MODEL_OPTIONS = [
  { value: 'sonnet', label: 'Sonnet' },
  { value: 'opus', label: 'Opus' }
];
const ALLOWED_MODELS = new Set(BUILTIN_MODEL_OPTIONS.map((model) => model.value));
const ALLOWED_CONFIG_SCOPES = new Set(['local', 'user', 'project']);
const PROJECT_STATUS_LABELS = Object.freeze({
  planning: '筹备中',
  active: '进行中',
  paused: '已暂停',
  completed: '已完成',
  archived: '已归档'
});
const ALLOWED_PROJECT_STATUSES = new Set(Object.keys(PROJECT_STATUS_LABELS));
const SECRET_KEY_ALLOWLIST = new Set([
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY'
]);
const AGENT_ENV_ALLOWLIST = new Set([
  'PATH',
  'Path',
  'PATHEXT',
  'HOME',
  'USERPROFILE',
  'APPDATA',
  'LOCALAPPDATA',
  'TEMP',
  'TMP',
  'SystemRoot',
  'ComSpec',
  'USERNAME',
  'USER',
  'LANG',
  'LC_ALL',
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'NO_PROXY',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY',
  'ANTHROPIC_BASE_URL',
  'OPENAI_BASE_URL',
  'CLAUDE_CONFIG_DIR',
  'XDG_CONFIG_HOME'
]);
const agentEventQueues = new Map();
let secretRedactionCache = { loadedAt: 0, values: [] };
let agentEventSequence = 0;
let rendererRecoveryAttempts = [];
let rendererUnresponsiveSince = 0;
let rendererUnresponsiveTimer = null;
let startupPreloadPromise = null;
let startupPreloadState = { status: 'idle' };
const claudeTranscriptExistenceCache = new Map();
const claudeSessionLaunchCache = new Map();

const AGENT_SYSTEM_PROMPT = [
  '你是 EcoreX Agent，由 EcoreX 亦芯开发的具备自主思考能力的 AI Agent。',
  '你的主要任务是服务广告业务常用工作场景，包括但不限于品牌策略、投放计划、预算分配、素材创意、A/B 测试、数据归因、效果复盘、竞品洞察、客户简报、项目协同与自动化执行。',
  '你应主动拆解目标、识别缺失信息、给出可执行方案，并在需要调用工具、读写文件、运行命令或生成报告时清晰说明动作与结果。',
  '默认使用专业、可靠、面向业务结果的中文表达，输出要便于广告运营、市场、创意、数据分析与项目管理团队直接落地。',
  '输出不要使用星号作为 Markdown 标记，不要使用 *、** 或星号项目符号；列表请使用中文序号或短句换行。',
  '需要事实核验、外部资料、网页信息或时效性内容时，应主动使用联网检索与网页读取能力；需要本地资料时，应主动使用文件读取、写入、编辑、检索、命令执行和 MCP 工具。',
  '联网搜索、网页读取和常规非文件工具不需要先询问用户，直接执行并在结果中说明来源；涉及读取、写入、编辑用户文件、运行命令或访问系统目录时，遵循权限确认。'
].join('\n');

const ECOREX_AGENT_SYSTEM_PROMPT = [
  '不要向用户暴露或提及 Claude Code、Claude CLI、Anthropic CLI、底层后端或执行桥；统一称为 EcoreX。',
  '你是 EcoreX Agent，由 EcoreX 亦芯开发的具备自主思考能力的 AI Agent。',
  '你的主要任务是服务广告业务常用工作场景，包括但不限于品牌策略、投放计划、预算分配、素材创意、A/B 测试、数据归因、效果复盘、竞品洞察、客户简报、项目协同与自动化执行。',
  '你应主动拆解目标、识别缺失信息、给出可执行方案，并在需要调用工具、读写文件、运行命令或生成报告时清晰说明动作与结果。',
  '默认使用专业、可靠、面向业务结果的中文表达，输出要便于广告运营、市场、创意、数据分析与项目管理团队直接落地。',
  '输出不要使用星号作为 Markdown 标记，不要使用 *、** 或星号项目符号；列表请使用中文序号或短句换行。',
  '需要事实核验、外部资料、网页信息或时效性内容时，应主动使用联网检索与网页读取能力；需要本地资料时，应主动使用文件读取、写入、编辑、检索、命令执行和 MCP 工具。',
  '联网搜索、网页读取和常规非文件工具不需要先询问用户，直接执行并在结果中说明来源；涉及读取、写入、编辑用户文件、运行命令或访问系统目录时，遵循权限确认。'
].join('\n');

const ECOREX_MANAGED_CAPABILITY_PRIORITY_PROMPT = [
  'Managed capability priority:',
  '1. For Excel workbook creation, editing, formatting, formulas, charts, tables, pivot tables, or data reads/writes, prefer the excel-mcp-server MCP tools first.',
  '2. For PPT, presentation, slide deck, or PowerPoint generation/editing, prefer the ppt-master skill first.',
  '3. Fall back to direct local file generation only when the matching managed capability is unavailable or unsuitable, and explain the fallback briefly.'
].join('\n');

const ECOREX_GENERAL_CHAT_ISOLATION_PROMPT = [
  'General chat isolation:',
  '1. This session is not bound to an EcoreX project. Do not inspect project folders, project-memory.md, .ecorex-memory, or project file libraries unless the user explicitly attaches or references those files in this chat.',
  '2. Use only the current general workspace, the user message, and explicit attachments/references for local file operations.',
  '3. Use only EcoreX native tools and managed capabilities surfaced by EcoreX; do not use personal or local developer plugins that are not visible in EcoreX capability management.'
].join('\n');

function devPath(...segments) {
  return path.join(ROOT_DIR, ...segments);
}

function backendPath(...segments) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', ...segments);
  }
  const sourceDir = path.join(ROOT_DIR, '终端源代码');
  if (fs.existsSync(sourceDir)) return path.join(sourceDir, ...segments);
  return path.join(ROOT_DIR, '终端源代码', ...segments);
}

function logPath() {
  return path.join(app.getPath('logs'), LOG_FILE_NAME);
}

function diagnosticsExportDir() {
  return path.join(app.getPath('downloads'), DIAGNOSTIC_EXPORT_DIR_NAME);
}

function diagnosticExportPathLabel(fileName = '') {
  return fileName ? `Downloads/${DIAGNOSTIC_EXPORT_DIR_NAME}/${fileName}` : `Downloads/${DIAGNOSTIC_EXPORT_DIR_NAME}`;
}

function rendererEntryPath() {
  return path.join(ROOT_DIR, 'dist', 'index.html');
}

function rendererEntryUrl() {
  return pathToFileURL(rendererEntryPath()).href;
}

function devRendererUrl() {
  return 'http://127.0.0.1:5188';
}

function probeDevRenderer(url = devRendererUrl(), timeoutMs = 1200) {
  return new Promise((resolve) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 500);
    });
    request.on('timeout', () => {
      request.destroy();
      resolve(false);
    });
    request.on('error', () => resolve(false));
  });
}

function isAllowedRendererUrl(url = '') {
  const value = String(url || '');
  if (startupSplashUrl && value === startupSplashUrl) return true;
  if (app.isPackaged) return value === rendererEntryUrl();
  if (value === rendererEntryUrl()) return true;
  return value === `${devRendererUrl()}/` || value.startsWith(`${devRendererUrl()}/`);
}

function knownSecretRedactionValues() {
  const now = Date.now();
  if (now - secretRedactionCache.loadedAt < 5000) return secretRedactionCache.values;

  const values = [];
  for (const key of SECRET_KEY_ALLOWLIST) {
    if (process.env[key]) values.push(String(process.env[key]));
  }
  try {
    for (const secret of readStoredSecrets({ includeValues: true })) {
      if (secret.value) values.push(String(secret.value));
    }
  } catch {
    // Redaction must be best-effort and side-effect free.
  }
  try {
    for (const profile of readStoredModelProfiles({ includeApiKey: true })) {
      if (profile.apiKey) values.push(String(profile.apiKey));
    }
  } catch {
    // Redaction must be best-effort and side-effect free.
  }

  secretRedactionCache = {
    loadedAt: now,
    values: [...new Set(values.map((value) => value.trim()).filter((value) => value.length >= 8))]
      .sort((a, b) => b.length - a.length)
      .slice(0, 50)
  };
  return secretRedactionCache.values;
}

function redactSensitiveText(value = '') {
  let text = String(value || '')
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, '[REDACTED_PRIVATE_KEY]')
    .replace(/(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/gi, '$1[REDACTED]@')
    .replace(/(authorization|cookie|token|api[_-]?key|auth[_-]?token)\s*[:=]\s*["']?[^"',\s]+/gi, '$1=[REDACTED]')
    .replace(/(sk-ant-[a-zA-Z0-9_-]{12,})/g, '[REDACTED_API_KEY]')
    .replace(/(sk-[a-zA-Z0-9_-]{12,})/g, '[REDACTED_API_KEY]')
    .replace(/(ghp_[a-zA-Z0-9_]{20,})/g, '[REDACTED_TOKEN]')
    .replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[REDACTED_AWS_KEY]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]{16,}/gi, 'Bearer [REDACTED]')
    .replace(/\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b/g, '[REDACTED_JWT]')
    .replace(/[a-f0-9]{64}/gi, '[REDACTED_TOKEN]');
  for (const secret of knownSecretRedactionValues()) {
    text = text.split(secret).join('[REDACTED_SECRET]');
  }
  return text;
}

function publicProductText(value = '') {
  return redactSensitiveText(value)
    .replace(/--dangerously-skip-permissions/gi, 'Full Access')
    .replace(/\bClaude Code CLI\b/gi, 'EcoreX')
    .replace(/\bClaude Code\b/gi, 'EcoreX')
    .replace(/\bClaude CLI\b/gi, 'EcoreX')
    .replace(/\bAnthropic CLI\b/gi, 'EcoreX')
    .replace(/\bClaude\b/gi, 'EcoreX')
    .replace(/\bclaude\s+mcp\b/gi, 'EcoreX MCP')
    .replace(/\bclaude\s+(plugin|plugins)\b/gi, 'EcoreX SKILLS')
    .replace(/\bMCP servers?\b/gi, 'MCP')
    .replace(/\bMCP\b/gi, 'MCP')
    .replace(/\bplugin marketplace\b/gi, 'SKILLS library')
    .replace(/\bplugins?\b/gi, 'SKILLS')
    .replace(/\bCLI\b/gi, 'execution bridge');
}

function redactForLog(value) {
  if (typeof value === 'string') return redactSensitiveText(value);
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(redactForLog);
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      diagnosticSensitiveKey(key)
        ? '[REDACTED]'
        : redactForLog(item)
    ])
  );
}

function safeOutputText(value = '', limit = MAX_IPC_OUTPUT_CHARS) {
  const text = publicProductText(String(value || ''));
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}\n[output truncated to ${limit} chars]`;
}

function safeJsonValue(value, limit = MAX_AGENT_EVENT_RAW_CHARS) {
  try {
    return JSON.parse(safeOutputText(JSON.stringify(redactForLog(value)), limit));
  } catch {
    return safeOutputText(String(value || ''), limit);
  }
}

function escapeRegExp(value = '') {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function safeAppPath(name) {
  try {
    return app.getPath(name);
  } catch {
    return '';
  }
}

function knownDiagnosticPathRoots() {
  const roots = [
    ['[APP_ROOT]', ROOT_DIR],
    ['[APP_PATH]', app.getAppPath?.()],
    ['[USER_DATA]', safeAppPath('userData')],
    ['[APP_LOGS]', safeAppPath('logs')],
    ['[SESSION_DATA]', (() => {
      try {
        return sessionTranscriptDir();
      } catch {
        return '';
      }
    })()],
    ['[HOME]', safeAppPath('home') || process.env.HOME || process.env.USERPROFILE],
    ['[APP_DATA]', safeAppPath('appData') || process.env.APPDATA],
    ['[TEMP]', safeAppPath('temp') || process.env.TEMP || process.env.TMP]
  ];
  try {
    const settings = readSettings();
    if (settings?.workspaceRoot) roots.push(['[WORKSPACE_ROOT]', settings.workspaceRoot]);
  } catch {
    // Diagnostics redaction must remain best-effort.
  }
  const seen = new Set();
  return roots
    .filter(([, value]) => typeof value === 'string' && value.trim())
    .map(([label, value]) => [label, path.resolve(value)])
    .filter(([, value]) => {
      const key = isWindows ? value.toLowerCase() : value;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((left, right) => right[1].length - left[1].length);
}

function localPathBasename(value, flavor = 'native') {
  const normalized = String(value || '').replace(/[\\/:]+$/, '');
  if (!normalized) return '';
  const base = flavor === 'win32' ? path.win32.basename(normalized) : path.posix.basename(normalized);
  return base && base !== normalized ? `/${base}` : '';
}

function redactLocalPaths(value = '') {
  let text = String(value || '');
  for (const [label, root] of knownDiagnosticPathRoots()) {
    const nativeRoot = root;
    const posixRoot = root.replace(/\\/g, '/');
    text = text.replace(new RegExp(escapeRegExp(nativeRoot), 'gi'), label);
    if (posixRoot !== nativeRoot) {
      text = text.replace(new RegExp(escapeRegExp(posixRoot), 'gi'), label);
    }
  }
  return text
    .replace(/\b[A-Za-z]:[\\/](?:[^\\/\s"'<>|{}[\],;]+[\\/])*[^\\/\s"'<>|{}[\],;]*/g, (match) => `[LOCAL_PATH]${localPathBasename(match, 'win32')}`)
    .replace(/\/(?:Users|home|var\/folders|tmp)\/[^\s"'<>|{}[\],;]*/g, (match) => `[LOCAL_PATH]${localPathBasename(match, 'posix')}`)
    .replace(/file:\/\/\/?\[LOCAL_PATH\]/gi, '[LOCAL_FILE]');
}

function safeDiagnosticText(value = '', limit = 4000) {
  const text = redactLocalPaths(publicProductText(value)).replace(/\s+$/g, '');
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}\n[diagnostic text truncated to ${limit} chars]`;
}

function diagnosticSensitiveKey(key = '') {
  return /(^|[_-])(key|token|secret|password|passphrase|authorization|cookie|credential|sessiontoken|authtoken|access[_-]?token|refresh[_-]?token|id[_-]?token|private[_-]?key|client[_-]?secret|bearer)($|[_-])|api[_-]?key/i.test(key);
}

function diagnosticPromptKey(key = '') {
  return /^(prompt|rawPrompt|fullPrompt|promptText)$/i.test(key);
}

function safeDiagnosticValue(value, options = {}) {
  const depth = Number(options.depth) || 0;
  const key = String(options.key || '');
  const stringLimit = Number(options.stringLimit) || 4000;
  const maxArrayItems = Number(options.maxArrayItems) || 40;
  if (diagnosticSensitiveKey(key)) return '[REDACTED]';
  if (diagnosticPromptKey(key)) return '[REDACTED_PROMPT]';
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return safeDiagnosticText(value, stringLimit);
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'bigint') return String(value);
  if (value instanceof Date) return value.toISOString();
  if (depth >= 6) return '[DEPTH_LIMIT]';
  if (Array.isArray(value)) {
    return value
      .slice(0, Math.max(0, maxArrayItems))
      .map((item) => safeDiagnosticValue(item, { ...options, depth: depth + 1, key: '' }));
  }
  if (typeof value === 'object') {
    const result = {};
    for (const [entryKey, entryValue] of Object.entries(value)) {
      if (typeof entryValue === 'undefined') continue;
      result[entryKey] = safeDiagnosticValue(entryValue, {
        ...options,
        depth: depth + 1,
        key: entryKey
      });
    }
    return result;
  }
  return safeDiagnosticText(String(value), stringLimit);
}

function diagnosticJson(payload) {
  return `${JSON.stringify(safeDiagnosticValue(payload, {
    stringLimit: 8000,
    maxArrayItems: 200
  }), null, 2)}\n`;
}

function safeCommandResult(result = {}, limit = MAX_IPC_OUTPUT_CHARS) {
  const rawStdout = String(result.stdout || '');
  const rawStderr = String(result.stderr || '');
  const stdout = safeOutputText(rawStdout, limit);
  const stderr = safeOutputText(rawStderr, limit);
  return {
    ok: Boolean(result.ok),
    code: typeof result.code === 'number' ? result.code : null,
    stdout,
    stderr,
    output: stdout || stderr,
    truncated: Boolean(result.truncated || rawStdout.length > limit || rawStderr.length > limit)
  };
}

function publicBridgeError(result = {}, fallback = 'EcoreX operation failed.') {
  const text = safeOutputText(result.stderr || result.stdout || '', 4000)
    .replace(/claude\s+(mcp|plugin|plugins)\b/gi, 'EcoreX')
    .replace(/Claude Code CLI/gi, 'EcoreX')
    .replace(/plugin(s)?/gi, 'skill pack$1');
  return text || fallback;
}

function publicAgentText(value = '', limit = MAX_AGENT_EVENT_TEXT_CHARS) {
  return publicProductText(safeOutputText(value, limit));
}

function parseJsonOutput(text = '') {
  try {
    return JSON.parse(String(text || '').trim());
  } catch {
    return null;
  }
}

function isUnsupportedCliResult(result = {}) {
  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  return /(unknown|invalid|unrecognized)\s+(command|option)|no such command|missing command|not supported/i.test(output);
}

function unsupportedCliResponse(action, details = {}) {
  const { command, result, raw, includeDiagnostics, ...publicDetails } = details;
  const response = {
    ok: false,
    unsupported: true,
    action,
    error: publicDetails.error || '本地能力桥接暂不支持该操作。',
    ...publicDetails
  };
  if (includeDiagnostics) {
    response.developerDiagnostics = {
      command: command ? safeOutputText(command, 500) : undefined,
      raw: raw ? safeOutputText(raw, 8000) : undefined,
      result
    };
  }
  return response;
}

function publicStableId(prefix, value) {
  const hash = crypto.createHash('sha1').update(String(value || prefix)).digest('hex').slice(0, 12);
  return `${prefix}-${hash}`;
}

function attachDeveloperDiagnostics(response, diagnostics, payload = {}) {
  if (payload?.includeDiagnostics) {
    response.developerDiagnostics = redactForLog(diagnostics);
  }
  return {
    ...response
  };
}

function sanitizeCliName(value, label = 'name') {
  const name = String(value || '').trim();
  if (!/^[a-zA-Z0-9_.@:-]{1,160}$/.test(name)) {
    throw new Error(`Invalid ${label}.`);
  }
  return name;
}

function sanitizeConfigScope(value) {
  const scope = String(value || 'local').trim();
  if (!ALLOWED_CONFIG_SCOPES.has(scope)) throw new Error('Invalid scope.');
  return scope;
}

function normalizeConfigJson(value) {
  const config = typeof value === 'string' ? parseJsonOutput(value) : value;
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('MCP config must be a JSON object.');
  }
  const json = JSON.stringify(config);
  if (json.length > 50_000) throw new Error('MCP config is too large.');
  return json;
}

function writeLog(level, message, meta = {}) {
  try {
    fs.mkdirSync(app.getPath('logs'), { recursive: true });
    const payload = {
      time: new Date().toISOString(),
      level,
      message,
      ...redactForLog(meta)
    };
    fs.appendFileSync(logPath(), `${JSON.stringify(payload)}\n`, 'utf8');
  } catch {
    // Logging must never break the app.
  }
}

function readRecentLogs(limit = MAX_LOG_LINES) {
  try {
    const file = logPath();
    if (!fs.existsSync(file)) return [];
    return fs
      .readFileSync(file, 'utf8')
      .split(/\r?\n/)
      .filter(Boolean)
      .slice(-Math.min(Math.max(Number(limit) || MAX_LOG_LINES, 1), MAX_LOG_LINES))
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return { time: null, level: 'info', message: line };
        }
      });
  } catch {
    return [];
  }
}

function readCrashEvents() {
  try {
    const file = crashSummaryPath();
    if (!fs.existsSync(file)) return [];
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const events = Array.isArray(raw?.events) ? raw.events : Array.isArray(raw) ? raw : [];
    return events.filter((event) => event && typeof event === 'object').slice(-MAX_CRASH_EVENTS * 2);
  } catch {
    return [];
  }
}

function crashProcessForKind(kind = '') {
  if (/^renderer/i.test(kind)) return 'renderer';
  if (/^main/i.test(kind)) return 'main';
  return 'app';
}

function isCrashRecoveryRestoredKind(kind = '') {
  return /(?:renderer-)?responsive|renderer-recovery-reload|renderer-restored|app-restored/i.test(kind);
}

function isCrashRecoveryInformationalKind(kind = '') {
  return isCrashRecoveryRestoredKind(kind) || /recovery-scheduled/i.test(kind);
}

function crashSeverityForKind(kind = '') {
  if (/recovery-failed|recovery-suppressed/i.test(kind)) return 'error';
  if (isCrashRecoveryInformationalKind(kind)) return 'info';
  if (/unresponsive|rejection/i.test(kind)) return 'warn';
  return 'error';
}

function normalizeCrashEvent(event = {}) {
  const time = typeof event.time === 'string' ? event.time : new Date().toISOString();
  const kind = safeDiagnosticText(event.kind || 'app-event', 120);
  return {
    id: typeof event.id === 'string' ? event.id : publicStableId('crash', `${time}:${kind}`),
    time,
    process: event.process || crashProcessForKind(kind),
    kind,
    severity: event.severity || crashSeverityForKind(kind),
    appVersion: event.appVersion || app.getVersion(),
    platform: event.platform || process.platform,
    arch: event.arch || process.arch,
    details: safeDiagnosticValue(event.details || {}, {
      stringLimit: 4000,
      maxArrayItems: 40
    })
  };
}

function readCrashSummary(limit = 20) {
  const max = Math.min(Math.max(Number(limit) || 20, 1), MAX_CRASH_EVENTS);
  const events = readCrashEvents().map(normalizeCrashEvent);
  const recent = events
    .sort((left, right) => new Date(right.time).getTime() - new Date(left.time).getTime())
    .slice(0, max);
  const problemEvents = recent.filter((event) => !isCrashRecoveryInformationalKind(event.kind));
  const latestProblem = problemEvents[0] || null;
  const latestRecovery = recent.find((event) => isCrashRecoveryRestoredKind(event.kind)) || null;
  const restored = !latestProblem || (latestRecovery && new Date(latestRecovery.time).getTime() >= new Date(latestProblem.time).getTime());
  const counts = recent.reduce((acc, event) => {
    acc.byKind[event.kind] = (acc.byKind[event.kind] || 0) + 1;
    acc.byProcess[event.process] = (acc.byProcess[event.process] || 0) + 1;
    acc.bySeverity[event.severity] = (acc.bySeverity[event.severity] || 0) + 1;
    return acc;
  }, { byKind: {}, byProcess: {}, bySeverity: {} });
  return {
    ok: true,
    total: events.length,
    lastCrashAt: latestProblem?.time || null,
    lastEventAt: recent[0]?.time || null,
    restored: Boolean(restored),
    counts,
    recent
  };
}

function errorCrashDetails(error) {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: safeDiagnosticText(error.message || '', 2000),
      stack: safeDiagnosticText(error.stack || '', 8000)
    };
  }
  return {
    message: safeDiagnosticText(String(error || ''), 2000)
  };
}

function recordCrashEvent(kind, details = {}) {
  const now = new Date().toISOString();
  const event = normalizeCrashEvent({
    id: `crash-${Date.now().toString(36)}-${crypto.randomBytes(4).toString('hex')}`,
    time: now,
    kind,
    process: crashProcessForKind(kind),
    severity: crashSeverityForKind(kind),
    appVersion: app.getVersion(),
    platform: process.platform,
    arch: process.arch,
    details: {
      ...details,
      window: details.window || windowDiagnosticSnapshot().window || null
    }
  });
  try {
    const events = [...readCrashEvents().map(normalizeCrashEvent), event].slice(-MAX_CRASH_EVENTS);
    atomicWriteJson(crashSummaryPath(), {
      version: 1,
      updatedAt: now,
      events
    });
  } catch {
    // Crash persistence is best-effort and must never cascade.
  }
  enqueueAnonymousTelemetry('crash', {
    kind: event.kind,
    process: event.process,
    severity: event.severity,
    appVersion: event.appVersion,
    platform: event.platform,
    arch: event.arch
  });
  return event;
}

function readTelemetryQueue() {
  try {
    const file = telemetryQueuePath();
    if (!fs.existsSync(file)) return { version: 1, events: [] };
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 512 * 1024) return { version: 1, events: [] };
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const events = Array.isArray(raw?.events) ? raw.events : [];
    return { version: 1, events: events.slice(-MAX_TELEMETRY_EVENTS) };
  } catch {
    return { version: 1, events: [] };
  }
}

function writeTelemetryQueue(events = []) {
  atomicWriteJson(telemetryQueuePath(), {
    version: 1,
    updatedAt: new Date().toISOString(),
    events: events.slice(-MAX_TELEMETRY_EVENTS)
  });
}

function publicTelemetryStatus(settings = readSettings()) {
  const queue = readTelemetryQueue();
  return {
    enabled: Boolean(settings.anonymousTelemetryEnabled),
    endpointConfigured: Boolean(settings.telemetryEndpoint),
    queuedEvents: queue.events.length,
    installId: settings.anonymousTelemetryEnabled ? settings.telemetryInstallId : '',
    privacy: {
      anonymous: true,
      includesPrompts: false,
      includesApiKeys: false,
      includesLocalPathBodies: false
    }
  };
}

function enqueueAnonymousTelemetry(kind, payload = {}) {
  try {
    const settings = readSettings();
    if (!settings.anonymousTelemetryEnabled) return null;
    const event = {
      id: `telemetry-${Date.now().toString(36)}-${crypto.randomBytes(3).toString('hex')}`,
      time: new Date().toISOString(),
      kind: safeOutputText(kind, 80),
      installId: settings.telemetryInstallId,
      appVersion: app.getVersion(),
      platform: process.platform,
      arch: process.arch,
      payload: JSON.parse(JSON.stringify(payload || {}))
    };
    const queue = readTelemetryQueue();
    writeTelemetryQueue([...queue.events, event]);
    return event;
  } catch {
    return null;
  }
}

async function flushAnonymousTelemetry(payload = {}) {
  optionalObjectPayload(payload, 'telemetry payload');
  const settings = readSettings();
  const status = publicTelemetryStatus(settings);
  if (!settings.anonymousTelemetryEnabled) return { ok: true, sent: 0, status, skipped: 'disabled' };
  if (!settings.telemetryEndpoint) return { ok: true, sent: 0, status, skipped: 'endpoint-not-configured' };
  const queue = readTelemetryQueue();
  if (!queue.events.length) return { ok: true, sent: 0, status };
  const endpoint = settings.telemetryEndpoint;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        schema: 'ecorex.anonymous-telemetry.v1',
        sentAt: new Date().toISOString(),
        events: queue.events
      }),
      signal: controller.signal
    });
    if (!response.ok) throw new Error(`telemetry upload failed: ${response.status}`);
    writeTelemetryQueue([]);
    return { ok: true, sent: queue.events.length, status: publicTelemetryStatus(readSettings()) };
  } catch (error) {
    return { ok: false, sent: 0, error: safeOutputText(error instanceof Error ? error.message : String(error), 1000), status };
  } finally {
    clearTimeout(timer);
  }
}

function claimRendererRecoverySlot(now = Date.now()) {
  rendererRecoveryAttempts = rendererRecoveryAttempts.filter((time) => now - time <= RENDERER_RECOVERY_WINDOW_MS);
  if (rendererRecoveryAttempts.length >= MAX_RENDERER_RECOVERY_ATTEMPTS) {
    return false;
  }
  rendererRecoveryAttempts.push(now);
  return true;
}

function clearRendererUnresponsiveRecovery() {
  rendererUnresponsiveSince = 0;
  if (rendererUnresponsiveTimer) {
    clearTimeout(rendererUnresponsiveTimer);
    rendererUnresponsiveTimer = null;
  }
}

function recoverRendererAfterCrash(details = {}) {
  const reason = String(details?.reason || '').toLowerCase();
  if (reason === 'clean-exit') return;
  if (!claimRendererRecoverySlot()) {
    recordCrashEvent('renderer-recovery-suppressed', {
      reason: details?.reason,
      exitCode: details?.exitCode,
      maxAttempts: MAX_RENDERER_RECOVERY_ATTEMPTS,
      windowMs: RENDERER_RECOVERY_WINDOW_MS
    });
    writeLog('error', 'Renderer recovery suppressed after repeated crashes', {
      reason: details?.reason,
      exitCode: details?.exitCode,
      maxAttempts: MAX_RENDERER_RECOVERY_ATTEMPTS,
      windowMs: RENDERER_RECOVERY_WINDOW_MS
    });
    return;
  }
  recordCrashEvent('renderer-recovery-scheduled', { reason: details?.reason, exitCode: details?.exitCode });
  setTimeout(() => {
    if (!mainWindow || mainWindow.isDestroyed?.()) return;
    try {
      if (!mainWindow.isVisible()) mainWindow.show();
      mainWindow.focus();
      mainWindow.webContents.reload();
      recordCrashEvent('renderer-recovery-reload', { reason: details?.reason, exitCode: details?.exitCode });
      writeLog('info', 'Renderer recovery reload scheduled', { reason: details?.reason, exitCode: details?.exitCode });
    } catch (error) {
      recordCrashEvent('renderer-recovery-failed', errorCrashDetails(error));
      writeLog('error', 'Renderer recovery failed', errorCrashDetails(error));
    }
  }, RENDERER_RECOVERY_DELAY_MS);
}

function armRendererUnresponsiveRecovery() {
  if (rendererUnresponsiveTimer) clearTimeout(rendererUnresponsiveTimer);
  rendererUnresponsiveTimer = setTimeout(() => {
    if (!rendererUnresponsiveSince || !mainWindow || mainWindow.isDestroyed?.()) return;
    const unresponsiveMs = Date.now() - rendererUnresponsiveSince;
    recordCrashEvent('renderer-unresponsive-recovery-scheduled', { unresponsiveMs });
    writeLog('warn', 'Renderer stayed unresponsive; scheduling recovery reload', { unresponsiveMs });
    stopAllAgents('renderer-unresponsive');
    recoverRendererAfterCrash({ reason: 'unresponsive-timeout', unresponsiveMs });
    clearRendererUnresponsiveRecovery();
  }, RENDERER_UNRESPONSIVE_RECOVERY_MS);
}

function filteredAgentEnv(extra = {}, options = {}) {
  const env = {};
  for (const key of AGENT_ENV_ALLOWLIST) {
    if (process.env[key]) env[key] = process.env[key];
  }
  const includeSecrets = options.includeSecrets !== false;
  return { ...env, ...(includeSecrets ? readAgentSecretsForEnv() : {}), ...extra };
}

function isTrustedSender(event) {
  const senderUrl = event?.senderFrame?.url || event?.sender?.getURL?.() || '';
  if (!senderUrl || !mainWindow || event?.sender !== mainWindow.webContents) return false;
  if (event?.senderFrame && event.sender?.mainFrame && event.senderFrame !== event.sender.mainFrame) return false;
  return isAllowedRendererUrl(senderUrl);
}

function isPathInside(base, target) {
  const resolvePath = (location) => {
    try {
      return fs.existsSync(location) ? fs.realpathSync.native(location) : path.resolve(location);
    } catch {
      return path.resolve(location);
    }
  };
  const relative = path.relative(resolvePath(base), resolvePath(target));
  return relative === '' || (relative && !relative.startsWith('..') && !path.isAbsolute(relative));
}

function pathContainsSymlink(base, target) {
  const basePath = path.resolve(base);
  const targetPath = path.resolve(target);
  const relative = path.relative(basePath, targetPath);
  if (!relative) return false;
  if (relative.startsWith('..') || path.isAbsolute(relative)) return true;
  let current = basePath;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    try {
      if (fs.lstatSync(current).isSymbolicLink()) return true;
    } catch {
      break;
    }
  }
  return false;
}

function defaultWorkspaceRoot() {
  const workspace = path.join(app.getPath('userData'), 'workspace');
  return workspace;
}

function agentRuntimeConfigDir() {
  const dir = path.join(app.getPath('userData'), ECOREX_AGENT_CONFIG_DIR_NAME);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function claudeSessionBindingsPath() {
  return path.join(agentRuntimeConfigDir(), CLAUDE_SESSION_BINDINGS_FILE_NAME);
}

function managedSkillPacksStatePath() {
  return path.join(agentRuntimeConfigDir(), MANAGED_SKILL_PACKS_FILE_NAME);
}

function managedMcpConfigPath() {
  return path.join(agentRuntimeConfigDir(), 'managed-mcp.json');
}

function managedSkillPacksStateCacheKey() {
  try {
    const stat = fs.statSync(managedSkillPacksStatePath());
    return `${stat.size}:${stat.mtimeMs}`;
  } catch {
    return 'missing';
  }
}

function managedSkillPacksDir() {
  const dir = path.join(agentRuntimeConfigDir(), MANAGED_SKILL_PACKS_DIR_NAME);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function generalAgentWorkspaceDir() {
  const dir = path.join(agentRuntimeConfigDir(), ECOREX_GENERAL_WORKSPACE_DIR_NAME);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function isolatedAgentRuntimeEnv() {
  const configDir = agentRuntimeConfigDir();
  const appDataDir = path.join(configDir, 'appdata');
  const localAppDataDir = path.join(configDir, 'local-appdata');
  fs.mkdirSync(appDataDir, { recursive: true });
  fs.mkdirSync(localAppDataDir, { recursive: true });
  return {
    HOME: configDir,
    USERPROFILE: configDir,
    APPDATA: appDataDir,
    LOCALAPPDATA: localAppDataDir,
    XDG_CONFIG_HOME: configDir,
    CLAUDE_CONFIG_DIR: configDir,
    ECOREX_AGENT_CONFIG_DIR: configDir,
    CLAUDE_CODE_DISABLE_AUTO_MEMORY: '1',
    ECOREX_SKILL_SCOPE: 'bundled-only'
  };
}

function isBlockedLocalSkillName(value = '') {
  const normalized = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/\\/g, '/');
  if (!normalized) return false;
  const compact = normalized.replace(/[_\s]+/g, '-');
  return [...BLOCKED_LOCAL_SKILL_NAMES].some((name) => compact === name || compact.includes(`/${name}/`) || compact.endsWith(`/${name}`));
}

function sanitizeSkillPackName(value = '', fallback = 'skill-pack') {
  const cleaned = String(value || '')
    .trim()
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
    .pop()
    ?.replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  const name = cleaned || fallback;
  if (isBlockedLocalSkillName(name)) throw new Error('This local skill pack is blocked by EcoreX isolation policy.');
  return name;
}

function decodeJsonFileBuffer(buffer) {
  if (!Buffer.isBuffer(buffer)) return String(buffer || '');
  if (buffer.length >= 3 && buffer[0] === 0xef && buffer[1] === 0xbb && buffer[2] === 0xbf) {
    return buffer.subarray(3).toString('utf8');
  }
  if (buffer.length >= 2 && buffer[0] === 0xff && buffer[1] === 0xfe) {
    return buffer.subarray(2).toString('utf16le');
  }
  if (buffer.length >= 2 && buffer[0] === 0xfe && buffer[1] === 0xff) {
    const swapped = Buffer.allocUnsafe(buffer.length - 2);
    for (let index = 2; index + 1 < buffer.length; index += 2) {
      swapped[index - 2] = buffer[index + 1];
      swapped[index - 1] = buffer[index];
    }
    return swapped.toString('utf16le');
  }
  const utf8 = buffer.toString('utf8');
  if (utf8.includes('\u0000')) {
    const sample = buffer.subarray(0, Math.min(buffer.length, 80));
    let oddNulls = 0;
    let evenNulls = 0;
    for (let index = 0; index < sample.length; index += 1) {
      if (sample[index] !== 0) continue;
      if (index % 2) oddNulls += 1;
      else evenNulls += 1;
    }
    if (oddNulls > evenNulls + 4) return buffer.toString('utf16le');
  }
  return utf8.replace(/^\uFEFF/, '');
}

function readJsonFileSafe(filePath, fallback = null) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(decodeJsonFileBuffer(fs.readFileSync(filePath)).replace(/^\uFEFF/, ''));
  } catch {
    return fallback;
  }
}

function writeJsonFileSafe(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function readManagedSkillPackState() {
  const raw = readJsonFileSafe(managedSkillPacksStatePath(), []);
  const rows = Array.isArray(raw) ? raw : Array.isArray(raw?.skillPacks) ? raw.skillPacks : [];
  return rows
    .filter((row) => row && typeof row === 'object')
    .map((row, index) => normalizeManagedSkillPackRecord(row, index))
    .filter(Boolean)
    .slice(0, MAX_MANAGED_ITEMS);
}

function writeManagedSkillPackState(rows = []) {
  const nextRows = (Array.isArray(rows) ? rows : [])
    .filter((row) => row && typeof row === 'object')
    .map((row, index) => normalizeManagedSkillPackRecord(row, index))
    .filter(Boolean)
    .slice(0, MAX_MANAGED_ITEMS);
  writeJsonFileSafe(managedSkillPacksStatePath(), nextRows);
  cachedCapabilities = null;
  cachedCapabilitiesKey = '';
  cachedBackendStatus = null;
  return nextRows;
}

function normalizeManagedSkillPackRecord(row = {}, index = 0) {
  const name = sanitizeSkillPackName(row.name || row.id || `skill-pack-${index + 1}`);
  const id = String(row.id || publicStableId('skillpack', name)).slice(0, 120);
  const installPath = row.installPath ? path.resolve(String(row.installPath)) : '';
  const sourcePath = row.sourcePath ? path.resolve(String(row.sourcePath)) : '';
  return {
    id,
    name,
    title: safeOutputText(row.title || row.displayName || name, 180),
    version: row.version ? safeOutputText(row.version, 80) : null,
    description: row.description ? safeOutputText(row.description, 1000) : '',
    category: safeOutputText(row.category || row.type || 'EcoreX', 80),
    enabled: row.enabled !== false,
    installed: row.installed !== false,
    installPath,
    sourcePath,
    sourceKind: String(row.sourceKind || 'local')
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_.-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 80) || 'local',
    generatedWrapper: Boolean(row.generatedWrapper),
    mcpConfig: row.mcpConfig && typeof row.mcpConfig === 'object' ? safeJsonValue(row.mcpConfig, 8000) : null,
    installedAt: row.installedAt || new Date().toISOString(),
    lastUpdated: row.lastUpdated || row.updatedAt || row.installedAt || new Date().toISOString()
  };
}

function installDestinationForSkillPack(name = '') {
  const root = managedSkillPacksDir();
  const safeName = sanitizeSkillPackName(name);
  const destination = path.join(root, safeName);
  if (!isPathInside(root, destination)) throw new Error('Invalid skill pack destination.');
  return destination;
}

function skillPluginManifest(pluginRoot = '') {
  return readJsonFileSafe(path.join(pluginRoot, '.claude-plugin', 'plugin.json'), null);
}

function skillPluginRootFromSource(sourcePath = '') {
  const resolved = path.resolve(String(sourcePath || ''));
  if (!fs.existsSync(resolved)) throw new Error(`Skill source not found: ${sourcePath}`);
  const stat = fs.statSync(resolved);
  const sourceDir = stat.isDirectory() ? resolved : path.dirname(resolved);
  const directManifest = path.join(sourceDir, '.claude-plugin', 'plugin.json');
  if (fs.existsSync(directManifest)) return sourceDir;
  const nestedManifest = path.join(sourceDir, 'skills', '.claude-plugin', 'plugin.json');
  if (fs.existsSync(nestedManifest)) return path.join(sourceDir, 'skills');
  return null;
}

function skillCollectionRootFromSource(sourcePath = '') {
  const resolved = path.resolve(String(sourcePath || ''));
  if (!fs.existsSync(resolved)) return null;
  const stat = fs.statSync(resolved);
  const sourceDir = stat.isDirectory() ? resolved : path.dirname(resolved);
  const candidates = [path.join(sourceDir, 'skills'), sourceDir];
  for (const candidate of candidates) {
    try {
      if (!fs.existsSync(candidate) || !fs.statSync(candidate).isDirectory()) continue;
      const skillDirs = fs.readdirSync(candidate, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .filter((entry) => fs.existsSync(path.join(candidate, entry.name, 'SKILL.md')));
      if (skillDirs.length) return { root: candidate, count: skillDirs.length };
    } catch {
      // Ignore malformed candidates and continue probing.
    }
  }
  return null;
}

function packageSkillPackName(packageName = '', fallback = 'skill-pack') {
  const raw = String(packageName || '').trim();
  if (raw === '@larksuite/cli') return 'lark-cli';
  return sanitizeSkillPackName(raw.replace(/^@/, '').replace(/[\\/]+/g, '-') || fallback);
}

function detectSkillSource(sourcePath = '') {
  const resolved = path.resolve(String(sourcePath || ''));
  if (!fs.existsSync(resolved)) throw new Error(`Skill source not found: ${sourcePath}`);
  const stat = fs.statSync(resolved);
  const sourceDir = stat.isDirectory() ? resolved : path.dirname(resolved);
  const pluginRoot = skillPluginRootFromSource(sourceDir);
  if (pluginRoot) {
    const manifest = skillPluginManifest(pluginRoot) || {};
    return {
      kind: 'plugin',
      sourcePath: resolved,
      pluginRoot,
      name: sanitizeSkillPackName(manifest.name || path.basename(pluginRoot)),
      title: manifest.displayName || manifest.title || manifest.name || path.basename(pluginRoot),
      version: manifest.version || null,
      description: manifest.description || readMarkdownSummary(path.join(pluginRoot, 'README.md')),
      category: manifest.category || 'EcoreX',
      generatedWrapper: false
    };
  }

  const skillCollection = skillCollectionRootFromSource(sourceDir);
  if (skillCollection) {
    const packageInfo = readJsonFileSafe(path.join(sourceDir, 'package.json'), {}) || {};
    const name = packageSkillPackName(packageInfo.name, path.basename(sourceDir));
    return {
      kind: 'skill-collection',
      sourcePath: resolved,
      pluginRoot: skillCollection.root,
      name,
      title: packageInfo.displayName || packageInfo.title || (packageInfo.name === '@larksuite/cli' ? 'lark-cli' : name),
      version: packageInfo.version || null,
      description: packageInfo.description || readMarkdownSummary(path.join(sourceDir, 'README.md')),
      category: 'EcoreX',
      generatedWrapper: true,
      skillCount: skillCollection.count
    };
  }

  const manifestPath = path.join(sourceDir, 'manifest.json');
  const mcpManifest = readJsonFileSafe(manifestPath, null);
  if (mcpManifest?.server || Array.isArray(mcpManifest?.tools)) {
    return {
      kind: 'mcp-wrapper',
      sourcePath: resolved,
      pluginRoot: null,
      name: sanitizeSkillPackName(mcpManifest.name || path.basename(sourceDir)),
      title: mcpManifest.displayName || mcpManifest.title || mcpManifest.name || path.basename(sourceDir),
      version: mcpManifest.version || null,
      description: mcpManifest.description || readMarkdownSummary(path.join(sourceDir, 'README.md')),
      category: 'MCP',
      generatedWrapper: true,
      mcpConfig: mcpManifest.server?.mcp_config || null,
      toolNames: Array.isArray(mcpManifest.tools) ? mcpManifest.tools.map((tool) => tool?.name).filter(Boolean).slice(0, 40) : []
    };
  }

  if (fs.existsSync(path.join(sourceDir, 'SKILL.md'))) {
    return {
      kind: 'skill-wrapper',
      sourcePath: resolved,
      pluginRoot: sourceDir,
      name: sanitizeSkillPackName(path.basename(sourceDir)),
      title: path.basename(sourceDir),
      version: null,
      description: readMarkdownSummary(path.join(sourceDir, 'SKILL.md')),
      category: 'EcoreX',
      generatedWrapper: true
    };
  }

  throw new Error('Skill source must contain .claude-plugin/plugin.json, SKILL.md, skills/*/SKILL.md, or an MCP manifest.json.');
}

function shouldSkipSkillPackCopy(relativePath = '') {
  const normalized = String(relativePath || '').replace(/\\/g, '/');
  return /(^|\/)(\.git|node_modules|__pycache__|\.venv|venv|dist|build|release|test-results)(\/|$)/i.test(normalized)
    || /(^|\/)(\.env|\.env\..*|secrets\.json|auth-session\.json|auth-users\.json|settings\.json)$/i.test(normalized);
}

function copyDirectoryBounded(sourceDir, targetDir, state = { bytes: 0 }) {
  const sourceRoot = path.resolve(sourceDir);
  const targetRoot = path.resolve(targetDir);
  fs.mkdirSync(targetRoot, { recursive: true });
  for (const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })) {
    const source = path.join(sourceRoot, entry.name);
    const relative = path.relative(sourceRoot, source);
    if (shouldSkipSkillPackCopy(relative)) continue;
    const target = path.join(targetRoot, entry.name);
    if (!isPathInside(targetRoot, target)) throw new Error('Invalid skill pack copy target.');
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      copyDirectoryBounded(source, target, state);
      continue;
    }
    if (!entry.isFile()) continue;
    const stat = fs.statSync(source);
    state.bytes += stat.size;
    if (state.bytes > MAX_SKILL_PACK_COPY_BYTES) throw new Error('Skill pack is too large.');
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(source, target);
  }
}

function createGeneratedSkillMarkdown(info = {}) {
  const name = sanitizeSkillPackName(info.name || 'skill-pack');
  const toolList = Array.isArray(info.toolNames) && info.toolNames.length
    ? `\n\nKnown tools:\n${info.toolNames.map((tool) => `- ${tool}`).join('\n')}`
    : '';
  const mcpConfig = info.mcpConfig ? `\n\nSuggested MCP config:\n\n\`\`\`json\n${JSON.stringify(info.mcpConfig, null, 2)}\n\`\`\`\n` : '';
  return [
    '---',
    `name: ${name}`,
    `description: ${safeOutputText(info.description || `${name} capability for EcoreX Agent.`, 240).replace(/\r?\n/g, ' ')}`,
    '---',
    '',
    `# ${safeOutputText(info.title || name, 160)}`,
    '',
    safeOutputText(info.description || 'Use this EcoreX-managed skill when the task matches its capability area.', 1000),
    '',
    'Use this skill inside EcoreX Agent only. Do not expose backend implementation details to the user.',
    toolList,
    mcpConfig
  ].join('\n');
}

function writeGeneratedSkillPlugin(destination, info = {}) {
  const name = sanitizeSkillPackName(info.name || path.basename(destination));
  const manifestDir = path.join(destination, '.claude-plugin');
  const skillDir = path.join(destination, 'skills', name);
  fs.mkdirSync(manifestDir, { recursive: true });
  fs.mkdirSync(skillDir, { recursive: true });
  writeJsonFileSafe(path.join(manifestDir, 'plugin.json'), {
    name,
    description: info.description || `${name} capability for EcoreX Agent.`,
    version: info.version || '1.0.0',
    skills: './skills'
  });
  fs.writeFileSync(path.join(skillDir, 'SKILL.md'), createGeneratedSkillMarkdown(info), 'utf8');
  if (info.mcpConfig) writeJsonFileSafe(path.join(skillDir, 'mcp-config.json'), info.mcpConfig);
}

function writeSkillCollectionPlugin(destination, info = {}) {
  const name = sanitizeSkillPackName(info.name || path.basename(destination));
  const manifestDir = path.join(destination, '.claude-plugin');
  const skillsRoot = path.join(destination, 'skills');
  fs.mkdirSync(manifestDir, { recursive: true });
  fs.mkdirSync(skillsRoot, { recursive: true });
  writeJsonFileSafe(path.join(manifestDir, 'plugin.json'), {
    name,
    displayName: info.title || name,
    description: info.description || `${name} skill collection for EcoreX Agent.`,
    version: info.version || '1.0.0',
    skills: './skills'
  });
  copyDirectoryBounded(info.pluginRoot, skillsRoot);
}

function installManagedSkillPack(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'skill install payload');
  const sourcePath = String(payload.sourcePath || payload.path || payload.localPath || '').trim();
  if (!sourcePath) throw new Error('Skill install requires sourcePath.');
  const info = detectSkillSource(sourcePath);
  const name = sanitizeSkillPackName(payload.name || info.name);
  const destination = installDestinationForSkillPack(name);
  const libraryRoot = managedSkillPacksDir();
  const resolvedDestination = path.resolve(destination);
  if (!isPathInside(libraryRoot, resolvedDestination) || resolvedDestination === path.resolve(libraryRoot)) {
    throw new Error('Invalid skill pack destination.');
  }
  fs.rmSync(resolvedDestination, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  fs.mkdirSync(resolvedDestination, { recursive: true });
  if (info.kind === 'plugin') {
    copyDirectoryBounded(info.pluginRoot, resolvedDestination);
  } else if (info.kind === 'skill-collection' && info.pluginRoot) {
    writeSkillCollectionPlugin(resolvedDestination, { ...info, name });
  } else if (info.kind === 'skill-wrapper' && info.pluginRoot) {
    writeGeneratedSkillPlugin(resolvedDestination, { ...info, name });
    const skillDir = path.join(resolvedDestination, 'skills', name);
    copyDirectoryBounded(info.pluginRoot, skillDir);
  } else {
    writeGeneratedSkillPlugin(resolvedDestination, { ...info, name });
  }

  const now = new Date().toISOString();
  const existing = readManagedSkillPackState().filter((row) => row.name !== name && row.id !== publicStableId('skillpack', name));
  const record = normalizeManagedSkillPackRecord({
    id: publicStableId('skillpack', name),
    name,
    title: payload.title || info.title || name,
    version: payload.version || info.version || null,
    description: payload.description || info.description || '',
    category: payload.category || info.category || 'EcoreX',
    enabled: payload.enabled !== false,
    installed: true,
    installPath: resolvedDestination,
    sourcePath: path.resolve(sourcePath),
    sourceKind: info.kind,
    generatedWrapper: info.generatedWrapper,
    mcpConfig: info.mcpConfig || null,
    installedAt: now,
    lastUpdated: now
  });
  const rows = writeManagedSkillPackState([...existing, record]);
  writeLog('info', 'Managed skill pack installed', {
    actor: authContext?.user?.email || null,
    name,
    sourceKind: info.kind,
    generatedWrapper: info.generatedWrapper
  });
  return {
    ok: true,
    installed: publicSkillPackRecord(record),
    record: publicSkillPackRecord(record),
    status: collectSkillStatus({ refresh: true }),
    count: rows.length
  };
}

function managedSkillPackMatchesPayload(record = {}, payload = {}) {
  const wanted = [payload.id, payload.name, payload.pluginId, payload.skill?.id, payload.skill?.name]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase());
  if (!wanted.length) return false;
  const own = [record.id, record.name, publicStableId('skillpack', record.name)]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase());
  if (wanted.some((value) => own.includes(value))) return true;
  return listSkillsInPluginRoot(record.installPath, record)
    .some((skill) => wanted.includes(String(skill.id || '').toLowerCase()) || wanted.includes(String(skill.name || '').toLowerCase()));
}

function updateManagedSkillEnabled(payload = {}, enabled = true) {
  optionalObjectPayload(payload, 'skill toggle payload');
  const rows = readManagedSkillPackState();
  const index = rows.findIndex((row) => managedSkillPackMatchesPayload(row, payload));
  if (index < 0) throw new Error('Skill pack not found.');
  rows[index] = normalizeManagedSkillPackRecord({
    ...rows[index],
    enabled,
    lastUpdated: new Date().toISOString()
  });
  writeManagedSkillPackState(rows);
  return {
    ok: true,
    updated: publicSkillPackRecord(rows[index]),
    status: collectSkillStatus({ refresh: true })
  };
}

function updateManagedSkillPack(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'skill update payload');
  const rows = readManagedSkillPackState();
  const record = rows.find((row) => managedSkillPackMatchesPayload(row, payload));
  if (!record) throw new Error('Skill pack not found.');
  if (!record.sourcePath) {
    return { ok: true, updated: publicSkillPackRecord(record), status: collectSkillStatus({ refresh: true }) };
  }
  return installManagedSkillPack({ sourcePath: record.sourcePath, name: record.name, enabled: record.enabled }, authContext);
}

function collectManagedSkillInventory(options = {}) {
  const includeDisabled = options.includeDisabled !== false;
  const rows = readManagedSkillPackState()
    .filter((row) => row.installed && row.installPath && fs.existsSync(row.installPath))
    .filter((row) => includeDisabled || row.enabled);
  const skillsByPack = new Map();
  const skills = rows.flatMap((row) => {
    const packSkills = listSkillsInPluginRoot(row.installPath, {
    ...row,
    id: row.name,
    name: row.name,
    version: row.version,
    scope: 'managed',
    enabled: row.enabled,
    installed: true
    }).map((skill) => ({
      ...skill,
      category: row.category || skill.category || 'EcoreX',
      enabled: row.enabled,
      installed: true,
      sourcePath: row.sourcePath,
      installPath: row.installPath,
      sourceKind: row.sourceKind
    }));
    skillsByPack.set(row.name, packSkills);
    return packSkills;
  });
  const skillPacks = rows.map((row) => {
    const packSkills = skillsByPack.get(row.name) || [];
    return {
      ...publicSkillPackRecord(row),
      type: row.sourceKind === 'mcp-wrapper' ? 'mcp-backed-skill-pack' : 'skill-pack',
      skills: 1,
      skillCount: 1,
      childSkillCount: packSkills.length || 1,
      commands: row.sourceKind === 'mcp-wrapper' ? 1 : 0,
      agents: 0,
      hooks: 0,
      provider: {
        id: publicStableId('skillpack', row.name),
        name: row.name,
        version: row.version || null
      }
    };
  });
  return { rows, skillPacks, skills };
}

function runtimeManagedSkillPlugins() {
  return collectManagedSkillInventory({ includeDisabled: false }).rows.map((row) => ({
    name: row.name,
    source: row.installPath,
    installPath: row.installPath,
    available: true,
    managed: true,
    enabled: row.enabled
  }));
}

function enabledManagedSkillPackNames() {
  return runtimeManagedSkillPlugins().map((plugin) => plugin.name);
}

function allowedRuntimePluginNames(requested = []) {
  const managedNames = enabledManagedSkillPackNames()
    .filter((plugin) => /^[a-zA-Z0-9_.-]{1,80}$/.test(String(plugin || '')))
    .filter((plugin) => !isBlockedLocalSkillName(plugin));
  const allowedNames = new Set([...ECOREX_BUILTIN_PLUGIN_ALLOWLIST, ...managedNames]);
  const requestedNames = Array.isArray(requested) ? requested : [];
  return [...new Set([...requestedNames, ...managedNames]
    .map((plugin) => String(plugin || '').trim())
    .filter((plugin) => /^[a-zA-Z0-9_.-]{1,80}$/.test(plugin))
    .filter((plugin) => allowedNames.has(plugin))
    .filter((plugin) => !isBlockedLocalSkillName(plugin)))]
    .slice(0, 12);
}

function enabledManagedMcpServers() {
  const servers = {};
  for (const row of collectManagedSkillInventory({ includeDisabled: false }).rows) {
    if (!row.mcpConfig || typeof row.mcpConfig !== 'object') continue;
    const name = sanitizeSkillPackName(row.name || row.id || 'managed-mcp');
    servers[name] = safeJsonValue(row.mcpConfig, 8000);
  }
  return servers;
}

function prepareManagedMcpConfigFile() {
  const mcpServers = enabledManagedMcpServers();
  const names = Object.keys(mcpServers);
  if (!names.length) return null;
  const file = managedMcpConfigPath();
  writeJsonFileSafe(file, { mcpServers });
  return file;
}

function runtimePluginPathAllowed(pluginPath, backendRoot, pluginName = '') {
  const name = String(pluginName || '').trim();
  if (!name || isBlockedLocalSkillName(name) || isBlockedLocalSkillName(pluginPath)) return false;
  const resolved = path.resolve(pluginPath);
  const managedNames = new Set(enabledManagedSkillPackNames());
  if (managedNames.has(name)) {
    return isPathInside(managedSkillPacksDir(), resolved);
  }
  if (!ECOREX_BUILTIN_PLUGIN_ALLOWLIST.has(name)) return false;
  return isPathInside(backendRoot, resolved);
}

function workspacePathKey(value) {
  const resolved = path.resolve(String(value || ''));
  return isWindows ? resolved.toLowerCase() : resolved;
}

function isSameWorkspacePath(left, right) {
  return workspacePathKey(left) === workspacePathKey(right);
}

function hasCustomWorkspaceRootConfirmation(payload = {}) {
  return (
    payload?.allowCustomWorkspaceRoot === true ||
    payload?.confirmCustomWorkspaceRoot === true ||
    payload?.workspaceRootConfirmed === true ||
    payload?.customWorkspaceRootConfirmed === true
  );
}

function protectedWorkspaceRoots() {
  const roots = [
    process.env.SystemRoot,
    process.env.WINDIR,
    process.env.ProgramFiles,
    process.env['ProgramFiles(x86)'],
    process.env.ProgramData
  ];
  for (const name of ['home', 'appData', 'userData', 'temp']) {
    try {
      roots.push(app.getPath(name));
    } catch {
      // Some Electron paths are only available after app initialization.
    }
  }
  return roots.filter(Boolean).map((item) => path.resolve(item));
}

function protectedWorkspaceDescendantRoots() {
  const roots = [
    process.env.SystemRoot,
    process.env.WINDIR,
    process.env.ProgramFiles,
    process.env['ProgramFiles(x86)'],
    process.env.ProgramData
  ];
  for (const name of ['appData', 'userData', 'temp']) {
    try {
      roots.push(app.getPath(name));
    } catch {
      // Some Electron paths are only available after app initialization.
    }
  }
  return roots.filter(Boolean).map((item) => path.resolve(item));
}

function isProtectedWorkspaceRoot(target) {
  const resolved = path.resolve(target);
  const parsed = path.parse(resolved);
  if (isSameWorkspacePath(resolved, defaultWorkspaceRoot())) return false;
  if (isSameWorkspacePath(resolved, parsed.root)) return true;
  const descendantRootKeys = new Set(protectedWorkspaceDescendantRoots().map(workspacePathKey));
  return protectedWorkspaceRoots().some((protectedRoot) => {
    if (isSameWorkspacePath(resolved, protectedRoot)) return true;
    const blocksChildren = descendantRootKeys.has(workspacePathKey(protectedRoot));
    return blocksChildren && isPathInside(protectedRoot, resolved);
  });
}

function settingsPath() {
  return path.join(app.getPath('userData'), SETTINGS_FILE_NAME);
}

function authSessionPath() {
  return path.join(app.getPath('userData'), AUTH_SESSION_FILE_NAME);
}

function authIdentityPath() {
  return path.join(app.getPath('userData'), AUTH_IDENTITY_FILE_NAME);
}

function authUsersPath() {
  return path.join(app.getPath('userData'), AUTH_USERS_FILE_NAME);
}

function enterpriseAdminJournalPath() {
  return path.join(app.getPath('userData'), ENTERPRISE_ADMIN_JOURNAL_FILE_NAME);
}

function secretsPath() {
  return path.join(app.getPath('userData'), SECRETS_FILE_NAME);
}

function modelProfilesPath() {
  return path.join(app.getPath('userData'), MODEL_PROFILES_FILE_NAME);
}

function sessionTranscriptDir() {
  return path.join(app.getPath('userData'), SESSION_TRANSCRIPT_DIR_NAME);
}

function runJournalPath() {
  return path.join(app.getPath('userData'), RUN_JOURNAL_FILE_NAME);
}

function crashSummaryPath() {
  return path.join(app.getPath('userData'), CRASH_SUMMARY_FILE_NAME);
}

function telemetryQueuePath() {
  return path.join(app.getPath('userData'), TELEMETRY_QUEUE_FILE_NAME);
}

function atomicWriteJson(file, payload) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tempFile = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tempFile, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  fs.renameSync(tempFile, file);
}

function normalizeUserRole(value, fallback = 'user') {
  const role = String(value || fallback || 'user').trim().toLowerCase().replace(/[-\s]+/g, '_');
  return USER_ROLES.has(role) ? role : fallback;
}

function normalizeUserDisplayName(value, email = '') {
  const text = String(value || '').trim().slice(0, 80);
  if (text) return text;
  const local = String(email || '').split('@')[0] || 'EcoreX User';
  return local.slice(0, 80);
}

function stableUserId(email = '') {
  return crypto.createHash('sha256').update(`ecorex-user/v1:${String(email || '').toLowerCase()}`).digest('hex').slice(0, 24);
}

function normalizeAuthUser(raw = {}, index = 0) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const email = normalizeLoginEmail(raw.email);
  const role = normalizeUserRole(raw.role, index === 0 ? 'super_admin' : 'user');
  const active = raw.active !== false;
  const user = {
    id: String(raw.id || stableUserId(email)).slice(0, 80),
    email,
    displayName: normalizeUserDisplayName(raw.displayName || raw.name, email),
    title: String(raw.title || '').trim().slice(0, 120),
    team: String(raw.team || '').trim().slice(0, 120),
    avatarInitials: String(raw.avatarInitials || '').trim().slice(0, 4),
    role,
    active,
    passwordHash: String(raw.passwordHash || ''),
    salt: String(raw.salt || ''),
    iterations: Number(raw.iterations) || LOCAL_AUTH_HASH_ITERATIONS,
    digest: raw.digest || 'sha256',
    createdAt: raw.createdAt || new Date().toISOString(),
    updatedAt: raw.updatedAt || raw.createdAt || new Date().toISOString()
  };
  if (!/^[a-f0-9]{64}$/i.test(user.passwordHash) || !/^[a-f0-9]{32,}$/i.test(user.salt)) return null;
  return user;
}

function publicAuthUser(user = {}) {
  if (!user?.email) return null;
  const permissions = USER_ROLE_PERMISSIONS[normalizeUserRole(user.role)] || USER_ROLE_PERMISSIONS.user;
  return {
    id: user.id,
    email: user.email,
    displayName: user.displayName || normalizeUserDisplayName('', user.email),
    name: user.displayName || normalizeUserDisplayName('', user.email),
    title: user.title || '',
    team: user.team || '',
    avatarInitials: user.avatarInitials || '',
    role: normalizeUserRole(user.role),
    roleLabel: userRoleLabel(user.role),
    permissions,
    active: user.active !== false,
    createdAt: user.createdAt || null,
    updatedAt: user.updatedAt || null
  };
}

function userRoleLabel(role = 'user') {
  const normalized = normalizeUserRole(role);
  if (normalized === 'super_admin') return '超级管理员';
  if (normalized === 'admin') return '管理员';
  return '成员';
}

function authUsersEnvelope(users = []) {
  return {
    version: 1,
    users: users.map((user, index) => normalizeAuthUser(user, index)).filter(Boolean)
  };
}

function writeAuthUsers(users = []) {
  const envelope = authUsersEnvelope(users);
  atomicWriteJson(authUsersPath(), {
    version: 1,
    encoding: 'safeStorage/v1',
    data: encryptLocalPayload(envelope),
    updatedAt: new Date().toISOString()
  });
  return envelope.users;
}

function authUsersUnavailableError(error) {
  const wrapped = new Error('Local auth users could not be read.');
  wrapped.code = 'E_AUTH_USERS_UNREADABLE';
  if (error) wrapped.cause = error;
  return wrapped;
}

function publicAuthUsersUnavailableSession(options = {}) {
  const summary = {
    ok: true,
    loggedIn: false,
    setupRequired: false,
    authMode: 'local-owner',
    authUnavailable: true,
    authLocked: true,
    error: 'Local auth users could not be read.'
  };
  if (options.includeToken) summary.token = '';
  return summary;
}

function legacyOwnerAsUser(identity = readAuthIdentity()) {
  if (!identity?.email) return null;
  return normalizeAuthUser({
    id: stableUserId(identity.email),
    email: identity.email,
    displayName: identity.displayName || identity.email.split('@')[0],
    title: '本机超级管理员',
    team: 'EcoreX',
    role: 'super_admin',
    active: true,
    passwordHash: identity.passwordHash,
    salt: identity.salt,
    iterations: identity.iterations,
    digest: identity.digest,
    createdAt: identity.createdAt,
    updatedAt: identity.updatedAt
  });
}

function readAuthUsers(options = {}) {
  try {
    const file = authUsersPath();
    if (!fs.existsSync(file)) {
      const legacyIdentityExists = fs.existsSync(authIdentityPath());
      const legacyUser = legacyOwnerAsUser();
      if (legacyUser && options.migrate !== false) return writeAuthUsers([legacyUser]);
      if (legacyIdentityExists) throw new Error('Legacy auth identity could not be read.');
      return [];
    }
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const envelope = raw?.encoding === 'safeStorage/v1' ? decryptLocalPayload(raw.data) : raw;
    const rawUsers = Array.isArray(envelope?.users) ? envelope.users : Array.isArray(envelope) ? envelope : null;
    if (!rawUsers) throw new Error('Invalid auth users file.');
    const users = rawUsers
      .map((user, index) => normalizeAuthUser(user, index))
      .filter(Boolean);
    if (!users.length) throw new Error('Auth users file contains no valid users.');
    if (users.length && raw?.encoding !== 'safeStorage/v1' && options.migrate !== false) {
      return writeAuthUsers(users);
    }
    return users;
  } catch (error) {
    writeLog('error', 'Failed to read auth users; local sign-in locked', { error: error?.message });
    throw authUsersUnavailableError(error);
  }
}

function findAuthUserByEmail(email) {
  const normalizedEmail = normalizeLoginEmail(email);
  return readAuthUsers().find((user) => user.email === normalizedEmail) || null;
}

function createUserRecord({ email, password, role = 'user', displayName = '', title = '', team = '' }) {
  const normalizedEmail = normalizeLoginEmail(email);
  const normalizedPassword = normalizeLocalPassword(password, { forSetup: true });
  const now = new Date().toISOString();
  const salt = crypto.randomBytes(16).toString('hex');
  return normalizeAuthUser({
    id: stableUserId(normalizedEmail),
    email: normalizedEmail,
    displayName: normalizeUserDisplayName(displayName, normalizedEmail),
    title,
    team,
    role: normalizeUserRole(role),
    active: true,
    salt,
    passwordHash: hashLocalPassword(normalizedPassword, salt),
    iterations: LOCAL_AUTH_HASH_ITERATIONS,
    digest: 'sha256',
    createdAt: now,
    updatedAt: now
  });
}

function authContextFromArgs(args = []) {
  const session = readAuthSession({ refresh: true });
  const token = extractAuthToken(args);
  if (!session?.token || !token || token.length !== session.token.length) return null;
  const expected = Buffer.from(session.token, 'utf8');
  const provided = Buffer.from(token, 'utf8');
  if (provided.length !== expected.length || !crypto.timingSafeEqual(expected, provided)) return null;
  const user = findAuthUserByEmail(session.email);
  if (!user || user.active === false) return null;
  return {
    session,
    user,
    publicUser: publicAuthUser(user),
    permissions: USER_ROLE_PERMISSIONS[normalizeUserRole(user.role)] || USER_ROLE_PERMISSIONS.user
  };
}

function authContextHasPermission(authContext, permission) {
  if (!permission) return true;
  return (authContext?.permissions || []).includes(permission);
}

function forbiddenResponse(permission = '') {
  return {
    ok: false,
    forbidden: true,
    error: permission ? `Permission required: ${permission}` : 'Forbidden'
  };
}

function publicAuthSession(session, options = {}) {
  let users = [];
  try {
    users = readAuthUsers({ migrate: true });
  } catch {
    return publicAuthUsersUnavailableSession(options);
  }
  const setupRequired = !users.length;
  if (!session?.token) {
    return {
      ok: true,
      loggedIn: false,
      setupRequired,
      authMode: 'local-owner'
    };
  }
  const user = users.find((item) => item.email === session.email) || null;
  const publicUser = publicAuthUser(user || { email: session.email, role: 'user', active: true });
  const summary = {
    ok: true,
    loggedIn: Boolean(user?.active !== false),
    email: session.email,
    user: publicUser,
    role: publicUser?.role || 'user',
    permissions: publicUser?.permissions || USER_ROLE_PERMISSIONS.user,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    expiresAt: session.expiresAt,
    setupRequired,
    authMode: 'local-owner'
  };
  const owner = users.find((item) => normalizeUserRole(item.role) === 'super_admin') || users[0];
  if (owner?.email) summary.ownerEmail = owner.email;
  if (options.includeToken) summary.token = session.token;
  return summary;
}

function authSessionExpiresAt(now = new Date()) {
  return new Date(now.getTime() + AUTH_SESSION_TTL_MS).toISOString();
}

function isAuthSessionExpired(session, nowMs = Date.now()) {
  if (!session?.expiresAt) return false;
  const expiresAt = Date.parse(session.expiresAt);
  return !Number.isFinite(expiresAt) || expiresAt <= nowMs;
}

function refreshAuthSessionIfNeeded(session, options = {}) {
  if (!session?.token) return null;
  const nowMs = Date.now();
  if (isAuthSessionExpired(session, nowMs)) {
    clearAuthSession();
    writeLog('info', 'Auth session expired', { email: session.email, expiresAt: session.expiresAt });
    return null;
  }
  const expiresAt = Date.parse(session.expiresAt || '');
  if (options.refresh && (!Number.isFinite(expiresAt) || expiresAt - nowMs <= AUTH_SESSION_REFRESH_THRESHOLD_MS)) {
    return writeAuthSession({
      ...session,
      updatedAt: new Date(nowMs).toISOString(),
      expiresAt: authSessionExpiresAt(new Date(nowMs))
    });
  }
  return session;
}

function validateAuthSession(raw) {
  if (
    raw &&
    typeof raw.email === 'string' &&
    typeof raw.token === 'string' &&
    /^[a-f0-9]{64}$/i.test(raw.token)
  ) {
    return {
      email: raw.email,
      token: raw.token,
      createdAt: raw.createdAt || null,
      updatedAt: raw.updatedAt || raw.createdAt || null,
      expiresAt: raw.expiresAt || null
    };
  }
  return null;
}

function validateAuthIdentity(raw) {
  if (
    raw &&
    typeof raw.email === 'string' &&
    typeof raw.passwordHash === 'string' &&
    typeof raw.salt === 'string' &&
    /^[a-f0-9]{64}$/i.test(raw.passwordHash) &&
    /^[a-f0-9]{32,}$/i.test(raw.salt)
  ) {
    return {
      version: 1,
      email: raw.email,
      passwordHash: raw.passwordHash,
      salt: raw.salt,
      iterations: Number(raw.iterations) || LOCAL_AUTH_HASH_ITERATIONS,
      digest: raw.digest || 'sha256',
      createdAt: raw.createdAt || null,
      updatedAt: raw.updatedAt || raw.createdAt || null
    };
  }
  return null;
}

function encryptLocalPayload(payload) {
  const mode = secretStorageMode();
  if (!mode.canEncrypt) {
    throw new Error('Secure local storage is unavailable.');
  }
  return safeStorage.encryptString(JSON.stringify(payload)).toString('base64');
}

function decryptLocalPayload(data) {
  if (!data || typeof data !== 'string') return null;
  const decrypted = safeStorage.decryptString(Buffer.from(data, 'base64'));
  return JSON.parse(decrypted);
}

function readAuthIdentity() {
  try {
    const file = authIdentityPath();
    if (!fs.existsSync(file)) return null;
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (raw?.encoding === 'safeStorage/v1') {
      return validateAuthIdentity(decryptLocalPayload(raw.data));
    }
    const legacyIdentity = validateAuthIdentity(raw);
    if (legacyIdentity) {
      try {
        writeAuthIdentity(legacyIdentity);
      } catch {
        clearAuthSession();
        return null;
      }
      return legacyIdentity;
    }
  } catch {
    // Invalid identity state is treated as a setup-required state.
  }
  return null;
}

function writeAuthIdentity(identity) {
  const safeIdentity = validateAuthIdentity(identity);
  if (!safeIdentity) throw new Error('Invalid auth identity.');
  atomicWriteJson(authIdentityPath(), {
    version: 1,
    encoding: 'safeStorage/v1',
    data: encryptLocalPayload(safeIdentity),
    updatedAt: safeIdentity.updatedAt || new Date().toISOString()
  });
  return safeIdentity;
}

function readAuthSession(options = {}) {
  try {
    const file = authSessionPath();
    if (!fs.existsSync(file)) return null;
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (raw?.encoding === 'safeStorage/v1') {
      const session = validateAuthSession(decryptLocalPayload(raw.data));
      const user = session?.email ? findAuthUserByEmail(session.email) : null;
      if (!user || user.active === false) {
        clearAuthSession();
        return null;
      }
      return refreshAuthSessionIfNeeded(session, options);
    }
    const legacySession = validateAuthSession(raw);
    if (legacySession) {
      const user = findAuthUserByEmail(legacySession.email);
      if (!user || user.active === false) {
        clearAuthSession();
        return null;
      }
      try {
        return refreshAuthSessionIfNeeded(writeAuthSession(legacySession), options);
      } catch {
        clearAuthSession();
        return null;
      }
    }
  } catch {
    // Invalid auth state is treated as logged out.
  }
  return null;
}

function writeAuthSession(session) {
  const safeSession = validateAuthSession(session);
  if (!safeSession) throw new Error('Invalid auth session.');
  const now = new Date();
  const nextSession = {
    ...safeSession,
    updatedAt: safeSession.updatedAt || now.toISOString(),
    expiresAt: safeSession.expiresAt || authSessionExpiresAt(now)
  };
  atomicWriteJson(authSessionPath(), {
    version: 1,
    encoding: 'safeStorage/v1',
    data: encryptLocalPayload(nextSession),
    updatedAt: nextSession.updatedAt
  });
  return nextSession;
}

function clearAuthSession() {
  try {
    const file = authSessionPath();
    if (fs.existsSync(file)) fs.unlinkSync(file);
  } catch (error) {
    writeLog('warn', 'Failed to clear auth session', { error: error?.message });
  }
}

function normalizeLoginEmail(value) {
  const email = String(value || '').trim().toLowerCase();
  if (!email || email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    throw new Error('A valid enterprise email is required.');
  }
  return email;
}

function normalizeLocalPassword(value, options = {}) {
  const password = String(value || '');
  if (options.forSetup && password.length < LOCAL_AUTH_MIN_PASSWORD_CHARS) {
    throw new Error(`Password must be at least ${LOCAL_AUTH_MIN_PASSWORD_CHARS} characters for first-time setup.`);
  }
  if (!password) {
    throw new Error('Password is required.');
  }
  return password;
}

function hashLocalPassword(password, salt, iterations = LOCAL_AUTH_HASH_ITERATIONS, digest = 'sha256') {
  return crypto.pbkdf2Sync(password, salt, iterations, 32, digest).toString('hex');
}

function createAuthIdentity(email, password) {
  const now = new Date().toISOString();
  const salt = crypto.randomBytes(16).toString('hex');
  return writeAuthIdentity({
    email,
    salt,
    passwordHash: hashLocalPassword(password, salt),
    iterations: LOCAL_AUTH_HASH_ITERATIONS,
    digest: 'sha256',
    createdAt: now,
    updatedAt: now
  });
}

function verifyLocalPassword(identity, password) {
  if (!identity?.passwordHash || !identity?.salt) return false;
  const hash = hashLocalPassword(password, identity.salt, identity.iterations, identity.digest);
  const expected = Buffer.from(identity.passwordHash, 'hex');
  const provided = Buffer.from(hash, 'hex');
  if (expected.length !== provided.length) return false;
  return crypto.timingSafeEqual(expected, provided);
}

function loginAuth(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Invalid login payload.');
  }
  const email = normalizeLoginEmail(payload.email);
  const loginType = String(payload.loginType || (payload.code ? 'code' : 'password')).toLowerCase();
  if (loginType === 'code' || payload.code) {
    throw new Error('Verification-code login requires enterprise SSO. Use password login for local desktop access.');
  }

  let users = readAuthUsers({ migrate: true });
  const password = normalizeLocalPassword(payload.password, { forSetup: !users.length });
  let user = users.find((item) => item.email === email) || null;
  let createdIdentity = false;

  if (!users.length) {
    user = createUserRecord({
      email,
      password,
      role: 'super_admin',
      displayName: payload.displayName || email.split('@')[0],
      title: '本机超级管理员',
      team: 'EcoreX'
    });
    users = writeAuthUsers([user]);
    try {
      createAuthIdentity(email, password);
    } catch {
      // The encrypted users store is now authoritative; legacy owner storage is best-effort.
    }
    createdIdentity = true;
  } else if (!user || user.active === false || !verifyLocalPassword(user, password)) {
    writeLog('warn', 'Local auth login rejected', { email, reason: !user ? 'email-mismatch' : user.active === false ? 'inactive-user' : 'password-mismatch' });
    throw new Error('Invalid email or password.');
  }

  const now = new Date().toISOString();
  const session = writeAuthSession({
    email: user.email,
    token: crypto.randomBytes(32).toString('hex'),
    createdAt: now,
    updatedAt: now
  });
  writeLog('info', createdIdentity ? 'Local auth super admin bound' : 'Auth session created', { email: user.email, role: user.role });
  return publicAuthSession(session, { includeToken: true });
}

function logoutAuth() {
  const session = readAuthSession();
  clearAuthSession();
  if (session?.email) writeLog('info', 'Auth session cleared', { email: session.email });
  return { ok: true, loggedIn: false };
}

function listAuthUsers(_payload = {}, authContext = null) {
  const canManageUsers = authContextHasPermission(authContext, 'users:manage');
  const allUsers = readAuthUsers({ migrate: true });
  const visibleUsers = canManageUsers
    ? allUsers
    : allUsers.filter((user) => user.email === authContext?.user?.email);
  const users = visibleUsers.map(publicAuthUser).filter(Boolean);
  return {
    ok: true,
    users,
    currentUser: authContext?.publicUser || null,
    roles: Object.keys(USER_ROLE_PERMISSIONS).map((role) => ({
      value: role,
      label: userRoleLabel(role),
      permissions: USER_ROLE_PERMISSIONS[role]
    })),
    canManageUsers,
    canManageEnterprise: authContextHasPermission(authContext, 'enterprise:manage')
  };
}

function createAuthUser(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'user payload');
  if (!authContextHasPermission(authContext, 'users:manage')) return forbiddenResponse('users:manage');
  const users = readAuthUsers({ migrate: true });
  const email = normalizeLoginEmail(payload.email);
  if (users.some((user) => user.email === email)) throw new Error('User already exists.');
  const user = createUserRecord({
    email,
    password: payload.password,
    role: payload.role || 'user',
    displayName: payload.displayName || payload.name,
    title: payload.title,
    team: payload.team
  });
  const nextUsers = writeAuthUsers([...users, user]);
  writeLog('info', 'Auth user created', { actor: authContext.user.email, email: user.email, role: user.role });
  return { ok: true, user: publicAuthUser(user), users: nextUsers.map(publicAuthUser).filter(Boolean) };
}

function updateAuthUser(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'user payload');
  const users = readAuthUsers({ migrate: true });
  const id = String(payload.id || '').trim();
  const email = payload.email ? normalizeLoginEmail(payload.email) : '';
  const index = users.findIndex((user) => user.id === id || (email && user.email === email));
  if (index < 0) throw new Error('User not found.');
  const target = users[index];
  const isSelf = target.email === authContext?.user?.email;
  const canManage = authContextHasPermission(authContext, 'users:manage');
  if (!canManage && !isSelf) return forbiddenResponse('users:manage');
  const nextUser = { ...target };
  for (const field of ['displayName', 'title', 'team', 'avatarInitials']) {
    if (Object.prototype.hasOwnProperty.call(payload, field)) nextUser[field] = String(payload[field] || '').trim().slice(0, field === 'avatarInitials' ? 4 : 120);
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'name')) {
    nextUser.displayName = normalizeUserDisplayName(payload.name, target.email);
  }
  if (canManage && Object.prototype.hasOwnProperty.call(payload, 'role')) {
    nextUser.role = normalizeUserRole(payload.role, target.role);
  }
  if (canManage && Object.prototype.hasOwnProperty.call(payload, 'active')) {
    nextUser.active = payload.active !== false;
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'password') && String(payload.password || '').trim()) {
    if (!canManage && !verifyLocalPassword(target, normalizeLocalPassword(payload.currentPassword))) {
      throw new Error('Current password is invalid.');
    }
    const salt = crypto.randomBytes(16).toString('hex');
    nextUser.salt = salt;
    nextUser.passwordHash = hashLocalPassword(normalizeLocalPassword(payload.password, { forSetup: true }), salt);
    nextUser.iterations = LOCAL_AUTH_HASH_ITERATIONS;
    nextUser.digest = 'sha256';
  }
  nextUser.displayName = normalizeUserDisplayName(nextUser.displayName, target.email);
  nextUser.updatedAt = new Date().toISOString();
  const nextUsers = [...users];
  nextUsers[index] = normalizeAuthUser(nextUser, index);
  if (!nextUsers.some((user) => user.active !== false && normalizeUserRole(user.role) === 'super_admin')) {
    throw new Error('At least one active super administrator is required.');
  }
  writeAuthUsers(nextUsers);
  writeLog('info', 'Auth user updated', { actor: authContext.user.email, email: target.email, role: nextUser.role });
  return {
    ok: true,
    user: publicAuthUser(nextUsers[index]),
    users: nextUsers.map(publicAuthUser).filter(Boolean),
    auth: publicAuthSession(readAuthSession(), { includeToken: false })
  };
}

function deleteAuthUser(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'user payload');
  if (!authContextHasPermission(authContext, 'users:manage')) return forbiddenResponse('users:manage');
  const users = readAuthUsers({ migrate: true });
  const id = String(payload.id || '').trim();
  const email = payload.email ? normalizeLoginEmail(payload.email) : '';
  const target = users.find((user) => user.id === id || (email && user.email === email));
  if (!target) throw new Error('User not found.');
  if (target.email === authContext?.user?.email) throw new Error('You cannot delete the current signed-in user.');
  const nextUsers = users.filter((user) => user.id !== target.id);
  if (!nextUsers.some((user) => user.active !== false && normalizeUserRole(user.role) === 'super_admin')) {
    throw new Error('At least one active super administrator is required.');
  }
  writeAuthUsers(nextUsers);
  writeLog('warn', 'Auth user deleted', { actor: authContext.user.email, email: target.email });
  return { ok: true, deletedId: target.id, users: nextUsers.map(publicAuthUser).filter(Boolean) };
}

function updateOwnProfile(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'profile payload');
  return updateAuthUser({ ...payload, id: authContext?.user?.id }, authContext);
}

function appendEnterpriseAdminJournal(entry = {}) {
  const line = JSON.stringify({
    ...entry,
    at: new Date().toISOString()
  });
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.appendFileSync(enterpriseAdminJournalPath(), `${line}\n`, 'utf8');
}

function runEnterpriseAdminAction(payload = {}, authContext = null) {
  optionalObjectPayload(payload, 'enterprise action payload');
  if (!authContextHasPermission(authContext, 'enterprise:manage')) return forbiddenResponse('enterprise:manage');
  const action = String(payload.action || '').trim();
  if (!ENTERPRISE_ADMIN_ACTIONS.has(action)) throw new Error('Unsupported enterprise action.');
  const operationId = crypto.randomUUID();
  const summary = safeOutputText(payload.summary || payload.name || action, 240);
  const skillSourcePaths = [
    ...(Array.isArray(payload.sourcePaths) ? payload.sourcePaths : []),
    ...(payload.sourcePath ? [payload.sourcePath] : []),
    ...(payload.path ? [payload.path] : [])
  ].map((item) => String(item || '').trim()).filter(Boolean);
  appendEnterpriseAdminJournal({
    operationId,
    action,
    actor: authContext.user.email,
    summary,
    payload: redactForLog(payload)
  });
  let refresh = null;
  let pushedSkills = [];
  try {
    if (action === 'syncMcp') {
      refresh = collectMcpStatus({ refresh: true });
    } else if (action === 'pushSkill') {
      pushedSkills = skillSourcePaths.map((sourcePath) => installManagedSkillPack({
        sourcePath,
        enabled: payload.enabled !== false,
        category: payload.category
      }, authContext).installed);
      refresh = collectSkillStatus({ refresh: true });
    }
  } catch (error) {
    refresh = {
      ok: false,
      error: safeOutputText(error instanceof Error ? error.message : String(error), 1000)
    };
  }
  writeLog('info', 'Enterprise admin action accepted', { operationId, action, actor: authContext.user.email });
  return {
    ok: true,
    operation: {
      id: operationId,
      action,
      status: refresh?.ok === false ? 'accepted' : 'queued',
      summary,
      createdAt: new Date().toISOString(),
      actor: authContext.publicUser
    },
    installedSkills: pushedSkills,
    refresh: refresh ? safeJsonValue(refresh, 12000) : null
  };
}

function extractAuthToken(args = []) {
  for (const arg of args) {
    if (!arg || typeof arg !== 'object' || Array.isArray(arg)) continue;
    const token = arg.authToken || arg.sessionToken || arg.token;
    if (typeof token === 'string' && token.trim()) return token.trim();
  }
  return '';
}

function isAuthorized(args = []) {
  return Boolean(authContextFromArgs(args));
}

function unauthorizedResponse() {
  return { ok: false, error: 'Unauthorized' };
}

function optionalObjectPayload(payload, label = 'payload') {
  if (payload === undefined || payload === null) return {};
  if (typeof payload !== 'object' || Array.isArray(payload)) throw new Error(`Invalid ${label}.`);
  return payload;
}

function secretStorageMode() {
  let encryptionAvailable = false;
  let canEncrypt = false;
  let selectedBackend = null;
  try {
    encryptionAvailable = Boolean(safeStorage?.isEncryptionAvailable?.());
    selectedBackend = safeStorage?.getSelectedStorageBackend?.() || null;
    if (encryptionAvailable) {
      const encrypted = safeStorage.encryptString('ecorex-safe-storage-check');
      canEncrypt = Buffer.isBuffer(encrypted);
    }
  } catch {
    canEncrypt = false;
  }
  return {
    provider: canEncrypt ? 'safeStorage' : 'unavailable',
    encryptionAvailable,
    selectedBackend,
    canEncrypt,
    insecure: !canEncrypt
  };
}

function sanitizeSecretKey(value) {
  const key = String(value || '').trim().toUpperCase();
  if (!SECRET_KEY_ALLOWLIST.has(key)) throw new Error('Unsupported secret key.');
  return key;
}

function normalizeSecretValue(value) {
  if (typeof value !== 'string') throw new Error('Secret value must be a string.');
  const secret = value.trim();
  if (!secret || secret.includes('\0') || secret.length > MAX_SECRET_VALUE_CHARS) {
    throw new Error('Invalid secret value.');
  }
  return secret;
}

function maskSecret(value = '') {
  const text = String(value || '').trim();
  if (!text) return '';
  if (text.length <= 8) return '****';
  return `${text.slice(0, 4)}...${text.slice(-4)}`;
}

function readSecretsFile() {
  const fallback = { version: 1, insecure: secretStorageMode().insecure, items: {}, exists: false };
  try {
    const file = secretsPath();
    if (!fs.existsSync(file)) return fallback;
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 1024 * 1024) return fallback;
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const items = {};
    if (raw?.items && typeof raw.items === 'object' && !Array.isArray(raw.items)) {
      for (const [rawKey, rawItem] of Object.entries(raw.items)) {
        const key = String(rawKey || '').trim().toUpperCase();
        if (!SECRET_KEY_ALLOWLIST.has(key) || !rawItem || typeof rawItem !== 'object') continue;
        const encoding = rawItem.encoding === 'safeStorage/v1' ? 'safeStorage/v1' : 'plain/v1';
        const data = typeof rawItem.data === 'string' ? rawItem.data : '';
        if (!data || data.length > MAX_SECRET_VALUE_CHARS * 2) continue;
        items[key] = {
          key,
          encoding,
          data,
          masked: typeof rawItem.masked === 'string' ? rawItem.masked.slice(0, 64) : '',
          createdAt: typeof rawItem.createdAt === 'string' ? rawItem.createdAt : null,
          updatedAt: typeof rawItem.updatedAt === 'string' ? rawItem.updatedAt : null
        };
      }
    }
    return {
      version: 1,
      insecure: Boolean(raw?.insecure),
      updatedAt: typeof raw?.updatedAt === 'string' ? raw.updatedAt : null,
      items,
      exists: true
    };
  } catch {
    return fallback;
  }
}

function writeSecretsFile(store = {}) {
  const mode = secretStorageMode();
  const items = {};
  for (const [rawKey, rawItem] of Object.entries(store.items || {})) {
    const key = String(rawKey || '').trim().toUpperCase();
    if (!SECRET_KEY_ALLOWLIST.has(key) || !rawItem || typeof rawItem !== 'object') continue;
    const encoding = rawItem.encoding === 'safeStorage/v1' ? 'safeStorage/v1' : null;
    if (!encoding) continue;
    const data = typeof rawItem.data === 'string' ? rawItem.data : '';
    if (!data) continue;
    items[key] = {
      key,
      encoding,
      data,
      masked: typeof rawItem.masked === 'string' ? rawItem.masked.slice(0, 64) : '',
      createdAt: rawItem.createdAt || new Date().toISOString(),
      updatedAt: rawItem.updatedAt || rawItem.createdAt || new Date().toISOString()
    };
  }

  const hasPlaintext = Object.values(items).some((item) => item.encoding === 'plain/v1');
  atomicWriteJson(secretsPath(), {
    version: 1,
    insecure: hasPlaintext || !mode.canEncrypt,
    updatedAt: new Date().toISOString(),
    items
  });
  secretRedactionCache = { loadedAt: 0, values: [] };
}

function encryptSecretValue(value) {
  const mode = secretStorageMode();
  if (mode.canEncrypt) {
    try {
      return {
        encoding: 'safeStorage/v1',
        data: safeStorage.encryptString(value).toString('base64'),
        insecure: false
      };
    } catch {
      // Fail closed. Secrets must not be persisted without OS-backed encryption.
    }
  }
  throw new Error('Secure storage is unavailable. Configure the OS credential store before saving secrets.');
}

function decryptSecretValue(item = {}) {
  if (!item?.data) return '';
  if (item.encoding === 'safeStorage/v1') {
    return safeStorage.decryptString(Buffer.from(item.data, 'base64'));
  }
  return '';
}

function readStoredSecrets(options = {}) {
  const store = readSecretsFile();
  return Object.entries(store.items)
    .map(([key, item]) => {
      let value = '';
      try {
        value = decryptSecretValue(item);
      } catch {
        value = '';
      }
      const record = {
        key,
        createdAt: item.createdAt || item.updatedAt || null,
        updatedAt: item.updatedAt || item.createdAt || null,
        masked: item.masked || maskSecret(value) || '****',
        configured: item.encoding === 'safeStorage/v1' && Boolean(value),
        insecure: item.encoding !== 'safeStorage/v1',
        status: item.encoding === 'safeStorage/v1' && value ? 'configured' : 'invalid'
      };
      if (options.includeValues && value) record.value = value;
      return record;
    })
    .sort((a, b) => a.key.localeCompare(b.key));
}

function readAgentSecretsForEnv() {
  const env = {};
  for (const secret of readStoredSecrets({ includeValues: true })) {
    if (SECRET_KEY_ALLOWLIST.has(secret.key) && secret.configured && secret.value) env[secret.key] = secret.value;
  }
  return env;
}

function secretsStatus() {
  const mode = secretStorageMode();
  const store = readSecretsFile();
  const items = Object.values(store.items);
  const plaintextCount = items.filter((item) => item.encoding === 'plain/v1').length;
  return {
    ok: true,
    supportedKeys: Array.from(SECRET_KEY_ALLOWLIST),
    provider: mode.provider,
    encryptionAvailable: mode.canEncrypt,
    safeStorageAvailable: mode.encryptionAvailable,
    selectedBackend: mode.selectedBackend,
    insecure: plaintextCount > 0 || !mode.canEncrypt,
    store: {
      exists: Boolean(store.exists),
      pathLabel: `userData:/${SECRETS_FILE_NAME}`,
      insecure: Boolean(store.insecure || plaintextCount > 0 || !mode.canEncrypt),
      secretCount: items.length,
      plaintextCount,
      encryptedCount: items.length - plaintextCount,
      updatedAt: store.updatedAt || null
    }
  };
}

function evaluationReportPath() {
  return path.join(app.getPath('userData'), EVALUATION_REPORT_FILE_NAME);
}

function readLastEvaluationReport() {
  try {
    const file = evaluationReportPath();
    if (!fs.existsSync(file)) return null;
    const report = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!report || typeof report !== 'object') return null;
    return {
      id: safeOutputText(report.id || '', 80),
      version: safeOutputText(report.version || '', 80),
      mode: safeOutputText(report.mode || '', 80),
      startedAt: report.startedAt || null,
      finishedAt: report.finishedAt || null,
      durationMs: Number(report.durationMs) || 0,
      aggregate: report.aggregate || null,
      sampleCount: Array.isArray(report.results) ? report.results.length : 0,
      memoryPolicy: report.memoryPolicy || ''
    };
  } catch (error) {
    writeLog('warn', 'Failed to read evaluation report', {
      error: error instanceof Error ? error.message : String(error)
    });
    return null;
  }
}

function listEvaluationStatus() {
  return {
    ...listEvaluationFramework(),
    lastReport: readLastEvaluationReport(),
    reportPathLabel: `userData:/${EVALUATION_REPORT_FILE_NAME}`
  };
}

function runEvaluationStatus(payload = {}) {
  optionalObjectPayload(payload, 'evaluation payload');
  const report = runEvaluationFramework(payload);
  atomicWriteJson(evaluationReportPath(), report);
  writeLog('info', 'Evaluation framework report generated', {
    id: report.id,
    mode: report.mode,
    sampleCount: EVALUATION_SAMPLE_COUNT,
    durationMs: report.durationMs
  });
  return {
    ...report,
    reportPathLabel: `userData:/${EVALUATION_REPORT_FILE_NAME}`
  };
}

function listSecrets(payload = {}) {
  optionalObjectPayload(payload, 'secret payload');
  return {
    ok: true,
    secrets: readStoredSecrets(),
    status: secretsStatus()
  };
}

function setSecret(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Invalid secret payload.');
  }
  const key = sanitizeSecretKey(payload.key);
  const value = normalizeSecretValue(payload.value);
  const store = readSecretsFile();
  const now = new Date().toISOString();
  const encrypted = encryptSecretValue(value);
  store.items[key] = {
    key,
    encoding: encrypted.encoding,
    data: encrypted.data,
    masked: maskSecret(value),
    createdAt: store.items[key]?.createdAt || now,
    updatedAt: now
  };
  writeSecretsFile(store);
  writeLog('info', 'Secret stored', { key, insecure: encrypted.insecure });
  return {
    ok: true,
    secret: readStoredSecrets().find((secret) => secret.key === key),
    status: secretsStatus()
  };
}

function deleteSecret(payload = {}) {
  const key = sanitizeSecretKey(typeof payload === 'string' ? payload : payload?.key);
  const store = readSecretsFile();
  const existed = Boolean(store.items[key]);
  delete store.items[key];
  writeSecretsFile(store);
  if (existed) writeLog('info', 'Secret deleted', { key });
  return {
    ok: true,
    deleted: existed,
    key,
    status: secretsStatus()
  };
}

function sanitizeModelProfileName(value) {
  const name = String(value || '').trim();
  if (
    !name ||
    name.length > 80 ||
    /[\0\r\n]/.test(name) ||
    /[\\/:*?"<>|]/.test(name)
  ) {
    throw new Error('Invalid profile name.');
  }
  return name;
}

function normalizeModelProfileText(value, label, options = {}) {
  const text = String(value || '').trim();
  const maxLength = options.maxLength || MAX_MODEL_PROFILE_TEXT_CHARS;
  if (!text && options.required !== false) throw new Error(`Invalid ${label}.`);
  if (text.includes('\0') || text.length > maxLength) throw new Error(`Invalid ${label}.`);
  return text;
}

function normalizeModelName(value, fallback = 'sonnet') {
  const candidate = String(value || '').trim();
  const fallbackText = String(fallback || 'sonnet').trim() || 'sonnet';
  const text = candidate || fallbackText;
  if (
    !text ||
    text.length > 160 ||
    text.includes('\0') ||
    /[\r\n]/.test(text) ||
    !/^[a-zA-Z0-9_.:/@+-]+$/.test(text)
  ) {
    return fallbackText;
  }
  return text;
}

function normalizeTelemetryEndpoint(value = '') {
  const raw = String(value || '').trim();
  if (!raw) return '';
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('Invalid telemetryEndpoint.');
  }
  if (url.protocol !== 'https:') throw new Error('telemetryEndpoint must use HTTPS.');
  if (url.username || url.password || url.hash) throw new Error('Invalid telemetryEndpoint.');
  url.search = '';
  return url.toString();
}

function normalizeTelemetryInstallId(value = '') {
  const raw = String(value || '').trim();
  if (/^[a-zA-Z0-9_-]{12,80}$/.test(raw)) return raw;
  return publicStableId('install', `${app.getPath('userData')}:${app.getName()}`);
}

function hasOwnValue(input = {}, key) {
  return Object.prototype.hasOwnProperty.call(input, key);
}

function firstModelProfileValue(input = {}, keys = []) {
  for (const key of keys) {
    if (hasOwnValue(input, key)) return input[key];
  }
  return undefined;
}

function normalizeModelBaseUrl(value) {
  const raw = normalizeModelProfileText(value, 'baseUrl');
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('Invalid baseUrl.');
  }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Invalid baseUrl.');
  if (url.username || url.password || url.hash) throw new Error('Invalid baseUrl.');
  url.search = '';
  return url.toString().replace(/\/+$/, '');
}

function isPrivateModelHost(hostname = '') {
  const host = String(hostname || '').trim().toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!host) return true;
  if (host === 'localhost' || host.endsWith('.localhost') || host === 'host.docker.internal') return true;
  if (host === '::' || host === '::1' || host.startsWith('fe80:') || /^f[cd][0-9a-f]*:/i.test(host)) return true;
  const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!ipv4) return false;
  const octets = ipv4.slice(1).map(Number);
  if (octets.some((part) => part < 0 || part > 255)) return true;
  const [first, second] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    first === 169 && second === 254 ||
    first === 172 && second >= 16 && second <= 31 ||
    first === 192 && second === 168
  );
}

function hasPrivateModelBaseUrlConfirmation(input = {}) {
  return (
    input?.allowPrivateBaseUrl === true ||
    input?.confirmPrivateBaseUrl === true ||
    input?.privateBaseUrlConfirmed === true
  );
}

function normalizeModelBaseUrlForUse(value, input = {}, options = {}) {
  const baseUrl = normalizeModelBaseUrl(value);
  const parsed = new URL(baseUrl);
  if (isPrivateModelHost(parsed.hostname) && options.allowStoredPrivateBaseUrl !== true && !hasPrivateModelBaseUrlConfirmation(input)) {
    throw new Error('Private or local model baseUrl requires explicit confirmation.');
  }
  return baseUrl;
}

function readModelProfilesFile() {
  const fallback = { version: 1, profiles: [], activeProfileName: null, exists: false };
  try {
    const file = modelProfilesPath();
    if (!fs.existsSync(file)) return fallback;
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 1024 * 1024) return fallback;
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const rawProfiles = Array.isArray(raw?.profiles) ? raw.profiles : [];
    const profiles = [];
    const seen = new Set();
    for (const rawProfile of rawProfiles.slice(0, MAX_MODEL_PROFILES)) {
      if (!rawProfile || typeof rawProfile !== 'object' || Array.isArray(rawProfile)) continue;
      let name;
      try {
        name = sanitizeModelProfileName(rawProfile.name);
      } catch {
        continue;
      }
      if (seen.has(name)) continue;
      seen.add(name);
      let baseUrl = '';
      try {
        baseUrl = normalizeModelBaseUrl(rawProfile.baseUrl);
      } catch {
        continue;
      }
      const apiKeyItem =
        rawProfile.apiKey &&
        typeof rawProfile.apiKey === 'object' &&
        rawProfile.apiKey.encoding === 'safeStorage/v1' &&
        typeof rawProfile.apiKey.data === 'string'
          ? {
              encoding: 'safeStorage/v1',
              data: rawProfile.apiKey.data,
              masked: typeof rawProfile.apiKey.masked === 'string' ? rawProfile.apiKey.masked.slice(0, 64) : '',
              createdAt: typeof rawProfile.apiKey.createdAt === 'string' ? rawProfile.apiKey.createdAt : null,
              updatedAt: typeof rawProfile.apiKey.updatedAt === 'string' ? rawProfile.apiKey.updatedAt : null
            }
          : null;
      profiles.push({
        name,
        label: normalizeModelProfileText(rawProfile.label || name, 'label', { maxLength: 120 }),
        baseUrl,
        apiKey: apiKeyItem,
        model: normalizeModelProfileText(rawProfile.model, 'model', { maxLength: 160 }),
        imageModel: normalizeImageModelName(normalizeModelProfileText(rawProfile.imageModel || '', 'imageModel', {
          required: false,
          maxLength: 160
        })),
        isActive: Boolean(rawProfile.isActive),
        createdAt: typeof rawProfile.createdAt === 'string' ? rawProfile.createdAt : null,
        updatedAt: typeof rawProfile.updatedAt === 'string' ? rawProfile.updatedAt : null
      });
    }
    const activeProfileName =
      profiles.find((profile) => profile.name === raw?.activeProfileName)?.name ||
      profiles.find((profile) => profile.isActive)?.name ||
      null;
    return { version: 1, profiles, activeProfileName, exists: true };
  } catch {
    return fallback;
  }
}

function writeModelProfilesFile(store = {}) {
  const now = new Date().toISOString();
  const profiles = [];
  const seen = new Set();
  for (const profile of (store.profiles || []).slice(0, MAX_MODEL_PROFILES)) {
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) continue;
    const name = sanitizeModelProfileName(profile.name);
    if (seen.has(name)) continue;
    seen.add(name);
    profiles.push({
      name,
      label: normalizeModelProfileText(profile.label || name, 'label', { maxLength: 120 }),
      baseUrl: normalizeModelBaseUrl(profile.baseUrl),
      apiKey: profile.apiKey || null,
      model: normalizeModelProfileText(profile.model, 'model', { maxLength: 160 }),
      imageModel: normalizeImageModelName(normalizeModelProfileText(profile.imageModel || DEFAULT_IMAGE_MODEL, 'imageModel', {
        required: false,
        maxLength: 160
      })),
      isActive: Boolean(profile.isActive),
      createdAt: profile.createdAt || now,
      updatedAt: profile.updatedAt || now
    });
  }
  let activeProfileName = store.activeProfileName && profiles.find((profile) => profile.name === store.activeProfileName)
    ? store.activeProfileName
    : null;
  if (!activeProfileName) activeProfileName = profiles.find((profile) => profile.isActive)?.name || null;
  for (const profile of profiles) {
    profile.isActive = profile.name === activeProfileName;
  }
  atomicWriteJson(modelProfilesPath(), {
    version: 1,
    updatedAt: now,
    activeProfileName,
    profiles
  });
  secretRedactionCache = { loadedAt: 0, values: [] };
  return { version: 1, profiles, activeProfileName, exists: true };
}

function decryptModelProfileApiKey(profile = {}) {
  try {
    return profile.apiKey ? decryptSecretValue(profile.apiKey) : '';
  } catch {
    return '';
  }
}

function publicModelProfile(profile = {}, options = {}) {
  const value = options.includeApiKey ? decryptModelProfileApiKey(profile) : '';
  const configured = Boolean(decryptModelProfileApiKey(profile));
  const result = {
    id: profile.name,
    profileId: profile.name,
    name: profile.name,
    label: profile.label || profile.name,
    baseUrl: profile.baseUrl,
    apiKey: '',
    apiKeyConfigured: configured,
    apiKeyMasked: profile.apiKey?.masked || (configured ? maskSecret(value) : ''),
    model: profile.model,
    modelName: profile.model,
    imageModel: profile.imageModel ? normalizeImageModelName(profile.imageModel) : '',
    imageModelName: profile.imageModel ? normalizeImageModelName(profile.imageModel) : '',
    isActive: Boolean(profile.isActive),
    active: Boolean(profile.isActive),
    current: Boolean(profile.isActive),
    createdAt: profile.createdAt || null,
    updatedAt: profile.updatedAt || null
  };
  if (options.includeApiKey) result.apiKey = value;
  return result;
}

function readStoredModelProfiles(options = {}) {
  return readModelProfilesFile().profiles.map((profile) => publicModelProfile(profile, options));
}

function activeModelProfile() {
  try {
    const store = readModelProfilesFile();
    return store.profiles.find((profile) => profile.isActive)
      || (store.activeProfileName ? store.profiles.find((profile) => profile.name === store.activeProfileName) : null)
      || store.profiles[0]
      || null;
  } catch {
    return null;
  }
}

function shouldPreferActiveModelProfile(value = '') {
  const normalized = String(value || '').trim().toLowerCase();
  return !normalized || normalized === 'sonnet' || normalized === 'opus';
}

function resolveAgentModelName(payloadModel, settings = {}) {
  const activeProfile = activeModelProfile();
  const activeModel = activeProfile?.model ? normalizeModelName(activeProfile.model, '') : '';
  const requested = normalizeModelName(payloadModel, settings.defaultModel || activeModel || 'sonnet');
  if (activeModel && (shouldPreferActiveModelProfile(payloadModel) || shouldPreferActiveModelProfile(requested))) {
    return activeModel;
  }
  return requested || activeModel || 'sonnet';
}

function modelCapabilityOptions() {
  const options = new Map(BUILTIN_MODEL_OPTIONS.map((model) => [model.value, model]));
  try {
    for (const profile of readModelProfilesFile().profiles) {
      if (!profile.model) continue;
      options.set(profile.model, {
        value: profile.model,
        label: profile.label ? `${profile.label} · ${profile.model}` : profile.model,
        profileName: profile.name,
        imageModel: profile.imageModel ? normalizeImageModelName(profile.imageModel) : '',
        isActive: Boolean(profile.isActive)
      });
    }
  } catch {
    // Capabilities should still be available when model profile storage is unreadable.
  }
  return Array.from(options.values());
}

function modelProfileEnvForModel(model) {
  try {
    const normalizedModel = normalizeModelName(model, '');
    if (!normalizedModel) return {};
    const store = readModelProfilesFile();
    const profile = store.profiles.find((item) => item.model === normalizedModel) || null;
    if (!profile) return {};
    const apiKey = decryptModelProfileApiKey(profile);
    const env = {
      ECOREX_ACTIVE_MODEL: profile.model,
      ECOREX_MODEL_PROFILE: profile.name,
      ECOREX_IMAGE_MODEL: normalizeImageModelName(profile.imageModel || DEFAULT_IMAGE_MODEL)
    };
    if (profile.baseUrl) {
      env.ANTHROPIC_BASE_URL = profile.baseUrl;
      env.OPENAI_BASE_URL = profile.baseUrl;
    }
    if (apiKey) {
      env.ANTHROPIC_API_KEY = apiKey;
      env.OPENAI_API_KEY = apiKey;
    }
    return env;
  } catch {
    return {};
  }
}

function listModelProfiles(payload = {}) {
  optionalObjectPayload(payload, 'model profile payload');
  const store = readModelProfilesFile();
  const profiles = store.profiles.map((profile) => publicModelProfile(profile));
  return {
    ok: true,
    profiles,
    activeProfileName: store.activeProfileName,
    activeProfile: profiles.find((profile) => profile.isActive) || null,
    storage: {
      pathLabel: `userData:/${MODEL_PROFILES_FILE_NAME}`,
      encryptedApiKeys: store.profiles.filter((profile) => profile.apiKey?.encoding === 'safeStorage/v1').length
    }
  };
}

function saveModelProfile(payload = {}) {
  const input = optionalObjectPayload(payload?.profile && typeof payload.profile === 'object' ? payload.profile : payload, 'model profile payload');
  const store = readModelProfilesFile();
  const now = new Date().toISOString();
  const rawName =
    firstModelProfileValue(input, ['name', 'id', 'profileId', 'label']) ||
    firstModelProfileValue(input, ['model', 'modelName']);
  const name = sanitizeModelProfileName(rawName);
  const existingIndex = store.profiles.findIndex((profile) => profile.name === name);
  const existing = existingIndex >= 0 ? store.profiles[existingIndex] : null;
  const hasApiKey = hasOwnValue(input, 'apiKey');
  const modelValue = firstModelProfileValue(input, ['model', 'modelName']);
  const imageModelValue = firstModelProfileValue(input, ['imageModel', 'imageModelName']);
  const profile = {
    name,
    label: hasOwnValue(input, 'label')
      ? normalizeModelProfileText(input.label || name, 'label', { maxLength: 120 })
      : existing?.label || name,
    baseUrl: hasOwnValue(input, 'baseUrl') ? normalizeModelBaseUrlForUse(input.baseUrl, input) : existing?.baseUrl,
    apiKey: existing?.apiKey || null,
    model: modelValue !== undefined
      ? normalizeModelProfileText(modelValue, 'model', { maxLength: 160 })
      : existing?.model,
    imageModel: imageModelValue !== undefined
      ? normalizeImageModelName(normalizeModelProfileText(imageModelValue || DEFAULT_IMAGE_MODEL, 'imageModel', { required: false, maxLength: 160 }))
      : normalizeImageModelName(existing?.imageModel || DEFAULT_IMAGE_MODEL),
    isActive: Boolean(existing?.isActive),
    createdAt: existing?.createdAt || now,
    updatedAt: now
  };
  if (!profile.baseUrl) throw new Error('Invalid baseUrl.');
  if (!profile.model) throw new Error('Invalid model.');
  if (hasApiKey) {
    const apiKey = String(input.apiKey || '').trim();
    if (apiKey) {
      const encrypted = encryptSecretValue(normalizeSecretValue(apiKey));
      profile.apiKey = {
        encoding: encrypted.encoding,
        data: encrypted.data,
        masked: maskSecret(apiKey),
        createdAt: existing?.apiKey?.createdAt || now,
        updatedAt: now
      };
    } else {
      profile.apiKey = null;
    }
  }
  if (existingIndex >= 0) {
    store.profiles[existingIndex] = profile;
  } else {
    if (store.profiles.length >= MAX_MODEL_PROFILES) throw new Error('Too many model profiles.');
    store.profiles.push(profile);
  }
  if (input.isActive === true || !store.activeProfileName) store.activeProfileName = name;
  const nextStore = writeModelProfilesFile(store);
  cachedCapabilities = null;
  const saved = nextStore.profiles.find((item) => item.name === name);
  writeLog('info', 'Model profile saved', { name, isActive: saved?.isActive });
  return { ...listModelProfiles(), profile: saved ? publicModelProfile(saved) : null };
}

function deleteModelProfile(payload = {}) {
  const name = sanitizeModelProfileName(
    typeof payload === 'string'
      ? payload
      : firstModelProfileValue(payload, ['name', 'id', 'profileId'])
  );
  const store = readModelProfilesFile();
  const nextProfiles = store.profiles.filter((profile) => profile.name !== name);
  const deleted = nextProfiles.length !== store.profiles.length;
  store.profiles = nextProfiles;
  if (store.activeProfileName === name) store.activeProfileName = nextProfiles[0]?.name || null;
  writeModelProfilesFile(store);
  cachedCapabilities = null;
  if (deleted) writeLog('info', 'Model profile deleted', { name });
  return { ...listModelProfiles(), deleted, name };
}

function activateModelProfile(payload = {}) {
  const name = sanitizeModelProfileName(
    typeof payload === 'string'
      ? payload
      : firstModelProfileValue(payload, ['name', 'id', 'profileId'])
  );
  const store = readModelProfilesFile();
  if (!store.profiles.find((profile) => profile.name === name)) throw new Error('Model profile not found.');
  store.activeProfileName = name;
  writeModelProfilesFile(store);
  cachedCapabilities = null;
  writeLog('info', 'Model profile activated', { name });
  return { ...listModelProfiles(), activeProfileName: name };
}

function modelProfileFromTestPayload(payload = {}) {
  const input = optionalObjectPayload(payload?.profile && typeof payload.profile === 'object' ? payload.profile : payload, 'model profile payload');
  const store = readModelProfilesFile();
  const rawName = firstModelProfileValue(input, ['name', 'id', 'profileId']);
  const name = rawName ? sanitizeModelProfileName(rawName) : store.activeProfileName;
  const stored = name ? store.profiles.find((profile) => profile.name === name) : null;
  if (!stored && !input.baseUrl) throw new Error('Model profile not found.');
  const apiKeyFromPayload = Object.prototype.hasOwnProperty.call(input, 'apiKey') ? String(input.apiKey || '').trim() : null;
  const hasPayloadApiKey = apiKeyFromPayload !== null;
  const hasPayloadBaseUrl = hasOwnValue(input, 'baseUrl');
  const payloadBaseUrl = hasPayloadBaseUrl ? normalizeModelBaseUrlForUse(input.baseUrl, input) : '';
  if (stored && hasPayloadBaseUrl && !hasPayloadApiKey && payloadBaseUrl !== stored.baseUrl) {
    throw new Error('Saved model credentials cannot be tested against a temporary baseUrl. Enter an API key or edit the saved profile.');
  }
  const modelValue = firstModelProfileValue(input, ['model', 'modelName']);
  const imageModelValue = firstModelProfileValue(input, ['imageModel', 'imageModelName']);
  return {
    name: stored?.name || name || null,
    baseUrl: hasPayloadBaseUrl ? payloadBaseUrl : stored?.baseUrl,
    apiKey: hasPayloadApiKey ? apiKeyFromPayload : decryptModelProfileApiKey(stored),
    model: modelValue !== undefined
      ? normalizeModelProfileText(modelValue, 'model', { maxLength: 160 })
      : stored?.model,
    imageModel: imageModelValue !== undefined
      ? normalizeImageModelName(normalizeModelProfileText(imageModelValue || DEFAULT_IMAGE_MODEL, 'imageModel', { required: false, maxLength: 160 }))
      : normalizeImageModelName(stored?.imageModel || DEFAULT_IMAGE_MODEL)
  };
}

async function postModelProfileTest(baseUrl, apiKey, endpointPath, body, timeoutMs) {
  const adapter = createModelAdapter({
    timeoutMs,
    retries: 1,
    maxResponseBytes: 64 * 1024,
    redactSecrets: knownSecretRedactionValues()
  });
  const type = String(endpointPath || '').includes('responses') ? 'responses' : 'chat';
  const result = await adapter.request(
    { baseUrl, apiKey, model: body?.model },
    { type, endpoint: endpointPath, body },
    { timeoutMs, type, endpoint: endpointPath }
  );
  return {
    ok: Boolean(result.ok),
    status: result.statusCode,
    latency: result.totalLatencyMs || result.latencyMs,
    model: result.model || body?.model || null,
    attempts: result.attempts,
    responseTruncated: Boolean(result.responseTruncated),
    error: result.ok ? null : safeOutputText(result.error?.message || 'Request failed.', 1000)
  };
}

async function testModelProfile(payload = {}) {
  const profile = modelProfileFromTestPayload(payload);
  if (!profile.baseUrl) throw new Error('Invalid baseUrl.');
  if (!profile.model) throw new Error('Invalid model.');
  const timeoutMs = Math.min(Math.max(Number(payload?.timeoutMs) || 15000, 1000), MODEL_PROFILE_TEST_TIMEOUT_MS);
  const startedAt = Date.now();
  const chatResult = await postModelProfileTest(
    profile.baseUrl,
    profile.apiKey,
    '/chat/completions',
    {
      model: profile.model,
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 1,
      temperature: 0
    },
    timeoutMs
  );
  let result = { ...chatResult, endpoint: 'chat/completions' };
  if (
    !chatResult.ok &&
    ([404, 405].includes(chatResult.status) ||
      (chatResult.status === 400 && /(unsupported|not supported|not compatible|responses)/i.test(chatResult.error || '')))
  ) {
    const remainingTimeout = Math.max(1000, timeoutMs - (Date.now() - startedAt));
    const responsesResult = await postModelProfileTest(
      profile.baseUrl,
      profile.apiKey,
      '/responses',
      {
        model: profile.model,
        input: 'ping',
        max_output_tokens: 1
      },
      remainingTimeout
    );
    result = { ...responsesResult, endpoint: 'responses' };
  }
  return {
    ok: Boolean(result.ok),
    status: result.ok ? 'connected' : 'error',
    statusLabel: result.ok ? '连接正常' : '连接失败',
    statusCode: result.status,
    latency: Date.now() - startedAt,
    latencyMs: Date.now() - startedAt,
    model: result.model || profile.model,
    imageModel: normalizeImageModelName(profile.imageModel || DEFAULT_IMAGE_MODEL),
    imageModelName: normalizeImageModelName(profile.imageModel || DEFAULT_IMAGE_MODEL),
    endpoint: result.endpoint,
    attempts: result.attempts,
    fallbackUsed: result.endpoint === 'responses',
    responseTruncated: Boolean(result.responseTruncated),
    profileName: profile.name,
    error: result.ok ? null : result.error
  };
}

function imageBodyFromPayload(payload = {}, profile = {}) {
  const input = optionalObjectPayload(payload?.body && typeof payload.body === 'object' ? payload.body : payload, 'image generation payload');
  const prompt = normalizeModelProfileText(input.prompt, 'prompt', { maxLength: 32000 });
  const allowedKeys = new Set([
    'background',
    'moderation',
    'n',
    'output_compression',
    'output_format',
    'quality',
    'response_format',
    'size',
    'style',
    'user'
  ]);
  const body = {
    model: normalizeImageModelName(normalizeModelProfileText(input.model || profile.imageModel || DEFAULT_IMAGE_MODEL, 'imageModel', {
      maxLength: 160
    })),
    prompt
  };
  for (const key of allowedKeys) {
    if (hasOwnValue(input, key)) body[key] = input[key];
  }
  return body;
}

async function generateModelProfileImage(payload = {}) {
  const profile = modelProfileFromTestPayload(payload);
  if (!profile.baseUrl) throw new Error('Invalid baseUrl.');
  const timeoutMs = Math.min(Math.max(Number(payload?.timeoutMs) || 30000, 1000), IMAGE_GENERATION_TIMEOUT_MS);
  const adapter = createModelAdapter({
    timeoutMs,
    retries: Math.min(Math.max(Number(payload?.retries ?? payload?.retryCount) || 0, 0), 3),
    maxResponseBytes: Math.min(Math.max(Number(payload?.maxResponseBytes) || 2 * 1024 * 1024, 1024), 4 * 1024 * 1024),
    redactSecrets: knownSecretRedactionValues()
  });
  const result = await adapter.generateImage(profile, imageBodyFromPayload(payload, profile), { timeoutMs });
  return {
    ok: Boolean(result.ok),
    status: result.ok ? 'connected' : 'error',
    statusCode: result.statusCode,
    latencyMs: result.totalLatencyMs || result.latencyMs,
    endpoint: String(result.endpoint || '').replace(/^\/+/, ''),
    attempts: result.attempts,
    model: normalizeImageModelName(result.model || profile.imageModel || DEFAULT_IMAGE_MODEL),
    profileName: profile.name,
    responseBytes: result.responseBytes,
    responseTruncated: Boolean(result.responseTruncated),
    data: result.ok ? result.data : null,
    error: result.ok ? null : safeOutputText(result.error?.message || 'Request failed.', 1000)
  };
}

function clampPromptChars(value) {
  const chars = Number(value);
  if (!Number.isFinite(chars)) return MAX_PROMPT_CHARS;
  return Math.min(Math.max(Math.floor(chars), MIN_PROMPT_CHARS), MAX_PROMPT_CHARS);
}

function normalizeWorkspaceRoot(value, options = {}) {
  const fallback = defaultWorkspaceRoot();
  const raw = typeof value === 'string' && value.trim() ? value.trim() : fallback;
  const resolved = path.resolve(raw);
  if (resolved.length > 500) {
    if (options.fallbackOnInvalid) return fallback;
    throw new Error('Invalid workspaceRoot.');
  }
  if (isSameWorkspacePath(resolved, fallback)) return fallback;
  if (isProtectedWorkspaceRoot(resolved)) {
    if (options.fallbackOnInvalid) return fallback;
    throw new Error('Workspace root cannot be a disk root, home folder, app data folder, or protected system directory.');
  }
  if (options.allowCustomWorkspaceRoot !== true) {
    if (options.fallbackOnInvalid) return fallback;
    throw new Error('Custom workspaceRoot requires explicit confirmation.');
  }
  return resolved;
}

function safeWorkspaceDialogDefaultPath(value) {
  try {
    const candidate = normalizeWorkspaceRoot(value, {
      allowCustomWorkspaceRoot: true,
      fallbackOnInvalid: true
    });
    return fs.existsSync(candidate) && fs.statSync(candidate).isDirectory() ? candidate : defaultWorkspaceRoot();
  } catch {
    return defaultWorkspaceRoot();
  }
}

function normalizePermissionMode(value, fallback = 'default') {
  const raw = String(value || '').trim();
  const normalized = PERMISSION_MODE_ALIASES.get(raw);
  if (normalized && PERMISSION_POLICIES[normalized]) return normalized;
  const fallbackMode = PERMISSION_MODE_ALIASES.get(String(fallback || '').trim());
  return fallbackMode && PERMISSION_POLICIES[fallbackMode] ? fallbackMode : 'default';
}

function sanitizePermissionMode(value, label = 'permissionMode') {
  const raw = String(value || '').trim();
  if (!ALLOWED_PERMISSION_MODES.has(raw)) throw new Error(`Invalid ${label}.`);
  const normalized = PERMISSION_MODE_ALIASES.get(raw);
  if (!normalized || !PERMISSION_POLICIES[normalized]) {
    throw new Error(`Invalid ${label}.`);
  }
  if (!PUBLIC_PERMISSION_MODES.includes(normalized)) {
    throw new Error(`Unsupported ${label}.`);
  }
  return normalized;
}

function permissionPolicyFor(mode) {
  const normalized = normalizePermissionMode(mode, 'default');
  return PERMISSION_POLICIES[normalized] || PERMISSION_POLICIES.default;
}

function publicPermissionPolicy(mode, options = {}) {
  const policy = permissionPolicyFor(mode);
  const exposed = {
    value: policy.value,
    accessMode: policy.accessMode,
    mode: policy.mode,
    permissionMode: policy.permissionMode,
    label: policy.label,
    description: publicProductText(policy.description),
    fullAccess: policy.fullAccess,
    requiresConfirmation: Boolean(policy.requiresConfirmation)
  };
  if (options.includeBackend === true) {
    exposed.cliMode = policy.cliMode;
    exposed.cliFlags = Array.isArray(policy.cliFlags) ? [...policy.cliFlags] : [];
  }
  return exposed;
}

function publicPermissionPolicies() {
  return PUBLIC_PERMISSION_MODES.map((mode) => publicPermissionPolicy(mode));
}

function hasFullAccessConfirmation(payload = {}) {
  return (
    payload?.fullAccessConfirmed === true ||
    payload?.confirmFullAccess === true ||
    payload?.fullAccessConfirmation === FULL_ACCESS_PERMISSION_MODE
  );
}

function normalizeSettings(raw = {}) {
  const normalizedAccessMode = normalizePermissionMode(raw.accessMode ?? raw.defaultPermissionMode ?? raw.permissionMode, 'default');
  const accessMode = PUBLIC_PERMISSION_MODES.includes(normalizedAccessMode) ? normalizedAccessMode : 'default';
  const workspaceRoot = normalizeWorkspaceRoot(raw.workspaceRoot, {
    allowCustomWorkspaceRoot: hasCustomWorkspaceRootConfirmation(raw),
    fallbackOnInvalid: true
  });
  const customWorkspaceRootConfirmed = !isSameWorkspacePath(workspaceRoot, defaultWorkspaceRoot());
  return {
    defaultModel: resolveAgentModelName(raw.defaultModel, {}),
    accessMode,
    permissionMode: accessMode,
    defaultPermissionMode: accessMode,
    workspaceRoot,
    customWorkspaceRootConfirmed,
    maxPromptChars: clampPromptChars(raw.maxPromptChars),
    autoRefreshBackend: typeof raw.autoRefreshBackend === 'boolean' ? raw.autoRefreshBackend : true,
    anonymousTelemetryEnabled: raw.anonymousTelemetryEnabled === true,
    telemetryEndpoint: normalizeTelemetryEndpoint(raw.telemetryEndpoint || ''),
    telemetryInstallId: normalizeTelemetryInstallId(raw.telemetryInstallId)
  };
}

function readSettings() {
  try {
    const file = settingsPath();
    const raw = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
    return normalizeSettings(raw);
  } catch {
    return normalizeSettings();
  }
}

function writeSettings(nextSettings) {
  const settings = normalizeSettings(nextSettings);
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.mkdirSync(settings.workspaceRoot, { recursive: true });
  const file = settingsPath();
  const tempFile = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tempFile, `${JSON.stringify(settings, null, 2)}\n`, 'utf8');
  fs.renameSync(tempFile, file);
  return settings;
}

function defaultAgentCwd() {
  const workspace = generalAgentWorkspaceDir();
  fs.mkdirSync(workspace, { recursive: true });
  return workspace;
}

function updateSettings(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Invalid settings payload.');
  }
  const current = readSettings();
  const next = { ...current };

  if (Object.prototype.hasOwnProperty.call(payload, 'defaultModel')) {
    next.defaultModel = normalizeModelName(payload.defaultModel, current.defaultModel);
  }
  const hasAccessMode = Object.prototype.hasOwnProperty.call(payload, 'accessMode');
  const hasPermissionMode = Object.prototype.hasOwnProperty.call(payload, 'permissionMode');
  const hasDefaultPermissionMode = Object.prototype.hasOwnProperty.call(payload, 'defaultPermissionMode');
  if (hasAccessMode || hasPermissionMode || hasDefaultPermissionMode) {
    const inputs = [
      ['accessMode', hasAccessMode, payload.accessMode],
      ['permissionMode', hasPermissionMode, payload.permissionMode],
      ['defaultPermissionMode', hasDefaultPermissionMode, payload.defaultPermissionMode]
    ].filter(([, present]) => present);
    const requestedMode = sanitizePermissionMode(inputs[0][2], inputs[0][0]);
    for (const [label, , value] of inputs.slice(1)) {
      if (sanitizePermissionMode(value, label) !== requestedMode) {
        throw new Error('Conflicting permissionMode values.');
      }
    }
    if (requestedMode === FULL_ACCESS_PERMISSION_MODE && !hasFullAccessConfirmation(payload)) {
      throw new Error('Full access permission requires explicit confirmation.');
    }
    next.accessMode = requestedMode;
    next.permissionMode = requestedMode;
    next.defaultPermissionMode = requestedMode;
    if (requestedMode === FULL_ACCESS_PERMISSION_MODE) {
      writeLog('warn', 'Full access permission saved as default');
    }
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'workspaceRoot')) {
    if (typeof payload.workspaceRoot !== 'string') {
      throw new Error('Invalid workspaceRoot.');
    }
    if (!payload.workspaceRoot.trim()) {
      next.workspaceRoot = defaultWorkspaceRoot();
    } else {
      next.workspaceRoot = normalizeWorkspaceRoot(payload.workspaceRoot, {
        allowCustomWorkspaceRoot: hasCustomWorkspaceRootConfirmation(payload)
      });
    }
    next.customWorkspaceRootConfirmed = !isSameWorkspacePath(next.workspaceRoot, defaultWorkspaceRoot());
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'maxPromptChars')) {
    next.maxPromptChars = clampPromptChars(payload.maxPromptChars);
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'autoRefreshBackend')) {
    if (typeof payload.autoRefreshBackend !== 'boolean') throw new Error('Invalid autoRefreshBackend.');
    next.autoRefreshBackend = payload.autoRefreshBackend;
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'anonymousTelemetryEnabled')) {
    if (typeof payload.anonymousTelemetryEnabled !== 'boolean') throw new Error('Invalid anonymousTelemetryEnabled.');
    next.anonymousTelemetryEnabled = payload.anonymousTelemetryEnabled;
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'telemetryEndpoint')) {
    next.telemetryEndpoint = normalizeTelemetryEndpoint(payload.telemetryEndpoint || '');
  }

  return writeSettings(next);
}

function sanitizeWorkspaceRelativePath(value = '') {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (raw.includes('\0') || path.isAbsolute(raw) || raw.length > 240) {
    throw new Error('Invalid workspace path.');
  }
  const normalized = path.normalize(raw);
  if (normalized === '..' || normalized.startsWith(`..${path.sep}`) || path.isAbsolute(normalized)) {
    throw new Error('Workspace path escapes root.');
  }
  return normalized === '.' ? '' : normalized;
}

function resolveWorkspacePath(relativePath = '') {
  const workspaceRoot = readSettings().workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const safeRelative = sanitizeWorkspaceRelativePath(relativePath);
  const target = path.resolve(workspaceRoot, safeRelative);
  if (!isPathInside(workspaceRoot, target)) throw new Error('Workspace path escapes root.');
  if (pathContainsSymlink(workspaceRoot, target)) throw new Error('Workspace path crosses a symbolic link.');
  return { workspaceRoot, target, relativePath: safeRelative };
}

function listWorkspace(payload = {}) {
  const { workspaceRoot, target, relativePath } = resolveWorkspacePath(payload?.relativePath);
  const entries = fs.existsSync(target)
    ? fs
        .readdirSync(target, { withFileTypes: true })
        .slice(0, 200)
        .map((entry) => {
          const absolute = path.join(target, entry.name);
          const linkStat = fs.lstatSync(absolute);
          if (linkStat.isSymbolicLink()) return null;
          const stat = fs.statSync(absolute);
          if (!isPathInside(workspaceRoot, absolute)) return null;
          return {
            name: entry.name,
            path: path.relative(workspaceRoot, absolute).replace(/\\/g, '/'),
            type: entry.isDirectory() ? 'directory' : 'file',
            size: entry.isFile() ? stat.size : 0,
            modifiedAt: stat.mtime.toISOString()
          };
        })
        .filter(Boolean)
    : [];
  return { ok: true, workspace: { pathLabel: 'workspace:/' }, relativePath: relativePath.replace(/\\/g, '/'), entries };
}

function ensureWorkspace(payload = {}) {
  if (typeof payload?.workspaceRoot === 'string' && payload.workspaceRoot.trim()) {
    const current = readSettings();
    const allowCurrentRoot = isSameWorkspacePath(payload.workspaceRoot, current.workspaceRoot);
    const requestedRoot = normalizeWorkspaceRoot(payload.workspaceRoot, {
      allowCustomWorkspaceRoot: allowCurrentRoot || hasCustomWorkspaceRootConfirmation(payload)
    });
    if (!isSameWorkspacePath(requestedRoot, current.workspaceRoot)) {
      updateSettings({
        workspaceRoot: payload.workspaceRoot,
        confirmCustomWorkspaceRoot: hasCustomWorkspaceRootConfirmation(payload)
      });
    }
  }
  const { workspaceRoot, target, relativePath } = resolveWorkspacePath(payload?.relativePath);
  fs.mkdirSync(target, { recursive: true });
  const stat = fs.statSync(target);
  if (!stat.isDirectory()) throw new Error('Workspace path is not a directory.');
  return {
    ok: true,
    workspace: { pathLabel: 'workspace:/' },
    pathLabel: publicWorkspacePathLabel(workspaceRoot, target),
    relativePath: relativePath.replace(/\\/g, '/'),
    exists: true
  };
}

function projectStatePath(workspaceRoot) {
  return path.join(workspaceRoot, PROJECT_STATE_FILE_NAME);
}

function sanitizeProjectDisplayName(value, label = 'project name') {
  const name = String(value || '').trim().replace(/\s+/g, ' ');
  if (!name || name.length > 80 || /[\0\r\n\\/]/.test(name)) {
    throw new Error(`Invalid ${label}.`);
  }
  return name;
}

function safeProjectDisplayName(value, fallback) {
  try {
    return sanitizeProjectDisplayName(value || fallback);
  } catch {
    return String(fallback || 'Project').slice(0, 80);
  }
}

function sanitizeProjectText(value, label = 'project field', maxLength = 240) {
  const text = String(value || '').trim().replace(/[\0\r\n]+/g, ' ').replace(/\s+/g, ' ');
  if (text.length > maxLength) throw new Error(`Invalid ${label}.`);
  return text;
}

function sanitizeProjectStatus(value = 'active') {
  const normalized = String(value || 'active').trim().toLowerCase();
  const aliases = {
    current: 'active',
    running: 'active',
    enabled: 'active',
    stopped: 'paused',
    pause: 'paused',
    done: 'completed',
    complete: 'completed',
    finished: 'completed',
    inactive: 'archived',
    disabled: 'archived'
  };
  const status = aliases[normalized] || normalized;
  if (!ALLOWED_PROJECT_STATUSES.has(status)) throw new Error('Invalid project status.');
  return status;
}

function sanitizeProjectDeliverables(value) {
  const items = Array.isArray(value)
    ? value
    : String(value || '')
        .split(/[\n,，;；、]+/);
  return items
    .map((item) => sanitizeProjectText(item, 'project deliverable', 80))
    .filter(Boolean)
    .slice(0, 12);
}

function normalizeProjectBusinessFields(raw = {}) {
  const industry = sanitizeProjectText(raw.industry || raw.vertical || '', 'project industry', 80);
  const scenario = sanitizeProjectText(raw.scenario || raw.scene || raw.channel || '', 'project scenario', 80);
  const fields = {
    status: sanitizeProjectStatus(raw.status || raw.state || (raw.archived ? 'archived' : 'active')),
    client: sanitizeProjectText(raw.client || raw.customer || raw.brand || '', 'project client', 100),
    goal: sanitizeProjectText(raw.goal || raw.objective || raw.target || '', 'project goal', 220),
    industry,
    scenario,
    budget: sanitizeProjectText(raw.budget || raw.spend || '', 'project budget', 80),
    period: sanitizeProjectText(raw.period || raw.timeline || raw.cycle || '', 'project period', 100),
    deliverables: sanitizeProjectDeliverables(raw.deliverables || raw.outputs || raw.assets),
    description: sanitizeProjectText(raw.description || raw.summary || raw.note || '', 'project description', 260),
    instructions: sanitizeProjectText(raw.instructions || raw.systemInstructions || raw.projectInstructions || '', 'project instructions', 5000)
  };
  if (!fields.description) {
    fields.description = [fields.client, fields.goal, fields.industry || fields.scenario].filter(Boolean).join(' · ');
  }
  return fields;
}

function sanitizeProjectId(value) {
  const id = String(value || '').trim();
  if (!/^[a-zA-Z0-9_-]{8,80}$/.test(id)) throw new Error('Invalid project id.');
  return id;
}

function projectSlugForName(name) {
  const slug = String(name || '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9\s._-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 60);
  return slug || `project-${crypto.createHash('sha1').update(String(name || 'project')).digest('hex').slice(0, 8)}`;
}

function uniqueProjectDir(workspaceRoot, baseSlug) {
  const safeBase = projectSlugForName(baseSlug);
  for (let index = 0; index < 100; index += 1) {
    const dirName = index === 0 ? safeBase : `${safeBase}-${index + 1}`;
    const projectPath = path.resolve(workspaceRoot, dirName);
    if (!isPathInside(workspaceRoot, projectPath)) throw new Error('Project path escapes workspace.');
    if (!fs.existsSync(projectPath)) return { dirName, projectPath };
  }
  throw new Error('Unable to allocate project directory.');
}

function uniqueProjectRenameDir(workspaceRoot, project, nextName) {
  const currentPath = path.resolve(project.projectPath);
  const safeBase = projectSlugForName(nextName);
  for (let index = 0; index < 100; index += 1) {
    const dirName = index === 0 ? safeBase : `${safeBase}-${index + 1}`;
    const projectPath = path.resolve(workspaceRoot, dirName);
    if (!isPathInside(workspaceRoot, projectPath)) throw new Error('Project path escapes workspace.');
    if (isSameWorkspacePath(projectPath, currentPath)) return { dirName, projectPath, changed: false };
    if (!fs.existsSync(projectPath)) return { dirName, projectPath, changed: true };
  }
  throw new Error('Unable to allocate project directory.');
}

function publicWorkspacePathLabel(workspaceRoot, target) {
  const resolved = path.resolve(target || workspaceRoot);
  if (!isPathInside(workspaceRoot, resolved)) return 'workspace:/';
  const relative = path.relative(workspaceRoot, resolved).replace(/\\/g, '/');
  return relative ? `workspace:/${relative}` : 'workspace:/';
}

function readWorkspaceProjectState(workspaceRoot) {
  try {
    const file = projectStatePath(workspaceRoot);
    if (!fs.existsSync(file)) return { activeProjectId: null };
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 64 * 1024) return { activeProjectId: null };
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    return {
      activeProjectId:
        typeof raw?.activeProjectId === 'string' && /^[a-zA-Z0-9_-]{8,80}$/.test(raw.activeProjectId)
          ? raw.activeProjectId
          : null,
      updatedAt: typeof raw?.updatedAt === 'string' ? raw.updatedAt : null
    };
  } catch {
    return { activeProjectId: null };
  }
}

function writeWorkspaceProjectState(workspaceRoot, state = {}) {
  const file = projectStatePath(workspaceRoot);
  if (!isPathInside(workspaceRoot, file)) throw new Error('Project state path escapes workspace.');
  atomicWriteJson(file, {
    version: 1,
    activeProjectId: state.activeProjectId || null,
    updatedAt: new Date().toISOString()
  });
}

function readProjectMetadata(projectPath, dirName) {
  const file = path.join(projectPath, PROJECT_METADATA_FILE_NAME);
  let raw = null;
  try {
    if (fs.existsSync(file)) {
      const stat = fs.statSync(file);
      if (stat.isFile() && stat.size <= 64 * 1024) raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    }
  } catch {
    raw = null;
  }
  const stat = fs.statSync(projectPath);
  const id =
    typeof raw?.id === 'string' && /^[a-zA-Z0-9_-]{8,80}$/.test(raw.id)
      ? raw.id
      : publicStableId('project', dirName);
  const updatedAt = typeof raw?.updatedAt === 'string' ? raw.updatedAt : stat.mtime.toISOString();
  let business;
  try {
    business = normalizeProjectBusinessFields(raw || {});
  } catch {
    business = normalizeProjectBusinessFields({});
  }
  return {
    id,
    name: safeProjectDisplayName(raw?.name, dirName),
    dirName,
    projectPath,
    hasMetadata: Boolean(raw),
    createdAt: typeof raw?.createdAt === 'string' ? raw.createdAt : stat.birthtime.toISOString(),
    updatedAt,
    ...business,
    statusLabel: PROJECT_STATUS_LABELS[business.status] || business.status
  };
}

function writeProjectMetadata(projectPath, metadata = {}) {
  const workspaceRoot = readSettings().workspaceRoot;
  if (!isPathInside(workspaceRoot, projectPath)) throw new Error('Project path escapes workspace.');
  const business = normalizeProjectBusinessFields(metadata);
  atomicWriteJson(path.join(projectPath, PROJECT_METADATA_FILE_NAME), {
    version: 1,
    id: sanitizeProjectId(metadata.id),
    name: sanitizeProjectDisplayName(metadata.name),
    status: business.status,
    client: business.client,
    goal: business.goal,
    industry: business.industry,
    scenario: business.scenario,
    budget: business.budget,
    period: business.period,
    deliverables: business.deliverables,
    description: business.description,
    createdAt: metadata.createdAt || new Date().toISOString(),
    updatedAt: metadata.updatedAt || new Date().toISOString()
  });
}

function collectProjectStats(projectPath) {
  const stats = { files: 0, directories: 0, sizeBytes: 0, truncated: false };
  const stack = [projectPath];
  let visited = 0;
  while (stack.length && visited < MAX_PROJECT_STATS_ITEMS) {
    const current = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (entry.name === PROJECT_METADATA_FILE_NAME || entry.name === PROJECT_STATE_FILE_NAME) continue;
      if (entry.isDirectory() && (entry.name === '.git' || entry.name === 'node_modules')) {
        stats.truncated = true;
        continue;
      }
      const absolute = path.join(current, entry.name);
      visited += 1;
      if (visited >= MAX_PROJECT_STATS_ITEMS) {
        stats.truncated = true;
        break;
      }
      try {
        const linkStat = fs.lstatSync(absolute);
        if (linkStat.isSymbolicLink() || !isPathInside(projectPath, absolute)) {
          stats.truncated = true;
          continue;
        }
        const stat = fs.statSync(absolute);
        if (entry.isDirectory()) {
          stats.directories += 1;
          stack.push(absolute);
        } else if (entry.isFile()) {
          stats.files += 1;
          stats.sizeBytes += stat.size;
        }
      } catch {
        stats.truncated = true;
      }
    }
  }
  if (stack.length) stats.truncated = true;
  stats.sizeMb = Number((stats.sizeBytes / 1024 / 1024).toFixed(2));
  return stats;
}

function projectMemoryPaths(projectPath) {
  const memoryDir = path.join(projectPath, PROJECT_MEMORY_DIR_NAME);
  const memoryFile = path.join(memoryDir, PROJECT_MEMORY_FILE_NAME);
  const contextFile = path.join(memoryDir, PROJECT_CONTEXT_FILE_NAME);
  if (!isPathInside(projectPath, memoryDir) || !isPathInside(projectPath, memoryFile) || !isPathInside(projectPath, contextFile)) {
    throw new Error('Project memory path escapes project.');
  }
  return { memoryDir, memoryFile, contextFile };
}

function projectMemoryTemplate(project = {}) {
  const lines = [
    `# ${project.name || 'EcoreX Project'} 记忆`,
    '',
    '- 用途：记录本广告项目长期有效的客户背景、投放结论、素材偏好、预算约束、复盘洞察与待办。',
    '- 规则：只记录当前项目相关信息；不要写入其他项目、密钥、完整客户隐私数据或一次性闲聊内容。',
    '',
    '## 项目简报',
    `- 客户：${project.client || '待补充'}`,
    `- 目标：${project.goal || '待补充'}`,
    `- 行业/场景：${[project.industry, project.scenario].filter(Boolean).join(' / ') || '待补充'}`,
    `- 预算/周期：${[project.budget, project.period].filter(Boolean).join(' / ') || '待补充'}`,
    `- 交付物：${Array.isArray(project.deliverables) && project.deliverables.length ? project.deliverables.join('、') : '待补充'}`,
    project.instructions ? `- 项目指令：${project.instructions}` : '',
    '',
    '## 长期记忆',
    '- '
  ].filter((line) => line !== '');
  return `${lines.join('\n')}\n`;
}

function ensureProjectMemory(project = {}) {
  const workspaceRoot = readSettings().workspaceRoot;
  if (!project.projectPath || !isPathInside(workspaceRoot, project.projectPath)) {
    throw new Error('Project path escapes workspace.');
  }
  const { memoryDir, memoryFile, contextFile } = projectMemoryPaths(project.projectPath);
  fs.mkdirSync(memoryDir, { recursive: true });
  if (!fs.existsSync(memoryFile)) {
    fs.writeFileSync(memoryFile, projectMemoryTemplate(project), 'utf8');
  }
  atomicWriteJson(contextFile, {
    version: 1,
    projectId: project.id,
    name: project.name,
    status: project.status,
    client: project.client || '',
    goal: project.goal || '',
    industry: project.industry || '',
    scenario: project.scenario || '',
    budget: project.budget || '',
    period: project.period || '',
    deliverables: Array.isArray(project.deliverables) ? project.deliverables : [],
    instructions: project.instructions || '',
    updatedAt: new Date().toISOString(),
    memoryFile: PROJECT_MEMORY_FILE_NAME
  });
  return {
    memoryDir,
    memoryFile,
    contextFile,
    memoryLabel: `${publicWorkspacePathLabel(workspaceRoot, memoryDir)}/${PROJECT_MEMORY_FILE_NAME}`.replace(/\/+/, '/')
  };
}

function projectContextFromProject(workspaceRoot, project) {
  if (!project || project.status === 'archived') return null;
  const memory = ensureProjectMemory(project);
  return {
    id: project.id,
    name: project.name,
    status: project.status,
    statusLabel: project.statusLabel,
    client: project.client || '',
    goal: project.goal || '',
    industry: project.industry || '',
    scenario: project.scenario || '',
    budget: project.budget || '',
    period: project.period || '',
    deliverables: Array.isArray(project.deliverables) ? project.deliverables : [],
    instructions: project.instructions || '',
    description: project.description || '',
    projectPath: project.projectPath,
    pathLabel: publicWorkspacePathLabel(workspaceRoot, project.projectPath),
    memoryDir: memory.memoryDir,
    memoryFile: memory.memoryFile,
    memoryLabel: memory.memoryLabel
  };
}

function publicProjectSummary(project, workspaceRoot, options = {}) {
  const memoryLabel = `${publicWorkspacePathLabel(workspaceRoot, path.join(project.projectPath, PROJECT_MEMORY_DIR_NAME))}/${PROJECT_MEMORY_FILE_NAME}`.replace(/\/+/, '/');
  return {
    id: project.id,
    name: project.name,
    status: project.status,
    statusLabel: project.statusLabel || PROJECT_STATUS_LABELS[project.status] || project.status,
    client: project.client || '',
    goal: project.goal || '',
    industry: project.industry || '',
    scenario: project.scenario || '',
    budget: project.budget || '',
    period: project.period || '',
    deliverables: Array.isArray(project.deliverables) ? project.deliverables : [],
    instructions: project.instructions || '',
    description: project.description || '',
    pathLabel: publicWorkspacePathLabel(workspaceRoot, project.projectPath),
    memoryLabel,
    archived: project.status === 'archived',
    updatedAt: project.updatedAt,
    stats: options.includeStats === false ? undefined : collectProjectStats(project.projectPath)
  };
}

function discoverProjects(options = {}) {
  const workspaceRoot = readSettings().workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const state = readWorkspaceProjectState(workspaceRoot);
  const includeArchived = options.includeArchived !== false;
  let directories = [];
  try {
    directories = fs
      .readdirSync(workspaceRoot, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
      .filter((entry) => !entry.name.startsWith('.') && entry.name !== 'node_modules')
      .slice(0, MAX_PROJECTS);
  } catch {
    directories = [];
  }

  const projects = directories
    .map((entry) => {
      const projectPath = path.resolve(workspaceRoot, entry.name);
      if (!isPathInside(workspaceRoot, projectPath)) return null;
      try {
        const linkStat = fs.lstatSync(projectPath);
        if (linkStat.isSymbolicLink()) return null;
        return readProjectMetadata(projectPath, entry.name);
      } catch {
        return null;
      }
    })
    .filter(Boolean)
    .filter((project) => includeArchived || project.status !== 'archived')
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());

  return {
    workspaceRoot,
    state,
    projects: projects.map((project) => ({
      ...project,
      active: project.status !== 'archived' && project.id === state.activeProjectId,
      stats: options.includeStats === false ? undefined : collectProjectStats(project.projectPath)
    }))
  };
}

function listProjects(payload = {}) {
  optionalObjectPayload(payload, 'project payload');
  const includeStats = payload?.includeStats !== false;
  const includeArchived = payload?.includeArchived !== false;
  const { workspaceRoot, state, projects } = discoverProjects({ includeStats, includeArchived });
  return {
    ok: true,
    workspace: {
      pathLabel: 'workspace:/',
      projectCount: projects.length,
      activeProjectId: state.activeProjectId || null
    },
    projects: projects.map((project) => ({
      ...publicProjectSummary(project, workspaceRoot, { includeStats: false }),
      active: project.active,
      stats: includeStats ? project.stats : undefined
    }))
  };
}

function createProject(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Invalid project payload.');
  }
  const name = sanitizeProjectDisplayName(payload.name);
  const workspaceRoot = readSettings().workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const { dirName, projectPath } = uniqueProjectDir(workspaceRoot, payload.slug || name);
  fs.mkdirSync(projectPath, { recursive: true });
  const now = new Date().toISOString();
  const metadata = {
    id: publicStableId('project', `${dirName}:${now}:${crypto.randomBytes(4).toString('hex')}`),
    name,
    ...normalizeProjectBusinessFields({
      status: payload.status || 'active',
      client: payload.client,
      goal: payload.goal,
      industry: payload.industry,
      scenario: payload.scenario,
      budget: payload.budget,
      period: payload.period,
      deliverables: payload.deliverables,
      instructions: payload.instructions,
      description: payload.description
    }),
    createdAt: now,
    updatedAt: now
  };
  writeProjectMetadata(projectPath, metadata);
  if (metadata.status !== 'archived' && payload.switch !== false && payload.activate !== false) {
    writeWorkspaceProjectState(workspaceRoot, { activeProjectId: metadata.id });
  }
  const project = readProjectMetadata(projectPath, dirName);
  ensureProjectMemory(project);
  return {
    ok: true,
    project: {
      ...publicProjectSummary(project, workspaceRoot),
      active: metadata.status !== 'archived' && payload.switch !== false && payload.activate !== false
    }
  };
}

function resolveProject(payload = {}, options = {}) {
  const id = sanitizeProjectId(typeof payload === 'string' ? payload : payload?.id || payload?.projectId);
  const { workspaceRoot, projects } = discoverProjects({ includeStats: false });
  const project = projects.find((item) => item.id === id);
  if (!project) throw new Error('Project not found.');
  if (project.status === 'archived' && options.allowArchived === false) {
    throw new Error('Archived project cannot be activated.');
  }
  return { workspaceRoot, project };
}

function switchProject(payload = {}) {
  const { workspaceRoot, project } = resolveProject(payload, { allowArchived: false });
  const now = new Date().toISOString();
  if (!project.hasMetadata) {
    writeProjectMetadata(project.projectPath, {
      id: project.id,
      name: project.name,
      ...normalizeProjectBusinessFields(project),
      createdAt: project.createdAt,
      updatedAt: now
    });
    project.updatedAt = now;
  }
  ensureProjectMemory(project);
  writeWorkspaceProjectState(workspaceRoot, { activeProjectId: project.id });
  return {
    ok: true,
    activeProject: {
      ...publicProjectSummary(project, workspaceRoot),
      active: true
    }
  };
}

function updateProject(payload = {}) {
  optionalObjectPayload(payload, 'project payload');
  const { workspaceRoot, project } = resolveProject(payload, { allowArchived: true });
  const now = new Date().toISOString();
  const nextName = Object.prototype.hasOwnProperty.call(payload, 'name') ? sanitizeProjectDisplayName(payload.name) : project.name;
  let projectPath = project.projectPath;
  let dirName = project.dirName;
  if (nextName !== project.name) {
    const linkStat = fs.lstatSync(project.projectPath);
    if (!linkStat.isDirectory() || linkStat.isSymbolicLink()) throw new Error('Project directory is not safe to rename.');
    const renamed = uniqueProjectRenameDir(workspaceRoot, project, nextName);
    if (renamed.changed) {
      fs.renameSync(project.projectPath, renamed.projectPath);
      projectPath = renamed.projectPath;
      dirName = renamed.dirName;
    }
  }
  const next = {
    id: project.id,
    name: nextName,
    status: Object.prototype.hasOwnProperty.call(payload, 'status') ? sanitizeProjectStatus(payload.status) : project.status,
    client: Object.prototype.hasOwnProperty.call(payload, 'client') ? payload.client : project.client,
    goal: Object.prototype.hasOwnProperty.call(payload, 'goal') ? payload.goal : project.goal,
    industry: Object.prototype.hasOwnProperty.call(payload, 'industry') ? payload.industry : project.industry,
    scenario: Object.prototype.hasOwnProperty.call(payload, 'scenario') ? payload.scenario : project.scenario,
    budget: Object.prototype.hasOwnProperty.call(payload, 'budget') ? payload.budget : project.budget,
    period: Object.prototype.hasOwnProperty.call(payload, 'period') ? payload.period : project.period,
    deliverables: Object.prototype.hasOwnProperty.call(payload, 'deliverables') ? payload.deliverables : project.deliverables,
    instructions: Object.prototype.hasOwnProperty.call(payload, 'instructions') ? payload.instructions : project.instructions,
    description: Object.prototype.hasOwnProperty.call(payload, 'description') ? payload.description : project.description,
    createdAt: project.createdAt,
    updatedAt: now
  };
  writeProjectMetadata(projectPath, next);
  const updated = readProjectMetadata(projectPath, dirName);
  ensureProjectMemory(updated);
  const state = readWorkspaceProjectState(workspaceRoot);
  if (updated.status === 'archived' && state.activeProjectId === updated.id) {
    writeWorkspaceProjectState(workspaceRoot, { activeProjectId: null });
  }
  return {
    ok: true,
    project: {
      ...publicProjectSummary(updated, workspaceRoot),
      active: updated.status !== 'archived' && state.activeProjectId === updated.id
    }
  };
}

function archiveProject(payload = {}) {
  return updateProject({ ...(typeof payload === 'string' ? { id: payload } : payload), status: 'archived' });
}

function deleteProject(payload = {}) {
  optionalObjectPayload(payload, 'project payload');
  if (payload.confirmDelete !== true && payload.deleteFilesConfirmed !== true) {
    throw new Error('Project deletion requires explicit confirmation.');
  }
  const { workspaceRoot, project } = resolveProject(payload, { allowArchived: true });
  const projectPath = path.resolve(project.projectPath);
  if (!isPathInside(workspaceRoot, projectPath) || isSameWorkspacePath(workspaceRoot, projectPath)) {
    throw new Error('Project path is not safe to delete.');
  }
  const linkStat = fs.lstatSync(projectPath);
  if (!linkStat.isDirectory() || linkStat.isSymbolicLink()) {
    throw new Error('Project directory is not safe to delete.');
  }
  fs.rmSync(projectPath, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  const state = readWorkspaceProjectState(workspaceRoot);
  if (state.activeProjectId === project.id) {
    writeWorkspaceProjectState(workspaceRoot, { activeProjectId: null });
  }
  return {
    ok: true,
    deletedProjectId: project.id,
    deletedProjectName: project.name,
    deletedPathLabel: publicWorkspacePathLabel(workspaceRoot, projectPath)
  };
}

function projectStatus(payload = {}) {
  optionalObjectPayload(payload, 'project payload');
  const includeStats = payload?.includeStats !== false;
  const { workspaceRoot, state, projects } = discoverProjects({ includeStats });
  const requestedId = payload?.id || payload?.projectId;
  const selectedId = requestedId ? sanitizeProjectId(requestedId) : state.activeProjectId;
  const selected = selectedId ? projects.find((project) => project.id === selectedId) : null;
  const active = selected && (requestedId || selected.status !== 'archived') ? selected : null;
  return {
    ok: true,
    workspace: {
      pathLabel: 'workspace:/',
      projectCount: projects.length,
      activeProjectId: state.activeProjectId || null
    },
    activeProject: active
      ? {
          ...publicProjectSummary(active, workspaceRoot, { includeStats: false }),
          active: active.id === state.activeProjectId,
          stats: includeStats ? active.stats : undefined
        }
      : null
  };
}

function projectFilesDir(projectPath) {
  const dir = path.join(projectPath, PROJECT_FILES_DIR_NAME);
  if (!isPathInside(projectPath, dir)) throw new Error('Project files path escapes project.');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function projectFileRelativePath(projectPath, filePath) {
  const filesRoot = path.join(projectPath, PROJECT_FILES_DIR_NAME);
  const relative = path.relative(filesRoot, path.resolve(filePath)).replace(/\\/g, '/');
  return sanitizeWorkspaceRelativePath(relative || path.basename(filePath));
}

function projectFileKindFromMime(extension = '', mimeType = '') {
  const ext = String(extension || '').toLowerCase();
  const mime = String(mimeType || '').toLowerCase();
  if (isImageAttachmentMime(mime)) return 'image';
  if (mime.startsWith('video/') || ['.mp4', '.webm', '.ogg', '.ogv', '.mov', '.m4v'].includes(ext)) return 'video';
  if (['.xls', '.xlsx', '.xlsm', '.csv'].includes(ext)) return 'sheet';
  if (['.ppt', '.pptx', '.pptm'].includes(ext)) return 'slide';
  if (['.doc', '.docx', '.md', '.txt', '.pdf'].includes(ext)) return 'document';
  if (FILE_PREVIEW_TEXT_EXTENSIONS.has(ext) || ['.html', '.htm'].includes(ext)) return 'code';
  return 'file';
}

function projectFilePreviewDataUrl(target, mimeType = '', stat = null) {
  if (!stat?.isFile?.() || stat.size > FILE_PREVIEW_IMAGE_MAX_BYTES || !isImageAttachmentMime(mimeType)) return '';
  try {
    return `data:${mimeType};base64,${fs.readFileSync(target).toString('base64')}`;
  } catch {
    return '';
  }
}

function projectFileEntryFromPath(workspaceRoot, projectPath, filePath) {
  const target = path.resolve(filePath);
  if (!isPathInside(projectPath, target)) throw new Error('Project file path escapes project.');
  const stat = fs.statSync(target);
  if (!stat.isFile()) return null;
  const name = path.basename(target);
  const extension = path.extname(target).toLowerCase();
  const mimeType = attachmentMimeFromPath(target) || filePreviewMimeFromExtension(extension);
  const relativePath = projectFileRelativePath(projectPath, target);
  const previewDataUrl = projectFilePreviewDataUrl(target, mimeType, stat);
  return {
    id: crypto.createHash('sha256').update(`${target}:${stat.size}:${stat.mtimeMs}`).digest('hex').slice(0, 16),
    name: redactSensitiveText(name).slice(0, 240),
    path: target,
    filePath: target,
    pathLabel: publicWorkspacePathLabel(workspaceRoot, target),
    relativePath,
    type: mimeType,
    mimeType,
    extension,
    kind: projectFileKindFromMime(extension, mimeType),
    previewDataUrl,
    previewUrl: previewDataUrl,
    thumbnail: previewDataUrl,
    sizeBytes: stat.size,
    sizeMb: Number((stat.size / 1024 / 1024).toFixed(2)),
    modifiedAt: stat.mtime.toISOString()
  };
}

function collectProjectFileEntries(workspaceRoot, projectPath, dir, depth = 0, entries = []) {
  if (depth > 8 || entries.length >= 200) return entries;
  let rows = [];
  try {
    rows = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return entries;
  }
  for (const row of rows) {
    if (entries.length >= 200) break;
    if (row.isSymbolicLink()) continue;
    const target = path.join(dir, row.name);
    if (!isPathInside(projectPath, target)) continue;
    if (row.isDirectory()) {
      collectProjectFileEntries(workspaceRoot, projectPath, target, depth + 1, entries);
      continue;
    }
    if (!row.isFile()) continue;
    try {
      const entry = projectFileEntryFromPath(workspaceRoot, projectPath, target);
      if (entry) entries.push(entry);
    } catch {
      // Skip unreadable files rather than failing the project overview.
    }
  }
  return entries;
}

function resolveProjectFileTarget(workspaceRoot, project, payload = {}) {
  const rawPath = payload.path || payload.filePath || payload.pathLabel || '';
  if (!rawPath) throw new Error('Project file path is required.');
  const target = candidatePreviewPath(rawPath, workspaceRoot);
  const filesDir = projectFilesDir(project.projectPath);
  if (!isPathInside(filesDir, target)) throw new Error('Project file is outside the project files directory.');
  if (pathContainsSymlink(filesDir, target) || fs.lstatSync(target).isSymbolicLink()) {
    throw new Error('Project file path crosses a symbolic link.');
  }
  return { target, filesDir };
}

function listProjectFiles(payload = {}) {
  const { workspaceRoot, project } = resolveProject(payload, { allowArchived: true });
  const dir = projectFilesDir(project.projectPath);
  let entries = [];
  try {
    entries = collectProjectFileEntries(workspaceRoot, project.projectPath, dir)
      .sort((left, right) => new Date(right.modifiedAt).getTime() - new Date(left.modifiedAt).getTime())
      .slice(0, 200);
  } catch {
    entries = [];
  }
  return {
    ok: true,
    projectId: project.id,
    files: entries
  };
}

function uniqueProjectFileTarget(dir, sourceName) {
  const parsed = path.parse(safeAttachmentName(sourceName || 'project-file'));
  const base = (parsed.name || 'project-file').replace(/[<>:"/\\|?*\x00-\x1f]+/g, '-').slice(0, 120) || 'project-file';
  const ext = (parsed.ext || '').replace(/[<>:"/\\|?*\x00-\x1f]+/g, '').slice(0, 24);
  for (let index = 0; index < 100; index += 1) {
    const name = index === 0 ? `${base}${ext}` : `${base}-${index + 1}${ext}`;
    const target = path.resolve(dir, name);
    if (!isPathInside(dir, target)) throw new Error('Project file target escapes project.');
    if (!fs.existsSync(target)) return target;
  }
  throw new Error('Unable to allocate project file name.');
}

async function addProjectFiles(event, payload = {}) {
  const input = optionalObjectPayload(payload, 'project file payload');
  const { workspaceRoot, project } = resolveProject(input, { allowArchived: false });
  const dir = projectFilesDir(project.projectPath);
  let filePaths = Array.isArray(input.files)
    ? input.files.map((item) => typeof item === 'string' ? item : item?.path || item?.filePath).filter(Boolean)
    : [];
  if (!filePaths.length) {
    const result = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender) || mainWindow, {
      title: '选择要加入项目记忆的文件',
      properties: ['openFile', 'multiSelections'],
      filters: [
        { name: '项目资料', extensions: ['pdf', 'docx', 'pptx', 'xlsx', 'xlsm', 'csv', 'txt', 'md', 'json', 'html', 'png', 'jpg', 'jpeg', 'webp'] },
        { name: '所有文件', extensions: ['*'] }
      ]
    });
    if (result.canceled || !result.filePaths?.length) {
      return { ok: true, canceled: true, files: [] };
    }
    filePaths = result.filePaths;
  }

  const copied = [];
  const skipped = [];
  for (const sourcePath of filePaths.slice(0, MAX_PROJECT_FILE_SELECTION)) {
    try {
      const source = path.resolve(sourcePath);
      const linkStat = fs.lstatSync(source);
      if (!linkStat.isFile() || linkStat.isSymbolicLink()) throw new Error('不是安全文件');
      const stat = fs.statSync(source);
      if (stat.size > MAX_PROJECT_FILE_BYTES) throw new Error('文件超过 100MB');
      const target = uniqueProjectFileTarget(dir, path.basename(source));
      fs.copyFileSync(source, target);
      copied.push(projectFileEntryFromPath(workspaceRoot, project.projectPath, target));
    } catch (error) {
      skipped.push({
        name: path.basename(String(sourcePath || 'file')),
        reason: error instanceof Error ? error.message : String(error)
      });
    }
  }
  const now = new Date().toISOString();
  writeProjectMetadata(project.projectPath, { ...project, updatedAt: now });
  return {
    ok: true,
    canceled: false,
    projectId: project.id,
    files: copied,
    skipped
  };
}

async function openProjectFile(payload = {}) {
  const input = optionalObjectPayload(payload, 'project file open payload');
  const { workspaceRoot, project } = resolveProject(input, { allowArchived: true });
  const { target } = resolveProjectFileTarget(workspaceRoot, project, input);
  const stat = fs.statSync(target);
  if (!stat.isFile()) throw new Error('Project file is not a file.');
  const opened = await openPathSafely(target);
  return {
    ok: Boolean(opened.opened),
    opened: Boolean(opened.opened),
    method: 'openPath',
    name: redactSensitiveText(path.basename(target)).slice(0, 240),
    pathLabel: publicWorkspacePathLabel(workspaceRoot, target),
    error: opened.error || undefined
  };
}

function removeProjectFile(payload = {}) {
  const input = optionalObjectPayload(payload, 'project file remove payload');
  const { workspaceRoot, project } = resolveProject(input, { allowArchived: false });
  const { target } = resolveProjectFileTarget(workspaceRoot, project, input);
  const stat = fs.statSync(target);
  if (!stat.isFile()) throw new Error('Project file is not a file.');
  fs.rmSync(target, { force: true });
  writeProjectMetadata(project.projectPath, { ...project, updatedAt: new Date().toISOString() });
  return {
    ok: true,
    removed: true,
    projectId: project.id,
    pathLabel: publicWorkspacePathLabel(workspaceRoot, target)
  };
}

async function openProjectFolder(payload = {}) {
  const { workspaceRoot, project } = resolveProject(payload, { allowArchived: true });
  const target = path.resolve(project.projectPath);
  if (!isPathInside(workspaceRoot, target) || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) {
    throw new Error('Project folder is not available.');
  }
  const opened = await openPathSafely(target);
  return {
    ok: Boolean(opened.opened),
    opened: Boolean(opened.opened),
    method: 'openPath',
    pathLabel: publicWorkspacePathLabel(workspaceRoot, target),
    error: opened.error || undefined
  };
}

function resolveProjectCwd(projectId) {
  if (!projectId) return null;
  const { workspaceRoot, project } = resolveProject({ id: projectId }, { allowArchived: false });
  return projectContextFromProject(workspaceRoot, project)?.projectPath || null;
}

function activeProjectContext() {
  const workspaceRoot = readSettings().workspaceRoot;
  const state = readWorkspaceProjectState(workspaceRoot);
  if (!state.activeProjectId) return null;
  try {
    const { project } = resolveProject({ id: state.activeProjectId }, { allowArchived: false });
    return projectContextFromProject(workspaceRoot, project);
  } catch {
    return null;
  }
}

function activeProjectCwd() {
  return activeProjectContext()?.projectPath || null;
}

function clampTimeout(timeoutMs) {
  const timeout = Number(timeoutMs);
  if (!Number.isFinite(timeout)) return AGENT_TIMEOUT_MS;
  return Math.min(Math.max(timeout, AGENT_MIN_TIMEOUT_MS), AGENT_TIMEOUT_MS);
}

function sanitizeSessionId(sessionId) {
  const value = String(sessionId || '').trim();
  return /^[a-zA-Z0-9_-]{8,80}$/.test(value) ? value : crypto.randomUUID();
}

function stableClaudeSessionUuid(value) {
  const hash = crypto
    .createHash('sha256')
    .update(`ecorex-claude-session/v1:${String(value || '')}`)
    .digest();
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  const hex = hash.subarray(0, 16).toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

function sanitizeClaudeSessionId(sessionId, fallbackKey = '') {
  const value = String(sessionId || '').trim();
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    return value.toLowerCase();
  }
  const stableSource = value || String(fallbackKey || '').trim();
  return stableSource ? stableClaudeSessionUuid(stableSource) : crypto.randomUUID();
}

function claudeProjectsRoot() {
  return path.join(agentRuntimeConfigDir(), 'projects');
}

function claudeTranscriptRoots() {
  const runtimeConfigDir = path.resolve(agentRuntimeConfigDir());
  const roots = [claudeProjectsRoot()];
  for (const configDir of [process.env.CLAUDE_CONFIG_DIR, process.env.ECOREX_AGENT_CONFIG_DIR]) {
    if (!configDir) continue;
    const resolvedConfigDir = path.resolve(configDir);
    if (isSameWorkspacePath(resolvedConfigDir, runtimeConfigDir) || isPathInside(runtimeConfigDir, resolvedConfigDir)) {
      roots.push(path.join(resolvedConfigDir, 'projects'));
    }
  }
  const seen = new Set();
  return roots
    .filter(Boolean)
    .map((root) => path.resolve(root))
    .filter((root) => {
      const key = isWindows ? root.toLowerCase() : root;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function invalidClaudeResumeEntry(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(sessionId)) return null;
  const entry = invalidClaudeSessionResumeCache.get(sessionId);
  if (!entry) return null;
  if (Date.now() - entry.at > CLAUDE_INVALID_RESUME_TTL_MS) {
    invalidClaudeSessionResumeCache.delete(sessionId);
    return null;
  }
  return entry;
}

function markClaudeSessionResumeInvalid(claudeSessionId, reason = 'missing-resume-target') {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(sessionId)) return;
  invalidClaudeSessionResumeCache.set(sessionId, { at: Date.now(), reason });
  claudeTranscriptExistenceCache.delete(sessionId);
  ensureClaudeSessionBindingCacheLoaded();
  claudeSessionBindingCache.delete(sessionId);
  try {
    persistClaudeSessionBindings();
  } catch {
    // Binding persistence is best-effort; invalidating the in-memory resume target is authoritative.
  }
}

function clearInvalidClaudeSessionResume(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (/^[0-9a-f-]{36}$/i.test(sessionId)) invalidClaudeSessionResumeCache.delete(sessionId);
}

function markClaudeSessionTranscriptSeen(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (/^[0-9a-f-]{36}$/i.test(sessionId)) {
    clearInvalidClaudeSessionResume(sessionId);
    claudeTranscriptExistenceCache.set(sessionId, { exists: true, checkedAt: Date.now() });
  }
}

function markClaudeSessionLaunched(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (/^[0-9a-f-]{36}$/i.test(sessionId)) {
    claudeSessionLaunchCache.set(sessionId, { launchedAt: Date.now() });
  }
}

function findClaudeSessionTranscript(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(sessionId)) return '';
  for (const root of claudeTranscriptRoots()) {
    try {
      if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) continue;
    } catch {
      continue;
    }

    const stack = [root];
    let visited = 0;
    while (stack.length && visited < 4000) {
      const current = stack.pop();
      visited += 1;
      let entries = [];
      try {
        entries = fs.readdirSync(current, { withFileTypes: true });
      } catch {
        continue;
      }
      for (const entry of entries) {
        const child = path.join(current, entry.name);
        if (entry.isDirectory()) {
          stack.push(child);
          continue;
        }
        if (entry.isFile() && entry.name.toLowerCase() === `${sessionId}.jsonl`) {
          return child;
        }
      }
    }
  }
  return '';
}

function refreshClaudeSessionTranscriptSeen(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(sessionId)) return false;
  if (invalidClaudeResumeEntry(sessionId)) return false;
  if (findClaudeSessionTranscript(sessionId)) {
    markClaudeSessionTranscriptSeen(sessionId);
    return true;
  }
  claudeTranscriptExistenceCache.delete(sessionId);
  return false;
}

function claudeSessionTranscriptExists(claudeSessionId) {
  const sessionId = String(claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(sessionId)) return false;
  if (invalidClaudeResumeEntry(sessionId)) return false;
  const cached = claudeTranscriptExistenceCache.get(sessionId);
  if (cached?.exists && Date.now() - cached.checkedAt <= CLAUDE_TRANSCRIPT_CACHE_TTL_MS) return true;
  return refreshClaudeSessionTranscriptSeen(sessionId);
}

function claudeSessionShouldResume(claudeSessionId) {
  return claudeSessionTranscriptExists(claudeSessionId);
}

function claudeSessionBindingForPayload(payload = {}) {
  const claudeSessionId = String(payload.claudeSessionId || '').trim().toLowerCase();
  if (!/^[0-9a-f-]{36}$/i.test(claudeSessionId)) return null;
  return {
    claudeSessionId,
    conversationId: String(payload.conversationId || payload.sessionId || '').trim(),
    projectId: String(payload.projectId || payload.projectContext?.id || '').trim(),
    cwd: payload.cwd ? path.resolve(String(payload.cwd || '')) : ''
  };
}

function isSameClaudeSessionBinding(left = {}, right = {}) {
  const leftCwd = left.cwd ? path.resolve(left.cwd) : '';
  const rightCwd = right.cwd ? path.resolve(right.cwd) : '';
  const leftCwdKey = isWindows ? leftCwd.toLowerCase() : leftCwd;
  const rightCwdKey = isWindows ? rightCwd.toLowerCase() : rightCwd;
  return (
    String(left.conversationId || '') === String(right.conversationId || '') &&
    String(left.projectId || '') === String(right.projectId || '') &&
    leftCwdKey === rightCwdKey
  );
}

function normalizeClaudeSessionBinding(raw = {}) {
  const binding = claudeSessionBindingForPayload(raw);
  if (!binding || !binding.conversationId || !binding.cwd) return null;
  return {
    ...binding,
    createdAt: Number(raw.createdAt) || Date.now(),
    updatedAt: Number(raw.updatedAt) || Number(raw.createdAt) || Date.now()
  };
}

function ensureClaudeSessionBindingCacheLoaded() {
  if (claudeSessionBindingCacheLoaded) return;
  claudeSessionBindingCacheLoaded = true;
  let payload = null;
  try {
    const file = claudeSessionBindingsPath();
    if (!fs.existsSync(file)) return;
    payload = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    writeLog('warn', 'Failed to read Claude session bindings', {
      error: error instanceof Error ? error.message : String(error)
    });
    return;
  }
  const rows = Array.isArray(payload?.bindings) ? payload.bindings : Array.isArray(payload) ? payload : [];
  for (const row of rows) {
    const binding = normalizeClaudeSessionBinding(row);
    if (binding) claudeSessionBindingCache.set(binding.claudeSessionId, binding);
  }
}

function persistClaudeSessionBindings() {
  ensureClaudeSessionBindingCacheLoaded();
  const bindings = Array.from(claudeSessionBindingCache.values())
    .sort((left, right) => (Number(right.updatedAt) || 0) - (Number(left.updatedAt) || 0))
    .slice(0, 1000)
    .map((binding) => ({
      claudeSessionId: binding.claudeSessionId,
      conversationId: binding.conversationId,
      projectId: binding.projectId || '',
      cwd: binding.cwd,
      createdAt: Number(binding.createdAt) || Number(binding.updatedAt) || Date.now(),
      updatedAt: Number(binding.updatedAt) || Date.now()
    }));
  try {
    atomicWriteJson(claudeSessionBindingsPath(), {
      version: 1,
      bindings,
      updatedAt: new Date().toISOString()
    });
  } catch (error) {
    writeLog('warn', 'Failed to persist Claude session bindings', {
      error: error instanceof Error ? error.message : String(error)
    });
  }
}

function rememberClaudeSessionBinding(payload = {}) {
  ensureClaudeSessionBindingCacheLoaded();
  const binding = claudeSessionBindingForPayload(payload);
  if (binding) {
    const previous = claudeSessionBindingCache.get(binding.claudeSessionId);
    claudeSessionBindingCache.set(binding.claudeSessionId, {
      ...previous,
      ...binding,
      createdAt: Number(previous?.createdAt) || Date.now(),
      updatedAt: Date.now()
    });
    persistClaudeSessionBindings();
  }
}

function claudeSessionBindingConflict(payload = {}) {
  ensureClaudeSessionBindingCacheLoaded();
  const nextBinding = claudeSessionBindingForPayload(payload);
  if (!nextBinding) return null;
  const existing = claudeSessionBindingCache.get(nextBinding.claudeSessionId);
  if (!existing || isSameClaudeSessionBinding(existing, nextBinding)) return null;
  return { existing, next: nextBinding };
}

function sanitizePayload(payload = {}) {
  const settings = readSettings();
  const maxPromptChars = Math.max(MIN_PROMPT_CHARS, Math.min(Number(settings.maxPromptChars) || MAX_PROMPT_CHARS, MAX_PROMPT_CHARS));
  const userPrompt = String(payload.prompt || '').trim().slice(0, maxPromptChars);
  const hasPayloadAccessMode = Object.prototype.hasOwnProperty.call(payload, 'accessMode');
  const hasPayloadPermissionMode = Object.prototype.hasOwnProperty.call(payload, 'permissionMode');
  const hasPayloadDefaultPermissionMode = Object.prototype.hasOwnProperty.call(payload, 'defaultPermissionMode');
  let permissionMode = settings.permissionMode;
  let permissionModeFromPayload = false;
  if (hasPayloadAccessMode || hasPayloadPermissionMode || hasPayloadDefaultPermissionMode) {
    permissionModeFromPayload = true;
    const inputs = [
      ['accessMode', hasPayloadAccessMode, payload.accessMode],
      ['permissionMode', hasPayloadPermissionMode, payload.permissionMode],
      ['defaultPermissionMode', hasPayloadDefaultPermissionMode, payload.defaultPermissionMode]
    ].filter(([, present]) => present);
    permissionMode = sanitizePermissionMode(inputs[0][2], inputs[0][0]);
    for (const [label, , value] of inputs.slice(1)) {
      if (sanitizePermissionMode(value, label) !== permissionMode) {
        throw new Error('Conflicting permissionMode values.');
      }
    }
  }
  const permissionPolicy = publicPermissionPolicy(permissionMode, { includeBackend: true });
  if (permissionPolicy.fullAccess && permissionModeFromPayload && !hasFullAccessConfirmation(payload)) {
    throw new Error('Full access permission requires explicit confirmation.');
  }
  const model = resolveAgentModelName(payload.model, settings);
  const workspaceRoot = settings.workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const projectContextDisabled = payload.disableProjectContext === true || !payload.projectId;
  const requestedProjectId = !projectContextDisabled && payload.projectId ? sanitizeProjectId(payload.projectId) : null;
  const projectContext = requestedProjectId
    ? projectContextFromProject(workspaceRoot, resolveProject({ id: requestedProjectId }, { allowArchived: false }).project)
    : null;
  const defaultRunRoot = projectContext?.projectPath || generalAgentWorkspaceDir();
  let cwd = defaultRunRoot;
  if (payload.cwd || payload.pathLabel) {
    const requested = String(payload.cwd || payload.pathLabel || '').trim();
    const requestedCwd = requested.startsWith('workspace:/')
      ? path.resolve(workspaceRoot, sanitizeWorkspaceRelativePath(requested.replace(/^workspace:\//, '')))
      : path.resolve(requested);
    const cwdRoot = defaultRunRoot;
    if (fs.existsSync(requestedCwd) && fs.statSync(requestedCwd).isDirectory() && isPathInside(cwdRoot, requestedCwd)) {
      cwd = requestedCwd;
    } else if (projectContext) {
      throw new Error('Run cwd must stay inside the current project.');
    } else {
      throw new Error('Run cwd must stay inside the general workspace.');
    }
  }
  const plugins = Array.isArray(payload.plugins)
    ? payload.plugins
        .map((plugin) => String(plugin || '').trim())
        .filter((plugin) => /^[a-zA-Z0-9_.-]{1,80}$/.test(plugin))
        .filter((plugin) => !isBlockedLocalSkillName(plugin))
        .slice(0, 12)
    : [];
  const attachmentContext = ingestAgentAttachments(payload, { cwd, projectContext });
  const prompt = composePromptWithAttachmentContext(userPrompt, attachmentContext, maxPromptChars);

  return {
    sessionId: sanitizeSessionId(payload.sessionId),
    conversationId: sanitizeSessionId(payload.conversationId || payload.sessionId),
    claudeSessionId: sanitizeClaudeSessionId(payload.claudeSessionId || payload.conversationId, payload.sessionId),
    messageId: String(payload.messageId || payload.assistantMessageId || '').trim().slice(0, 120),
    prompt,
    userPrompt,
    attachmentContext,
    accessMode: permissionPolicy.accessMode,
    permissionMode: permissionPolicy.permissionMode,
    permissionCliMode: permissionPolicy.cliMode,
    permissionCliFlags: permissionPolicy.cliFlags,
    permissionLabel: permissionPolicy.label,
    permissionPolicy,
    model,
    cwd,
    projectId: projectContext?.id || null,
    projectContext,
    plugins,
    timeoutMs: clampTimeout(payload.timeoutMs),
    bare: Boolean(payload.bare)
  };
}

function createAgentPermissionSnapshot(safePayload = {}) {
  const permissionPolicy = safePayload.permissionPolicy || publicPermissionPolicy(safePayload.accessMode || safePayload.permissionMode, { includeBackend: true });
  const permissionCliFlags = Object.freeze([...(safePayload.permissionCliFlags || permissionPolicy.cliFlags || [])]);
  const plugins = Object.freeze([...(safePayload.plugins || [])]);
  const snapshot = Object.freeze({
    runtimeKind: AGENT_RUNTIME_KIND,
    sessionId: safePayload.sessionId,
    claudeSessionId: safePayload.claudeSessionId,
    accessMode: permissionPolicy.accessMode || safePayload.accessMode,
    permissionMode: permissionPolicy.permissionMode || safePayload.permissionMode,
    permissionCliMode: safePayload.permissionCliMode ?? permissionPolicy.cliMode ?? null,
    permissionCliFlags,
    permissionLabel: safePayload.permissionLabel || permissionPolicy.label,
    fullAccess: Boolean(permissionPolicy.fullAccess),
    model: safePayload.model,
    cwd: path.resolve(safePayload.cwd || defaultCommandCwd()),
    workspacePath: publicWorkspacePath(safePayload.cwd),
    projectId: safePayload.projectId || null,
    projectName: safePayload.projectContext?.name || '',
    projectPath: safePayload.projectContext?.pathLabel || '',
    projectMemoryLabel: safePayload.projectContext?.memoryLabel || '',
    plugins,
    createdAt: new Date().toISOString()
  });
  return assertAgentPermissionSnapshotIsolated(snapshot);
}

function assertAgentPermissionSnapshotIsolated(snapshot = {}) {
  if (!Object.isFrozen(snapshot) || !Object.isFrozen(snapshot.permissionCliFlags) || !Object.isFrozen(snapshot.plugins)) {
    throw new Error('Agent permission snapshot must be immutable.');
  }
  const includesFullAccessFlag = snapshot.permissionCliFlags.includes(FULL_ACCESS_CLAUDE_FLAG);
  if (Boolean(snapshot.fullAccess) !== includesFullAccessFlag) {
    throw new Error('Agent permission snapshot is inconsistent.');
  }
  if (!snapshot.sessionId || !snapshot.claudeSessionId || !snapshot.cwd) {
    throw new Error('Agent permission snapshot is incomplete.');
  }
  return snapshot;
}

function createAgentSessionActor(sessionId, safePayload, startLock, metadata = {}) {
  const permissionSnapshot = createAgentPermissionSnapshot(safePayload);
  const actor = {
    actorId: crypto.randomUUID(),
    runtimeKind: AGENT_RUNTIME_KIND,
    sessionId,
    ownerId: startLock.ownerId,
    signature: startLock.signature,
    claudeSessionId: safePayload.claudeSessionId,
    permissionSnapshot,
    status: 'starting',
    state: 'running',
    createdAt: Date.now(),
    startedAt: Date.now(),
    lastActivityAt: Date.now(),
    claudeResumeMode: metadata.claudeResumeExistingSession ? 'resume' : 'new',
    transport: null,
    stop(reason = 'cancelled') {
      this.status = agentFinalStatus(reason);
      this.state = 'stopping';
      this.stopReason = reason;
      if (this.transport && typeof this.transport.stop === 'function') {
        return this.transport.stop(reason);
      }
      return false;
    }
  };
  agentSessionActors.set(sessionId, actor);
  return actor;
}

function createCliAgentTransport(child, metadata = {}) {
  return {
    kind: 'claude-cli-child',
    pid: child?.pid || null,
    actorId: metadata.actorId || null,
    resumeMode: metadata.resumeMode || 'new',
    startedAt: Date.now(),
    stopped: false,
    stop(reason = 'cancelled') {
      if (this.stopped) return false;
      this.stopped = true;
      this.stopReason = reason;
      this.stoppedAt = Date.now();
      killProcessTree(child);
      return true;
    }
  };
}

function disposeAgentSessionActor(sessionId, reason = 'stopped') {
  const actor = agentSessionActors.get(sessionId);
  if (!actor) return false;
  actor.status = agentFinalStatus(reason);
  actor.state = 'stopped';
  actor.endedAt = Date.now();
  if (actor.transport) actor.transport.endedAt = actor.endedAt;
  agentSessionActors.delete(sessionId);
  return true;
}

function runtimeStatusSnapshot() {
  const now = Date.now();
  const actors = Array.from(agentSessionActors.values()).map((actor) => {
    const snapshot = actor.permissionSnapshot || {};
    return {
      actorId: actor.actorId,
      sessionId: actor.sessionId,
      runtimeKind: actor.runtimeKind || AGENT_RUNTIME_KIND,
      status: actor.status || 'running',
      state: actor.state || 'running',
      startedAt: new Date(actor.startedAt || actor.createdAt || now).toISOString(),
      uptimeMs: Math.max(0, now - (actor.startedAt || actor.createdAt || now)),
      lastActivityAt: new Date(actor.lastActivityAt || actor.startedAt || now).toISOString(),
      claudeSessionId: publicStableId('claude-session', actor.claudeSessionId),
      claudeResumeMode: actor.claudeResumeMode || 'new',
      transport: actor.transport?.kind || 'pending',
      pid: actor.transport?.pid || null,
      accessMode: snapshot.accessMode || 'default',
      permissionMode: snapshot.permissionMode || 'default',
      permissionLabel: snapshot.permissionLabel || '',
      fullAccess: Boolean(snapshot.fullAccess),
      projectId: snapshot.projectId || null,
      projectName: snapshot.projectName || '',
      workspacePath: snapshot.workspacePath || publicWorkspacePath(snapshot.cwd),
      model: snapshot.model || null
    };
  });
  return {
    ok: true,
    runtimeKind: AGENT_RUNTIME_KIND,
    status: actors.length ? 'running' : 'ready',
    activeActors: actors.length,
    maxRunning: MAX_RUNNING_AGENTS,
    preloadStatus: startupPreloadState.status,
    generatedAt: new Date(now).toISOString(),
    actors
  };
}

function publicAgentRuntimeStatus() {
  const snapshot = runtimeStatusSnapshot();
  return {
    ok: true,
    runtimeKind: snapshot.runtimeKind,
    status: snapshot.status,
    activeActors: snapshot.activeActors,
    maxRunning: snapshot.maxRunning,
    preloadStatus: snapshot.preloadStatus,
    generatedAt: snapshot.generatedAt,
    actors: snapshot.actors.map((actor) => ({
      actorId: actor.actorId,
      sessionId: actor.sessionId,
      status: actor.status,
      state: actor.state,
      transport: actor.transport,
      claudeResumeMode: actor.claudeResumeMode,
      accessMode: actor.accessMode,
      permissionMode: actor.permissionMode,
      permissionLabel: actor.permissionLabel,
      fullAccess: actor.fullAccess,
      projectId: actor.projectId,
      projectName: actor.projectName,
      model: actor.model,
      uptimeMs: actor.uptimeMs
    }))
  };
}

function readProjectMemoryPreview(projectContext) {
  if (!projectContext?.memoryFile) return '';
  try {
    const stat = fs.statSync(projectContext.memoryFile);
    if (!stat.isFile() || stat.size > 256 * 1024) return '';
    return safeOutputText(fs.readFileSync(projectContext.memoryFile, 'utf8'), 5000);
  } catch {
    return '';
  }
}

function agentSystemPromptForProject(projectContext) {
  if (!projectContext) {
    return [
      ECOREX_AGENT_SYSTEM_PROMPT,
      ECOREX_MANAGED_CAPABILITY_PRIORITY_PROMPT,
      ECOREX_GENERAL_CHAT_ISOLATION_PROMPT
    ].filter(Boolean).join('\n\n');
  }
  const fields = [
    `项目：${projectContext.name}`,
    projectContext.client ? `客户：${projectContext.client}` : '',
    projectContext.goal ? `目标：${projectContext.goal}` : '',
    projectContext.industry || projectContext.scenario
      ? `行业/场景：${[projectContext.industry, projectContext.scenario].filter(Boolean).join(' / ')}`
      : '',
    projectContext.budget || projectContext.period
      ? `预算/周期：${[projectContext.budget, projectContext.period].filter(Boolean).join(' / ')}`
      : '',
    projectContext.deliverables?.length ? `交付物：${projectContext.deliverables.join('、')}` : '',
    projectContext.instructions ? `项目指令：${projectContext.instructions}` : '',
    `项目目录：${projectContext.pathLabel}`,
    `项目资料目录：${publicWorkspacePathLabel(projectContext.projectPath, path.join(projectContext.projectPath, PROJECT_FILES_DIR_NAME))}`,
    `项目记忆：${projectContext.memoryLabel}`
  ].filter(Boolean);
  const memoryPreview = readProjectMemoryPreview(projectContext);
  return [
    ECOREX_AGENT_SYSTEM_PROMPT,
    ECOREX_MANAGED_CAPABILITY_PRIORITY_PROMPT,
    '',
    '当前任务已绑定到一个 EcoreX 广告项目。你必须把文件读写、命令执行和长期记忆限制在该项目上下文内。',
    fields.join('\n'),
    '如需沉淀长期结论，请更新项目目录下的 .ecorex-memory/project-memory.md；不要把其他项目的客户信息、预算或素材偏好混入当前项目。',
    memoryPreview ? `当前项目记忆摘要：\n${memoryPreview}` : ''
  ].filter(Boolean).join('\n');
}

function projectEnvForAgent(projectContext) {
  if (!projectContext) return {};
  return {
    ECOREX_PROJECT_ID: projectContext.id,
    ECOREX_PROJECT_NAME: projectContext.name,
    ECOREX_PROJECT_CLIENT: projectContext.client || '',
    ECOREX_PROJECT_GOAL: projectContext.goal || '',
    ECOREX_PROJECT_INDUSTRY: projectContext.industry || '',
    ECOREX_PROJECT_SCENARIO: projectContext.scenario || '',
    ECOREX_PROJECT_BUDGET: projectContext.budget || '',
    ECOREX_PROJECT_PERIOD: projectContext.period || '',
    ECOREX_PROJECT_INSTRUCTIONS: projectContext.instructions || '',
    ECOREX_PROJECT_MEMORY_FILE: PROJECT_MEMORY_FILE_NAME,
    ECOREX_PROJECT_MEMORY_DIR: PROJECT_MEMORY_DIR_NAME,
    ECOREX_PROJECT_FILES_DIR: PROJECT_FILES_DIR_NAME,
    ECOREX_PROJECT_MEMORY_LABEL: projectContext.memoryLabel || ''
  };
}

function windowDiagnosticSnapshot(window = mainWindow) {
  if (!window || window.isDestroyed?.()) {
    return { window: null };
  }
  let bounds = null;
  let contentBounds = null;
  let url = '';
  try {
    bounds = window.getBounds();
    contentBounds = window.getContentBounds();
    url = window.webContents?.getURL?.() || '';
  } catch {
    // Window diagnostics are best-effort and must not affect startup.
  }
  return {
    window: {
      id: window.id,
      title: window.getTitle?.() || 'EcoreX Agent',
      bounds,
      contentBounds,
      isVisible: window.isVisible?.() || false,
      isMinimized: window.isMinimized?.() || false,
      isMaximized: window.isMaximized?.() || false,
      isFocused: window.isFocused?.() || false,
      webContentsId: window.webContents?.id || null,
      url
    }
  };
}

function defaultWindowBounds() {
  const fallback = { width: 1400, height: 900 };
  let workArea = null;
  try {
    workArea = screen.getPrimaryDisplay()?.workAreaSize || null;
  } catch {
    workArea = null;
  }
  if (!workArea?.width || !workArea?.height) return fallback;
  return {
    width: Math.min(fallback.width, Math.max(800, workArea.width - 32)),
    height: Math.min(fallback.height, Math.max(600, workArea.height - 32))
  };
}

function startupBrandIconPath() {
  const candidates = [
    devPath('build', 'icon.ico'),
    devPath('build', 'icon.png'),
    devPath('dist', 'icon.png'),
    devPath('public', 'icon.png'),
    devPath('前端UI视觉', 'ecorex_app_icon_transparent.png')
  ];
  const iconPath = candidates.find((candidate) => {
    try {
      return fs.existsSync(candidate) && fs.statSync(candidate).isFile();
    } catch {
      return false;
    }
  });
  return iconPath || '';
}

function startupSplashHtml(iconFileName = '') {
  const iconMarkup = iconFileName
    ? `<img class="mark" src="${iconFileName}" alt="" aria-hidden="true" />`
    : '<div class="mark fallback" aria-hidden="true">EX</div>';
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>EcoreX Agent 正在启动</title>
  <style>
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #070c12; color: #f3f6fa; font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif; }
    body { display: grid; place-items: center; background: radial-gradient(circle at 55% 44%, rgba(255, 90, 0, .18), transparent 34%), linear-gradient(145deg, #050910, #0a1119); }
    .splash { display: grid; justify-items: center; gap: 16px; transform: translateY(-8px); }
    .mark { width: 68px; height: 68px; border-radius: 18px; display: block; object-fit: contain; filter: drop-shadow(0 20px 60px rgba(0,0,0,.42)); }
    .mark.fallback { display: grid; place-items: center; background: #111820; color: #ff5a00; font-size: 24px; font-weight: 900; box-shadow: inset 0 1px 0 rgba(255,255,255,.08); }
    .title { margin: 0; font-size: 24px; font-weight: 800; letter-spacing: 0; }
    .sub { margin: -6px 0 2px; color: #a9b0bb; font-size: 13px; }
    .spinner { width: 34px; height: 34px; border-radius: 50%; border: 3px solid rgba(255,255,255,.12); border-top-color: #ff5a00; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <main class="splash" aria-live="polite">
    ${iconMarkup}
    <h1 class="title">EcoreX Agent</h1>
    <p class="sub">正在预加载本地能力与会话环境</p>
    <div class="spinner" aria-hidden="true"></div>
  </main>
</body>
</html>`;
}

function startupSplashDataUrl() {
  return `data:text/html;charset=utf-8,${encodeURIComponent(startupSplashHtml(''))}`;
}

function startupSplashUrlForWindow() {
  try {
    const splashDir = path.join(app.getPath('userData'), 'startup');
    fs.mkdirSync(splashDir, { recursive: true });
    const iconPath = startupBrandIconPath();
    let iconFileName = '';
    if (iconPath) {
      const iconExt = path.extname(iconPath).toLowerCase() || '.png';
      iconFileName = `startup-icon${iconExt}`;
      fs.copyFileSync(iconPath, path.join(splashDir, iconFileName));
    }
    const htmlPath = path.join(splashDir, 'startup-splash.html');
    fs.writeFileSync(htmlPath, startupSplashHtml(iconFileName), 'utf8');
    return pathToFileURL(htmlPath).href;
  } catch (error) {
    writeLog('warn', 'Startup splash file could not be prepared', {
      error: error instanceof Error ? error.message : String(error)
    });
    return startupSplashDataUrl();
  }
}

function waitForStartupPreload(promise, timeoutMs = STARTUP_PRELOAD_TIMEOUT_MS) {
  if (!promise) return Promise.resolve({ status: 'skipped' });
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      writeLog('warn', 'Startup preload timed out; renderer will continue while preload finishes', {
        timeoutMs,
        state: startupPreloadState.status
      });
      resolve({ status: 'timeout', timeoutMs });
    }, timeoutMs);
    promise
      .then((result) => resolve(result))
      .catch((error) => resolve({
        status: 'failed',
        error: error instanceof Error ? error.message : String(error)
      }))
      .finally(() => clearTimeout(timer));
  });
}

function startStartupPreload(reason = 'startup') {
  if (startupPreloadPromise) return startupPreloadPromise;
  const startedAt = Date.now();
  startupPreloadState = { status: 'running', reason, startedAt };
  startupPreloadPromise = Promise.allSettled([
    locateClaude(),
    collectBackendStatus(null, { refresh: false, lightweight: true }),
    Promise.resolve().then(() => collectCapabilities()),
    Promise.resolve().then(() => publicAuthSession(readAuthSession({ refresh: true })))
  ]).then((results) => {
    const [claudeResult, backendResult, capabilitiesResult, authResult] = results;
    startupPreloadState = {
      status: 'ready',
      reason,
      durationMs: Date.now() - startedAt,
      claudeReady: claudeResult.status === 'fulfilled' && Boolean(claudeResult.value?.command),
      backendReady: backendResult.status === 'fulfilled' && Boolean(backendResult.value?.ok),
      capabilitiesReady: capabilitiesResult.status === 'fulfilled' && capabilitiesResult.value?.ok !== false,
      loggedIn: authResult.status === 'fulfilled' && Boolean(authResult.value?.loggedIn)
    };
    writeLog('info', 'Startup preload completed', startupPreloadState);
    return startupPreloadState;
  }).catch((error) => {
    startupPreloadState = {
      status: 'failed',
      reason,
      durationMs: Date.now() - startedAt,
      error: error instanceof Error ? error.message : String(error)
    };
    writeLog('warn', 'Startup preload failed', startupPreloadState);
    return startupPreloadState;
  });
  return startupPreloadPromise;
}

function createWindow() {
  const bounds = defaultWindowBounds();
  let startupMaximizeApplied = false;
  function revealStartupWindow(reason) {
    if (!mainWindow) return;
    if (!startupMaximizeApplied) {
      startupMaximizeApplied = true;
      try {
        mainWindow.maximize();
      } catch (error) {
        writeLog('warn', 'Startup maximize failed', {
          reason,
          error: error instanceof Error ? error.message : String(error)
        });
      }
    }
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
  }

  mainWindow = new BrowserWindow({
    width: bounds.width,
    height: bounds.height,
    minWidth: 800,
    minHeight: 600,
    center: true,
    title: 'EcoreX Agent',
    backgroundColor: '#070c12',
    icon: devPath('build', 'icon.ico'),
    frame: false,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  mainWindow.on('closed', () => {
    stopAllAgents('window-closed');
    clearRendererUnresponsiveRecovery();
    mainWindow = null;
  });

  revealStartupWindow('created-dark-shell');

  mainWindow.on('ready-to-show', () => {
    revealStartupWindow('ready-to-show');
    writeLog('info', 'Main window ready to show', windowDiagnosticSnapshot(mainWindow));
  });

  mainWindow.on('show', () => {
    writeLog('info', 'Main window shown', windowDiagnosticSnapshot(mainWindow));
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    writeLog('warn', 'Blocked window.open', { url });
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedRendererUrl(url)) {
      event.preventDefault();
      writeLog('warn', 'Blocked navigation', { url });
    }
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    clearRendererUnresponsiveRecovery();
    recordCrashEvent('renderer-gone', {
      reason: details?.reason,
      exitCode: details?.exitCode,
      details
    });
    writeLog('error', 'Renderer process gone', {
      ...windowDiagnosticSnapshot(mainWindow),
      details
    });
    stopAllAgents('renderer-gone');
    recoverRendererAfterCrash(details);
  });
  mainWindow.webContents.on('unresponsive', () => {
    rendererUnresponsiveSince = Date.now();
    recordCrashEvent('renderer-unresponsive', {
      reason: 'unresponsive'
    });
    writeLog('warn', 'Renderer became unresponsive', windowDiagnosticSnapshot(mainWindow));
    armRendererUnresponsiveRecovery();
  });
  mainWindow.webContents.on('responsive', () => {
    recordCrashEvent('renderer-responsive', {
      unresponsiveMs: rendererUnresponsiveSince ? Date.now() - rendererUnresponsiveSince : 0
    });
    clearRendererUnresponsiveRecovery();
    writeLog('info', 'Renderer became responsive', windowDiagnosticSnapshot(mainWindow));
  });
  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    writeLog('error', 'Renderer failed to load', {
      ...windowDiagnosticSnapshot(mainWindow),
      errorCode,
      errorDescription,
      validatedURL,
      isMainFrame
    });
  });
  mainWindow.webContents.on('did-fail-provisional-load', (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    writeLog('error', 'Renderer provisional load failed', {
      ...windowDiagnosticSnapshot(mainWindow),
      errorCode,
      errorDescription,
      validatedURL,
      isMainFrame
    });
  });
  mainWindow.webContents.on('did-finish-load', () => {
    revealStartupWindow('did-finish-load');
    writeLog('info', 'Renderer finished loading', windowDiagnosticSnapshot(mainWindow));
  });

  writeLog('info', 'Main window created', windowDiagnosticSnapshot(mainWindow));

  async function loadRendererEntry() {
    startupSplashUrl = '';
    if (app.isPackaged) {
      const target = rendererEntryPath();
      mainWindow.loadFile(target).catch((error) => {
        writeLog('error', 'mainWindow.loadFile failed', {
          ...windowDiagnosticSnapshot(mainWindow),
          target,
          error: error instanceof Error ? error.message : String(error)
        });
      });
      return;
    }

    const target = devRendererUrl();
    const devRendererReady = await probeDevRenderer(target);
    if (!devRendererReady && fs.existsSync(rendererEntryPath())) {
      writeLog('warn', 'Dev renderer server unavailable; falling back to built renderer', {
        target,
        fallback: rendererEntryPath()
      });
      mainWindow.loadFile(rendererEntryPath()).catch((error) => {
        writeLog('error', 'mainWindow.loadFile fallback failed', {
          ...windowDiagnosticSnapshot(mainWindow),
          target: rendererEntryPath(),
          error: error instanceof Error ? error.message : String(error)
        });
      });
      return;
    }
    mainWindow.loadURL(target).catch((error) => {
      writeLog('error', 'mainWindow.loadURL failed', {
        ...windowDiagnosticSnapshot(mainWindow),
        target,
        error: error instanceof Error ? error.message : String(error)
      });
    });
    if (process.env.ECOREX_OPEN_DEVTOOLS === '1') {
      mainWindow.webContents.openDevTools({ mode: 'detach' });
    }
  }

  startupSplashUrl = startupSplashUrlForWindow();
  mainWindow.loadURL(startupSplashUrl)
    .then(async () => {
      revealStartupWindow('startup-splash-loaded');
      const startupPreload = startStartupPreload('native-loading');
      await waitForStartupPreload(startupPreload);
      loadRendererEntry();
    })
    .catch((error) => {
      revealStartupWindow('startup-splash-failed');
      const startupPreload = startStartupPreload('native-loading');
      writeLog('warn', 'Startup splash failed, loading renderer directly', {
        ...windowDiagnosticSnapshot(mainWindow),
        error: error instanceof Error ? error.message : String(error)
      });
      waitForStartupPreload(startupPreload).finally(loadRendererEntry);
    });
}

app.whenReady().then(createWindow);

app.whenReady().then(() => {
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const responseUrl = String(details.url || '');
    if (/^http:\/\/127\.0\.0\.1:(?!5188(?:\/|$))/.test(responseUrl)) {
      callback({ responseHeaders: details.responseHeaders });
      return;
    }
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          app.isPackaged
            ? "default-src 'self'; img-src 'self' data: https: http://127.0.0.1:*; media-src 'self' data: blob: https: http://127.0.0.1:*; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' http://127.0.0.1:*; frame-src 'self' http://127.0.0.1:*"
            : "default-src 'self' http://127.0.0.1:5188 ws://127.0.0.1:5188; img-src 'self' data: https: http://127.0.0.1:*; media-src 'self' data: blob: https: http://127.0.0.1:*; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-eval'; connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:5188; frame-src 'self' http://127.0.0.1:*"
        ]
      }
    });
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  stopAllAgents('app-quit');
  stopVueOfficePreviewServer('app-quit');
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

process.on('uncaughtException', (error) => {
  recordCrashEvent('main-uncaught-exception', errorCrashDetails(error));
  writeLog('error', 'Main process uncaughtException', {
    error: safeOutputText(error?.message || '', 2000),
    stack: safeOutputText(error?.stack || '', 8000)
  });
});

process.on('unhandledRejection', (reason) => {
  recordCrashEvent('main-unhandled-rejection', errorCrashDetails(reason));
  writeLog('error', 'Main process unhandledRejection', {
    error: safeOutputText(reason instanceof Error ? reason.message : String(reason), 2000),
    stack: reason instanceof Error ? safeOutputText(reason.stack || '', 8000) : undefined
  });
});

function normalizeAgentState(kind = 'debug', status = '') {
  const value = `${kind || ''} ${status || ''}`.toLowerCase();
  if (/cancel/.test(value)) return 'cancelled';
  if (/(complete|completed|done|success|result)/.test(value)) return 'completed';
  if (/(fail|failed|error|stderr|timeout)/.test(value)) return 'failed';
  if (/(stop|stopped|idle)/.test(value)) return 'stopped';
  if (/(start|started|running|tool|hook|assistant|system|init|progress)/.test(value)) return 'running';
  return 'info';
}

function normalizeAgentStatus(kind = 'debug', status = '') {
  const raw = String(status || kind || 'event').trim().toLowerCase().replace(/[^a-z0-9_.:-]+/g, '-');
  if (!raw) return 'event';
  if (/^(completed|complete|done|success|result)$/.test(raw)) return 'completed';
  if (/^(failed|fail|error|stderr)$/.test(raw)) return 'failed';
  if (/^(cancelled|canceled|cancel)$/.test(raw)) return 'cancelled';
  if (/^(timeout|timed-out|idle-timeout)$/.test(raw)) return 'timeout';
  if (/^(stopped|stop)$/.test(raw)) return 'stopped';
  if (/^(started|start|init|initializing)$/.test(raw)) return 'started';
  if (/^(assistant|tool|hook|running|progress|system)$/.test(raw)) return 'running';
  return raw.slice(0, 80);
}

function agentTaskType(kind = 'debug') {
  if (kind === 'assistant') return 'message';
  if (kind === 'tool') return 'tool';
  if (kind === 'result') return 'result';
  if (kind === 'stderr' || kind === 'error') return 'diagnostic';
  return 'session';
}

function publicAgentToolName(tool = '') {
  const rawName = typeof tool === 'string' ? tool : tool?.name;
  const name = publicProductText(String(rawName || '').trim());
  if (!name) return 'EcoreX 原生能力';
  if (/^mcp__/i.test(name)) return 'MCP';
  if (/^Skill$/i.test(name)) return 'SKILLS';
  if (/^ToolSearch$/i.test(name)) return 'ToolSearch';
  return name.slice(0, 120);
}

function publicAgentToolLabel(tool = '') {
  const rawName = typeof tool === 'string' ? tool : tool?.name;
  const name = String(rawName || '').trim();
  const input = typeof tool === 'object' && tool ? tool.input || {} : {};
  const query = String(input?.query || '').trim();
  if (/^ToolSearch$/i.test(name) && /WebSearch/i.test(query)) return '准备联网检索';
  if (/^ToolSearch$/i.test(name) && /WebFetch/i.test(query)) return '准备读取网页';
  if (/^ToolSearch$/i.test(name)) return '准备调用工具';
  if (/^WebSearch$/i.test(name)) return '联网检索';
  if (/^WebFetch$/i.test(name)) return '读取网页';
  if (/^TodoWrite$/i.test(name)) return '更新任务清单';
  if (/^TodoRead$/i.test(name)) return '读取任务清单';
  if (/^Task$|^TaskCreate$|^SendMessage$/i.test(name)) return '调度子 Agent';
  if (/^Read$|^Grep$|^Glob$|^LS$|^NotebookRead$/i.test(name)) return '查看文件';
  if (/^Write$|^Edit$|^MultiEdit$|^NotebookEdit$/i.test(name)) return '准备修改文件';
  if (/^Bash$|^PowerShell$|^Cmd$/i.test(name)) return '执行本地命令';
  if (/^mcp__/i.test(name)) return '调用 MCP';
  if (/^Skill$/i.test(name)) return '调用 SKILLS';
  if (/^Cron|^Monitor$/i.test(name)) return '管理后台任务';
  if (/^EnterPlanMode$|^ExitPlanMode$/i.test(name)) return '更新计划状态';
  return publicProductText(name || '调用原生能力').slice(0, 120);
}

function publicAgentToolInput(toolName, input) {
  const name = String(toolName || '').trim();
  if (!input || typeof input !== 'object') return undefined;
  if (/^Skill$|^ToolSearch$/i.test(name) || /^mcp__/i.test(name)) return undefined;
  return safeJsonValue(input, 4000);
}

function safeLedgerText(value = '', limit = MAX_TOOL_LEDGER_SUMMARY_CHARS) {
  return safeOutputText(value, limit)
    .replace(/\b[A-Za-z]:\\(?:[^\\\s"'<>|]+\\)*[^\\\s"'<>|]+/g, '[local-path]')
    .replace(/\/(?:Users|home|var|tmp|etc|Volumes)\/[^\s"'<>]+/g, '[local-path]');
}

function summarizeToolInput(input = {}) {
  if (!input || typeof input !== 'object') return '';
  return safeLedgerText(JSON.stringify(redactForLog(input)), MAX_TOOL_LEDGER_SUMMARY_CHARS);
}

function summarizeToolOutput(value = '') {
  if (Array.isArray(value)) {
    return safeLedgerText(value.map((item) => item?.text || item?.content || '').join('\n'), MAX_TOOL_LEDGER_SUMMARY_CHARS);
  }
  if (value && typeof value === 'object') {
    return safeLedgerText(JSON.stringify(redactForLog(value)), MAX_TOOL_LEDGER_SUMMARY_CHARS);
  }
  return safeLedgerText(String(value || ''), MAX_TOOL_LEDGER_SUMMARY_CHARS);
}

function inferToolAction(toolName = '', input = {}) {
  const name = String(toolName || '').trim();
  if (/^WebSearch$/i.test(name)) return 'web-search';
  if (/^WebFetch$/i.test(name)) return 'web-fetch';
  if (/^Read$|^NotebookRead$/i.test(name)) return 'file-read';
  if (/^Write$|^Edit$|^MultiEdit$|^NotebookEdit$/i.test(name)) return 'file-write';
  if (/^Bash$|^PowerShell$|^Cmd$/i.test(name)) return 'command';
  if (/^Grep$|^Glob$|^LS$/i.test(name)) return 'file-discovery';
  if (/^Todo(Read|Write)$/i.test(name)) return 'task-list';
  if (/^Task$|^TaskCreate$|^SendMessage$/i.test(name)) return 'sub-agent';
  if (/^mcp__/i.test(name)) return 'mcp';
  if (/^ToolSearch$/i.test(name)) return String(input?.query || '').toLowerCase().includes('web') ? 'tool-search-web' : 'tool-search';
  return 'tool';
}

function toolLedgerMapForSession(sessionId) {
  const key = String(sessionId || 'global');
  let map = agentToolLedger.get(key);
  if (!map) {
    map = new Map();
    agentToolLedger.set(key, map);
  }
  return map;
}

function pruneToolLedgerMap(map) {
  while (map.size > MAX_TOOL_LEDGER_ACTIVE_ENTRIES) {
    const oldestKey = map.keys().next().value;
    if (!oldestKey) break;
    map.delete(oldestKey);
  }
}

function toolLedgerStartEvent(sessionId, tool = {}) {
  const toolUseId = String(tool.id || crypto.randomUUID());
  const toolName = String(tool.name || 'tool').trim() || 'tool';
  const now = Date.now();
  const entry = {
    toolUseId,
    toolName,
    action: inferToolAction(toolName, tool.input || {}),
    inputSummary: summarizeToolInput(tool.input || {}),
    startedAt: now
  };
  const sessionMap = toolLedgerMapForSession(sessionId);
  sessionMap.set(toolUseId, entry);
  pruneToolLedgerMap(sessionMap);
  return {
    type: 'tool',
    phase: 'start',
    toolUseId: publicStableId('tool-use', toolUseId),
    toolName: publicAgentToolName(toolName),
    action: entry.action,
    inputSummary: entry.inputSummary,
    startedAt: new Date(now).toISOString()
  };
}

function toolLedgerFinishEvent(sessionId, toolUseId, result = {}) {
  const sessionMap = toolLedgerMapForSession(sessionId);
  const rawToolUseId = String(toolUseId || '').trim();
  const started = sessionMap.get(rawToolUseId);
  if (rawToolUseId) sessionMap.delete(rawToolUseId);
  if (!sessionMap.size) agentToolLedger.delete(String(sessionId || 'global'));
  const now = Date.now();
  const toolName = started?.toolName || result.toolName || 'tool';
  const failed = Boolean(result.failed || result.isError || result.error);
  return {
    type: 'tool',
    phase: 'finish',
    toolUseId: rawToolUseId ? publicStableId('tool-use', rawToolUseId) : undefined,
    toolName: publicAgentToolName(toolName),
    action: started?.action || inferToolAction(toolName, {}),
    inputSummary: started?.inputSummary || '',
    outputSummary: summarizeToolOutput(result.output || result.text || ''),
    status: failed ? 'failed' : 'completed',
    exit: result.exitCode ?? undefined,
    error: failed ? safeLedgerText(result.error || result.text || 'tool failed', 500) : undefined,
    startedAt: started?.startedAt ? new Date(started.startedAt).toISOString() : undefined,
    endedAt: new Date(now).toISOString(),
    durationMs: started?.startedAt ? now - started.startedAt : undefined
  };
}

function safeToolLedger(ledger = {}) {
  if (!ledger || typeof ledger !== 'object') return undefined;
  return {
    type: ledger.type === 'tool' ? 'tool' : 'task',
    phase: safeLedgerText(ledger.phase || '', 40),
    toolUseId: ledger.toolUseId || undefined,
    toolName: ledger.toolName ? safeLedgerText(ledger.toolName, 120) : undefined,
    action: ledger.action ? safeLedgerText(ledger.action, 80) : undefined,
    inputSummary: ledger.inputSummary ? safeLedgerText(ledger.inputSummary) : undefined,
    outputSummary: ledger.outputSummary ? safeLedgerText(ledger.outputSummary) : undefined,
    status: ledger.status ? safeLedgerText(ledger.status, 80) : undefined,
    exit: ledger.exit,
    error: ledger.error ? safeLedgerText(ledger.error, 500) : undefined,
    startedAt: ledger.startedAt || undefined,
    endedAt: ledger.endedAt || undefined,
    durationMs: Number.isFinite(Number(ledger.durationMs)) ? Number(ledger.durationMs) : undefined
  };
}

function safeToolLedgerValue(ledger) {
  if (Array.isArray(ledger)) {
    return ledger
      .map((item) => safeToolLedger(item))
      .filter(Boolean)
      .slice(0, 20);
  }
  return safeToolLedger(ledger);
}

function inferredToolLedgerFromPayload(sessionId, payload = {}, status = '') {
  if (payload.ledger || payload.kind !== 'tool') return undefined;
  const isTerminal = status === 'completed' || status === 'failed';
  if (!isTerminal || !payload.toolUseId) return undefined;
  return toolLedgerFinishEvent(sessionId, payload.toolUseId, {
    toolName: payload.toolName || payload.name || 'tool',
    failed: status === 'failed',
    output: payload.text || '',
    text: payload.text || '',
    error: status === 'failed' ? payload.text || '' : ''
  });
}

function normalizeAgentEvent(payload = {}, options = {}) {
  const now = new Date().toISOString();
  const kind = String(payload.kind || 'debug').trim() || 'debug';
  const detailStatus = payload.status ? String(payload.status).trim() : '';
  const status = normalizeAgentStatus(kind, detailStatus);
  const state = normalizeAgentState(kind, status || detailStatus);
  const sessionId = String(payload.sessionId || '').trim();
  const taskType = payload.task?.type || agentTaskType(kind);
  const rawTaskName =
    payload.task?.name ||
    payload.toolName ||
    payload.name ||
    (taskType === 'session' ? 'Agent session' : kind);
  const taskName = taskType === 'tool' ? publicAgentToolLabel(rawTaskName) : publicProductText(rawTaskName);
  const taskId =
    payload.task?.id ||
    payload.taskId ||
    `${sessionId || 'agent'}:${taskType}:${String(taskName).toLowerCase().replace(/[^a-z0-9_.:-]+/g, '-').slice(0, 80)}`;
  const text = safeOutputText(payload.text || '', options.textLimit || MAX_AGENT_EVENT_TEXT_CHARS);
  const ledger = payload.ledger || inferredToolLedgerFromPayload(sessionId, payload, status);
  const safeTools = Array.isArray(payload.tools)
    ? payload.tools.slice(0, 20).map((tool, index) => ({
        id: tool?.id ? publicStableId('capability', tool.id) : `capability-${index + 1}`,
        name: publicAgentToolName(tool),
        status: tool?.status,
        input: publicAgentToolInput(tool?.name, tool?.input)
      }))
    : undefined;
  const event = {
    ...payload,
    __seq: payload.__seq || ++agentEventSequence,
    sessionId,
    kind,
    time: payload.time || now,
    status,
    state,
    detailStatus: detailStatus && detailStatus !== status ? detailStatus : undefined,
    taskId,
    parentTaskId: payload.parentTaskId || payload.task?.parentId || sessionId || null,
    task: {
      id: taskId,
      parentId: payload.parentTaskId || payload.task?.parentId || sessionId || null,
      type: taskType,
      name: safeOutputText(taskName, 120),
      status,
      state
    },
    text
  };

  if (safeTools) event.tools = safeTools;
  if (ledger) event.ledger = safeToolLedgerValue(ledger);
  if (event.toolName) event.toolName = publicAgentToolLabel(event.toolName);
  if (options.includeBackend !== true) {
    delete event.permissionCliMode;
    delete event.permissionCliFlags;
  }

  if (Object.prototype.hasOwnProperty.call(payload, 'raw')) {
    if (options.includeRaw === false) {
      delete event.raw;
    } else {
      event.raw = safeJsonValue(payload.raw, MAX_AGENT_EVENT_RAW_CHARS);
    }
  }
  return event;
}

function setAgentStreamsPaused(sessionId, paused) {
  const entry = runningAgents.get(sessionId);
  if (!entry?.child || entry.streamsPaused === paused) return;
  for (const stream of [entry.child.stdout, entry.child.stderr]) {
    try {
      if (paused && typeof stream?.pause === 'function') stream.pause();
      if (!paused && typeof stream?.resume === 'function') stream.resume();
    } catch {
      // Stream pause/resume is best-effort; process lifecycle remains authoritative.
    }
  }
  entry.streamsPaused = paused;
  writeLog(paused ? 'warn' : 'info', paused ? 'Agent event stream paused' : 'Agent event stream resumed', {
    sessionId,
    queuedEvents: agentEventQueues.get(sessionId)?.events?.length || 0
  });
}

function flushAgentEvents(sessionId, options = {}) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const queue = agentEventQueues.get(sessionId);
  if (!queue) return;
  clearTimeout(queue.timer);
  while (queue.events.length) {
    const events = queue.events.splice(0, MAX_AGENT_EVENT_BATCH);
    mainWindow.webContents.send('agent:events', {
      sessionId,
      events,
      count: events.length,
      dropped: queue.dropped || 0,
      flushedAt: new Date().toISOString()
    });
    queue.dropped = 0;
    if (!options.drain) break;
  }
  if (queue.events.length) {
    if (queue.events.length <= AGENT_EVENT_RESUME_LOW_WATER) {
      setAgentStreamsPaused(sessionId, false);
    }
    queue.timer = setTimeout(() => flushAgentEvents(sessionId), AGENT_EVENT_FLUSH_MS);
    agentEventQueues.set(sessionId, queue);
    return;
  }
  setAgentStreamsPaused(sessionId, false);
  agentEventQueues.delete(sessionId);
}

function flushAllAgentEvents() {
  for (const sessionId of Array.from(agentEventQueues.keys())) {
    flushAgentEvents(sessionId, { drain: true });
  }
}

function emitAgentEvent(payload, options = {}) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const event = normalizeAgentEvent(payload, { includeRaw: false });
  const sessionId = event.sessionId || 'global';
  let queue = agentEventQueues.get(sessionId);
  if (!queue) {
    queue = { events: [], dropped: 0, timer: null };
    agentEventQueues.set(sessionId, queue);
  }
  queue.events.push(event);
  if (queue.events.length > MAX_AGENT_EVENT_QUEUE) {
    setAgentStreamsPaused(sessionId, true);
  }
  if (queue.events.length > HARD_MAX_AGENT_EVENT_QUEUE) {
    const retainedEventCount = HARD_MAX_AGENT_EVENT_QUEUE - 1;
    const droppedEvents = queue.events.length - retainedEventCount;
    queue.dropped += droppedEvents;
    queue.events = [
      normalizeAgentEvent({
        sessionId,
        kind: 'status',
        status: 'backpressure',
        text: `EcoreX buffered ${droppedEvents} older events under renderer backpressure. Session transcript retained the durable summary.`
      }, { includeRaw: false }),
      ...queue.events.slice(-retainedEventCount)
    ];
    writeLog('error', 'Agent event queue hard limit reached', { sessionId, droppedEvents });
  }
  if (queue.events.length >= AGENT_EVENT_PAUSE_HIGH_WATER) {
    setAgentStreamsPaused(sessionId, true);
  }
  if (options.immediate || queue.events.length >= MAX_AGENT_EVENT_BATCH) {
    flushAgentEvents(sessionId, { drain: Boolean(options.immediate) });
    return;
  }
  if (!queue.timer) {
    queue.timer = setTimeout(() => flushAgentEvents(sessionId), AGENT_EVENT_FLUSH_MS);
  }
}

function clearAgentTimers(entry) {
  if (!entry) return;
  clearTimeout(entry.totalTimer);
  clearTimeout(entry.idleTimer);
}

function armIdleTimer(sessionId) {
  const entry = runningAgents.get(sessionId);
  if (!entry) return;
  clearTimeout(entry.idleTimer);
  entry.idleTimer = setTimeout(() => {
    stopAgent(sessionId, 'idle-timeout');
  }, AGENT_IDLE_TIMEOUT_MS);
}

function killProcessTree(child) {
  if (!child || child.killed) return;
  if (isWindows && child.pid) {
    const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore'
    });
    const fallbackKill = () => {
      try {
        child.kill();
      } catch {
        // Ignore process cleanup races.
      }
    };
    killer.on('error', fallbackKill);
    killer.on('close', (code) => {
      if (code !== 0) fallbackKill();
    });
    return;
  }

  try {
    child.kill('SIGTERM');
    setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        try {
          child.kill('SIGKILL');
        } catch {
          // Ignore process cleanup races.
        }
      }
    }, 2500);
  } catch {
    // Ignore process cleanup races.
  }
}

function publicPromptPreview(prompt = '') {
  return safeTranscriptText(prompt).slice(0, MAX_PROMPT_PREVIEW_CHARS);
}

function agentRecoveryHint(codeOrStatus = '', details = {}) {
  const code = String(codeOrStatus || details.code || details.status || details.reason || '').toLowerCase();
  if (code === 'too-many-sessions') {
    return `Close or cancel one running session, then retry. Up to ${details.maxRunning || MAX_RUNNING_AGENTS} sessions can run at once.`;
  }
  if (code === 'duplicate-session' || code === 'duplicate-start') {
    return 'This task is already starting or running. Switch to the active session or wait a moment before retrying.';
  }
  if (code === 'timeout' || code === 'idle-timeout') {
    return 'The task timed out. Narrow the request, split it into smaller steps, or increase the timeout before retrying.';
  }
  if (code === 'cancelled' || code === 'canceled') {
    return 'The task was cancelled. Review the latest partial output and start a new task when ready.';
  }
  if (code === 'not-found') {
    return 'The session is no longer running. Refresh session status before retrying.';
  }
  return 'The task did not finish. Check the runtime status and retry with a smaller prompt if needed.';
}

function duplicateAgentSessionResponse(sessionId, requestedSessionId, owner = {}) {
  return {
    ok: false,
    sessionId,
    requestedSessionId,
    duplicateOf: sessionId,
    code: 'duplicate-session',
    status: 'running',
    state: 'running',
    conversationId: owner.conversationId || '',
    claudeSessionId: owner.claudeSessionId || '',
    messageId: owner.messageId || '',
    projectId: owner.projectId || null,
    projectName: owner.projectName || '',
    error: 'This conversation already has a task running.',
    recoveryHint: agentRecoveryHint('duplicate-session')
  };
}

function publicWorkspacePath(cwd) {
  const workspaceRoot = readSettings().workspaceRoot;
  const target = path.resolve(String(cwd || workspaceRoot));
  if (!isPathInside(workspaceRoot, target)) return 'workspace:/';
  const relative = path.relative(workspaceRoot, target).replace(/\\/g, '/');
  return relative ? `workspace:/${relative}` : 'workspace:/';
}

function formatDuration(ms) {
  const seconds = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function agentStartSignature(payload = {}) {
  return crypto
    .createHash('sha256')
    .update(
      JSON.stringify({
        prompt: crypto.createHash('sha256').update(payload.prompt || '').digest('hex'),
        cwd: path.resolve(payload.cwd || defaultAgentCwd()),
        model: payload.model,
        accessMode: payload.accessMode,
        permissionMode: payload.permissionMode,
        permissionCliMode: payload.permissionCliMode,
        permissionCliFlags: payload.permissionCliFlags,
        projectId: payload.projectId || null,
        projectMemory: payload.projectContext?.memoryLabel || '',
        plugins: payload.plugins || []
      })
    )
    .digest('hex');
}

function pruneAgentStartLocks(now = Date.now()) {
  for (const [sessionId, entry] of pendingAgentStarts.entries()) {
    if (now - entry.startedAt > AGENT_START_PENDING_TTL_MS) {
      entry.cancelled = true;
      entry.cancelReason = 'start-timeout';
      pendingAgentStarts.delete(sessionId);
    }
  }
  for (const [ownerId, entry] of recentAgentStartsByWindow.entries()) {
    if (now - entry.startedAt > AGENT_START_DEBOUNCE_MS) {
      recentAgentStartsByWindow.delete(ownerId);
    }
  }
}

function claimAgentStart(payload = {}, options = {}) {
  const now = Date.now();
  pruneAgentStartLocks(now);

  const exactSessionOwner = runningAgents.get(payload.sessionId) || pendingAgentStarts.get(payload.sessionId);
  if (exactSessionOwner) {
    return duplicateAgentSessionResponse(payload.sessionId, payload.sessionId, exactSessionOwner);
  }

  if (runningAgents.has(payload.sessionId) || pendingAgentStarts.has(payload.sessionId)) {
    return {
      ok: false,
      sessionId: payload.sessionId,
      code: 'duplicate-session',
      status: 'running',
      state: 'running',
      error: '该会话已有任务正在运行。',
      recoveryHint: agentRecoveryHint('duplicate-session')
    };
  }

  const requestedClaudeSessionId = String(payload.claudeSessionId || '').trim();
  if (requestedClaudeSessionId) {
    const activeClaudeSession = Array.from(runningAgents.entries()).find(([, entry]) => entry.claudeSessionId === requestedClaudeSessionId);
    const pendingClaudeSession = Array.from(pendingAgentStarts.entries()).find(([, entry]) => entry.claudeSessionId === requestedClaudeSessionId);
    const duplicateClaudeSession = activeClaudeSession || pendingClaudeSession;
    if (duplicateClaudeSession) {
      return duplicateAgentSessionResponse(duplicateClaudeSession[0], payload.sessionId, duplicateClaudeSession[1] || {});
    }
    if (duplicateClaudeSession) {
      return {
        ok: false,
        sessionId: duplicateClaudeSession[0],
        requestedSessionId: payload.sessionId,
        duplicateOf: duplicateClaudeSession[0],
        code: 'duplicate-session',
        status: 'running',
        state: 'running',
        error: '该会话已有任务正在运行。',
        recoveryHint: agentRecoveryHint('duplicate-session')
      };
    }
  }

  const bindingConflict = claudeSessionBindingConflict(payload);
  if (bindingConflict) {
    writeLog('warn', 'Blocked Claude session reuse across isolated context', {
      claudeSessionId: publicStableId('claude-session', payload.claudeSessionId),
      existingConversationId: publicStableId('conversation', bindingConflict.existing.conversationId || ''),
      nextConversationId: publicStableId('conversation', bindingConflict.next.conversationId || ''),
      existingProjectId: bindingConflict.existing.projectId || null,
      nextProjectId: bindingConflict.next.projectId || null,
      existingCwd: publicWorkspacePath(bindingConflict.existing.cwd),
      nextCwd: publicWorkspacePath(bindingConflict.next.cwd)
    });
    return {
      ok: false,
      sessionId: payload.sessionId,
      code: 'session-context-conflict',
      status: 'rejected',
      state: 'stopped',
      error: 'This conversation is already bound to a different workspace context.',
      recoveryHint: '请为该项目新建会话，或重新打开原项目会话后继续。'
    };
  }

  const activeSessionCount = runningAgents.size + pendingAgentStarts.size;
  if (activeSessionCount >= MAX_RUNNING_AGENTS) {
    return {
      ok: false,
      sessionId: payload.sessionId,
      code: 'too-many-sessions',
      status: 'rejected',
      state: 'stopped',
      runningCount: activeSessionCount,
      maxRunning: MAX_RUNNING_AGENTS,
      error: `Too many agent sessions are running. Limit is ${MAX_RUNNING_AGENTS}.`,
      recoveryHint: agentRecoveryHint('too-many-sessions', { maxRunning: MAX_RUNNING_AGENTS })
    };
  }

  const ownerId = String(options.ownerId || 'main-window');
  const signature = agentStartSignature(payload);
  const recent = recentAgentStartsByWindow.get(ownerId);
  if (recent && recent.signature === signature && now - recent.startedAt <= AGENT_START_DEBOUNCE_MS) {
    return {
      ok: false,
      sessionId: recent.sessionId,
      requestedSessionId: payload.sessionId,
      duplicateOf: recent.sessionId,
      code: 'duplicate-start',
      status: 'ignored',
      state: 'stopped',
      error: 'Duplicate agent start ignored.',
      recoveryHint: agentRecoveryHint('duplicate-start')
    };
  }

  const lock = {
    ownerId,
    signature,
    startedAt: now,
    lastActivityAt: now,
    status: 'starting',
    state: 'running',
    runtimeKind: AGENT_RUNTIME_KIND,
    promptPreview: publicPromptPreview(payload.userPrompt || payload.prompt),
    workspacePath: publicWorkspacePath(payload.cwd),
    model: payload.model,
    conversationId: payload.conversationId || '',
    messageId: payload.messageId || '',
    accessMode: payload.accessMode,
    permissionMode: payload.permissionMode,
    permissionCliMode: payload.permissionCliMode,
    permissionCliFlags: payload.permissionCliFlags,
    permissionLabel: payload.permissionLabel,
    permissionPolicy: payload.permissionPolicy,
    claudeSessionId: payload.claudeSessionId,
    projectId: payload.projectId || null,
    projectName: payload.projectContext?.name || '',
    projectPath: payload.projectContext?.pathLabel || '',
    projectMemoryLabel: payload.projectContext?.memoryLabel || '',
    cancelled: false,
    cancelReason: null
  };
  pendingAgentStarts.set(payload.sessionId, lock);
  recentAgentStartsByWindow.set(ownerId, {
    ownerId,
    signature,
    sessionId: payload.sessionId,
    startedAt: now
  });
  return { ok: true, lock };
}

function releaseAgentStart(sessionId, lock) {
  if (!lock || pendingAgentStarts.get(sessionId) === lock) {
    pendingAgentStarts.delete(sessionId);
  }
}

function agentFinalStatus(reason = 'stopped') {
  if (reason === 'completed') return 'completed';
  if (reason === 'cancelled' || reason === 'canceled') return 'cancelled';
  if (reason === 'timeout' || reason === 'idle-timeout') return 'timeout';
  if (reason === 'failed' || reason === 'error') return 'failed';
  return 'stopped';
}

function agentFinalKind(status) {
  if (status === 'completed') return 'done';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'timeout') return 'timeout';
  if (status === 'stopped') return 'status';
  return 'error';
}

function agentFinalText(status, details = {}) {
  if (status === 'completed') return 'done';
  if (status === 'cancelled') return '任务已取消。';
  if (status === 'timeout') return details.reason === 'idle-timeout' ? '任务空闲超时。' : '任务已超时。';
  if (details.code !== undefined && details.code !== null) return '本地执行进程异常退出。请检查任务状态后重试。';
  if (details.reason) return `任务已停止：${details.reason}。`;
  return '任务执行失败。';
}

function finalizeAgentSession(sessionId, entry, details = {}) {
  if (!entry || entry.finished) return false;
  entry.finished = true;
  runningAgents.delete(sessionId);
  agentToolLedger.delete(sessionId);
  disposeAgentSessionActor(sessionId, details.reason || details.status || 'stopped');
  clearAgentTimers(entry);
  if (typeof entry.flushBufferedOutput === 'function') {
    entry.flushBufferedOutput();
  }

  const status = details.status || agentFinalStatus(details.reason);
  const event = {
    sessionId,
    kind: agentFinalKind(status),
    status,
    reason: details.reason,
    exitCode: details.code,
    signal: details.signal,
    text: details.text || agentFinalText(status, details),
    recoveryHint: status === 'completed' ? undefined : agentRecoveryHint(status, details)
  };
  recordSessionEvent(entry, event);
  appendRunJournalEntry(sessionId, entry, status, {
    event: 'finish',
    reason: details.reason,
    code: details.code,
    signal: details.signal,
    endedAt: Date.now()
  });
  writeSessionTranscript(sessionId, entry, {
    status,
    reason: details.reason,
    code: details.code,
    signal: details.signal
  });
  emitAgentEvent(event, { immediate: true });
  return true;
}

function stopAgent(sessionId, reason = 'cancelled') {
  const entry = runningAgents.get(sessionId);
  if (!entry) {
    const pending = pendingAgentStarts.get(sessionId);
    if (pending) {
      pending.cancelled = true;
      pending.cancelReason = reason;
      pendingAgentStarts.delete(sessionId);
      const status = agentFinalStatus(reason);
      return { ok: true, reason, status, recoveryHint: agentRecoveryHint(status, { reason }) };
    }
    return { ok: false, reason: 'not-found', status: 'not-found', error: '未找到本地执行会话。', recoveryHint: agentRecoveryHint('not-found') };
  }
  if (entry.transport && typeof entry.transport.stop === 'function') {
    entry.transport.stop(reason);
  } else if (entry.actor && typeof entry.actor.stop === 'function') {
    entry.actor.stop(reason);
  } else {
    killProcessTree(entry.child);
  }
  const status = agentFinalStatus(reason);
  finalizeAgentSession(sessionId, entry, { status, reason });
  writeLog(reason === 'cancelled' ? 'info' : 'warn', 'Agent session stopped', {
    sessionId,
    reason,
    durationMs: Date.now() - entry.startedAt
  });
  return { ok: true, reason, status, recoveryHint: agentRecoveryHint(status, { reason }) };
}

function stopAllAgents(reason = 'app-quit') {
  for (const sessionId of Array.from(runningAgents.keys())) {
    stopAgent(sessionId, reason);
  }
  for (const sessionId of Array.from(pendingAgentStarts.keys())) {
    stopAgent(sessionId, reason);
  }
  flushAllAgentEvents();
}

function defaultCommandCwd() {
  if (app.isPackaged && process.resourcesPath && fs.existsSync(process.resourcesPath)) {
    return process.resourcesPath;
  }
  return ROOT_DIR;
}

function commandOutput(command, args = [], options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd || defaultCommandCwd(),
      env: filteredAgentEnv(options.env || {}, { includeSecrets: options.includeSecrets === true }),
      windowsHide: true,
      shell: false
    });

    let stdout = '';
    let stderr = '';
    let truncated = false;
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(result);
    };
    const timeout = setTimeout(() => {
      killProcessTree(child);
      finish({ ok: false, code: -1, stdout, stderr: `${stderr}\nCommand timed out.`.trim(), timedOut: true, truncated });
    }, options.timeoutMs || 15000);

    child.stdout.on('data', (chunk) => {
      if (stdout.length < MAX_COMMAND_OUTPUT_CHARS) {
        stdout += chunk.toString();
        if (stdout.length > MAX_COMMAND_OUTPUT_CHARS) {
          stdout = stdout.slice(0, MAX_COMMAND_OUTPUT_CHARS);
          truncated = true;
        }
      } else {
        truncated = true;
      }
    });
    child.stderr.on('data', (chunk) => {
      if (stderr.length < MAX_COMMAND_OUTPUT_CHARS) {
        stderr += chunk.toString();
        if (stderr.length > MAX_COMMAND_OUTPUT_CHARS) {
          stderr = stderr.slice(0, MAX_COMMAND_OUTPUT_CHARS);
          truncated = true;
        }
      } else {
        truncated = true;
      }
    });
    child.on('error', (error) => {
      finish({ ok: false, code: -1, stdout, stderr: safeOutputText(error.message, 4000), truncated });
    });
    child.on('close', (code) => {
      finish({ ok: code === 0, code, stdout: stdout.trim(), stderr: stderr.trim(), truncated });
    });
  });
}

function nativeClaudePackageName() {
  if (process.platform === 'win32' && process.arch === 'x64') return 'claude-code-win32-x64';
  if (process.platform === 'darwin' && process.arch === 'arm64') return 'claude-code-darwin-arm64';
  if (process.platform === 'darwin' && process.arch === 'x64') return 'claude-code-darwin-x64';
  if (process.platform === 'linux' && process.arch === 'x64') return 'claude-code-linux-x64';
  if (process.platform === 'linux' && process.arch === 'arm64') return 'claude-code-linux-arm64';
  return null;
}

function claudeInvocation(candidate, args = []) {
  const lower = String(candidate).toLowerCase();
  if (lower.endsWith('.ps1')) {
    const baseArgs = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', candidate];
    return { command: 'powershell.exe', baseArgs, args: [...baseArgs, ...args] };
  }
  if (lower.endsWith('.cmd') || lower.endsWith('.bat')) {
    const quoted = `"${String(candidate).replace(/"/g, '""')}"`;
    const baseArgs = ['/d', '/s', '/c', quoted];
    return { command: 'cmd.exe', baseArgs, args: [...baseArgs, ...args] };
  }
  return { command: candidate, baseArgs: [], args };
}

function isPackagedClaudeWrapperStub(candidate) {
  if (!isWindows) return false;
  const value = String(candidate || '');
  const normalized = value.replace(/\\/g, '/').toLowerCase();
  if (!normalized.endsWith('/@anthropic-ai/claude-code/bin/claude.exe')) return false;
  try {
    return fs.existsSync(value) && fs.statSync(value).size < 4096;
  } catch {
    return false;
  }
}

function candidateClaudePaths() {
  const exeName = isWindows ? 'claude.exe' : 'claude';
  const nativePackage = nativeClaudePackageName();
  const candidates = [];

  if (app.isPackaged) {
    if (nativePackage) {
      candidates.push(
        path.join(
          process.resourcesPath,
          'app.asar.unpacked',
          'node_modules',
          '@anthropic-ai',
          nativePackage,
          exeName
        ),
        path.join(
          process.resourcesPath,
          'app.asar.unpacked',
          'node_modules',
          '@anthropic-ai',
          'claude-code',
          'node_modules',
          '@anthropic-ai',
          nativePackage,
          exeName
        )
      );
    }
    candidates.push(
      path.join(
        process.resourcesPath,
        'app.asar.unpacked',
        'node_modules',
        '@anthropic-ai',
        'claude-code',
        'bin',
        exeName
      )
    );
  }

  if (nativePackage) {
    candidates.push(
      devPath('node_modules', '@anthropic-ai', nativePackage, exeName),
      devPath('node_modules', '@anthropic-ai', 'claude-code', 'node_modules', '@anthropic-ai', nativePackage, exeName)
    );
  }
  candidates.push(devPath('node_modules', '@anthropic-ai', 'claude-code', 'bin', exeName));

  if (process.env.APPDATA) {
    if (nativePackage) {
      candidates.push(
        path.join(process.env.APPDATA, 'npm', 'node_modules', '@anthropic-ai', nativePackage, exeName),
        path.join(
          process.env.APPDATA,
          'npm',
          'node_modules',
          '@anthropic-ai',
          'claude-code',
          'node_modules',
          '@anthropic-ai',
          nativePackage,
          exeName
        )
      );
    }
    candidates.push(
      path.join(process.env.APPDATA, 'npm', 'node_modules', '@anthropic-ai', 'claude-code', 'bin', exeName),
      path.join(process.env.APPDATA, 'npm', 'claude.cmd'),
      path.join(process.env.APPDATA, 'npm', 'claude.ps1')
    );
  }

  candidates.push('claude');
  return [...new Set(candidates)];
}

async function locateClaude() {
  if (cachedClaude?.command) return cachedClaude;
  if (cachedClaude && Date.now() - cachedClaudeCheckedAt < 5000) return cachedClaude;
  if (locateClaudeInflight) return locateClaudeInflight;

  locateClaudeInflight = (async () => {
    for (const candidate of candidateClaudePaths()) {
      const looksLikePath = candidate.includes(path.sep) || candidate.endsWith('.cmd') || candidate.endsWith('.ps1');
      if (looksLikePath && !fs.existsSync(candidate)) continue;
      if (looksLikePath && isPackagedClaudeWrapperStub(candidate)) continue;

      const invocation = claudeInvocation(candidate, ['--version']);
      const result = await commandOutput(invocation.command, invocation.args, { timeoutMs: 8000 });
      if (result.ok && result.stdout) {
        cachedClaude = {
          command: invocation.command,
          baseArgs: invocation.baseArgs,
          path: looksLikePath ? candidate : 'PATH: claude',
          version: result.stdout
        };
        cachedClaudeCheckedAt = Date.now();
        return cachedClaude;
      }
    }

    cachedClaude = {
      command: null,
      baseArgs: [],
      path: null,
      version: null
    };
    cachedClaudeCheckedAt = Date.now();
    return cachedClaude;
  })().finally(() => {
    locateClaudeInflight = null;
  });

  return locateClaudeInflight;
}

async function runClaudeCommand(args, options = {}) {
  const claude = await locateClaude();
  if (!claude.command) {
    return { ok: false, code: -1, stdout: '', stderr: '未找到本地执行引擎。' };
  }
  return commandOutput(claude.command, [...claude.baseArgs, ...args], {
    ...options,
    env: {
      ...isolatedAgentRuntimeEnv(),
      ...(options.env || {})
    }
  });
}

function parsePluginInventory() {
  const repoRoot = backendPath('claude-code-main');
  const safeRepoRoot = path.resolve(repoRoot);
  const marketplacePath = path.join(repoRoot, '.claude-plugin', 'marketplace.json');
  let marketplace = { plugins: [] };
  try {
    marketplace = readJsonFileSafe(marketplacePath, { plugins: [] });
  } catch {
    marketplace = { plugins: [] };
  }

  return (marketplace.plugins || []).map((plugin) => {
    const pluginName = String(plugin.name || '').trim();
    const fallbackSource = `plugins/${pluginName.replace(/[^a-zA-Z0-9_.-]/g, '')}`;
    const source = String(plugin.source || '').replace(/^\.\//, '') || fallbackSource;
    if (isBlockedLocalSkillName(pluginName) || isBlockedLocalSkillName(source)) return null;
    const requestedRoot = path.resolve(repoRoot, source);
    const pluginRoot = isPathInside(safeRepoRoot, requestedRoot) ? requestedRoot : path.resolve(repoRoot, fallbackSource);
    const safeList = (relative) => {
      const absolute = path.join(pluginRoot, relative);
      if (!isPathInside(pluginRoot, absolute)) return [];
      return fs.existsSync(absolute) ? fs.readdirSync(absolute, { withFileTypes: true }) : [];
    };
    const commands = safeList('commands').filter((entry) => entry.isFile() && entry.name.endsWith('.md')).length;
    const agents = safeList('agents').filter((entry) => entry.isFile() && entry.name.endsWith('.md')).length;
    const skillsRoot = path.join(pluginRoot, 'skills');
    const skills = fs.existsSync(skillsRoot)
      ? fs
          .readdirSync(skillsRoot, { withFileTypes: true })
          .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(skillsRoot, entry.name, 'SKILL.md'))).length
      : 0;
    const hooks = fs.existsSync(path.join(pluginRoot, 'hooks', 'hooks.json')) ? 1 : 0;
    const bins = safeList('bin').filter((entry) => entry.isFile()).length;
    const readmePath = path.join(pluginRoot, 'README.md');
    const readme = fs.existsSync(readmePath) ? fs.readFileSync(readmePath, 'utf8') : '';
    const summary =
      readme
        .split(/\r?\n/)
        .find((line) => line.trim() && !line.trim().startsWith('#'))
        ?.trim() || plugin.description;

    return {
      name: pluginName,
      category: plugin.category || 'workflow',
      description: plugin.description || summary,
      summary,
      commands,
      agents,
      skills,
      hooks,
      bins,
      source: path.relative(repoRoot, pluginRoot).replace(/\\/g, '/'),
      available: fs.existsSync(pluginRoot)
    };
  }).filter(Boolean);
}

function sourceMapStats() {
  const mapPath = backendPath('cli.js.map');
  if (!fs.existsSync(mapPath)) {
    return { available: false, path: mapPath, sizeMb: 0, sourceCount: 0, categories: [] };
  }

  const stat = fs.statSync(mapPath);
  let sources = [];
  try {
    const fd = fs.openSync(mapPath, 'r');
    const buffer = Buffer.alloc(Math.min(stat.size, 10 * 1024 * 1024));
    fs.readSync(fd, buffer, 0, buffer.length, 0);
    fs.closeSync(fd);
    const head = buffer.toString('utf8');
    const match = head.match(/"sources":\[(.*?)\],"sourcesContent"/s);
    if (match) {
      sources = JSON.parse(`[${match[1]}]`);
    }
  } catch {
    sources = [];
  }

  const buckets = [
    ['核心工具', /src[\\/]tools/i],
    ['MCP 协议', /mcp/i],
    ['Agent 会话', /src[\\/](services|screens|messages|commands)/i],
    ['权限与 Hooks', /(permission|hooks?)/i],
    ['插件系统', /plugin/i],
    ['终端渲染', /(ink|terminal|tui)/i],
    ['SDK/API', /(@anthropic-ai[\\/]sdk|api|client)/i]
  ].map(([label, pattern]) => ({
    label,
    count: sources.filter((source) => pattern.test(source)).length
  }));

  return {
    available: true,
    path: mapPath,
    sizeMb: Number((stat.size / 1024 / 1024).toFixed(1)),
    sourceCount: sources.length,
    categories: buckets
  };
}

function publicSourceMapStats(map = sourceMapStats()) {
  return {
    available: Boolean(map.available),
    sizeMb: map.sizeMb || 0,
    sourceCount: map.sourceCount || 0,
    categories: Array.isArray(map.categories) ? map.categories : []
  };
}

function parseMcpServices(raw = '') {
  if (!raw || /No MCP servers configured/i.test(raw)) return [];

  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/^name\s+/i.test(line) && !/^-{3,}$/.test(line))
    .map((line, index) => {
      const safeLine = safeOutputText(line, 2000);
      const urlMatch = safeLine.match(/https?:\/\/\S+/i);
      const nameMatch = safeLine.match(/^([^\s:|]+)/);
      const status = /(disabled|offline|failed|error|disconnected)/i.test(safeLine) ? 'offline' : 'online';
      const name = nameMatch?.[1] || `Connection ${index + 1}`;
      return {
        id: publicStableId('connection', name),
        name,
        url: urlMatch?.[0] || safeLine.replace(/\s+/g, ' '),
        summary: safeLine.replace(/\s+/g, ' '),
        tags: ['MCP'],
        auth: /oauth/i.test(safeLine) ? 'OAuth 2.0' : 'Local',
        authState: status === 'online' ? '已授权' : '需授权',
        status,
        ping: status === 'online' ? '已连接' : '-',
        sync: '刚刚',
        permissions: '读写'
      };
    });
}

async function collectMcpStatus(payload = {}) {
  const managed = collectManagedSkillInventory({ includeDisabled: true });
  const services = managed.rows
    .filter((row) => row.sourceKind === 'mcp-wrapper')
    .map((row) => {
      const mcpArgs = Array.isArray(row.mcpConfig?.args) ? row.mcpConfig.args.join(' ') : '';
      const command = [row.mcpConfig?.command, mcpArgs].filter(Boolean).join(' ').trim();
      return {
        id: publicStableId('mcp', row.name),
        name: row.title || row.name,
        displayName: row.title || row.name,
        packageName: row.name,
        summary: row.description || 'EcoreX managed MCP capability.',
        endpointLabel: command ? `本地 MCP：${row.name}` : 'EcoreX MCP 端点',
        command: command || undefined,
        tags: ['MCP', row.category || 'EcoreX'],
        auth: '本地认证',
        authState: row.enabled ? '已配置' : '未启用',
        status: row.enabled ? 'configured' : 'disabled',
        enabled: row.enabled,
        connected: false,
        online: false,
        ping: '-',
        permissions: '读写',
        updatedAt: row.lastUpdated || row.installedAt || undefined,
        sourceKind: row.sourceKind,
        installed: row.installed !== false
      };
    });
  writeLog('info', 'MCP status collected', {
    services: services.map((service) => service.packageName || service.name),
    totalServices: services.length
  });
  return attachDeveloperDiagnostics({
    ok: true,
    source: 'EcoreX MCP',
    refreshedAt: new Date().toISOString(),
    configured: services.length > 0,
    services,
    servers: services,
    defaultEmpty: services.length === 0,
    message: services.length
      ? 'EcoreX managed MCP entries are available.'
      : 'EcoreX starts with no MCP entries. Add project MCP entries inside EcoreX.'
  }, {
    bridge: 'ecorex-managed',
    command: 'managed: local Claude/Codex MCP inventory is intentionally not scanned',
    managedMcp: services.map((service) => service.packageName || service.name)
  }, payload);
}

async function getMcpServer(payload = {}) {
  let lookupName = payload?.name;
  if (!lookupName && payload?.id) {
    const status = await collectMcpStatus();
    lookupName = status.services?.find((service) => service.id === payload.id)?.name || payload.id;
  }
  const name = sanitizeCliName(lookupName, 'MCP server name');
  const result = await runClaudeCommand(['mcp', 'get', name], {
    timeoutMs: 15000,
    cwd: defaultAgentCwd()
  });
  if (!result.ok && isUnsupportedCliResult(result)) {
    return unsupportedCliResponse('mcp:get', {
      includeDiagnostics: Boolean(payload?.includeDiagnostics),
      command: 'claude mcp get <name>',
      result: safeCommandResult(result)
    });
  }
  const output = result.stdout || result.stderr || '';
  return attachDeveloperDiagnostics({
    ok: result.ok,
    source: 'EcoreX MCP',
    name,
    server: {
      id: publicStableId('connection', name),
      name,
      summary: safeOutputText(output, 4000)
    },
    error: result.ok ? undefined : publicBridgeError(result, 'MCP server lookup failed.')
  }, {
    bridge: 'claude-code',
    command: 'claude mcp get <name>',
    raw: safeOutputText(output),
    result: safeCommandResult(result)
  }, payload);
}

async function updateMcpConfig(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Invalid MCP config payload.');
  }
  const name = sanitizeCliName(payload.name || payload.id, 'MCP server name');
  const scope = sanitizeConfigScope(payload.scope);
  const configJson = normalizeConfigJson(
    Object.prototype.hasOwnProperty.call(payload, 'config') ? payload.config : payload.json
  );
  const result = await runClaudeCommand(['mcp', 'add-json', '--scope', scope, name, configJson], {
    timeoutMs: 30000,
    cwd: defaultAgentCwd()
  });
  if (!result.ok && isUnsupportedCliResult(result)) {
    return unsupportedCliResponse('mcp:update-config', {
      includeDiagnostics: Boolean(payload?.includeDiagnostics),
      command: 'claude mcp add-json --scope <scope> <name> <json>',
      result: safeCommandResult(result)
    });
  }
  const response = {
    ok: result.ok,
    source: 'EcoreX MCP',
    name,
    scope,
    error: result.ok ? undefined : publicBridgeError(result, 'MCP config update failed.')
  };
  if (result.ok) response.status = await collectMcpStatus();
  return attachDeveloperDiagnostics(response, {
    bridge: 'claude-code',
    command: 'claude mcp add-json --scope <scope> <name> <json>',
    result: safeCommandResult(result)
  }, payload);
}

function unsupportedMcpToggle(action) {
  const label = action === 'enable' ? '启用' : '禁用';
  return unsupportedCliResponse(`mcp:${action}`, {
    command: null,
    error: `当前版本暂不支持直接${label}连接器，请通过连接器配置调整。`
  });
}

function readMarkdownSummary(filePath) {
  try {
    if (!fs.existsSync(filePath)) return '';
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size > 512 * 1024) return '';
    const raw = fs.readFileSync(filePath, 'utf8');
    return (
      raw
        .replace(/^---[\s\S]*?---/, '')
        .split(/\r?\n/)
        .map((line) => line.trim())
        .find((line) => line && !line.startsWith('#') && !line.startsWith('```')) || ''
    ).slice(0, 500);
  } catch {
    return '';
  }
}

function readSkillManifest(skillDir, plugin = {}) {
  const file = path.join(skillDir, 'SKILL.md');
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 512 * 1024) return null;
    const raw = fs.readFileSync(file, 'utf8');
    const frontmatter = raw.match(/^---\s*\r?\n([\s\S]*?)\r?\n---/);
    const meta = {};
    if (frontmatter) {
      for (const line of frontmatter[1].split(/\r?\n/)) {
        const match = line.match(/^([a-zA-Z0-9_-]+):\s*(.*)$/);
        if (match) meta[match[1]] = match[2].replace(/^["']|["']$/g, '').trim();
      }
    }
    const name = meta.name || path.basename(skillDir);
    const title = raw.replace(/^---[\s\S]*?---/, '').match(/^#\s+(.+)$/m)?.[1]?.trim();
    const providerName = plugin.displayName || plugin.name || plugin.id || 'EcoreX skill pack';
    const providerId = publicStableId('skillpack', plugin.id || providerName);
    return {
      id: publicStableId('skill', `${plugin.id || providerName}:${name}`),
      type: 'skill',
      name,
      title: title || name,
      description: (meta.description || readMarkdownSummary(file)).slice(0, 500),
      provider: {
        id: providerId,
        name: providerName,
        version: plugin.version || null
      },
      scope: plugin.scope || null,
      enabled: plugin.enabled !== false,
      installed: Boolean(plugin.installed),
      status: plugin.enabled === false ? 'disabled' : plugin.installed ? 'enabled' : 'available',
      updatedAt: stat.mtime.toISOString()
    };
  } catch {
    return null;
  }
}

function listSkillsInPluginRoot(pluginRoot, plugin) {
  const manifest = skillPluginManifest(pluginRoot) || {};
  const skillsField = typeof manifest.skills === 'string' ? manifest.skills : '';
  const skillsRoot = skillsField
    ? path.resolve(pluginRoot, skillsField)
    : path.join(pluginRoot, 'skills');
  try {
    if (!fs.existsSync(skillsRoot)) return [];
    if (fs.existsSync(path.join(skillsRoot, 'SKILL.md'))) {
      const directSkill = readSkillManifest(skillsRoot, plugin);
      return directSkill ? [directSkill] : [];
    }
    return fs
      .readdirSync(skillsRoot, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .slice(0, MAX_SKILLS_PER_PLUGIN)
      .map((entry) => readSkillManifest(path.join(skillsRoot, entry.name), plugin))
      .filter(Boolean);
  } catch {
    return [];
  }
}

function normalizePluginRecord(plugin = {}, options = {}) {
  const id = String(plugin.id || plugin.pluginId || plugin.name || '').trim();
  const name = String(plugin.name || id.split('@')[0] || id).trim();
  const installPath = typeof plugin.installPath === 'string' ? plugin.installPath : '';
  return {
    id,
    name,
    marketplaceName: plugin.marketplaceName || id.split('@')[1] || null,
    version: plugin.version || null,
    scope: plugin.scope || null,
    enabled: plugin.enabled !== false,
    installed: options.installed ?? Boolean(plugin.installed || plugin.installPath),
    installPath,
    installPathExists: Boolean(installPath && fs.existsSync(installPath)),
    installedAt: plugin.installedAt || null,
    lastUpdated: plugin.lastUpdated || null,
    projectPath: plugin.projectPath || null,
    description: plugin.description ? safeOutputText(plugin.description, 1000) : undefined,
    installCount: plugin.installCount,
    source: plugin.source ? safeJsonValue(plugin.source, 4000) : undefined
  };
}

function publicSkillPackRecord(plugin = {}) {
  return {
    id: plugin.id || publicStableId('skillpack', plugin.name),
    name: plugin.name || 'Skill pack',
    title: plugin.title || plugin.displayName || plugin.name || 'Skill pack',
    version: plugin.version || null,
    scope: plugin.scope || null,
    enabled: plugin.enabled !== false,
    installed: Boolean(plugin.installed),
    status: plugin.enabled === false ? 'disabled' : plugin.installed ? 'enabled' : 'available',
    description: plugin.description ? safeOutputText(plugin.description, 500) : undefined,
    installCount: plugin.installCount,
    category: plugin.category || plugin.type || undefined,
    sourceKind: plugin.sourceKind || undefined,
    managed: Boolean(plugin.installPath),
    installPath: plugin.installPath ? publicWorkspacePath(plugin.installPath) : undefined,
    updatedAt: plugin.lastUpdated || plugin.updatedAt || plugin.installedAt || undefined
  };
}

async function collectClaudePlugins(payload = {}) {
  const includeAvailable = Boolean(payload?.includeAvailable);
  const args = ['plugin', 'list', '--json'];
  if (includeAvailable) args.splice(2, 0, '--available');
  const result = await runClaudeCommand(args, {
    timeoutMs: includeAvailable ? 30000 : 15000,
    cwd: defaultAgentCwd()
  });
  if (!result.ok && result.code === -1 && /not found/i.test(result.stderr || '')) {
    return attachDeveloperDiagnostics({
      ok: false,
      unavailable: true,
      installed: [],
      available: [],
      error: 'The local skill bridge is unavailable.'
    }, {
      bridge: 'claude-code',
      command: includeAvailable ? 'claude plugin list --available --json' : 'claude plugin list --json',
      result: safeCommandResult(result)
    }, payload);
  }
  if (!result.ok && isUnsupportedCliResult(result)) {
    return unsupportedCliResponse('plugin:list', {
      includeDiagnostics: Boolean(payload?.includeDiagnostics),
      command: includeAvailable ? 'claude plugin list --available --json' : 'claude plugin list --json',
      installed: [],
      available: [],
      result: safeCommandResult(result)
    });
  }

  const parsed = parseJsonOutput(result.stdout);
  const installed = Array.isArray(parsed)
    ? parsed
    : Array.isArray(parsed?.installed)
      ? parsed.installed
      : [];
  const available = Array.isArray(parsed?.available) ? parsed.available : [];
  return {
    ok: result.ok,
    command: includeAvailable ? 'claude plugin list --available --json' : 'claude plugin list --json',
    installed: installed.map((plugin) => normalizePluginRecord(plugin, { installed: true })).slice(0, MAX_MANAGED_ITEMS),
    available: available.map((plugin) => normalizePluginRecord(plugin, { installed: false })).slice(0, MAX_MANAGED_ITEMS),
    availableTruncated: available.length > MAX_MANAGED_ITEMS,
    result: safeCommandResult(result),
    error: result.ok ? undefined : publicBridgeError(result, 'SKILLS 清单刷新失败。')
  };
}

function collectBundledSkills() {
  const repoRoot = backendPath('claude-code-main');
  const safeRepoRoot = path.resolve(repoRoot);
  return parsePluginInventory()
    .filter((plugin) => plugin.available)
    .flatMap((plugin) => {
      const pluginRoot = path.resolve(repoRoot, plugin.source);
      if (!isPathInside(safeRepoRoot, pluginRoot)) return [];
      return listSkillsInPluginRoot(pluginRoot, {
        id: plugin.name,
        name: plugin.name,
        version: null,
        scope: 'bundled',
        enabled: false,
        installed: false,
        source: 'bundled-backend'
      });
    })
    .slice(0, MAX_MANAGED_ITEMS);
}

function collectSkillStatus(payload = {}) {
  const managed = collectManagedSkillInventory({ includeDisabled: true });
  const installedSkillPacks = managed.skillPacks;
  const skills = installedSkillPacks;
  writeLog('info', 'Skill status collected', {
    skillPacks: installedSkillPacks.map((pack) => pack.name),
    totalSkillPacks: installedSkillPacks.length,
    childSkills: managed.skills.length
  });
  return attachDeveloperDiagnostics({
    ok: true,
    source: 'EcoreX Skill Library',
    refreshedAt: new Date().toISOString(),
    unsupportedActions: [],
    installedSkillPacks,
    availableSkillPacks: [],
    availableSkillPacksTruncated: false,
    skills,
    childSkills: managed.skills,
    counts: {
      installedSkillPacks: installedSkillPacks.length,
      installedSkills: installedSkillPacks.filter((skill) => skill.installed).length,
      bundledSkills: 0,
      totalSkills: installedSkillPacks.length,
      childSkills: managed.skills.length
    },
    defaultEmpty: skills.length === 0,
    partial: false,
    message: skills.length
      ? 'EcoreX managed skills are available.'
      : 'EcoreX starts with no skills. Install EcoreX skills from the app-managed library later.'
  }, {
    bridge: 'ecorex-managed',
    command: 'managed: local Claude/Codex skill inventory is intentionally not scanned',
    installedSkillPacks: installedSkillPacks.map((pack) => pack.name),
    availableCount: 0
  }, payload);
}

function unsupportedSkillAction(action) {
  const labels = {
    install: '安装',
    enable: '启用',
    disable: '禁用',
    update: '更新'
  };
  return unsupportedCliResponse(`skill:${action}`, {
    command: null,
    error: `当前版本暂不支持直接${labels[action] || '管理'}单个 Skill，本地能力库会继续展示真实可用列表。`
  });
}

function publicBackendAuthStatus(authStatus = {}) {
  const safe = authStatus && typeof authStatus === 'object' ? redactForLog(authStatus) : {};
  return {
    loggedIn: Boolean(safe.loggedIn || safe.authenticated),
    status: safe.status ? safeOutputText(safe.status, 120) : undefined,
    account: safe.account || safe.email || safe.username || undefined
  };
}

async function buildBackendStatus(options = {}) {
  const lightweight = Boolean(options.lightweight || options.preload);
  const claude = await locateClaude();
  const [auth, mcp, plugins] = lightweight
    ? [null, null, null]
    : await Promise.all([
        claude.command ? runClaudeCommand(['auth', 'status'], { timeoutMs: 10000 }) : Promise.resolve(null),
        claude.command ? runClaudeCommand(['mcp', 'list'], { timeoutMs: 10000 }) : Promise.resolve(null),
        claude.command ? runClaudeCommand(['plugin', 'list'], { timeoutMs: 10000 }) : Promise.resolve(null)
      ]);

  let authStatus = null;
  const authOutput = auth?.stdout || auth?.stderr || '';
  if (authOutput) {
    try {
      authStatus = JSON.parse(authOutput);
    } catch {
      authStatus = {
        loggedIn: /(logged\s*in|authenticated|valid)/i.test(authOutput) && !/(not|no|unauthenticated)/i.test(authOutput),
        status: safeOutputText(authOutput.split(/\r?\n/).find(Boolean) || '', 120)
      };
    }
  }

  return {
    ok: Boolean(claude.command),
    refreshedAt: new Date().toISOString(),
    agentBridge: {
      available: Boolean(claude.command),
      version: claude.version || null
    },
    auth: publicBackendAuthStatus(authStatus || { loggedIn: false }),
    dataConnections: {
      configured: lightweight ? false : Boolean(mcp?.stdout && !mcp.stdout.includes('No MCP servers configured')),
      services: parseMcpServices(mcp?.stdout || mcp?.stderr || '')
    },
    mcp: undefined,
    skillPacks: {
      summary: lightweight
        ? '启动预检已完成，MCP 与 SKILLS 清单会在打开系统设置时按需刷新。'
        : (plugins?.ok ? 'SKILLS 清单可用。' : 'SKILLS 清单暂不可用。')
    },
    previewEngine: publicVueOfficePreviewStatus(),
    sourceMap: publicSourceMapStats()
  };
}

async function collectBackendStatus(_event, payload = {}) {
  const forceRefresh = Boolean(payload?.refresh || payload?.forceRefresh);
  const lightweight = Boolean(payload?.lightweight || payload?.preload);
  if (lightweight) return buildBackendStatus({ lightweight: true });
  const now = Date.now();
  if (!forceRefresh && cachedBackendStatus && now - cachedBackendStatus.cachedAt <= BACKEND_STATUS_TTL_MS) {
    return {
      ...cachedBackendStatus.value,
      cached: true,
      cacheAgeMs: now - cachedBackendStatus.cachedAt
    };
  }
  if (!forceRefresh && backendStatusInflight) return backendStatusInflight;

  const request = buildBackendStatus().then((status) => {
    cachedBackendStatus = {
      cachedAt: Date.now(),
      value: status
    };
    return status;
  });

  if (forceRefresh) return request;
  backendStatusInflight = request.finally(() => {
    backendStatusInflight = null;
  });
  return backendStatusInflight;
}

function publicSessionSummary(sessionId, entry = {}, now = Date.now()) {
  const startedAtMs = Number(entry.startedAt) || now;
  const lastActivityAtMs = Number(entry.lastActivityAt) || startedAtMs;
  const durationMs = Math.max(0, now - startedAtMs);
  const permissionPolicy = publicPermissionPolicy(entry.accessMode || entry.permissionMode);
  const permissionMode = entry.accessMode
    ? (entry.permissionMode || permissionPolicy.permissionMode)
    : permissionPolicy.permissionMode;
  return {
    sessionId,
    conversationId: entry.conversationId || '',
    claudeSessionId: entry.claudeSessionId || '',
    messageId: entry.messageId || '',
    status: entry.status || 'running',
    state: entry.state || 'running',
    runtimeKind: entry.runtimeKind || AGENT_RUNTIME_KIND,
    actorId: entry.actorId || null,
    transport: entry.transport?.kind || null,
    startedAt: startedAtMs,
    startedAtIso: new Date(startedAtMs).toISOString(),
    startedAtMs,
    lastActivity: new Date(lastActivityAtMs).toISOString(),
    lastActivityAt: lastActivityAtMs,
    lastActivityAtMs,
    duration: formatDuration(durationMs),
    durationMs,
    promptPreview: entry.promptPreview || '',
    promptHash: entry.promptHash || undefined,
    workspacePath: entry.workspacePath || publicWorkspacePath(entry.cwd),
    projectId: entry.projectId || null,
    projectName: entry.projectName || '',
    projectPath: entry.projectPath || '',
    projectMemoryLabel: entry.projectMemoryLabel || '',
    model: entry.model,
    accessMode: entry.accessMode || permissionPolicy.accessMode,
    permissionMode,
    permissionLabel: entry.permissionLabel || permissionPolicy.label,
    permissionPolicy,
    fullAccess: Boolean(entry.permissionSnapshot?.fullAccess || permissionPolicy.fullAccess),
    attachmentCount: Number(entry.attachmentCount) || 0,
    ledgerEventCount: Number(entry.ledgerEventCount) || 0,
    eventCount: Array.isArray(entry.transcript) ? entry.transcript.length : 0
  };
}

function getRunningSessionSummaries() {
  const now = Date.now();
  const running = Array.from(runningAgents.entries()).map(([sessionId, entry]) =>
    publicSessionSummary(sessionId, entry, now)
  );
  const pending = Array.from(pendingAgentStarts.entries())
    .filter(([sessionId]) => !runningAgents.has(sessionId))
    .map(([sessionId, entry]) => publicSessionSummary(sessionId, entry, now));
  return [...running, ...pending].sort((a, b) => b.startedAtMs - a.startedAtMs);
}

function safeTranscriptText(value = '') {
  return redactSensitiveText(value).replace(/\s+/g, ' ').trim();
}

function safeTranscriptTextPreview(value = '') {
  return safeTranscriptText(value).slice(0, MAX_TRANSCRIPT_TEXT_PREVIEW_CHARS);
}

function compactSessionTranscriptEvents(events = []) {
  if (events.length <= MAX_TRANSCRIPT_EVENTS) return events;
  const keep = new Map();
  const add = (index) => {
    if (index < 0 || index >= events.length || keep.size >= MAX_TRANSCRIPT_EVENTS) return;
    keep.set(index, events[index]);
  };
  const isImportant = (event = {}) => {
    const kind = String(event.kind || '').trim();
    if (!kind || kind !== 'assistant') return true;
    return Boolean(event.toolName || event.tools?.length || event.task?.type === 'tool');
  };
  const headCount = Math.min(12, MAX_TRANSCRIPT_HEAD_EVENTS, Math.floor(MAX_TRANSCRIPT_EVENTS / 4));
  for (let index = 0; index < headCount; index += 1) add(index);
  events.forEach((event, index) => {
    if (isImportant(event)) add(index);
  });
  for (let index = events.length - 1; index >= 0 && keep.size < MAX_TRANSCRIPT_EVENTS; index -= 1) {
    add(index);
  }
  return [...keep.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([, event]) => event);
}

function recordSessionEvent(entry, event) {
  if (!entry) return;
  if (!Array.isArray(entry.transcript)) entry.transcript = [];
  const normalized = normalizeAgentEvent(event, { includeRaw: false, textLimit: 2000 });
  entry.lastEventStatus = normalized.status;
  entry.lastEventState = normalized.state;
  if (['error', 'failed', 'timeout'].includes(String(normalized.status || '').toLowerCase())) {
    entry.status = normalized.status;
    entry.state = normalized.state || entry.state;
  }
  entry.lastActivityAt = Date.now();
  entry.lastEventAt = normalized.time;
  if (entry.actor) {
    entry.actor.lastEventStatus = normalized.status || entry.actor.lastEventStatus;
    entry.actor.lastEventState = normalized.state || entry.actor.lastEventState;
    entry.actor.lastActivityAt = entry.lastActivityAt;
  }
  if (normalized.claudeResultStatus === 'failed') {
    entry.claudeResultFailed = true;
  }
  if (normalized.ledger) {
    entry.ledgerEventCount = (Number(entry.ledgerEventCount) || 0) + 1;
  }
  registerAgentArtifactsFromEvent(entry, normalized);
  entry.transcript.push({
    time: normalized.time,
    kind: normalized.kind,
    status: normalized.status,
    state: normalized.state,
    detailStatus: normalized.detailStatus,
    task: normalized.task,
    ledger: normalized.ledger ? safeToolLedger(normalized.ledger) : undefined,
    textPreview: safeTranscriptTextPreview(normalized.text)
  });
  if (entry.transcript.length > MAX_TRANSCRIPT_EVENTS) {
    entry.transcript = compactSessionTranscriptEvents(entry.transcript);
  }
}

function writeSessionTranscript(sessionId, entry, result = {}) {
  if (!entry || entry.transcriptWritten) return null;
  entry.transcriptWritten = true;
  const endedAt = Date.now();
  const fileName = `${new Date(endedAt).toISOString().replace(/[:.]/g, '-')}-${sessionId}.json`;
  const file = path.join(sessionTranscriptDir(), fileName);
  const payload = {
    sessionId,
    conversationId: entry.conversationId || '',
    claudeSessionId: entry.claudeSessionId || '',
    messageId: entry.messageId || '',
    status: result.status || 'ended',
    reason: result.reason,
    exitCode: result.code,
    signal: result.signal,
    promptPreview: entry.promptPreview || '',
    promptHash: entry.promptHash || undefined,
    workspacePath: entry.workspacePath || publicWorkspacePath(entry.cwd),
    projectId: entry.projectId || null,
    projectName: entry.projectName || '',
    projectPath: entry.projectPath || '',
    projectMemoryLabel: entry.projectMemoryLabel || '',
    attachmentCount: Number(entry.attachmentCount) || 0,
    ledgerEventCount: Number(entry.ledgerEventCount) || 0,
    startedAt: new Date(entry.startedAt).toISOString(),
    endedAt: new Date(endedAt).toISOString(),
    durationMs: endedAt - entry.startedAt,
    events: Array.isArray(entry.transcript) ? entry.transcript : []
  };
  try {
    atomicWriteJson(file, payload);
    return file;
  } catch (error) {
    writeLog('warn', 'Failed to write session transcript', {
      sessionId,
      error: error instanceof Error ? error.message : String(error)
    });
    return null;
  }
}

function journalSessionSummary(sessionId, entry = {}, status = 'running', details = {}) {
  return {
    schema: 'ecorex.run-journal.v1',
    time: new Date().toISOString(),
    event: details.event || status,
    sessionId,
    publicSessionId: publicStableId('session', sessionId),
    status,
    reason: details.reason,
    exitCode: details.code,
    signal: details.signal,
    startedAt: entry.startedAt ? new Date(entry.startedAt).toISOString() : undefined,
    endedAt: details.endedAt ? new Date(details.endedAt).toISOString() : undefined,
    durationMs: details.endedAt && entry.startedAt ? details.endedAt - entry.startedAt : undefined,
    promptFingerprint: entry.promptHash ? String(entry.promptHash).slice(0, 12) : undefined,
    workspacePath: entry.workspacePath || publicWorkspacePath(entry.cwd),
    projectId: entry.projectId || null,
    projectName: entry.projectName || '',
    projectPath: entry.projectPath || '',
    model: entry.model || null,
    accessMode: entry.accessMode || undefined,
    permissionMode: entry.permissionMode || undefined,
    attachmentCount: Number(entry.attachmentCount) || 0,
    ledgerEventCount: Number(entry.ledgerEventCount) || 0
  };
}

function appendRunJournalEntry(sessionId, entry = {}, status = 'running', details = {}) {
  const file = runJournalPath();
  const payload = redactForLog(journalSessionSummary(sessionId, entry, status, details));
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, `${JSON.stringify(payload)}\n`, 'utf8');
    trimRunJournalFile(file);
  } catch (error) {
    writeLog('warn', 'Failed to append agent run journal', {
      sessionId,
      error: error instanceof Error ? error.message : String(error)
    });
  }
}

function trimRunJournalFile(file = runJournalPath()) {
  try {
    const stat = fs.statSync(file);
    if (stat.size <= MAX_RUN_JOURNAL_BYTES) return;
    const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/).filter(Boolean).slice(-MAX_RUN_JOURNAL_ENTRIES);
    fs.writeFileSync(file, `${lines.join('\n')}\n`, 'utf8');
  } catch {
    // Journal trimming is best-effort and must not affect task execution.
  }
}

function readRunJournalEntries(limit = MAX_RUN_JOURNAL_ENTRIES) {
  try {
    const file = runJournalPath();
    if (!fs.existsSync(file)) return [];
    const stat = fs.statSync(file);
    const start = Math.max(0, stat.size - MAX_RUN_JOURNAL_BYTES);
    const fd = fs.openSync(file, 'r');
    const buffer = Buffer.alloc(stat.size - start);
    fs.readSync(fd, buffer, 0, buffer.length, start);
    fs.closeSync(fd);
    return buffer
      .toString('utf8')
      .split(/\r?\n/)
      .filter(Boolean)
      .slice(-Math.min(Math.max(Number(limit) || 1, 1), MAX_RUN_JOURNAL_ENTRIES))
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

function recentUnfinishedRunJournals(limit = 20) {
  const latestBySession = new Map();
  for (const entry of readRunJournalEntries(MAX_RUN_JOURNAL_ENTRIES)) {
    if (!entry?.sessionId) continue;
    latestBySession.set(entry.sessionId, entry);
  }
  return [...latestBySession.values()]
    .filter((entry) => entry.event === 'start' || entry.status === 'running' || entry.status === 'started')
    .sort((left, right) => new Date(right.time).getTime() - new Date(left.time).getTime())
    .slice(0, Math.min(Math.max(Number(limit) || 1, 1), 50))
    .map((entry) => ({
      sessionId: entry.publicSessionId || publicStableId('session', entry.sessionId),
      status: entry.status || 'running',
      startedAt: entry.startedAt || entry.time,
      workspacePath: entry.workspacePath,
      projectId: entry.projectId || null,
      projectName: entry.projectName || '',
      model: entry.model || null,
      accessMode: entry.accessMode,
      permissionMode: entry.permissionMode,
      attachmentCount: Number(entry.attachmentCount) || 0,
      ledgerEventCount: Number(entry.ledgerEventCount) || 0
    }));
}

function publicTranscriptEventSummary(event = {}, index = 0) {
  const kind = String(event.kind || 'status').slice(0, 40);
  const status = String(event.status || event.state || kind).slice(0, 80);
  const taskName = event.task?.name ? publicProductText(event.task.name) : '';
  const textPreview = safeTranscriptTextPreview(event.textPreview || event.text || '');
  return {
    index,
    time: event.time,
    kind,
    status,
    state: event.state,
    task: taskName ? { name: taskName, type: event.task?.type || kind } : undefined,
    ledger: event.ledger ? safeToolLedger(event.ledger) : undefined,
    textPreview
  };
}

function sessionTranscriptSummaryFromFile(file, options = {}) {
  try {
    const stat = fs.statSync(file);
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    const sessionId = sanitizeSessionId(raw.sessionId);
    const events = Array.isArray(raw.events) ? raw.events : [];
    const lastEvent = [...events].reverse().find(Boolean) || {};
    const summary = {
      id: sessionId,
      sessionId,
      conversationId: raw.conversationId || '',
      claudeSessionId: raw.claudeSessionId || '',
      messageId: raw.messageId || '',
      status: raw.status || lastEvent.status || 'history',
      state: raw.state || lastEvent.state || raw.status || 'history',
      reason: raw.reason,
      exitCode: raw.exitCode,
      signal: raw.signal,
      title: raw.promptPreview || `Session ${sessionId.slice(0, 8)}`,
      promptPreview: raw.promptPreview || '',
      promptHash: raw.promptHash || undefined,
      workspacePath: raw.workspacePath || publicWorkspacePath(raw.cwd),
      projectId: raw.projectId || null,
      projectName: raw.projectName || '',
      projectPath: raw.projectPath || '',
      projectMemoryLabel: raw.projectMemoryLabel || '',
      attachmentCount: Number(raw.attachmentCount) || 0,
      ledgerEventCount: Number(raw.ledgerEventCount) || 0,
      startedAt: raw.startedAt,
      endedAt: raw.endedAt,
      modifiedAt: stat.mtime.toISOString(),
      durationMs: Number(raw.durationMs) || 0,
      eventCount: events.length,
      lastEvent: publicTranscriptEventSummary(lastEvent, Math.max(0, events.length - 1)),
      hasTranscript: true
    };
    if (options.includeEvents) {
      summary.events = events
        .slice(-MAX_TRANSCRIPT_EVENTS)
        .map((event, index) => publicTranscriptEventSummary(event, Math.max(0, events.length - MAX_TRANSCRIPT_EVENTS) + index));
    }
    return summary;
  } catch (error) {
    writeLog('warn', 'Failed to read session transcript summary', {
      file: path.basename(file),
      error: error instanceof Error ? error.message : String(error)
    });
    return null;
  }
}

function sessionTranscriptSummaries(limit = MAX_RECENT_SESSION_FILES) {
  try {
    const dir = sessionTranscriptDir();
    if (!fs.existsSync(dir)) return [];
    const max = limit === Infinity
      ? Infinity
      : Math.min(Math.max(Number(limit) || MAX_RECENT_SESSION_FILES, 1), MAX_RECENT_SESSION_FILES);
    return fs
      .readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.endsWith('.json'))
      .map((entry) => sessionTranscriptSummaryFromFile(path.join(dir, entry.name)))
      .filter(Boolean)
      .sort((a, b) => new Date(b.modifiedAt).getTime() - new Date(a.modifiedAt).getTime())
      .slice(0, max);
  } catch {
    return [];
  }
}

function recentSessionFiles(limit = MAX_RECENT_SESSION_FILES) {
  return sessionTranscriptSummaries(limit);
}

function recentRecoverableSessionSummaries(limit = 6) {
  const cutoffMs = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return sessionTranscriptSummaries(Math.max(limit * 4, 20))
    .filter((session) => {
      const endedAt = Date.parse(session.endedAt || session.modifiedAt || '');
      if (Number.isFinite(endedAt) && endedAt < cutoffMs) return false;
      const status = String(session.status || '').toLowerCase();
      const reason = String(session.reason || '').toLowerCase();
      return ['stopped', 'cancelled', 'canceled', 'failed', 'timeout'].includes(status)
        && /(app-quit|window-closed|renderer|crash|unresponsive|gone|quit|closed|timeout)/i.test(reason || status);
    })
    .slice(0, Math.min(Math.max(Number(limit) || 1, 1), 12))
    .map((session) => ({
      ...session,
      recoverable: true,
      recoveryStatus: session.status || 'stopped',
      recoveryHint: '上次关闭或重启时任务被中断，可在原会话继续或重试；已生成的产物会继续留在原项目/会话内。'
    }));
}

function getSessionHistorySummary(payload = {}) {
  const recent = recentSessionFiles(payload?.limit);
  const sessionId = String(payload?.sessionId || '').trim();
  if (!sessionId) {
    return { ok: true, recentSessionHistory: recent, recentSessionFiles: recent };
  }
  const safeSessionId = sanitizeSessionId(sessionId);
  const session = sessionTranscriptSummaries(Infinity).find((item) => item.sessionId === safeSessionId);
  if (!session) {
    return {
      ok: false,
      code: 'not-found',
      status: 'not-found',
      error: 'Session transcript was not found.',
      recoveryHint: agentRecoveryHint('not-found'),
      recentSessionHistory: recent,
      recentSessionFiles: recent
    };
  }
  return { ok: true, session, recentSessionHistory: recent, recentSessionFiles: recent };
}

function fileSummary(filePath) {
  try {
    const stat = fs.statSync(filePath);
    return {
      path: filePath,
      exists: true,
      type: stat.isDirectory() ? 'directory' : 'file',
      size: stat.isFile() ? stat.size : 0,
      sizeMb: stat.isFile() ? Number((stat.size / 1024 / 1024).toFixed(1)) : 0,
      modifiedAt: stat.mtime.toISOString()
    };
  } catch {
    return { path: filePath, exists: false };
  }
}

function filePreviewMimeFromExtension(extension = '') {
  if (extension === '.png') return 'image/png';
  if (extension === '.jpg' || extension === '.jpeg') return 'image/jpeg';
  if (extension === '.webp') return 'image/webp';
  if (extension === '.gif') return 'image/gif';
  if (extension === '.svg') return 'image/svg+xml';
  if (extension === '.mp4' || extension === '.m4v') return 'video/mp4';
  if (extension === '.webm') return 'video/webm';
  if (extension === '.ogg' || extension === '.ogv') return 'video/ogg';
  if (extension === '.mov') return 'video/quicktime';
  if (extension === '.pdf') return 'application/pdf';
  if (extension === '.html' || extension === '.htm') return 'text/html';
  if (extension === '.md' || extension === '.markdown') return 'text/markdown';
  if (extension === '.json' || extension === '.jsonl') return 'application/json';
  if (extension === '.csv') return 'text/csv';
  if (extension === '.css') return 'text/css';
  if (['.js', '.mjs', '.cjs'].includes(extension)) return 'text/javascript';
  if (extension === '.docx') return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  if (extension === '.doc') return 'application/msword';
  if (extension === '.xlsx') return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  if (extension === '.xlsm') return 'application/vnd.ms-excel.sheet.macroEnabled.12';
  if (extension === '.xls') return 'application/vnd.ms-excel';
  if (extension === '.pptx') return 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
  if (extension === '.pptm') return 'application/vnd.ms-powerpoint.presentation.macroEnabled.12';
  if (extension === '.ppt') return 'application/vnd.ms-powerpoint';
  if (extension === '.txt' || extension === '.log') return 'text/plain';
  return 'application/octet-stream';
}

function filePreviewLanguageFromExtension(extension = '') {
  if (extension === '.html' || extension === '.htm') return 'html';
  if (extension === '.md' || extension === '.markdown') return 'markdown';
  if (extension === '.json' || extension === '.jsonl') return 'json';
  if (extension === '.csv') return 'csv';
  if (extension === '.css') return 'css';
  if (['.js', '.mjs', '.cjs'].includes(extension)) return 'javascript';
  if (extension === '.log') return 'log';
  return 'text';
}

function isImagePreviewExtension(extension = '') {
  return FILE_PREVIEW_IMAGE_EXTENSIONS.has(String(extension || '').toLowerCase());
}

function isDocumentMetadataPreviewExtension(extension = '') {
  return FILE_PREVIEW_DOCUMENT_EXTENSIONS.has(String(extension || '').toLowerCase());
}

function isHtmlPreviewExtension(extension = '') {
  return extension === '.html' || extension === '.htm';
}

function filePreviewRenderMode(extension = '') {
  if (isHtmlPreviewExtension(extension)) return 'sandbox-srcdoc';
  if (extension === '.md' || extension === '.markdown') return 'markdown';
  if (extension === '.json' || extension === '.jsonl') return 'json';
  if (extension === '.csv') return 'csv';
  if (extension === '.css') return 'code';
  if (['.js', '.mjs', '.cjs'].includes(extension)) return 'code';
  return 'text';
}

function resolveWorkspacePathLabel(label = '', workspaceRoot = readSettings().workspaceRoot) {
  const raw = String(label || '').trim();
  if (!raw || !raw.startsWith('workspace:/')) return '';
  return path.resolve(workspaceRoot, sanitizeWorkspaceRelativePath(raw.replace(/^workspace:\//, '')));
}

function filePreviewArtifactExtensions() {
  return new Set([
    ...FILE_PREVIEW_TEXT_EXTENSIONS,
    ...FILE_PREVIEW_IMAGE_EXTENSIONS,
    ...FILE_PREVIEW_DOCUMENT_EXTENSIONS,
    ...VUE_OFFICE_DOCUMENT_EXTENSIONS
  ]);
}

function isPreviewableArtifactExtension(target = '') {
  return filePreviewArtifactExtensions().has(path.extname(target).toLowerCase());
}

function filePreviewArtifactExtensionPattern() {
  return [...filePreviewArtifactExtensions()]
    .map((extension) => extension.replace(/^\./, ''))
    .sort((left, right) => right.length - left.length)
    .map((extension) => extension.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
}

function pruneAgentArtifactAccess(now = Date.now()) {
  for (const [key, entry] of agentArtifactAccess.entries()) {
    if (!entry || entry.expiresAt <= now) agentArtifactAccess.delete(key);
  }
  if (agentArtifactAccess.size <= MAX_AGENT_ARTIFACT_ACCESS) return;
  const entries = [...agentArtifactAccess.entries()].sort((left, right) => left[1].createdAt - right[1].createdAt);
  for (const [key] of entries.slice(0, agentArtifactAccess.size - MAX_AGENT_ARTIFACT_ACCESS)) {
    agentArtifactAccess.delete(key);
  }
}

function agentArtifactKey(sessionId, target) {
  return `${sanitizeSessionId(sessionId)}:${path.resolve(target).toLowerCase()}`;
}

function cleanPreviewArtifactPath(raw = '') {
  return String(raw || '')
    .trim()
    .replace(/^['"`<([{]+/, '')
    .replace(/['"`>)\]}.,;，。；：:]+$/g, '')
    .replace(/(?:#L?\d+(?:-L?\d+)?|:\d+(?::\d+)?)$/i, '')
    .trim();
}

function looksLikeRemotePreviewPathSubstring(token = '', source = '', start = 0) {
  const raw = String(token || '').trim();
  if (!raw || /^(?:file:|workspace:|[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/]|~[\\/]|\/)/.test(raw)) return false;
  const prefix = String(source || '').slice(Math.max(0, Number(start) - 24), Number(start)).toLowerCase();
  if (/(?:https?:\/\/|www\.)[^ \r\n<>"'`]*$/.test(prefix)) return true;
  return /^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[\\/]|$)/i.test(raw);
}

function extractPreviewArtifactTargets(text = '', workspaceRoot = readSettings().workspaceRoot) {
  const source = String(text || '');
  if (!source) return [];
  const extPattern = filePreviewArtifactExtensionPattern();
  const targets = [];

  const addTarget = (rawValue) => {
    const raw = cleanPreviewArtifactPath(rawValue);
    if (!raw) return;
    try {
      const target = candidatePreviewPath(raw, workspaceRoot);
      if (isPreviewableArtifactExtension(target) && !targets.some((item) => isSameWorkspacePath(item, target))) {
        targets.push(target);
      }
    } catch {
      // Ignore non-local or malformed path-like text.
    }
  };

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
      const token = match[1] || match[0];
      if (looksLikeRemotePreviewPathSubstring(token, source, match.index || 0)) continue;
      addTarget(token);
      if (targets.length >= 24) return targets.slice(0, 24);
    }
  }
  return targets.slice(0, 24);
}

function artifactCreationEvidenceText(event = {}) {
  return [
    event.text,
    event.textPreview,
    event.ledger?.inputSummary,
    event.ledger?.outputSummary,
    event.ledger?.error,
    Array.isArray(event.tools) ? JSON.stringify(event.tools) : ''
  ].filter(Boolean).join('\n');
}

function eventLooksLikeArtifactWrite(event = {}) {
  if (event.kind !== 'tool') return false;
  const text = artifactCreationEvidenceText(event);
  return /(File created successfully|created successfully|created at|wrote|written|saved|generated|updated|modified|write|edit|创建|写入|保存|生成|更新|修改)/i.test(text);
}

function registerAgentArtifactAccess(sessionId, target, context = {}) {
  const safeSessionId = sanitizeSessionId(sessionId);
  if (!safeSessionId || !target || !isPreviewableArtifactExtension(target)) return;
  pruneAgentArtifactAccess();
  const resolved = path.resolve(target);
  agentArtifactAccess.set(agentArtifactKey(safeSessionId, resolved), {
    sessionId: safeSessionId,
    target: resolved,
    source: context.source || 'agent-tool',
    createdAt: Date.now(),
    expiresAt: Date.now() + AGENT_ARTIFACT_ACCESS_TTL_MS
  });
}

function registerAgentArtifactsFromEvent(entry, event = {}) {
  const sessionId = event.sessionId || entry?.sessionId;
  if (!sessionId || !eventLooksLikeArtifactWrite(event)) return;
  const workspaceRoot = readSettings().workspaceRoot;
  const evidence = artifactCreationEvidenceText(event);
  for (const target of extractPreviewArtifactTargets(evidence, workspaceRoot)) {
    registerAgentArtifactAccess(sessionId, target, { source: 'agent-tool-event' });
  }
}

function sessionTranscriptForPreview(sessionId) {
  const safeSessionId = sanitizeSessionId(sessionId);
  if (!safeSessionId) return null;
  try {
    const dir = sessionTranscriptDir();
    const entry = fs
      .readdirSync(dir, { withFileTypes: true })
      .filter((item) => item.isFile() && item.name.endsWith(`${safeSessionId}.json`))
      .map((item) => path.join(dir, item.name))
      .sort()
      .at(-1);
    if (!entry) return null;
    const stat = fs.statSync(entry);
    if (!stat.isFile() || stat.size > MAX_RUN_JOURNAL_BYTES) return null;
    return JSON.parse(fs.readFileSync(entry, 'utf8'));
  } catch {
    return null;
  }
}

function transcriptAuthorizesAgentArtifact(sessionId, target, workspaceRoot) {
  const transcript = sessionTranscriptForPreview(sessionId);
  if (!transcript || !Array.isArray(transcript.events)) return false;
  const resolved = path.resolve(target);
  return transcript.events.some((event) => {
    if (!eventLooksLikeArtifactWrite(event)) return false;
    return extractPreviewArtifactTargets(artifactCreationEvidenceText(event), workspaceRoot)
      .some((candidate) => isSameWorkspacePath(candidate, resolved));
  });
}

function isRegisteredAgentArtifact(target, input = {}, workspaceRoot = readSettings().workspaceRoot) {
  pruneAgentArtifactAccess();
  const sessionIds = [input.sessionId, input.agentSessionId, input.claudeSessionId]
    .map((value) => sanitizeSessionId(value))
    .filter(Boolean);
  if (!sessionIds.length) return false;
  const resolved = path.resolve(target);
  return sessionIds.some((sessionId) => {
    const entry = agentArtifactAccess.get(agentArtifactKey(sessionId, resolved));
    if (entry) return true;
    return transcriptAuthorizesAgentArtifact(sessionId, resolved, workspaceRoot);
  });
}

function isAgentArtifactPreviewContext(context = {}) {
  const source = String(context.source || context.kind || '').toLowerCase();
  return /assistant|agent|artifact/.test(source)
    || Boolean(context.sessionId || context.agentSessionId || context.claudeSessionId);
}

function addSessionPreviewRoots(roots, sessionId, workspaceRoot) {
  const addRoot = (kind, label, root) => {
    const value = String(root || '').trim();
    if (!value) return;
    const resolved = path.resolve(value);
    if (!roots.some((entry) => isSameWorkspacePath(entry.root, resolved))) roots.push({ kind, label, root: resolved });
  };
  const safeSessionId = sanitizeSessionId(sessionId);
  if (!safeSessionId) return;
  const running = runningAgents.get(safeSessionId);
  if (running?.cwd) addRoot('session', 'session', running.cwd);
  if (running?.projectId) {
    try {
      addRoot('project', 'project', resolveProject({ id: running.projectId }, { allowArchived: true }).project.projectPath);
    } catch {
      // Historical project context may have been deleted or renamed.
    }
  }
  const transcript = sessionTranscriptForPreview(safeSessionId);
  if (!transcript) return;
  const workspacePath = resolveWorkspacePathLabel(transcript.workspacePath, workspaceRoot);
  const projectPath = resolveWorkspacePathLabel(transcript.projectPath, workspaceRoot);
  addRoot('session', 'session', workspacePath);
  addRoot('project', 'project', projectPath);
}

function filePreviewAllowedRoots(context = {}) {
  const roots = [];
  const addRoot = (kind, label, root) => {
    const value = String(root || '').trim();
    if (!value) return;
    const resolved = path.resolve(value);
    if (!roots.some((entry) => isSameWorkspacePath(entry.root, resolved))) roots.push({ kind, label, root: resolved });
  };
  try {
    addRoot('workspace', 'workspace', readSettings().workspaceRoot);
  } catch {
    addRoot('workspace', 'workspace', defaultWorkspaceRoot());
  }
  const workspaceRoot = roots.find((root) => root.kind === 'workspace')?.root || defaultWorkspaceRoot();
  if (context.projectId) {
    try {
      addRoot('project', 'project', resolveProject({ id: context.projectId }, { allowArchived: true }).project.projectPath);
    } catch {
      // Project-scoped preview falls back to the active workspace roots.
    }
  }
  addSessionPreviewRoots(roots, context.sessionId || context.agentSessionId || context.claudeSessionId, workspaceRoot);
  if (isAgentArtifactPreviewContext(context)) {
    addRoot('desktop', 'desktop', safeAppPath('desktop'));
    addRoot('documents', 'documents', safeAppPath('documents'));
    addRoot('downloads', 'downloads', safeAppPath('downloads'));
  }
  addRoot('diagnostics', 'diagnostics', diagnosticsExportDir());
  addRoot('sessions', 'sessions', sessionTranscriptDir());
  addRoot('runtime', 'runtime', process.cwd());
  addRoot('app', 'app', ROOT_DIR);
  return roots;
}

function candidatePreviewPath(rawPath, workspaceRoot) {
  const raw = String(rawPath || '').trim();
  if (!raw || raw.includes('\0') || raw.length > 1000) throw new Error('Invalid file preview path.');
  if (raw.startsWith('workspace:/')) {
    return path.resolve(workspaceRoot, sanitizeWorkspaceRelativePath(raw.replace(/^workspace:\//, '')));
  }
  if (/^file:/i.test(raw)) return path.resolve(fileURLToPath(raw));
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:\/\//.test(raw)) throw new Error('File preview only supports local files.');
  if (/^Users[\\/]/i.test(raw)) return path.resolve(path.parse(safeAppPath('home')).root, raw);
  const resolved = path.resolve(raw);
  return path.isAbsolute(raw) ? resolved : path.resolve(workspaceRoot, raw);
}

function resolveMojibakePreviewPath(target) {
  const input = String(target || '');
  if (!input || fs.existsSync(input) || !/[�?\uFFFD]/.test(path.basename(input))) return input;
  const dir = path.dirname(input);
  const ext = path.extname(input).toLowerCase();
  if (!ext || !fs.existsSync(dir)) return input;
  const tokens = path.basename(input, ext)
    .split(/[^A-Za-z0-9]+/)
    .map((token) => token.trim().toLowerCase())
    .filter((token) => token.length >= 2);
  try {
    const candidates = fs.readdirSync(dir, { withFileTypes: true })
      .filter((item) => item.isFile() && path.extname(item.name).toLowerCase() === ext)
      .map((item) => {
        const fullPath = path.join(dir, item.name);
        let stat = null;
        try {
          stat = fs.statSync(fullPath);
        } catch {
          return null;
        }
        const lowerName = item.name.toLowerCase();
        const score = tokens.reduce((total, token) => total + (lowerName.includes(token) ? 1 : 0), 0);
        return { fullPath, score, mtimeMs: stat?.mtimeMs || 0 };
      })
      .filter(Boolean)
      .filter((item) => !tokens.length || item.score > 0)
      .sort((left, right) => (right.score - left.score) || (right.mtimeMs - left.mtimeMs));
    return candidates[0]?.fullPath || input;
  } catch {
    return input;
  }
}

function resolveFilePreviewTarget(payload = {}) {
  const input = optionalObjectPayload(payload, 'file preview payload');
  const roots = filePreviewAllowedRoots(input);
  const workspaceRoot = roots.find((root) => root.kind === 'workspace')?.root || defaultWorkspaceRoot();
  const rawPath = input.path || input.filePath || input.pathLabel || input.url;
  let target = candidatePreviewPath(rawPath, workspaceRoot);
  let root = roots.find((entry) => isPathInside(entry.root, target));
  if (!root && isRegisteredSelectedAttachment(target, input)) {
    root = { kind: 'selected', label: 'selected', root: path.dirname(target) };
  }
  if (!root && isRegisteredAgentArtifact(target, input, workspaceRoot)) {
    root = { kind: 'agent-artifact', label: 'artifact', root: path.dirname(target) };
  }
  if (!root) throw new Error('File preview path is outside allowed roots.');
  const repairedTarget = resolveMojibakePreviewPath(target);
  if (repairedTarget !== target && isPathInside(root.root, repairedTarget)) target = repairedTarget;
  if (pathContainsSymlink(root.root, target)) throw new Error('File preview path crosses a symbolic link.');
  return { target, root };
}

function filePreviewPathLabel(target, root) {
  if (root?.kind === 'workspace' || root?.kind === 'project' || root?.kind === 'session') return publicWorkspacePathLabel(root.root, target);
  if (root?.kind === 'selected') return `selected:/${redactSensitiveText(path.basename(target)).slice(0, 240)}`;
  if (root?.kind === 'agent-artifact') return `artifact:/${redactSensitiveText(path.basename(target)).slice(0, 240)}`;
  const relative = path.relative(root.root, target).replace(/\\/g, '/');
  return `${root?.label || 'local'}:/${relative}`;
}

function looksLikeBinaryBuffer(buffer) {
  if (!Buffer.isBuffer(buffer) || !buffer.length) return false;
  const sample = buffer.subarray(0, Math.min(buffer.length, 4096));
  let controlCount = 0;
  for (const byte of sample) {
    if (byte === 0) return true;
    if (byte < 7 || (byte > 13 && byte < 32)) controlCount += 1;
  }
  return controlCount / sample.length > 0.08;
}

function sha256Hex(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function safeAttachmentName(value = '', fallback = 'attachment') {
  const name = redactSensitiveText(path.basename(String(value || '').trim() || fallback))
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 240);
  return name || fallback;
}

function isTextAttachmentMime(mimeType = '', extension = '') {
  const mime = String(mimeType || '').toLowerCase();
  if (mime.startsWith('text/')) return true;
  if (mime === 'application/json' || mime === 'application/x-ndjson') return true;
  return FILE_PREVIEW_TEXT_EXTENSIONS.has(String(extension || '').toLowerCase());
}

function isImageAttachmentMime(mimeType = '') {
  return /^image\/(png|jpe?g|webp|gif|svg\+xml)$/i.test(String(mimeType || ''));
}

function isVideoAttachmentMime(mimeType = '') {
  return /^video\/(mp4|webm|ogg|quicktime|x-m4v)$/i.test(String(mimeType || ''));
}

function parseAttachmentDataUrl(value = '') {
  const raw = String(value || '').trim();
  if (!raw || raw.length > ATTACHMENT_DATA_URL_MAX_CHARS || !raw.startsWith('data:')) return null;
  const match = raw.match(/^data:([^;,]+)?(;base64)?,([\s\S]*)$/i);
  if (!match) return null;
  const mimeType = (match[1] || 'application/octet-stream').toLowerCase();
  const isBase64 = Boolean(match[2]);
  try {
    const body = isBase64
      ? Buffer.from(match[3] || '', 'base64')
      : Buffer.from(decodeURIComponent(match[3] || ''), 'utf8');
    return { mimeType, buffer: body, isBase64 };
  } catch {
    return null;
  }
}

function publicAttachmentPathLabel(target, root) {
  if (root?.kind === 'workspace' || root?.kind === 'project' || root?.kind === 'cwd') {
    return publicWorkspacePathLabel(root.root, target);
  }
  if (root?.kind === 'selected') return `selected:/${safeAttachmentName(target)}`;
  return filePreviewPathLabel(target, root);
}

function attachmentAllowedRoots(context = {}) {
  const roots = [];
  const addRoot = (kind, label, root) => {
    const value = String(root || '').trim();
    if (!value) return;
    const resolved = path.resolve(value);
    if (!roots.some((entry) => isSameWorkspacePath(entry.root, resolved))) {
      roots.push({ kind, label, root: resolved });
    }
  };
  try {
    addRoot('workspace', 'workspace', readSettings().workspaceRoot);
  } catch {
    addRoot('workspace', 'workspace', defaultWorkspaceRoot());
  }
  addRoot('project', 'project', context.projectContext?.projectPath);
  addRoot('cwd', 'cwd', context.cwd);
  return roots;
}

function pruneSelectedAttachmentAccess(now = Date.now()) {
  for (const [key, entry] of selectedAttachmentAccess.entries()) {
    if (!entry || entry.expiresAt <= now) selectedAttachmentAccess.delete(key);
  }
  if (selectedAttachmentAccess.size <= MAX_SELECTED_ATTACHMENT_ACCESS) return;
  const entries = [...selectedAttachmentAccess.entries()].sort((left, right) => left[1].selectedAt - right[1].selectedAt);
  for (const [key] of entries.slice(0, selectedAttachmentAccess.size - MAX_SELECTED_ATTACHMENT_ACCESS)) {
    selectedAttachmentAccess.delete(key);
  }
}

function registerSelectedAttachmentAccess(filePath, summary = {}) {
  pruneSelectedAttachmentAccess();
  const target = path.resolve(filePath);
  selectedAttachmentAccess.set(target, {
    id: String(summary.id || '').trim(),
    name: safeAttachmentName(summary.name || target),
    sizeBytes: Number(summary.sizeBytes) || 0,
    modifiedAt: summary.modifiedAt || null,
    selectedAt: Date.now(),
    expiresAt: Date.now() + SELECTED_ATTACHMENT_ACCESS_TTL_MS
  });
}

function isRegisteredSelectedAttachment(target, input = {}) {
  pruneSelectedAttachmentAccess();
  const entry = selectedAttachmentAccess.get(path.resolve(target));
  if (!entry) return false;
  const requestedId = String(input.id || input.attachmentId || '').trim();
  if (!requestedId || !entry.id || requestedId !== entry.id) return false;
  try {
    const stat = fs.statSync(target);
    if (!stat.isFile()) return false;
    if (entry.sizeBytes && stat.size !== entry.sizeBytes) return false;
    return true;
  } catch {
    return false;
  }
}

function candidateAttachmentPath(rawPath, workspaceRoot) {
  const raw = String(rawPath || '').trim();
  if (!raw || raw.includes('\0') || raw.length > 1000) throw new Error('Invalid attachment path.');
  if (raw.startsWith('workspace:/')) {
    return path.resolve(workspaceRoot, sanitizeWorkspaceRelativePath(raw.replace(/^workspace:\//, '')));
  }
  if (/^file:/i.test(raw)) return path.resolve(fileURLToPath(raw));
  if (/^[a-zA-Z][a-zA-Z\d+.-]*:\/\//.test(raw)) throw new Error('Attachment path only supports local files.');
  return path.isAbsolute(raw) ? path.resolve(raw) : path.resolve(workspaceRoot, raw);
}

function resolveAttachmentTarget(input = {}, context = {}) {
  const rawPath = input.path || input.filePath || input.localPath || '';
  if (!rawPath) return null;
  const roots = attachmentAllowedRoots(context);
  const workspaceRoot = roots.find((root) => root.kind === 'workspace')?.root || defaultWorkspaceRoot();
  const target = candidateAttachmentPath(rawPath, workspaceRoot);
  let root = roots.find((entry) => isPathInside(entry.root, target));
  if (!root && isRegisteredSelectedAttachment(target, input)) {
    root = { kind: 'selected', label: 'selected', root: path.dirname(target) };
  }
  if (!root) throw new Error('Attachment path is outside the current workspace, project, or selected-file grant.');
  if (pathContainsSymlink(root.root, target)) throw new Error('Attachment path crosses a symbolic link.');
  return { target, root };
}

function attachmentMetadataFromPath(target, root, stat, input = {}) {
  const extension = path.extname(target).toLowerCase();
  const mimeType = String(input.mime || input.type || attachmentMimeFromPath(target) || filePreviewMimeFromExtension(extension)).toLowerCase();
  return {
    id: crypto.createHash('sha256').update(`${path.resolve(target)}:${stat?.size || 0}:${stat?.mtimeMs || 0}`).digest('hex').slice(0, 16),
    name: safeAttachmentName(input.name || target),
    source: root?.kind || 'file',
    pathLabel: redactSensitiveText(publicAttachmentPathLabel(target, root)).slice(0, 500),
    extension,
    mimeType,
    sizeBytes: stat?.isFile?.() ? stat.size : 0,
    modifiedAt: stat?.mtime ? stat.mtime.toISOString() : null
  };
}

function attachmentMetadataFromBuffer(input = {}, parsed = {}) {
  const mimeType = String(input.mime || input.type || parsed.mimeType || 'application/octet-stream').toLowerCase();
  return {
    id: crypto.createHash('sha256').update(`${input.name || 'clipboard'}:${parsed.buffer?.length || 0}:${mimeType}`).digest('hex').slice(0, 16),
    name: safeAttachmentName(input.name || 'clipboard-attachment'),
    source: 'clipboard',
    pathLabel: 'clipboard:/',
    extension: '',
    mimeType,
    sizeBytes: parsed.buffer?.length || 0,
    modifiedAt: null
  };
}

function textAttachmentContextFromBuffer(buffer, metadata) {
  if (buffer.length > ATTACHMENT_TEXT_MAX_BYTES) {
    return { ...metadata, kind: 'text', previewable: false, reason: 'too-large', maxBytes: ATTACHMENT_TEXT_MAX_BYTES };
  }
  if (looksLikeBinaryBuffer(buffer)) {
    return { ...metadata, kind: 'binary', previewable: false, reason: 'binary' };
  }
  const rawText = buffer.toString('utf8').replace(/^\uFEFF/, '');
  const content = redactSensitiveText(rawText).slice(0, ATTACHMENT_INLINE_TEXT_CHARS);
  return {
    ...metadata,
    kind: 'text',
    previewable: true,
    language: filePreviewLanguageFromExtension(metadata.extension),
    content,
    textExcerpt: content,
    truncated: rawText.length > ATTACHMENT_INLINE_TEXT_CHARS,
    redacted: content !== rawText.slice(0, ATTACHMENT_INLINE_TEXT_CHARS)
  };
}

function imageAttachmentContextFromBuffer(buffer, metadata) {
  if (buffer.length > ATTACHMENT_IMAGE_MAX_BYTES) {
    return { ...metadata, kind: 'image', previewable: false, reason: 'too-large', maxBytes: ATTACHMENT_IMAGE_MAX_BYTES };
  }
  const digest = sha256Hex(buffer);
  return {
    ...metadata,
    kind: 'image',
    previewable: true,
    sha256: digest,
    base64Sample: buffer.toString('base64').slice(0, ATTACHMENT_IMAGE_BASE64_SAMPLE_CHARS),
    base64SampleTruncated: buffer.toString('base64').length > ATTACHMENT_IMAGE_BASE64_SAMPLE_CHARS
  };
}

function ingestAttachmentFromPath(input = {}, context = {}) {
  const resolved = resolveAttachmentTarget(input, context);
  if (!resolved) return null;
  const { target, root } = resolved;
  let stat;
  try {
    stat = fs.statSync(target);
  } catch {
    return {
      ok: false,
      kind: 'missing',
      name: safeAttachmentName(input.name || target),
      pathLabel: root ? publicAttachmentPathLabel(target, root) : 'attachment:/',
      reason: 'not-found'
    };
  }
  const metadata = attachmentMetadataFromPath(target, root, stat, input);
  if (!stat.isFile()) return { ...metadata, kind: 'unsupported', previewable: false, reason: 'not-file' };
  const isText = isTextAttachmentMime(metadata.mimeType, metadata.extension);
  const isImage = isImageAttachmentMime(metadata.mimeType);
  if (!isText && !isImage) {
    return { ...metadata, kind: 'binary', previewable: false, reason: 'unsupported-type' };
  }
  if (stat.size > Math.max(ATTACHMENT_TEXT_MAX_BYTES, ATTACHMENT_IMAGE_MAX_BYTES)) {
    return { ...metadata, kind: isImage ? 'image' : 'text', previewable: false, reason: 'too-large' };
  }
  const buffer = fs.readFileSync(target);
  if (isText) return textAttachmentContextFromBuffer(buffer, metadata);
  return imageAttachmentContextFromBuffer(buffer, metadata);
}

function ingestAttachmentFromDataUrl(input = {}) {
  const dataUrl = input.previewUrl || input.previewDataUrl || input.dataUrl || '';
  const parsed = parseAttachmentDataUrl(dataUrl);
  if (!parsed) return null;
  const metadata = attachmentMetadataFromBuffer(input, parsed);
  if (isTextAttachmentMime(metadata.mimeType, metadata.extension)) {
    return textAttachmentContextFromBuffer(parsed.buffer, metadata);
  }
  if (isImageAttachmentMime(metadata.mimeType)) {
    return imageAttachmentContextFromBuffer(parsed.buffer, metadata);
  }
  return { ...metadata, kind: 'binary', previewable: false, reason: 'unsupported-type' };
}

function publicAttachmentContextItem(item = {}) {
  const base = {
    id: item.id,
    name: item.name,
    source: item.source,
    pathLabel: item.pathLabel,
    mimeType: item.mimeType,
    sizeBytes: item.sizeBytes,
    kind: item.kind,
    previewable: Boolean(item.previewable),
    reason: item.reason,
    sha256: item.sha256 ? String(item.sha256).slice(0, 64) : undefined,
    redacted: Boolean(item.redacted),
    truncated: Boolean(item.truncated)
  };
  if (item.kind === 'text' && item.textExcerpt) base.textExcerpt = safeOutputText(item.textExcerpt, ATTACHMENT_INLINE_TEXT_CHARS);
  if (item.kind === 'image' && item.base64Sample) {
    base.base64Sample = item.base64Sample;
    base.base64SampleTruncated = Boolean(item.base64SampleTruncated);
  }
  return base;
}

function rendererAttachmentNotes(payload = {}) {
  if (!payload.attachmentContext) return '';
  if (typeof payload.attachmentContext === 'string') {
    return redactSensitiveText(payload.attachmentContext).slice(0, 4000);
  }
  return safeOutputText(JSON.stringify(redactForLog(payload.attachmentContext)), 4000);
}

function buildAttachmentPromptBlock(context = {}) {
  const items = Array.isArray(context.items) ? context.items : [];
  const lines = [];
  if (items.length) {
    lines.push('[EcoreX attachment context]');
    lines.push('The following uploaded or pasted attachments were safely ingested by the desktop app. Use their content when answering. Path labels are public labels; do not assume hidden absolute paths.');
  }
  items.forEach((item, index) => {
    lines.push(`Attachment ${index + 1}: ${item.name || 'attachment'}`);
    lines.push(`- kind: ${item.kind || 'unknown'}`);
    lines.push(`- mime: ${item.mimeType || 'application/octet-stream'}`);
    lines.push(`- sizeBytes: ${item.sizeBytes || 0}`);
    if (item.pathLabel) lines.push(`- source: ${item.pathLabel}`);
    if (item.reason) lines.push(`- ingestion: ${item.reason}`);
    if (item.kind === 'text' && item.textExcerpt) {
      lines.push('- text excerpt:');
      lines.push(item.textExcerpt);
    } else if (item.kind === 'image') {
      if (item.sha256) lines.push(`- sha256: ${item.sha256}`);
      if (item.base64Sample) {
        lines.push(`- base64 sample${item.base64SampleTruncated ? ' (truncated)' : ''}:`);
        lines.push(item.base64Sample);
      }
    }
  });
  if (context.notes) {
    if (!lines.length) lines.push('[EcoreX attachment context]');
    lines.push('Renderer attachment notes:');
    lines.push(context.notes);
  }
  if (!lines.length) return '';
  const text = lines.join('\n').slice(0, MAX_ATTACHMENT_PROMPT_CHARS);
  return `${text}\n[/EcoreX attachment context]`;
}

function ingestAgentAttachments(payload = {}, context = {}) {
  const inputItems = [
    ...(Array.isArray(payload.attachments) ? payload.attachments : []),
    ...(Array.isArray(payload.attachmentContext?.attachments) ? payload.attachmentContext.attachments : [])
  ].slice(0, MAX_AGENT_ATTACHMENTS);
  const items = [];
  const warnings = [];
  for (const rawItem of inputItems) {
    const item = rawItem && typeof rawItem === 'object' ? rawItem : {};
    try {
      const ingested = ingestAttachmentFromPath(item, context) || ingestAttachmentFromDataUrl(item);
      if (ingested) {
        items.push(publicAttachmentContextItem(ingested));
      } else {
        warnings.push({ name: safeAttachmentName(item.name || 'attachment'), reason: 'no-readable-source' });
      }
    } catch (error) {
      warnings.push({
        name: safeAttachmentName(item.name || item.path || 'attachment'),
        reason: safeOutputText(error instanceof Error ? error.message : String(error), 500)
      });
    }
  }
  const notes = rendererAttachmentNotes(payload);
  const publicContext = {
    count: items.length,
    items,
    warnings,
    notes
  };
  return {
    ...publicContext,
    promptText: buildAttachmentPromptBlock(publicContext)
  };
}

function composePromptWithAttachmentContext(prompt, attachmentContext, maxPromptChars = MAX_PROMPT_CHARS) {
  const block = String(attachmentContext?.promptText || '').trim();
  if (!block) return prompt;
  const max = Math.max(MIN_PROMPT_CHARS, Math.min(Number(maxPromptChars) || MAX_PROMPT_CHARS, MAX_PROMPT_CHARS));
  const availablePromptChars = Math.max(MIN_PROMPT_CHARS, max - block.length - 2);
  const safePrompt = String(prompt || '').slice(0, availablePromptChars);
  return `${safePrompt}\n\n${block}`.slice(0, max);
}

function ingestAttachmentsForPreview(payload = {}) {
  const input = optionalObjectPayload(payload, 'attachment ingest payload');
  const settings = readSettings();
  const workspaceRoot = settings.workspaceRoot;
  const projectContext = input.projectId
    ? projectContextFromProject(workspaceRoot, resolveProject({ id: input.projectId }, { allowArchived: false }).project)
    : activeProjectContext();
  const cwd = projectContext?.projectPath || workspaceRoot;
  const attachmentContext = ingestAgentAttachments(input, { cwd, projectContext });
  return { ok: true, attachmentContext };
}

function filePreviewMetadata(target, root, stat = null) {
  const extension = path.extname(target).toLowerCase();
  return {
    id: crypto
      .createHash('sha256')
      .update(`${path.resolve(target)}:${stat?.size || 0}:${stat?.mtimeMs || 0}`)
      .digest('hex')
      .slice(0, 16),
    name: redactSensitiveText(path.basename(target)).slice(0, 240),
    pathLabel: redactSensitiveText(filePreviewPathLabel(target, root)).slice(0, 500),
    extension,
    mimeType: filePreviewMimeFromExtension(extension),
    sizeBytes: stat?.isFile?.() ? stat.size : 0,
    modifiedAt: stat?.mtime ? stat.mtime.toISOString() : null
  };
}

function documentPreviewKind(extension = '') {
  if (extension === '.pdf') return 'pdf';
  if (['.doc', '.docx'].includes(extension)) return 'word';
  if (['.xls', '.xlsx', '.xlsm'].includes(extension)) return 'spreadsheet';
  if (['.ppt', '.pptx', '.pptm'].includes(extension)) return 'presentation';
  return 'document';
}

function previewMetadataOnly(file, reason = 'metadata-only') {
  return {
    ok: true,
    previewable: false,
    reason,
    renderMode: 'metadata',
    file,
    metadata: {
      documentType: documentPreviewKind(file.extension),
      mimeType: file.mimeType,
      sizeBytes: file.sizeBytes,
      modifiedAt: file.modifiedAt
    }
  };
}

function vueOfficeResourceRoots() {
  const roots = [];
  const addRoot = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return;
    const resolved = path.resolve(raw);
    if (!roots.some((entry) => isSameWorkspacePath(entry, resolved))) roots.push(resolved);
  };
  if (app.isPackaged) {
    if (process.resourcesPath) addRoot(path.join(process.resourcesPath, VUE_OFFICE_VENDOR_DIR_NAME));
  } else {
    addRoot(process.env.ECOREX_VUE_OFFICE_HOME);
    addRoot(path.join(ROOT_DIR, 'vendor', VUE_OFFICE_VENDOR_DIR_NAME));
  }
  return roots;
}

function firstExistingFile(candidates = []) {
  for (const candidate of candidates.filter(Boolean)) {
    try {
      if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return path.resolve(candidate);
    } catch {
      // Keep probing other packaged resource shapes.
    }
  }
  return '';
}

function locateVueOfficeResource({ refresh = false } = {}) {
  if (!refresh && vueOfficePreviewState.resource) return vueOfficePreviewState.resource;
  for (const root of vueOfficeResourceRoots()) {
    const indexPath = firstExistingFile([path.join(root, 'index.html')]);
    const assets = {
      docxScript: firstExistingFile([path.join(root, 'js-preview-lib', 'docx.umd.js')]),
      docxStyle: firstExistingFile([path.join(root, 'js-preview-lib', 'docx.css')]),
      excelScript: firstExistingFile([path.join(root, 'js-preview-lib', 'excel.umd.js')]),
      excelStyle: firstExistingFile([path.join(root, 'js-preview-lib', 'excel.css')]),
      pdfScript: firstExistingFile([path.join(root, 'js-preview-lib', 'pdf.umd.js')]),
      pptxScript: firstExistingFile([path.join(root, 'js-preview-lib', 'pptx-preview.umd.js')])
    };
    if (indexPath && assets.docxScript && assets.docxStyle && assets.excelScript && assets.excelStyle && assets.pdfScript && assets.pptxScript) {
      vueOfficePreviewState.resource = {
        ok: true,
        root,
        indexPath,
        assets,
        packaged: app.isPackaged
      };
      return vueOfficePreviewState.resource;
    }
  }
  vueOfficePreviewState.resource = {
    ok: false,
    root: '',
    indexPath: '',
    assets: {},
    error: 'vue-office static viewer is not prepared. Run npm run prepare:vue-office.'
  };
  return vueOfficePreviewState.resource;
}

function vueOfficePreviewType(extension = '') {
  if (extension === '.pdf') return 'pdf';
  if (extension === '.docx') return 'docx';
  if (['.xls', '.xlsx', '.xlsm'].includes(extension)) return 'excel';
  if (['.pptx', '.pptm'].includes(extension)) return 'pptx';
  return '';
}

function vueOfficeAssetMime(filePath = '') {
  const extension = path.extname(filePath).toLowerCase();
  if (extension === '.html') return 'text/html; charset=utf-8';
  if (extension === '.js') return 'text/javascript; charset=utf-8';
  if (extension === '.css') return 'text/css; charset=utf-8';
  if (extension === '.json') return 'application/json; charset=utf-8';
  if (extension === '.svg') return 'image/svg+xml';
  if (extension === '.png') return 'image/png';
  if (extension === '.jpg' || extension === '.jpeg') return 'image/jpeg';
  if (extension === '.woff2') return 'font/woff2';
  return 'application/octet-stream';
}

function vueOfficeViewerCsp() {
  return [
    "default-src 'self' blob: data:",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self' http://127.0.0.1:* blob: data:",
    "worker-src 'self' blob: data:"
  ].join('; ');
}

function serveVueOfficeAsset(request, response, url) {
  const resource = locateVueOfficeResource();
  if (!resource.ok) {
    response.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(resource.error || 'vue-office viewer unavailable');
    return;
  }
  let relative = '';
  try {
    relative = decodeURIComponent(url.pathname || '/').replace(/\\/g, '/').replace(/^\/+/, '');
  } catch {
    response.writeHead(400);
    response.end();
    return;
  }
  if (!relative || relative.endsWith('/')) relative = `${relative}index.html`;
  if (relative.includes('\0')) {
    response.writeHead(400);
    response.end();
    return;
  }
  const target = path.resolve(resource.root, ...relative.split('/').filter(Boolean));
  if (!isPathInside(resource.root, target)) {
    response.writeHead(403);
    response.end();
    return;
  }
  let stat;
  try {
    stat = fs.statSync(target);
  } catch {
    response.writeHead(404);
    response.end();
    return;
  }
  if (!stat.isFile()) {
    response.writeHead(404);
    response.end();
    return;
  }
  response.writeHead(200, {
    'Content-Type': vueOfficeAssetMime(target),
    'Content-Length': stat.size,
    'Cache-Control': path.extname(target).toLowerCase() === '.html' ? 'no-store' : 'public, max-age=3600',
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': vueOfficeViewerCsp()
  });
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  fs.createReadStream(target).pipe(response);
}

function pruneVueOfficePreviewGrants(now = Date.now()) {
  for (const [token, grant] of vueOfficePreviewState.grants.entries()) {
    if (!grant || grant.expiresAt <= now) vueOfficePreviewState.grants.delete(token);
  }
  if (vueOfficePreviewState.grants.size <= VUE_OFFICE_MAX_GRANTS) return;
  const entries = [...vueOfficePreviewState.grants.entries()].sort((left, right) => left[1].createdAt - right[1].createdAt);
  for (const [token] of entries.slice(0, vueOfficePreviewState.grants.size - VUE_OFFICE_MAX_GRANTS)) {
    vueOfficePreviewState.grants.delete(token);
  }
}

function serveVueOfficePreviewFile(request, response, url) {
  const match = url.pathname.match(/^\/preview-file\/([a-f0-9]{32,64})(?:\/.*)?$/i);
  if (!match) {
    response.writeHead(404);
    response.end();
    return;
  }
  pruneVueOfficePreviewGrants();
  const grant = vueOfficePreviewState.grants.get(match[1]);
  if (!grant) {
    response.writeHead(404);
    response.end();
    return;
  }
  const stat = fs.statSync(grant.target);
  if (!stat.isFile() || stat.size !== grant.sizeBytes || stat.size > VUE_OFFICE_PREVIEW_MAX_BYTES) {
    response.writeHead(403);
    response.end();
    return;
  }
  response.writeHead(200, {
    'Content-Type': grant.mimeType || 'application/octet-stream',
    'Content-Length': stat.size,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Content-Disposition': `inline; filename*=UTF-8''${encodeURIComponent(grant.name || 'preview-file')}`
  });
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  fs.createReadStream(grant.target).pipe(response);
}

function handleVueOfficePreviewRequest(request, response) {
  try {
    if (!['GET', 'HEAD'].includes(request.method || '')) {
      response.writeHead(405);
      response.end();
      return;
    }
    const url = new URL(request.url || '/', 'http://127.0.0.1');
    if (url.pathname.startsWith('/preview-file/')) {
      serveVueOfficePreviewFile(request, response, url);
      return;
    }
    serveVueOfficeAsset(request, response, url);
  } catch (error) {
    response.writeHead(500);
    response.end();
    writeLog('warn', 'vue-office preview request failed', { error: safeOutputText(error?.message || String(error), 1000) });
  }
}

function ensureVueOfficePreviewServer() {
  if (vueOfficePreviewState.server && vueOfficePreviewState.port) {
    return Promise.resolve({ ok: true, port: vueOfficePreviewState.port, resource: vueOfficePreviewState.resource });
  }
  if (vueOfficePreviewState.starting) return vueOfficePreviewState.starting;
  vueOfficePreviewState.starting = new Promise((resolve) => {
    const resource = locateVueOfficeResource();
    if (!resource.ok) {
      vueOfficePreviewState.lastError = resource.error;
      resolve({ ok: false, reason: 'missing-vendor', error: resource.error, resource });
      return;
    }
    const server = http.createServer(handleVueOfficePreviewRequest);
    server.on('error', (error) => {
      vueOfficePreviewState.lastError = error?.message || String(error);
      resolve({ ok: false, reason: 'server-failed', error: vueOfficePreviewState.lastError, resource });
    });
    server.listen(0, '127.0.0.1', () => {
      vueOfficePreviewState.server = server;
      vueOfficePreviewState.port = server.address()?.port || 0;
      writeLog('info', 'vue-office preview server ready', { port: vueOfficePreviewState.port });
      resolve({ ok: Boolean(vueOfficePreviewState.port), port: vueOfficePreviewState.port, resource });
    });
  }).finally(() => {
    vueOfficePreviewState.starting = null;
  });
  return vueOfficePreviewState.starting;
}

function createVueOfficeSourceUrl(target, file) {
  pruneVueOfficePreviewGrants();
  const token = crypto.randomBytes(24).toString('hex');
  vueOfficePreviewState.grants.set(token, {
    target: path.resolve(target),
    name: file.name || path.basename(target),
    mimeType: file.mimeType || 'application/octet-stream',
    sizeBytes: file.sizeBytes || fs.statSync(target).size,
    createdAt: Date.now(),
    expiresAt: Date.now() + VUE_OFFICE_GRANT_TTL_MS
  });
  return `http://127.0.0.1:${vueOfficePreviewState.port}/preview-file/${token}/${encodeURIComponent(file.name || path.basename(target))}`;
}

function createVueOfficePreviewUrl(port, sourceUrl, file) {
  const params = new URLSearchParams({
    type: vueOfficePreviewType(file.extension),
    src: sourceUrl,
    name: file.name || 'preview-file'
  });
  return `http://127.0.0.1:${port}/index.html?${params.toString()}`;
}

function stopVueOfficePreviewServer(reason = 'stop') {
  if (vueOfficePreviewState.server) {
    try {
      vueOfficePreviewState.server.close();
    } catch {
      // Best-effort cleanup during app shutdown.
    }
    vueOfficePreviewState.server = null;
    vueOfficePreviewState.port = 0;
  }
  vueOfficePreviewState.grants.clear();
  writeLog('info', 'vue-office preview server stopped', { reason });
}

function publicVueOfficePreviewStatus() {
  const resource = locateVueOfficeResource();
  return {
    name: 'vue-office',
    available: Boolean(resource.ok),
    enabled: true,
    running: Boolean(vueOfficePreviewState.server && vueOfficePreviewState.port),
    port: vueOfficePreviewState.port || null,
    lastError: vueOfficePreviewState.lastError || resource.error || '',
    vendorRoot: resource.root ? redactSensitiveText(resource.root).slice(0, 500) : '',
    hasViewer: Boolean(resource.indexPath),
    assets: {
      docx: Boolean(resource.assets?.docxScript && resource.assets?.docxStyle),
      excel: Boolean(resource.assets?.excelScript && resource.assets?.excelStyle),
      pdf: Boolean(resource.assets?.pdfScript),
      pptx: Boolean(resource.assets?.pptxScript)
    }
  };
}

function canUseVueOfficePreview(file, stat) {
  if (!file || !stat?.isFile?.()) return false;
  if (!VUE_OFFICE_DOCUMENT_EXTENSIONS.has(file.extension)) return false;
  if (!vueOfficePreviewType(file.extension)) return false;
  return stat.size > 0 && stat.size <= VUE_OFFICE_PREVIEW_MAX_BYTES;
}

async function previewWithVueOffice(target, file, stat) {
  if (!canUseVueOfficePreview(file, stat)) return null;
  const engine = await ensureVueOfficePreviewServer();
  if (!engine.ok) {
    writeLog('warn', 'vue-office preview unavailable; falling back', {
      reason: engine.reason,
      error: safeOutputText(engine.error || '', 1000),
      file: file.name
    });
    return null;
  }
  const sourceUrl = createVueOfficeSourceUrl(target, file);
  return {
    ok: true,
    previewable: true,
    reason: null,
    kind: file.extension === '.pdf' ? 'pdf' : 'office',
    renderMode: 'vue-office',
    file,
    previewUrl: createVueOfficePreviewUrl(engine.port, sourceUrl, file),
    metadata: {
      documentType: documentPreviewKind(file.extension),
      mimeType: file.mimeType,
      sizeBytes: file.sizeBytes,
      modifiedAt: file.modifiedAt,
      previewEngine: 'vue-office',
      selectionBridge: false
    }
  };
}

function previewImageFile(target, file, stat) {
  if (stat.size > FILE_PREVIEW_IMAGE_MAX_BYTES) {
    return {
      ok: true,
      previewable: false,
      reason: 'too-large',
      renderMode: 'image',
      maxPreviewBytes: FILE_PREVIEW_IMAGE_MAX_BYTES,
      file,
      metadata: {
        mimeType: file.mimeType,
        sizeBytes: stat.size,
        modifiedAt: file.modifiedAt
      }
    };
  }
  const buffer = fs.readFileSync(target);
  const dataUrl = `data:${file.mimeType};base64,${buffer.toString('base64')}`;
  return {
    ok: true,
    previewable: true,
    reason: null,
    renderMode: 'image',
    file,
    metadata: {
      mimeType: file.mimeType,
      sizeBytes: stat.size,
      modifiedAt: file.modifiedAt,
      sha256: sha256Hex(buffer)
    },
    dataUrl,
    maxPreviewBytes: FILE_PREVIEW_IMAGE_MAX_BYTES
  };
}

function decodeXmlEntities(value = '') {
  return String(value || '')
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/&#x([0-9a-f]+);/gi, (_match, hex) => String.fromCodePoint(Number.parseInt(hex, 16) || 0))
    .replace(/&#(\d+);/g, (_match, dec) => String.fromCodePoint(Number.parseInt(dec, 10) || 0))
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

function stripXmlTags(value = '') {
  return decodeXmlEntities(String(value || '').replace(/<[^>]+>/g, ' '))
    .replace(/[ \t]+/g, ' ')
    .replace(/\s+\n/g, '\n')
    .trim();
}

function readZipEntries(buffer, wanted) {
  const wantedSet = new Set(Array.isArray(wanted) ? wanted : []);
  const result = new Map();
  if (!Buffer.isBuffer(buffer) || buffer.length < 22) return result;
  const maxCommentSearch = Math.max(0, buffer.length - 0xffff - 22);
  let eocd = -1;
  for (let offset = buffer.length - 22; offset >= maxCommentSearch; offset -= 1) {
    if (buffer.readUInt32LE(offset) === 0x06054b50) {
      eocd = offset;
      break;
    }
  }
  if (eocd < 0) return result;
  const totalEntries = buffer.readUInt16LE(eocd + 10);
  const centralOffset = buffer.readUInt32LE(eocd + 16);
  let offset = centralOffset;
  for (let index = 0; index < totalEntries && offset + 46 <= buffer.length; index += 1) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) break;
    const compression = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const fileNameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localOffset = buffer.readUInt32LE(offset + 42);
    const nameStart = offset + 46;
    const nameEnd = nameStart + fileNameLength;
    const name = buffer.subarray(nameStart, nameEnd).toString('utf8').replace(/\\/g, '/');
    if ((!wantedSet.size || wantedSet.has(name)) && localOffset + 30 <= buffer.length && compressedSize <= FILE_PREVIEW_OFFICE_MAX_BYTES) {
      try {
        if (buffer.readUInt32LE(localOffset) === 0x04034b50) {
          const localNameLength = buffer.readUInt16LE(localOffset + 26);
          const localExtraLength = buffer.readUInt16LE(localOffset + 28);
          const dataStart = localOffset + 30 + localNameLength + localExtraLength;
          const dataEnd = dataStart + compressedSize;
          if (dataEnd <= buffer.length) {
            const compressed = buffer.subarray(dataStart, dataEnd);
            const data = compression === 0
              ? compressed
              : compression === 8
                ? zlib.inflateRawSync(compressed)
                : null;
            if (data) result.set(name, data.toString('utf8'));
          }
        }
      } catch {
        // Keep preview best-effort; malformed entries simply fall back to metadata.
      }
    }
    offset = nameEnd + extraLength + commentLength;
  }
  return result;
}

function xmlTextValues(xml = '', tagName = 't') {
  const escaped = String(tagName || 't').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`<[^:>/]*:?${escaped}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/[^:>]*:?${escaped}>`, 'gi');
  return [...String(xml || '').matchAll(pattern)]
    .map((match) => stripXmlTags(match[1]))
    .filter(Boolean);
}

function excelColumnIndex(cellRef = '') {
  const letters = String(cellRef || '').match(/^[A-Z]+/i)?.[0]?.toUpperCase() || '';
  let index = 0;
  for (const letter of letters) index = index * 26 + (letter.charCodeAt(0) - 64);
  return Math.max(0, index - 1);
}

function parseSharedStrings(xml = '') {
  return [...String(xml || '').matchAll(/<si[\s\S]*?<\/si>/gi)]
    .map((match) => xmlTextValues(match[0], 't').join(' ').trim())
    .map((value) => decodeXmlEntities(value));
}

function parseWorksheetPreview(xml = '', sharedStrings = []) {
  const rows = [];
  for (const rowMatch of String(xml || '').matchAll(/<row\b[\s\S]*?<\/row>/gi)) {
    const cells = [];
    for (const cellMatch of rowMatch[0].matchAll(/<c\b([^>]*)>([\s\S]*?)<\/c>/gi)) {
      const attrs = cellMatch[1] || '';
      const body = cellMatch[2] || '';
      const ref = attrs.match(/\br="([^"]+)"/i)?.[1] || '';
      const type = attrs.match(/\bt="([^"]+)"/i)?.[1] || '';
      const column = excelColumnIndex(ref) || cells.length;
      let value = '';
      if (type === 's') {
        const indexText = body.match(/<v[^>]*>([\s\S]*?)<\/v>/i)?.[1];
        value = sharedStrings[Number(indexText)] || '';
      } else if (type === 'inlineStr') {
        value = xmlTextValues(body, 't').join(' ');
      } else {
        value = stripXmlTags(body.match(/<v[^>]*>([\s\S]*?)<\/v>/i)?.[1] || '');
      }
      cells[column] = value;
    }
    if (cells.some(Boolean)) rows.push(cells.map((cell) => String(cell || '').trim()));
    if (rows.length >= 40) break;
  }
  return rows;
}

function markdownTableFromRows(rows = []) {
  const visible = rows.filter((row) => row.some(Boolean)).slice(0, 40);
  if (!visible.length) return '';
  const width = Math.min(12, Math.max(...visible.map((row) => row.length)));
  const normalized = visible.map((row) => Array.from({ length: width }, (_item, index) => String(row[index] || '').replace(/\|/g, '\\|').slice(0, 120)));
  const [first, ...rest] = normalized;
  return [
    `| ${first.join(' | ')} |`,
    `| ${Array.from({ length: width }, () => '---').join(' | ')} |`,
    ...rest.map((row) => `| ${row.join(' | ')} |`)
  ].join('\n');
}

function previewOpenXmlOfficeFile(target, file, stat) {
  if (stat.size > FILE_PREVIEW_OFFICE_MAX_BYTES) {
    return previewMetadataOnly(file, 'too-large');
  }
  const buffer = fs.readFileSync(target);
  const extension = file.extension;
  if (extension === '.docx') {
    const entries = readZipEntries(buffer, ['word/document.xml']);
    const content = xmlTextValues(entries.get('word/document.xml') || '', 't').join('\n').trim();
    return content
      ? { ok: true, previewable: true, kind: 'text', renderMode: 'text', language: 'markdown', file, content: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), text: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), truncated: content.length > FILE_PREVIEW_OFFICE_MAX_CHARS, metadata: { documentType: 'word', mimeType: file.mimeType, sizeBytes: file.sizeBytes } }
      : previewMetadataOnly(file, 'empty-document');
  }
  if (extension === '.pptx' || extension === '.pptm') {
    const slideNames = [];
    for (let index = 1; index <= 80; index += 1) slideNames.push(`ppt/slides/slide${index}.xml`);
    const entries = readZipEntries(buffer, slideNames);
    const slides = [...entries.entries()]
      .sort(([left], [right]) => Number(left.match(/slide(\d+)/i)?.[1] || 0) - Number(right.match(/slide(\d+)/i)?.[1] || 0))
      .map(([name, xml], index) => {
        const lines = xmlTextValues(xml, 't').map((text) => text.trim()).filter(Boolean);
        return lines.length ? `## 幻灯片 ${index + 1}\n${lines.map((line) => `- ${line}`).join('\n')}` : '';
      })
      .filter(Boolean);
    const content = slides.join('\n\n').trim();
    return content
      ? { ok: true, previewable: true, kind: 'text', renderMode: 'text', language: 'markdown', file, content: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), text: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), truncated: content.length > FILE_PREVIEW_OFFICE_MAX_CHARS, metadata: { documentType: 'presentation', mimeType: file.mimeType, sizeBytes: file.sizeBytes } }
      : previewMetadataOnly(file, 'empty-presentation');
  }
  if (extension === '.xlsx' || extension === '.xlsm') {
    const wanted = ['xl/sharedStrings.xml'];
    for (let index = 1; index <= 20; index += 1) wanted.push(`xl/worksheets/sheet${index}.xml`);
    const entries = readZipEntries(buffer, wanted);
    const sharedStrings = parseSharedStrings(entries.get('xl/sharedStrings.xml') || '');
    const sheets = [...entries.entries()]
      .filter(([name]) => /^xl\/worksheets\/sheet\d+\.xml$/i.test(name))
      .sort(([left], [right]) => Number(left.match(/sheet(\d+)/i)?.[1] || 0) - Number(right.match(/sheet(\d+)/i)?.[1] || 0))
      .map(([name, xml], index) => {
        const table = markdownTableFromRows(parseWorksheetPreview(xml, sharedStrings));
        return table ? `## 工作表 ${index + 1}\n${table}` : '';
      })
      .filter(Boolean);
    const content = sheets.join('\n\n').trim();
    return content
      ? { ok: true, previewable: true, kind: 'text', renderMode: 'text', language: 'markdown', file, content: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), text: content.slice(0, FILE_PREVIEW_OFFICE_MAX_CHARS), truncated: content.length > FILE_PREVIEW_OFFICE_MAX_CHARS, metadata: { documentType: 'spreadsheet', mimeType: file.mimeType, sizeBytes: file.sizeBytes } }
      : previewMetadataOnly(file, 'empty-spreadsheet');
  }
  return previewMetadataOnly(file, 'metadata-only');
}

async function previewFile(payload = {}) {
  const input = optionalObjectPayload(payload, 'file preview payload');
  const { target, root } = resolveFilePreviewTarget(input);
  let stat;
  try {
    stat = fs.statSync(target);
  } catch {
    return {
      ok: false,
      previewable: false,
      reason: 'not-found',
      file: filePreviewMetadata(target, root),
      error: 'File was not found.'
    };
  }

  const file = filePreviewMetadata(target, root, stat);
  if (!stat.isFile()) {
    return { ok: true, previewable: false, reason: 'not-file', renderMode: 'metadata', file };
  }
  if (input.validateOnly === true || input.metadataOnly === true) {
    return {
      ok: true,
      previewable: true,
      reason: null,
      renderMode: 'metadata',
      file
    };
  }
  if (isImagePreviewExtension(file.extension)) {
    return previewImageFile(target, file, stat);
  }
  if (VUE_OFFICE_DOCUMENT_EXTENSIONS.has(file.extension)) {
    const richPreview = await previewWithVueOffice(target, file, stat);
    if (richPreview) return richPreview;
  }
  if (['.docx', '.xlsx', '.xlsm', '.pptx', '.pptm'].includes(file.extension)) {
    return previewOpenXmlOfficeFile(target, file, stat);
  }
  if (isDocumentMetadataPreviewExtension(file.extension)) {
    return previewMetadataOnly(file, 'metadata-only');
  }
  if (stat.size > FILE_PREVIEW_MAX_BYTES) {
    return {
      ok: true,
      previewable: false,
      reason: 'too-large',
      renderMode: 'metadata',
      maxPreviewBytes: FILE_PREVIEW_MAX_BYTES,
      file
    };
  }
  if (!FILE_PREVIEW_TEXT_EXTENSIONS.has(file.extension)) {
    return { ok: true, previewable: false, reason: 'unsupported-type', renderMode: 'metadata', file };
  }

  const buffer = fs.readFileSync(target);
  if (looksLikeBinaryBuffer(buffer)) {
    return { ok: true, previewable: false, reason: 'binary', renderMode: 'metadata', file };
  }

  const rawText = buffer.toString('utf8').replace(/^\uFEFF/, '');
  const content = redactSensitiveText(rawText);
  return {
    ok: true,
    previewable: true,
    reason: null,
    file,
    content,
    text: content,
    encoding: 'utf8',
    language: filePreviewLanguageFromExtension(file.extension),
    renderMode: filePreviewRenderMode(file.extension),
    sandbox: isHtmlPreviewExtension(file.extension)
      ? { allowScripts: false, allowSameOrigin: false, allowForms: false, allowPopups: false }
      : undefined,
    redacted: content !== rawText,
    maxPreviewBytes: FILE_PREVIEW_MAX_BYTES
  };
}

async function openArtifactFile(payload = {}) {
  const input = optionalObjectPayload(payload, 'artifact open payload');
  const { target, root } = resolveFilePreviewTarget({
    ...input,
    source: input.source || 'assistant-artifact'
  });
  if (pathContainsSymlink(root.root, target) || fs.lstatSync(target).isSymbolicLink()) {
    throw new Error('Artifact path crosses a symbolic link.');
  }
  const stat = fs.statSync(target);
  if (!stat.isFile()) throw new Error('Artifact is not a file.');
  const opened = await openPathSafely(target);
  return {
    ok: Boolean(opened.opened),
    opened: Boolean(opened.opened),
    method: 'openPath',
    name: redactSensitiveText(path.basename(target)).slice(0, 240),
    pathLabel: redactSensitiveText(filePreviewPathLabel(target, root)).slice(0, 500),
    error: opened.error || undefined
  };
}

function attachmentMimeFromPath(filePath = '') {
  const ext = path.extname(filePath).toLowerCase();
  if (['.png'].includes(ext)) return 'image/png';
  if (['.jpg', '.jpeg'].includes(ext)) return 'image/jpeg';
  if (['.webp'].includes(ext)) return 'image/webp';
  if (['.gif'].includes(ext)) return 'image/gif';
  if (['.svg'].includes(ext)) return 'image/svg+xml';
  if (['.mp4', '.m4v'].includes(ext)) return 'video/mp4';
  if (['.webm'].includes(ext)) return 'video/webm';
  if (['.ogg', '.ogv'].includes(ext)) return 'video/ogg';
  if (['.mov'].includes(ext)) return 'video/quicktime';
  if (['.pdf'].includes(ext)) return 'application/pdf';
  if (['.csv'].includes(ext)) return 'text/csv';
  if (ext === '.html' || ext === '.htm') return 'text/html';
  if (ext === '.css') return 'text/css';
  if (['.js', '.mjs', '.cjs'].includes(ext)) return 'text/javascript';
  if (['.txt', '.md', '.markdown', '.json', '.jsonl', '.log'].includes(ext)) return 'text/plain';
  if (['.xlsx'].includes(ext)) return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  if (['.xls'].includes(ext)) return 'application/vnd.ms-excel';
  if (['.docx'].includes(ext)) return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  if (['.pptx'].includes(ext)) return 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
  return 'application/octet-stream';
}

function selectedAttachmentPreviewDataUrl(absolutePath, type, stat) {
  if (isImageAttachmentMime(type)) {
    try {
      const image = nativeImage.createFromPath(absolutePath);
      if (!image.isEmpty()) {
        const size = image.getSize();
        const maxEdge = 160;
        const scale = Math.min(1, maxEdge / Math.max(size.width || maxEdge, size.height || maxEdge));
        const thumbnail = scale < 1
          ? image.resize({ width: Math.max(1, Math.round(size.width * scale)), height: Math.max(1, Math.round(size.height * scale)) })
          : image;
        return thumbnail.toDataURL();
      }
    } catch {
      // Fall through to the raw-data fallback for formats nativeImage cannot decode.
    }
  }
  if ((isImageAttachmentMime(type) || isVideoAttachmentMime(type)) && stat.size <= Math.floor(ATTACHMENT_PREVIEW_MAX_BYTES * 0.55)) {
    try {
      return `data:${type};base64,${fs.readFileSync(absolutePath).toString('base64')}`;
    } catch {
      return '';
    }
  }
  return '';
}

function selectedAttachmentSummary(filePath) {
  const absolutePath = path.resolve(filePath);
  const stat = fs.statSync(absolutePath);
  if (!stat.isFile()) throw new Error('Selected item is not a file.');
  const type = attachmentMimeFromPath(absolutePath);
  const entry = {
    id: crypto.createHash('sha256').update(`${absolutePath}:${stat.size}:${stat.mtimeMs}`).digest('hex').slice(0, 16),
    name: path.basename(absolutePath),
    path: absolutePath,
    type,
    sizeBytes: stat.size,
    modifiedAt: stat.mtime.toISOString(),
    previewDataUrl: ''
  };
  entry.previewDataUrl = selectedAttachmentPreviewDataUrl(absolutePath, type, stat);
  registerSelectedAttachmentAccess(absolutePath, entry);
  return entry;
}

async function selectAttachmentFiles(event, payload = {}) {
  optionalObjectPayload(payload, 'attachment selection payload');
  const limit = Math.max(1, Math.min(20, Number(payload.limit) || 10));
  const result = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender) || mainWindow, {
    title: '选择要发送给 EcoreX 亦芯的文件',
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: '常用文件', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'mp4', 'webm', 'mov', 'm4v', 'ogg', 'ogv', 'pdf', 'csv', 'xlsx', 'xls', 'docx', 'pptx', 'txt', 'md', 'json'] },
      { name: '所有文件', extensions: ['*'] }
    ]
  });
  if (result.canceled || !result.filePaths?.length) {
    return { ok: true, canceled: true, files: [] };
  }
  const files = [];
  for (const filePath of result.filePaths.slice(0, limit)) {
    try {
      files.push(selectedAttachmentSummary(filePath));
    } catch (error) {
      writeLog('warn', 'Skipped invalid attachment selection', {
        error: error instanceof Error ? error.message : String(error)
      });
    }
  }
  return { ok: true, canceled: false, files };
}

async function openSelectedAttachmentFile(payload = {}) {
  const input = optionalObjectPayload(payload, 'attachment open payload');
  const rawPath = input.path || input.filePath || input.localPath || '';
  if (!rawPath) return { ok: false, opened: false, error: 'Attachment has no local file path.' };
  const target = path.resolve(fileURLToPath(/^file:/i.test(String(rawPath)) ? rawPath : pathToFileURL(path.resolve(rawPath)).toString()));
  if (!isRegisteredSelectedAttachment(target, input)) {
    throw new Error('Attachment open requires a selected-file grant.');
  }
  const parent = path.dirname(target);
  if (pathContainsSymlink(parent, target) || fs.lstatSync(target).isSymbolicLink()) {
    throw new Error('Attachment path crosses a symbolic link.');
  }
  const stat = fs.statSync(target);
  if (!stat.isFile()) throw new Error('Attachment is not a file.');
  const opened = await openPathSafely(target);
  return {
    ok: Boolean(opened.opened),
    opened: Boolean(opened.opened),
    method: 'openPath',
    name: path.basename(target),
    pathLabel: `selected:/${redactSensitiveText(path.basename(target)).slice(0, 240)}`,
    error: opened.error || undefined
  };
}

function collectReleaseInstallers() {
  const releaseDir = devPath('release');
  const installerPattern = /\.(exe|msi|dmg|pkg|appimage|deb|rpm)$/i;
  let entries = [];
  try {
    entries = fs
      .readdirSync(releaseDir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && installerPattern.test(entry.name))
      .map((entry) => fileSummary(path.join(releaseDir, entry.name)));
  } catch {
    entries = [];
  }
  return {
    directory: releaseDir,
    exists: fs.existsSync(releaseDir),
    installers: entries,
    metadata: collectReleaseMetadata(releaseDir, entries)
  };
}

function collectReleaseMetadata(releaseDir = devPath('release'), installers = []) {
  const latest = fileSummary(path.join(releaseDir, 'latest.yml'));
  const metadata = {
    latest,
    blockmaps: [],
    status: 'missing',
    issues: []
  };
  try {
    if (fs.existsSync(releaseDir)) {
      metadata.blockmaps = fs
        .readdirSync(releaseDir, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith('.blockmap'))
        .map((entry) => fileSummary(path.join(releaseDir, entry.name)));
    }
  } catch (error) {
    metadata.issues.push(error instanceof Error ? error.message : String(error));
  }

  if (!latest.exists) metadata.issues.push('latest.yml is missing.');
  if (!Array.isArray(installers) || installers.length === 0) metadata.issues.push('No release installer artifacts were found.');
  if (metadata.blockmaps.length === 0) metadata.issues.push('No blockmap files were found.');

  metadata.status = metadata.issues.length ? 'incomplete' : 'present';
  return metadata;
}

function collectSigningStatus() {
  return {
    status: 'not_checked',
    signed: null,
    placeholder: true,
    reason: 'Code signing verification is not implemented in the desktop health check yet.',
    executable: fileSummary(process.execPath)
  };
}

function collectPathDiagnostics(settings = readSettings()) {
  return {
    root: ROOT_DIR,
    appPath: app.getAppPath(),
    userData: app.getPath('userData'),
    logs: app.getPath('logs'),
    logFile: logPath(),
    workspaceRoot: settings.workspaceRoot,
    main: fileSummary(__filename),
    preload: fileSummary(path.join(__dirname, 'preload.cjs')),
    distIndex: fileSummary(devPath('dist', 'index.html')),
    resources: process.resourcesPath || null
  };
}

function summarizeBackendStatus(status = {}) {
  return {
    ok: Boolean(status.ok),
    refreshedAt: status.refreshedAt || null,
    agentBridgeAvailable: Boolean(status.agentBridge?.available),
    agentBridgeVersion: status.agentBridge?.version || null,
    authLoggedIn: Boolean(status.auth?.loggedIn),
    dataConnectionsConfigured: Boolean(status.dataConnections?.configured),
    dataConnectionCount: Array.isArray(status.dataConnections?.services)
      ? status.dataConnections.services.length
      : 0,
    skillInventorySummary: status.skillPacks?.summary || null
  };
}

function summarizeCapabilities(capabilities = {}) {
  return {
    ok: true,
    capabilityPacks: Array.isArray(capabilities.capabilityPacks)
      ? capabilities.capabilityPacks.length
      : 0,
    totals: capabilities.totals || { commands: 0, agents: 0, skills: 0, hooks: 0, bins: 0 },
    permissionModes: Array.isArray(capabilities.permissionModes)
      ? capabilities.permissionModes.map((mode) => mode.value)
      : [],
    models: Array.isArray(capabilities.models)
      ? capabilities.models.map((model) => model.value)
      : [],
    builtIns: Array.isArray(capabilities.builtIns) ? capabilities.builtIns.length : 0
  };
}

function appVersionSummary(version = {}) {
  return {
    name: version.name || app.getName(),
    version: version.app || version.version || app.getVersion(),
    electron: version.electron || process.versions.electron,
    chrome: version.chrome || process.versions.chrome,
    node: version.node || process.versions.node,
    v8: version.v8 || process.versions.v8,
    platform: version.platform || process.platform,
    arch: version.arch || process.arch,
    packaged: Object.prototype.hasOwnProperty.call(version, 'packaged') ? Boolean(version.packaged) : app.isPackaged
  };
}

function diagnosticFileSummary(summaryOrPath) {
  const summary = typeof summaryOrPath === 'string' ? fileSummary(summaryOrPath) : (summaryOrPath || {});
  return {
    name: summary.path ? path.basename(summary.path) : null,
    exists: Boolean(summary.exists),
    type: summary.type || null,
    size: typeof summary.size === 'number' ? summary.size : 0,
    sizeMb: typeof summary.sizeMb === 'number' ? summary.sizeMb : 0,
    modifiedAt: summary.modifiedAt || null
  };
}

function releaseArtifactSummary(release = collectReleaseInstallers()) {
  const metadata = release.metadata || {};
  return {
    exists: Boolean(release.exists),
    status: metadata.status || 'missing',
    installerCount: Array.isArray(release.installers) ? release.installers.length : 0,
    installers: Array.isArray(release.installers)
      ? release.installers.map(diagnosticFileSummary)
      : [],
    latest: diagnosticFileSummary(metadata.latest || {}),
    blockmaps: Array.isArray(metadata.blockmaps)
      ? metadata.blockmaps.map(diagnosticFileSummary)
      : [],
    issues: Array.isArray(metadata.issues)
      ? metadata.issues.map((issue) => safeDiagnosticText(issue, 500))
      : []
  };
}

function safeSessionSummaryForDiagnostics(session = {}) {
  const lastEvent = session.lastEvent && typeof session.lastEvent === 'object'
    ? {
        time: session.lastEvent.time || null,
        kind: safeDiagnosticText(session.lastEvent.kind || '', 80),
        status: safeDiagnosticText(session.lastEvent.status || '', 120),
        state: safeDiagnosticText(session.lastEvent.state || '', 80)
      }
    : null;
  return {
    id: session.sessionId ? publicStableId('session', session.sessionId) : null,
    status: safeDiagnosticText(session.status || 'unknown', 80),
    state: safeDiagnosticText(session.state || session.status || 'unknown', 80),
    reason: session.reason ? safeDiagnosticText(session.reason, 120) : undefined,
    exitCode: typeof session.exitCode === 'number' ? session.exitCode : undefined,
    signal: session.signal ? safeDiagnosticText(session.signal, 80) : undefined,
    startedAt: session.startedAtIso || session.startedAt || null,
    endedAt: session.endedAt || null,
    modifiedAt: session.modifiedAt || null,
    durationMs: Number(session.durationMs) || 0,
    eventCount: Number(session.eventCount) || 0,
    promptFingerprint: session.promptHash ? String(session.promptHash).slice(0, 12) : undefined,
    workspace: session.workspacePath ? safeDiagnosticText(session.workspacePath, 240) : undefined,
    model: session.model ? safeDiagnosticText(session.model, 80) : undefined,
    accessMode: session.accessMode ? safeDiagnosticText(session.accessMode, 80) : undefined,
    permissionMode: session.permissionMode ? safeDiagnosticText(session.permissionMode, 80) : undefined,
    attachmentCount: Number(session.attachmentCount) || 0,
    ledgerEventCount: Number(session.ledgerEventCount) || 0,
    hasTranscript: Boolean(session.hasTranscript),
    lastEvent
  };
}

function safeLogSummary(limit = MAX_DIAGNOSTIC_LOG_LINES) {
  const max = Math.min(Math.max(Number(limit) || MAX_DIAGNOSTIC_LOG_LINES, 1), MAX_LOG_LINES);
  const recent = readRecentLogs(max).map((entry = {}) => {
    const { time, level, message, ...meta } = entry;
    const safeMeta = safeDiagnosticValue(meta, {
      stringLimit: 1200,
      maxArrayItems: 20
    });
    return {
      time: time || null,
      level: safeDiagnosticText(level || 'info', 40),
      message: safeDiagnosticText(message || '', 500),
      meta: Object.keys(safeMeta || {}).length ? safeMeta : undefined
    };
  });
  const byLevel = recent.reduce((acc, entry) => {
    acc[entry.level] = (acc[entry.level] || 0) + 1;
    return acc;
  }, {});
  return {
    file: diagnosticFileSummary(logPath()),
    count: recent.length,
    byLevel,
    recent
  };
}

function healthSummaryForDiagnostics(health = {}) {
  const release = releaseArtifactSummary(health.release || collectReleaseInstallers());
  const crashes = readCrashSummary(10);
  const runningSessions = Array.isArray(health.runningSessions) ? health.runningSessions : [];
  return {
    ok: Boolean(health.ok),
    generatedAt: health.generatedAt || new Date().toISOString(),
    version: appVersionSummary(health.version),
    window: safeDiagnosticValue(health.window || null, {
      stringLimit: 1000,
      maxArrayItems: 20
    }),
    backend: safeDiagnosticValue(health.backend || {}, {
      stringLimit: 1000,
      maxArrayItems: 20
    }),
    runtimeEngine: {
      available: Boolean(health.cli?.available),
      version: health.cli?.version ? safeDiagnosticText(health.cli.version, 120) : null,
      nativePackage: health.cli?.nativePackage ? safeDiagnosticText(health.cli.nativePackage, 120) : null
    },
    agentRuntime: safeDiagnosticValue(health.agentRuntime || publicAgentRuntimeStatus(), {
      stringLimit: 1000,
      maxArrayItems: 12
    }),
    capabilities: safeDiagnosticValue(health.capabilities || {}, {
      stringLimit: 1000,
      maxArrayItems: 40
    }),
    sessions: {
      runningCount: runningSessions.length
    },
    logs: {
      file: diagnosticFileSummary(logPath()),
      recentCount: Array.isArray(health.logs?.recent) ? health.logs.recent.length : 0
    },
    crashes: {
      total: crashes.total,
      lastCrashAt: crashes.lastCrashAt,
      counts: crashes.counts
    },
    release: {
      exists: release.exists,
      status: release.status,
      installerCount: release.installerCount,
      issues: release.issues
    }
  };
}

async function buildDiagnosticsPackage(payload = {}) {
  const logLimit = Math.min(Math.max(Number(payload.logLimit) || MAX_DIAGNOSTIC_LOG_LINES, 1), MAX_LOG_LINES);
  const sessionLimit = Math.min(Math.max(Number(payload.sessionLimit) || MAX_DIAGNOSTIC_SESSIONS, 1), MAX_RECENT_SESSION_FILES);
  const healthResult = await Promise.allSettled([
    collectStartupHealth(null, { refresh: Boolean(payload.refresh || payload.forceRefresh) })
  ]);
  const health = healthResult[0].status === 'fulfilled'
    ? healthResult[0].value
    : {
        ok: false,
        generatedAt: new Date().toISOString(),
        error: healthResult[0].reason instanceof Error
          ? healthResult[0].reason.message
          : String(healthResult[0].reason || 'Startup health collection failed.')
      };
  const release = collectReleaseInstallers();
  const crashes = readCrashSummary(MAX_CRASH_EVENTS);
  return {
    schema: 'ecorex.diagnostics.v1',
    generatedAt: new Date().toISOString(),
    app: appVersionSummary(health.version),
    health: healthSummaryForDiagnostics(health),
    logs: safeLogSummary(logLimit),
    sessions: {
      running: getRunningSessionSummaries().map(safeSessionSummaryForDiagnostics),
      recent: recentSessionFiles(sessionLimit).map(safeSessionSummaryForDiagnostics),
      unfinished: recentUnfinishedRunJournals(sessionLimit)
    },
    crashes,
    releaseArtifacts: releaseArtifactSummary(release),
    privacy: {
      redacted: ['api keys', 'auth tokens', 'passwords', 'prompt text', 'local path roots'],
      includesApiKeys: false,
      includesPromptFullText: false,
      includesLocalPathBodies: false
    }
  };
}

async function openPathSafely(target) {
  try {
    const error = await shell.openPath(target);
    return {
      opened: !error,
      error: error ? safeDiagnosticText(error, 500) : null
    };
  } catch (error) {
    return {
      opened: false,
      error: safeDiagnosticText(error instanceof Error ? error.message : String(error), 500)
    };
  }
}

function normalizeExternalUrl(rawValue = '') {
  const raw = String(rawValue || '').trim();
  if (!raw || raw.length > 2048 || /[\u0000-\u001F\u007F]/.test(raw)) {
    throw new Error('Invalid external URL.');
  }
  const value = /^www\./i.test(raw) ? `https://${raw}` : raw;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('Invalid external URL.');
  }
  if (!['http:', 'https:', 'mailto:'].includes(parsed.protocol)) {
    throw new Error('Only http, https and mailto links can be opened.');
  }
  if ((parsed.protocol === 'http:' || parsed.protocol === 'https:') && (parsed.username || parsed.password)) {
    throw new Error('External URLs with embedded credentials are blocked.');
  }
  return parsed.toString();
}

async function openExternalUrl(payload = {}) {
  const input = optionalObjectPayload(payload, 'external URL payload');
  const url = normalizeExternalUrl(input.url || input.href || input.value || '');
  await shell.openExternal(url, { activate: true });
  return {
    ok: true,
    opened: true,
    method: 'openExternal',
    protocol: new URL(url).protocol
  };
}

async function revealDiagnosticsPackage(savedPath) {
  if (!savedPath) return { requested: false, opened: false, method: null, error: null };
  const target = path.resolve(savedPath);
  const exportDir = diagnosticsExportDir();
  if (!isPathInside(exportDir, target)) {
    return {
      requested: true,
      opened: false,
      method: null,
      error: 'Diagnostics path is outside the export directory.'
    };
  }
  if (!fs.existsSync(target)) {
    return {
      requested: true,
      opened: false,
      method: null,
      error: 'Diagnostics package was not found.'
    };
  }
  try {
    shell.showItemInFolder(target);
    return { requested: true, opened: true, method: 'showItemInFolder', error: null };
  } catch (error) {
    const fallback = await openPathSafely(path.dirname(target));
    return {
      requested: true,
      opened: fallback.opened,
      method: 'openPath',
      error: fallback.error || safeDiagnosticText(error instanceof Error ? error.message : String(error), 500)
    };
  }
}

async function openDiagnosticsLocation(_event, payload = {}) {
  const options = optionalObjectPayload(payload, 'diagnostics location payload');
  const exportDir = diagnosticsExportDir();
  fs.mkdirSync(exportDir, { recursive: true });
  const requestedPath = typeof options.path === 'string' && options.path.trim()
    ? path.resolve(options.path)
    : exportDir;
  if (!isPathInside(exportDir, requestedPath)) {
    throw new Error('Diagnostics location is outside the export directory.');
  }
  const target = fs.existsSync(requestedPath) && fs.statSync(requestedPath).isFile()
    ? requestedPath
    : exportDir;
  const revealResult = fs.existsSync(target) && fs.statSync(target).isFile()
    ? await revealDiagnosticsPackage(target)
    : await (async () => {
        const opened = await openPathSafely(target);
        return {
          requested: true,
          opened: opened.opened,
          method: 'openPath',
          error: opened.error
        };
      })();
  return {
    ok: Boolean(revealResult.opened),
    opened: Boolean(revealResult.opened),
    method: revealResult.method,
    path: target,
    pathLabel: target === exportDir ? diagnosticExportPathLabel() : diagnosticExportPathLabel(path.basename(target)),
    error: revealResult.error || undefined
  };
}

async function exportDiagnosticsPackage(_event, payload = {}) {
  const options = optionalObjectPayload(payload, 'diagnostics export payload');
  const packageData = await buildDiagnosticsPackage(options);
  const json = diagnosticJson(packageData);
  const diagnosticsPackage = JSON.parse(json);
  const generatedAt = diagnosticsPackage.generatedAt || new Date().toISOString();
  const fileName = `ecorex-diagnostics-${generatedAt.replace(/[:.]/g, '-')}.json`;
  const bytes = Buffer.byteLength(json, 'utf8');
  let savedPath = '';
  let revealResult = { requested: false, opened: false, method: null, error: null };
  if (options.saveToFile !== false) {
    const exportDir = diagnosticsExportDir();
    fs.mkdirSync(exportDir, { recursive: true });
    savedPath = path.join(exportDir, fileName);
    fs.writeFileSync(savedPath, json, 'utf8');
    if (options.openLocation !== false && options.revealInFolder !== false) {
      revealResult = await revealDiagnosticsPackage(savedPath);
    }
  }
  writeLog('info', 'Diagnostics package exported', {
    fileName,
    bytes,
    saved: Boolean(savedPath),
    openedLocation: Boolean(revealResult.opened),
    logEntries: diagnosticsPackage.logs?.count || 0,
    crashEvents: diagnosticsPackage.crashes?.recent?.length || 0
  });
  return {
    ok: true,
    format: 'json',
    mimeType: 'application/json',
    fileName,
    saved: Boolean(savedPath),
    path: savedPath,
    pathLabel: savedPath ? diagnosticExportPathLabel(fileName) : '',
    directory: savedPath ? path.dirname(savedPath) : '',
    directoryLabel: savedPath ? diagnosticExportPathLabel() : '',
    openLocation: revealResult,
    openedLocation: Boolean(revealResult.opened),
    bytes,
    generatedAt,
    diagnosticsPackage,
    json
  };
}

async function selectWorkspaceDirectory(event, payload = {}) {
  optionalObjectPayload(payload, 'workspace selection payload');
  const result = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender) || mainWindow, {
    title: 'Select EcoreX workspace folder',
    defaultPath: safeWorkspaceDialogDefaultPath(payload.current),
    properties: ['openDirectory', 'createDirectory']
  });
  if (result.canceled || !result.filePaths?.[0]) {
    return { ok: true, canceled: true, path: '' };
  }
  const workspaceRoot = normalizeWorkspaceRoot(result.filePaths[0], {
    allowCustomWorkspaceRoot: true
  });
  return {
    ok: true,
    path: workspaceRoot,
    workspaceRoot,
    confirmed: true,
    confirmCustomWorkspaceRoot: true
  };
}

function getCrashRecoveryStatus(payload = {}) {
  const options = optionalObjectPayload(payload, 'crash recovery payload');
  const summary = readCrashSummary(options.limit || 10);
  return {
    ok: true,
    status: summary.lastCrashAt && !summary.restored ? 'attention' : 'ok',
    restored: Boolean(summary.restored),
    lastCrashAt: summary.lastCrashAt,
    lastEventAt: summary.lastEventAt,
    total: summary.total,
    counts: summary.counts,
    recentCrashes: summary.recent,
    crashes: summary.recent,
    events: summary.recent
  };
}

async function collectStartupHealth(_event, payload = {}) {
  const settings = readSettings();
  const [claudeResult, backendResult, capabilitiesResult] = await Promise.allSettled([
    locateClaude(),
    collectBackendStatus(null, { refresh: Boolean(payload?.refresh || payload?.forceRefresh) }),
    Promise.resolve().then(() => collectCapabilities())
  ]);
  const claude = claudeResult.status === 'fulfilled' ? claudeResult.value : {};
  const backendStatus = backendResult.status === 'fulfilled' ? backendResult.value : {
    ok: false,
    error: backendResult.reason instanceof Error ? backendResult.reason.message : String(backendResult.reason || '本地能力状态检查失败。')
  };
  const capabilities = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : {
    ok: false,
    error: capabilitiesResult.reason instanceof Error ? capabilitiesResult.reason.message : String(capabilitiesResult.reason || '能力清单检查失败。')
  };

  const health = {
    ok: true,
    generatedAt: new Date().toISOString(),
    version: {
      name: app.getName(),
      app: app.getVersion(),
      electron: process.versions.electron,
      chrome: process.versions.chrome,
      node: process.versions.node,
      v8: process.versions.v8,
      platform: process.platform,
      arch: process.arch,
      packaged: app.isPackaged
    },
    window: windowDiagnosticSnapshot().window,
    paths: collectPathDiagnostics(settings),
    signing: collectSigningStatus(),
    release: collectReleaseInstallers(),
    backend: summarizeBackendStatus(backendStatus),
    cli: {
      available: Boolean(claude.command),
      path: claude.path || null,
      version: claude.version || null,
      nativePackage: nativeClaudePackageName()
    },
    capabilities: summarizeCapabilities(capabilities),
    agentRuntime: publicAgentRuntimeStatus(),
    runningSessions: getRunningSessionSummaries(),
    crashes: readCrashSummary(10),
    telemetry: publicTelemetryStatus(settings),
    logs: {
      path: logPath(),
      recent: readRecentLogs(40)
    }
  };

  if (!backendStatus.ok) health.backend.error = safeOutputText(backendStatus.error || '本地能力状态暂不可用。', 1000);
  if (capabilities.ok === false) health.capabilities.error = safeOutputText(capabilities.error || '能力清单暂不可用。', 1000);

  writeLog('info', 'Startup health check collected', {
    backend: health.backend,
    cli: health.cli,
    crashes: health.crashes,
    releaseStatus: health.release.metadata?.status,
    window: health.window
  });
  return health;
}

async function collectDiagnostics() {
  const [claude, settings] = await Promise.all([locateClaude(), Promise.resolve(readSettings())]);
  const backendSource = backendPath('claude-code-main');
  const backendMap = backendPath('cli.js.map');
  const nativePackage = nativeClaudePackageName();
  return {
    ok: true,
    app: {
      name: app.getName(),
      version: app.getVersion(),
      platform: process.platform,
      arch: process.arch,
      isPackaged: app.isPackaged,
      userData: app.getPath('userData')
    },
    window: windowDiagnosticSnapshot().window,
    auth: publicAuthSession(readAuthSession()),
    settings,
    workspaceRoot: settings.workspaceRoot,
    paths: collectPathDiagnostics(settings),
    signing: collectSigningStatus(),
    agentBridge: {
      path: claude.path,
      version: claude.version,
      nativePackage
    },
    runtime: publicAgentRuntimeStatus(),
    logs: {
      path: logPath(),
      recent: readRecentLogs()
    },
    runningSessions: getRunningSessionSummaries(),
    unfinishedRuns: recentUnfinishedRunJournals(),
    runJournal: {
      path: runJournalPath(),
      recentUnfinished: recentUnfinishedRunJournals()
    },
    recentSessionHistory: recentSessionFiles(),
    recentSessionFiles: recentSessionFiles(),
    crashes: readCrashSummary(),
    telemetry: publicTelemetryStatus(settings),
    backend: {
      source: fileSummary(backendSource),
      sourceMap: fileSummary(backendMap),
      dist: fileSummary(devPath('dist', 'index.html')),
      packagedBackend: fileSummary(path.join(process.resourcesPath || '', 'backend')),
      nativeBinaryPackage: nativePackage
        ? fileSummary(devPath('node_modules', '@anthropic-ai', nativePackage))
        : { path: null, exists: false }
    },
    previewEngine: publicVueOfficePreviewStatus(),
    release: collectReleaseInstallers()
  };
}

async function collectCapabilities() {
  const cacheKey = managedSkillPacksStateCacheKey();
  if (cachedCapabilities && cachedCapabilitiesKey === cacheKey) return cachedCapabilities;
  const map = sourceMapStats();
  const managed = collectManagedSkillInventory({ includeDisabled: false });
  const capabilityPacks = managed.skillPacks.map((pack) => ({
    ...pack,
    commands: 0,
    agents: 0,
    skills: managed.skills.filter((skill) => skill.provider?.name === pack.name).length || 1,
    hooks: 0,
    bins: 0,
    available: true
  }));
  const totals = {
    commands: 0,
    agents: 0,
    skills: managed.skills.length,
    hooks: 0,
    bins: 0
  };

  cachedCapabilities = {
    capabilityPacks,
    plugins: capabilityPacks,
    totals,
    sourceMap: publicSourceMapStats(map),
    permissionModes: publicPermissionPolicies(),
    models: modelCapabilityOptions(),
    builtIns: [
      '本地文件读取、写入、编辑、检索与目录遍历',
      '本地命令执行与工作区操作',
      '联网检索与网页信息获取',
      'MCP 与工具调用',
      '默认权限与完全访问权限模式',
      '后台任务、多 Agent 调度与取消恢复',
      '生命周期保护、权限确认与诊断能力',
      '结构化流式输出'
    ]
  };
  cachedCapabilitiesKey = cacheKey;
  writeLog('info', 'Capabilities collected', {
    capabilityPacks: capabilityPacks.map((pack) => pack.name),
    totalCapabilityPacks: capabilityPacks.length,
    childSkills: managed.skills.length
  });
  return cachedCapabilities;
}

function normalizeClaudeEvent(sessionId, json) {
  const base = { sessionId, raw: json, time: new Date().toISOString() };
  const contextManagement = json.context_management || json.contextManagement || null;
  if (json.type === 'stream_event') {
    const streamEvent = json.event || {};
    const streamType = String(streamEvent.type || '').trim();
    const block = streamEvent.content_block || streamEvent.contentBlock || {};
    const delta = streamEvent.delta || {};
    if (streamType === 'content_block_delta' && delta.type === 'text_delta' && delta.text) {
      return {
        ...base,
        kind: 'assistant',
        status: 'running',
        text: publicAgentText(delta.text),
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    if (streamType === 'content_block_start') {
      if (block.type === 'text' && block.text) {
        return {
          ...base,
          kind: 'assistant',
          status: 'running',
          text: publicAgentText(block.text),
          contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
        };
      }
      if (block.type === 'tool_use' || block.type === 'server_tool_use') {
        const tool = {
          id: block.id,
          name: block.name || block.tool_name || 'tool',
          input: safeJsonValue(block.input || {}, 8000)
        };
        const ledger = toolLedgerStartEvent(sessionId, tool);
        return {
          ...base,
          kind: 'tool',
          status: 'running',
          toolName: publicAgentToolLabel(tool),
          tools: [tool],
          ledger,
          text: `${publicAgentToolLabel(tool)}。`,
          contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
        };
      }
      if (block.type === 'thinking') {
        return {
          ...base,
          kind: 'status',
          status: 'running',
          text: '正在分析任务',
          contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
        };
      }
    }
    if (streamType === 'content_block_delta' && delta.type === 'input_json_delta') {
      return {
        ...base,
        kind: 'tool',
        status: 'running',
        toolName: '整理工具参数',
        text: '正在整理工具参数',
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    if (streamType === 'message_start') {
      return {
        ...base,
        kind: 'status',
        status: 'running',
        text: '开始生成回复',
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    if (streamType === 'message_stop') {
      return {
        ...base,
        kind: 'status',
        status: 'completed',
        text: '回复生成完成',
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    if (streamType === 'error') {
      const errorInfo = streamEvent.error || json.error || {};
      const message = errorInfo.message || streamEvent.message || json.message || 'Agent stream error.';
      return {
        ...base,
        kind: 'error',
        status: 'failed',
        claudeResultStatus: 'failed',
        errorType: errorInfo.type || streamEvent.type || 'stream_error',
        text: publicAgentText(message),
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    return null;
  }

  if (json.type === 'assistant') {
    const content = json.message?.content || json.content || [];
    const tools = Array.isArray(content)
      ? content
          .filter((block) => block?.type === 'tool_use' || block?.type === 'server_tool_use')
          .map((block) => ({
            id: block.id,
            name: block.name || 'tool',
            input: safeJsonValue(block.input || {}, 8000)
          }))
      : [];
    const text = Array.isArray(content)
      ? content
        .map((block) => {
          if (typeof block === 'string') return block;
          if (block?.type === 'text') return block.text;
          if (block?.type === 'tool_use' || block?.type === 'server_tool_use') return '';
          return '';
        })
          .join('')
      : '';
    if (tools.length && !text.trim()) {
      const toolLabel = publicAgentToolLabel(tools[0]);
      const ledger = tools.length === 1
        ? toolLedgerStartEvent(sessionId, tools[0])
        : {
            type: 'tool',
            phase: 'start',
            toolName: 'tool-batch',
            action: 'tool-batch',
            inputSummary: safeLedgerText(JSON.stringify(tools.map((tool) => ({
              name: publicAgentToolName(tool),
              action: inferToolAction(tool.name, tool.input || {})
            }))), MAX_TOOL_LEDGER_SUMMARY_CHARS),
            startedAt: new Date().toISOString()
          };
      if (tools.length > 1) {
        for (const tool of tools) toolLedgerStartEvent(sessionId, tool);
      }
      return {
        ...base,
        kind: 'tool',
        status: 'running',
        toolName: toolLabel,
        tools,
        ledger,
        text: `${toolLabel}。`,
        contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
      };
    }
    return { ...base, kind: 'assistant', tools, text: publicAgentText(text), contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined };
  }

  if (json.type === 'user') {
    const content = json.message?.content || json.content || [];
    const toolResults = Array.isArray(content)
      ? content.filter((block) => block?.type === 'tool_result')
      : [];
    if (toolResults.length) {
      const failed = toolResults.some((block) => block.is_error);
      const resultInfos = Array.isArray(json.toolUseResult)
        ? json.toolUseResult
        : [json.toolUseResult || {}];
      const toolResultText = (block = {}) => {
        if (typeof block.content === 'string') return block.content;
        if (Array.isArray(block.content)) {
          return block.content.map((item) => item?.text || '').join('\n');
        }
        return '';
      };
      const resultToolNameFor = (block = {}, index = 0) => {
        const match = resultInfos.find((item) =>
          item &&
          (item.tool_use_id === block.tool_use_id || item.toolUseId === block.tool_use_id || item.id === block.tool_use_id)
        ) || resultInfos[index] || resultInfos[0] || {};
        if (match.commandName === 'using-superpowers') return 'SKILLS';
        if (match.query) return 'WebSearch';
        return match.commandName || match.name || match.toolName || '';
      };
      const resultToolName = resultToolNameFor(toolResults[0], 0);
      const outputText = publicAgentText(toolResults
        .map(toolResultText)
        .filter(Boolean)
        .join('\n'));
      const finishLedgers = toolResults.map((block, index) => {
        const blockOutputText = publicAgentText(toolResultText(block));
        const blockFailed = Boolean(block.is_error);
        const blockToolName = resultToolNameFor(block, index);
        return toolLedgerFinishEvent(sessionId, block.tool_use_id, {
          toolName: blockToolName,
          failed: blockFailed,
          output: blockOutputText,
          text: blockOutputText,
          error: blockFailed ? blockOutputText : ''
        });
      }).filter(Boolean);
      const resultLabel = resultToolName ? publicAgentToolLabel(resultToolName) : '工具返回结果';
      return {
        ...base,
        kind: 'tool',
        status: failed ? 'failed' : 'completed',
        toolName: resultLabel,
        toolUseId: toolResults[0].tool_use_id,
        ledger: finishLedgers.length === 1 ? finishLedgers[0] : finishLedgers,
        text: outputText
      };
    }
  }

  if (json.type === 'result') {
    const resultSubtype = String(json.subtype || '').trim();
    const resultFailed = Boolean(json.is_error || json.error || /^error/i.test(resultSubtype));
    return {
      ...base,
      kind: resultFailed ? 'error' : 'result',
      status: resultFailed ? 'failed' : 'completed',
      subtype: resultSubtype || undefined,
      claudeResultStatus: resultFailed ? 'failed' : 'completed',
      text: publicAgentText(json.result || json.error?.message || (json.error ? JSON.stringify(json.error) : json.message || '')),
      costUsd: json.total_cost_usd,
      durationMs: json.duration_ms,
      contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
    };
  }

  if (json.type === 'error') {
    const errorInfo = json.error || {};
    const message = errorInfo.message || json.message || JSON.stringify(Object.keys(errorInfo).length ? errorInfo : json);
    return {
      ...base,
      kind: 'error',
      status: 'failed',
      claudeResultStatus: 'failed',
      errorType: errorInfo.type || json.subtype || 'error',
      text: publicAgentText(message),
      contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
    };
  }

  if (json.type === 'system') {
    return {
      ...base,
      kind: 'status',
      status: json.subtype || 'system',
      text: publicAgentText(json.cwd || json.session_id || json.message || '会话已初始化'),
      contextManagement: contextManagement ? safeJsonValue(contextManagement, 8000) : undefined
    };
  }

  if (json.type?.includes('hook') || json.type?.includes('tool')) {
    return {
      ...base,
      kind: 'tool',
      status: json.type,
      toolName: publicAgentToolLabel(json.toolName || json.name || ''),
      text: publicAgentText(json.message || 'EcoreX 原生能力事件')
    };
  }

  return { ...base, kind: 'debug', text: publicAgentText(JSON.stringify(json)) };
}

function runAgent(payload = {}, options = {}) {
  return new Promise(async (resolve) => {
    let safePayload;
    try {
      safePayload = sanitizePayload(payload);
    } catch (error) {
      resolve({
        ok: false,
        sessionId: sanitizeSessionId(payload?.sessionId),
        status: 'failed',
        error: safeOutputText(error instanceof Error ? error.message : String(error), 4000),
        recoveryHint: agentRecoveryHint('failed')
      });
      return;
    }
    const sessionId = safePayload.sessionId;
    const prompt = safePayload.prompt;
    if (!prompt) {
      resolve({ ok: false, sessionId, status: 'failed', error: '提示词为空。', recoveryHint: '请输入任务内容后再开始。' });
      return;
    }

    const startClaim = claimAgentStart(safePayload, options);
    if (!startClaim.ok) {
      resolve(startClaim);
      return;
    }

    const startLock = startClaim.lock;

    let runtimeActor = null;
    try {
      const claude = await locateClaude();
      if (startLock.cancelled || pendingAgentStarts.get(sessionId) !== startLock) {
        releaseAgentStart(sessionId, startLock);
        resolve({
          ok: false,
          sessionId,
          status: agentFinalStatus(startLock.cancelReason || 'cancelled'),
          error: 'Agent start was cancelled.',
          recoveryHint: agentRecoveryHint(startLock.cancelReason || 'cancelled')
        });
        return;
      }
      if (!claude.command) {
        releaseAgentStart(sessionId, startLock);
        resolve({ ok: false, sessionId, status: 'failed', error: '本地执行引擎未就绪。', recoveryHint: '请等待本地执行引擎预加载完成，或重启 EcoreX 后重试。' });
        return;
      }

      let {
        accessMode,
        permissionMode,
        permissionCliMode,
        permissionCliFlags,
        permissionLabel,
        permissionPolicy,
        model,
        claudeSessionId,
        cwd,
        projectContext
      } = safePayload;
      const invalidResume = invalidClaudeResumeEntry(claudeSessionId);
      if (invalidResume) {
        const previousClaudeSessionId = claudeSessionId;
        claudeSessionId = crypto.randomUUID();
        safePayload = { ...safePayload, claudeSessionId };
        writeLog('warn', 'Rotated invalid Claude resume target', {
          previousClaudeSessionId: publicStableId('claude-session', previousClaudeSessionId),
          nextClaudeSessionId: publicStableId('claude-session', claudeSessionId),
          reason: invalidResume.reason || 'invalid-resume'
        });
      }
    const selectedPlugins = allowedRuntimePluginNames(safePayload.plugins || []);
    safePayload = { ...safePayload, plugins: selectedPlugins };
    const managedMcpConfigFile = prepareManagedMcpConfigFile();
    const repoRoot = backendPath('claude-code-main');
    const safeRepoRoot = path.resolve(repoRoot);
    const pluginInventory = [...parsePluginInventory(), ...runtimeManagedSkillPlugins()];
    const pluginPathByName = new Map(
      pluginInventory
        .filter((plugin) => plugin.available)
        .map((plugin) => [plugin.name, plugin.installPath ? path.resolve(plugin.installPath) : path.resolve(repoRoot, plugin.source)])
    );
    const claudeResumeExistingSession = claudeSessionTranscriptExists(claudeSessionId);
    runtimeActor = createAgentSessionActor(sessionId, safePayload, startLock, { claudeResumeExistingSession });
    const permissionSnapshot = runtimeActor.permissionSnapshot;
    const args = [
      ...claude.baseArgs,
      '--print',
      '--output-format',
      'stream-json',
      '--verbose',
      '--include-partial-messages',
      '--include-hook-events',
      '--tools',
      'default',
      '--allowedTools',
      CLAUDE_AUTO_ALLOWED_TOOL_SET,
      '--model',
      model,
      '--append-system-prompt',
      agentSystemPromptForProject(projectContext),
      '--name',
      'EcoreX Desktop Agent'
    ];
    if (claudeResumeExistingSession) {
      args.push('--resume', claudeSessionId);
    } else {
      args.push('--session-id', claudeSessionId);
    }
    if (permissionCliMode) args.push('--permission-mode', permissionCliMode);
    for (const flag of permissionCliFlags || []) {
      args.push(flag);
    }
    if (managedMcpConfigFile) {
      args.push('--mcp-config', managedMcpConfigFile);
    }

    for (const pluginName of selectedPlugins) {
      const pluginPath = pluginPathByName.get(pluginName);
      if (
        pluginPath &&
        runtimePluginPathAllowed(pluginPath, safeRepoRoot, pluginName) &&
        fs.existsSync(pluginPath)
      ) {
        args.push('--plugin-dir', pluginPath);
      }
    }

    const modelProfileEnv = modelProfileEnvForModel(model);
    const runtimeEnv = {
      ...isolatedAgentRuntimeEnv(),
      ...modelProfileEnv,
      ...projectEnvForAgent(projectContext),
      CLAUDE_CODE_NO_FLICKER: '1',
      ...(process.platform === 'win32' ? { CLAUDE_CODE_USE_POWERSHELL_TOOL: '1' } : {})
    };
    const child = spawn(claude.command, args, {
      cwd,
      env: filteredAgentEnv(runtimeEnv),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      shell: false
    });
    const transport = createCliAgentTransport(child, {
      actorId: runtimeActor.actorId,
      resumeMode: claudeResumeExistingSession ? 'resume' : 'new'
    });
    runtimeActor.transport = transport;
    runtimeActor.status = 'started';
    runtimeActor.lastActivityAt = Date.now();
    try {
      child.stdin.end(`${prompt}\n`);
    } catch (error) {
      writeLog('warn', 'Failed to write agent prompt to stdin', {
        sessionId,
        error: error instanceof Error ? error.message : String(error)
      });
    }
    writeLog('info', 'Agent session started', {
      sessionId,
      runtimeKind: AGENT_RUNTIME_KIND,
      actorId: runtimeActor.actorId,
      cwd,
      model,
      accessMode,
      permissionMode,
      permissionLabel,
      claudeSessionId: publicStableId('claude-session', claudeSessionId),
      claudeResumeMode: claudeResumeExistingSession ? 'resume' : 'new',
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      modelProfile: modelProfileEnv.ECOREX_MODEL_PROFILE || null,
      capabilityPacks: selectedPlugins.map((name) => publicStableId('capability-pack', name))
    });
    if (permissionPolicy?.fullAccess) {
      writeLog('warn', 'Agent session started with full access permission', {
      sessionId,
      accessMode
    });
    }

    let lineBuffer = '';
    const startedEvent = {
      sessionId,
      kind: 'status',
      status: 'started',
      runtimeKind: AGENT_RUNTIME_KIND,
      actorId: runtimeActor.actorId,
      accessMode,
      permissionMode,
      permissionCliMode,
      permissionCliFlags,
      permissionLabel,
      claudeSessionId: publicStableId('claude-session', claudeSessionId),
      claudeResumeMode: claudeResumeExistingSession ? 'resume' : 'new',
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      text: '本地执行引擎会话已启动'
    };
    const entry = {
      child,
      transport,
      actor: runtimeActor,
      actorId: runtimeActor.actorId,
      runtimeKind: AGENT_RUNTIME_KIND,
      sessionId,
      conversationId: safePayload.conversationId,
      messageId: safePayload.messageId || '',
      permissionSnapshot,
      promptHash: crypto.createHash('sha256').update(prompt).digest('hex'),
      cwd,
      ownerId: startLock.ownerId,
      signature: startLock.signature,
      model,
      accessMode,
      permissionMode,
      permissionCliMode,
      permissionCliFlags,
      permissionLabel,
      permissionPolicy,
      claudeSessionId,
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      attachmentContext: safePayload.attachmentContext,
      attachmentCount: safePayload.attachmentContext?.count || 0,
      promptPreview: startLock.promptPreview,
      workspacePath: startLock.workspacePath,
      startedAt: Date.now(),
      lastActivityAt: Date.now(),
      status: 'starting',
      state: 'running',
      claudeResultFailed: false,
      ledgerEventCount: 0,
      finished: false,
      transcript: [],
      transcriptWritten: false,
      lastStderr: '',
      flushBufferedOutput: null,
      totalTimer: null,
      idleTimer: null
    };
    appendRunJournalEntry(sessionId, entry, 'running', { event: 'start' });
    entry.flushBufferedOutput = () => {
      if (!lineBuffer.trim()) return;
      const buffered = lineBuffer.trim();
      lineBuffer = '';
      try {
        const event = normalizeClaudeEvent(sessionId, JSON.parse(buffered));
        if (event) {
          recordSessionEvent(entry, event);
          emitAgentEvent(event);
        }
      } catch {
        const event = { sessionId, kind: 'assistant', text: buffered };
        recordSessionEvent(entry, event);
        emitAgentEvent(event);
      }
    };
    recordSessionEvent(entry, startedEvent);
    entry.totalTimer = setTimeout(() => {
      stopAgent(sessionId, 'timeout');
    }, safePayload.timeoutMs);
    runningAgents.set(sessionId, entry);
    rememberClaudeSessionBinding(safePayload);
    releaseAgentStart(sessionId, startLock);
    armIdleTimer(sessionId);
    resolve({
      ok: true,
      sessionId,
      claudeSessionId,
      accessMode,
      permissionMode,
      permissionLabel,
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      initialEvent: normalizeAgentEvent(startedEvent, { includeRaw: false })
    });

    emitAgentEvent({
      sessionId,
      kind: 'status',
      status: 'started',
      runtimeKind: AGENT_RUNTIME_KIND,
      actorId: runtimeActor.actorId,
      accessMode,
      permissionMode,
      permissionCliMode,
      permissionCliFlags,
      permissionLabel,
      claudeResumeMode: claudeResumeExistingSession ? 'resume' : 'new',
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      text: '本地执行引擎会话已启动'
    });

    child.stdout.on('data', (chunk) => {
      const entry = runningAgents.get(sessionId);
      if (entry) {
        entry.lastActivityAt = Date.now();
        if (entry.actor) entry.actor.lastActivityAt = entry.lastActivityAt;
      }
      armIdleTimer(sessionId);
      lineBuffer += chunk.toString();
      if (lineBuffer.length > MAX_AGENT_LINE_BUFFER_CHARS) {
        writeLog('warn', 'Agent line buffer truncated', { sessionId, length: lineBuffer.length });
        lineBuffer = lineBuffer.slice(-MAX_AGENT_LINE_BUFFER_CHARS);
      }
      const lines = lineBuffer.split(/\r?\n/);
      lineBuffer = lines.pop() || '';
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          const json = JSON.parse(trimmed);
          const event = normalizeClaudeEvent(sessionId, json);
          if (event) {
            recordSessionEvent(runningAgents.get(sessionId), event);
            emitAgentEvent(event);
          }
        } catch {
          const event = { sessionId, kind: 'assistant', text: trimmed };
          recordSessionEvent(runningAgents.get(sessionId), event);
          emitAgentEvent(event);
        }
      }
    });

    child.stderr.on('data', (chunk) => {
      const entry = runningAgents.get(sessionId);
      if (entry) {
        entry.lastActivityAt = Date.now();
        if (entry.actor) entry.actor.lastActivityAt = entry.lastActivityAt;
      }
      armIdleTimer(sessionId);
      const stderrText = safeOutputText(chunk.toString(), MAX_AGENT_EVENT_TEXT_CHARS);
      if (entry) {
        entry.lastStderr = safeOutputText(`${entry.lastStderr || ''}${stderrText}`, 4000);
      }
      const publicStderrText = /session id .*already in use/i.test(stderrText)
        ? '本地会话正在切换到可恢复模式。'
        : publicProductText(stderrText);
      const event = {
        sessionId,
        kind: 'stderr',
        text: publicStderrText
      };
      recordSessionEvent(entry, event);
      emitAgentEvent(event);
      writeLog('warn', 'Agent stderr', { sessionId, text: stderrText.slice(0, 4000) });
    });

    child.on('error', (error) => {
      const entry = runningAgents.get(sessionId);
      if (!entry) return;
      writeLog('error', 'Agent process error', { sessionId, error: safeOutputText(error.message, 4000) });
      finalizeAgentSession(sessionId, entry, {
        status: 'failed',
        reason: 'error',
        text: safeOutputText(error.message, 4000)
      });
    });

    child.on('close', (code, signal) => {
      const entry = runningAgents.get(sessionId);
      if (!entry) return;
      entry.flushBufferedOutput?.();
      const finalStatus = code === 0 && !entry.claudeResultFailed ? 'completed' : 'failed';
      const sessionReuseConflict = finalStatus === 'failed' && /session id .*already in use/i.test(entry.lastStderr || '');
      const missingResumeTarget = finalStatus === 'failed' && /no conversation found with session id/i.test(entry.lastStderr || '');
      if (finalStatus === 'completed') markClaudeSessionLaunched(entry.claudeSessionId);
      if (sessionReuseConflict || missingResumeTarget) {
        markClaudeSessionResumeInvalid(entry.claudeSessionId, missingResumeTarget ? 'missing-resume-target' : 'session-reuse-conflict');
      }
      finalizeAgentSession(sessionId, entry, {
        status: finalStatus,
        code,
        signal,
        text: sessionReuseConflict
          ? '本地会话仍在释放或已存在。EcoreX 已切换为可恢复会话，请直接重试。'
          : undefined
      });
      if (!sessionReuseConflict && !missingResumeTarget) {
        refreshClaudeSessionTranscriptSeen(entry.claudeSessionId);
      }
      writeLog(finalStatus === 'completed' ? 'info' : 'error', 'Agent session closed', {
        sessionId,
        code,
        signal,
        status: finalStatus,
        durationMs: Date.now() - entry.startedAt
      });
    });
    } catch (error) {
      disposeAgentSessionActor(sessionId, 'failed');
      releaseAgentStart(sessionId, startLock);
      writeLog('error', 'Agent session failed to start', {
        sessionId,
        error: safeOutputText(error instanceof Error ? error.message : String(error), 4000)
      });
      resolve({
        ok: false,
        sessionId,
        status: 'failed',
        error: safeOutputText(error instanceof Error ? error.message : String(error), 4000),
        recoveryHint: agentRecoveryHint('failed')
      });
    }
  });
}

function handleSafe(channel, handler, options = {}) {
  ipcMain.handle(channel, async (event, ...args) => {
    try {
      if (!isTrustedSender(event)) {
        writeLog('warn', 'Blocked untrusted IPC invoke', { channel, url: event.senderFrame?.url });
        return { ok: false, error: 'Untrusted renderer.' };
      }
      let authContext = null;
      if (options.authRequired) {
        authContext = authContextFromArgs(args);
        if (!authContext) {
          writeLog('warn', 'Blocked unauthorized IPC invoke', { channel });
          return unauthorizedResponse();
        }
        if (options.requiredPermission && !authContextHasPermission(authContext, options.requiredPermission)) {
          writeLog('warn', 'Blocked forbidden IPC invoke', { channel, permission: options.requiredPermission, email: authContext.user?.email });
          return forbiddenResponse(options.requiredPermission);
        }
      }
      return await handler(event, ...args, authContext);
    } catch (error) {
      writeLog('error', 'IPC handler failed', {
        channel,
        error: error instanceof Error ? error.message : String(error)
      });
      return {
        ok: false,
        error: safeOutputText(error instanceof Error ? error.message : String(error), 4000)
      };
    }
  });
}

function assertTrustedIpc(event, channel) {
  if (isTrustedSender(event)) return true;
  writeLog('warn', 'Blocked untrusted IPC message', { channel, url: event.senderFrame?.url });
  return false;
}

function assertAuthorizedIpc(event, channel, args = []) {
  if (!assertTrustedIpc(event, channel)) return { ok: false, error: 'Untrusted renderer.' };
  if (isAuthorized(args)) return null;
  writeLog('warn', 'Blocked unauthorized IPC message', { channel });
  return unauthorizedResponse();
}

handleSafe('backend:status', collectBackendStatus);
handleSafe('backend:capabilities', collectCapabilities);
handleSafe('auth:login', (_event, payload) => loginAuth(payload));
handleSafe('auth:logout', logoutAuth, { authRequired: true });
handleSafe('auth:status', (_event, payload = {}) =>
  publicAuthSession(readAuthSession({ refresh: Boolean(payload?.refresh) }), {
    includeToken: Boolean(payload?.includeToken)
  })
);
handleSafe('auth:users:list', (_event, payload, authContext) => listAuthUsers(payload, authContext), { authRequired: true });
handleSafe('auth:user:create', (_event, payload, authContext) => createAuthUser(payload, authContext), { authRequired: true, requiredPermission: 'users:manage' });
handleSafe('auth:user:update', (_event, payload, authContext) => updateAuthUser(payload, authContext), { authRequired: true });
handleSafe('auth:user:delete', (_event, payload, authContext) => deleteAuthUser(payload, authContext), { authRequired: true, requiredPermission: 'users:manage' });
handleSafe('auth:profile:update', (_event, payload, authContext) => updateOwnProfile(payload, authContext), { authRequired: true, requiredPermission: 'profile:update' });
handleSafe('enterprise:action', (_event, payload, authContext) => runEnterpriseAdminAction(payload, authContext), { authRequired: true, requiredPermission: 'enterprise:manage' });
handleSafe('secrets:status', (_event, payload) => {
  optionalObjectPayload(payload, 'secret payload');
  return secretsStatus();
}, { authRequired: true });
handleSafe('secrets:list', (_event, payload) => listSecrets(payload), { authRequired: true });
handleSafe('secrets:set', (_event, payload) => setSecret(payload), { authRequired: true, requiredPermission: 'secrets:manage' });
handleSafe('secrets:delete', (_event, payload) => deleteSecret(payload), { authRequired: true, requiredPermission: 'secrets:manage' });
handleSafe('listModelProfiles', (_event, payload) => listModelProfiles(payload), { authRequired: true });
handleSafe('saveModelProfile', (_event, payload) => saveModelProfile(payload), { authRequired: true, requiredPermission: 'models:manage' });
handleSafe('deleteModelProfile', (_event, payload) => deleteModelProfile(payload), { authRequired: true, requiredPermission: 'models:manage' });
handleSafe('activateModelProfile', (_event, payload) => activateModelProfile(payload), { authRequired: true, requiredPermission: 'models:manage' });
handleSafe('testModelProfile', (_event, payload) => testModelProfile(payload), { authRequired: true });
handleSafe('modelAdapter:testProfile', (_event, payload) => testModelProfile(payload), { authRequired: true });
handleSafe('modelAdapter:generateImage', (_event, payload) => generateModelProfileImage(payload), { authRequired: true });
handleSafe('settings:get', () => ({ ok: true, settings: writeSettings(readSettings()) }), { authRequired: true });
handleSafe('settings:update', (_event, payload) => ({ ok: true, settings: updateSettings(payload) }), { authRequired: true, requiredPermission: 'settings:manage' });
handleSafe('evaluation:list', () => listEvaluationStatus(), { authRequired: true });
handleSafe('evaluation:run', (_event, payload) => runEvaluationStatus(payload), { authRequired: true, requiredPermission: 'enterprise:manage' });
handleSafe('startup:health', collectStartupHealth, { authRequired: true });
handleSafe('diagnostics:get', collectDiagnostics, { authRequired: true });
handleSafe('diagnostics:export', exportDiagnosticsPackage, { authRequired: true });
handleSafe('diagnostics:open-location', openDiagnosticsLocation, { authRequired: true });
handleSafe('diagnostics:crash-recovery', (_event, payload) => getCrashRecoveryStatus(payload), { authRequired: true });
handleSafe('telemetry:status', () => ({ ok: true, telemetry: publicTelemetryStatus() }), { authRequired: true });
handleSafe('telemetry:flush', (_event, payload) => flushAnonymousTelemetry(payload), { authRequired: true });
handleSafe('workspace:select-directory', (event, payload) => selectWorkspaceDirectory(event, payload), { authRequired: true });
handleSafe('workspace:list', (_event, payload) => listWorkspace(payload), { authRequired: true });
handleSafe('workspace:ensure', (_event, payload) => ensureWorkspace(payload), { authRequired: true });
handleSafe('file:preview', (_event, payload) => previewFile(payload), { authRequired: true });
handleSafe('artifact:open-file', (_event, payload) => openArtifactFile(payload), { authRequired: true });
handleSafe('attachment:ingest', (_event, payload) => ingestAttachmentsForPreview(payload), { authRequired: true });
handleSafe('attachment:select-files', (event, payload) => selectAttachmentFiles(event, payload), { authRequired: true });
handleSafe('attachment:open-file', (_event, payload) => openSelectedAttachmentFile(payload), { authRequired: true });
handleSafe('shell:open-external', (_event, payload) => openExternalUrl(payload), { authRequired: true });
handleSafe('project:list', (_event, payload) => listProjects(payload), { authRequired: true });
handleSafe('project:create', (_event, payload) => createProject(payload), { authRequired: true });
handleSafe('project:switch', (_event, payload) => switchProject(payload), { authRequired: true });
handleSafe('project:update', (_event, payload) => updateProject(payload), { authRequired: true });
handleSafe('project:archive', (_event, payload) => archiveProject(payload), { authRequired: true });
handleSafe('project:delete', (_event, payload) => deleteProject(payload), { authRequired: true });
handleSafe('project:status', (_event, payload) => projectStatus(payload), { authRequired: true });
handleSafe('project:open-folder', (_event, payload) => openProjectFolder(payload), { authRequired: true });
handleSafe('project:list-files', (_event, payload) => listProjectFiles(payload), { authRequired: true });
handleSafe('project:add-files', (event, payload) => addProjectFiles(event, payload), { authRequired: true });
handleSafe('project:open-file', (_event, payload) => openProjectFile(payload), { authRequired: true });
handleSafe('project:remove-file', (_event, payload) => removeProjectFile(payload), { authRequired: true });
handleSafe('mcp:list', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:status', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:refresh', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:get', (_event, payload) => getMcpServer(payload), { authRequired: true });
handleSafe('mcp:update', (_event, payload) => updateMcpConfig(payload), { authRequired: true, requiredPermission: 'mcp:manage' });
handleSafe('mcp:update-config', (_event, payload) => updateMcpConfig(payload), { authRequired: true, requiredPermission: 'mcp:manage' });
handleSafe('mcp:enable', () => unsupportedMcpToggle('enable'), { authRequired: true, requiredPermission: 'mcp:manage' });
handleSafe('mcp:disable', () => unsupportedMcpToggle('disable'), { authRequired: true, requiredPermission: 'mcp:manage' });
handleSafe('skill:list', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:status', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:refresh', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:install', (_event, payload, authContext) => installManagedSkillPack(payload, authContext), { authRequired: true, requiredPermission: 'skills:manage' });
handleSafe('skill:enable', (_event, payload) => updateManagedSkillEnabled(payload, true), { authRequired: true, requiredPermission: 'skills:manage' });
handleSafe('skill:disable', (_event, payload) => updateManagedSkillEnabled(payload, false), { authRequired: true, requiredPermission: 'skills:manage' });
handleSafe('skill:update', (_event, payload, authContext) => updateManagedSkillPack(payload, authContext), { authRequired: true, requiredPermission: 'skills:manage' });
handleSafe('agent:run', (event, payload) => runAgent(payload, { ownerId: event.sender.id }), { authRequired: true });
handleSafe('agent:stop', (_event, payload) => {
  const sessionId = typeof payload === 'object' && payload ? payload.sessionId : payload;
  return stopAgent(sessionId, 'cancelled');
}, { authRequired: true });
handleSafe('agent:sessions', () => {
  const running = getRunningSessionSummaries();
  const runningIds = new Set(running.map((session) => session.sessionId));
  const recoverable = recentRecoverableSessionSummaries().filter((session) => !runningIds.has(session.sessionId));
  return { ok: true, sessions: [...running, ...recoverable], unfinishedRuns: recentUnfinishedRunJournals() };
}, { authRequired: true });
handleSafe('agent:session-history', (_event, payload = {}) => getSessionHistorySummary(payload), { authRequired: true });
handleSafe('backend:open-auth', async () => {
  const claude = await locateClaude();
  if (!claude.command) return { ok: false, error: '本地执行引擎未就绪。' };
  if (isWindows) {
    const file = claude.command.replace(/'/g, "''");
    const args = [...claude.baseArgs, 'auth', 'login'].join(' ').replace(/'/g, "''");
    spawn('powershell.exe', ['-NoProfile', '-Command', `Start-Process -FilePath '${file}' -ArgumentList '${args}'`], {
      detached: true,
      env: filteredAgentEnv(isolatedAgentRuntimeEnv(), { includeSecrets: true }),
      stdio: 'ignore',
      windowsHide: true
    }).unref();
  } else {
    spawn(claude.command, [...claude.baseArgs, 'auth', 'login'], {
      detached: true,
      env: filteredAgentEnv(isolatedAgentRuntimeEnv(), { includeSecrets: true }),
      stdio: 'ignore'
    }).unref();
  }
  return { ok: true };
}, { authRequired: true });

ipcMain.on('window:control', (event, action) => {
  const payload = action && typeof action === 'object' && !Array.isArray(action) ? action : { action };
  if (!assertTrustedIpc(event, 'window:control')) return;
  if (!WINDOW_CONTROL_ACTIONS.has(payload.action)) {
    writeLog('warn', 'Blocked invalid window control action', { action: payload.action });
    return;
  }
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window) return;
  if (payload.action === 'minimize') window.minimize();
  if (payload.action === 'maximize') {
    if (window.isMaximized()) window.unmaximize();
    else window.maximize();
  }
  if (payload.action === 'close') window.close();
});

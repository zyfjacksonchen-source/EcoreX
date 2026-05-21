const { app, BrowserWindow, ipcMain, session, safeStorage, dialog, screen, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { pathToFileURL } = require('url');
const { createModelAdapter, DEFAULT_IMAGE_MODEL } = require('./model-adapter.cjs');

const ROOT_DIR = path.resolve(__dirname, '..');
const isWindows = process.platform === 'win32';
const runningAgents = new Map();
const pendingAgentStarts = new Map();
const recentAgentStartsByWindow = new Map();
let mainWindow = null;
let cachedClaude = null;
let cachedCapabilities = null;
let cachedBackendStatus = null;
let backendStatusInflight = null;
const AGENT_TIMEOUT_MS = 30 * 60 * 1000;
const AGENT_IDLE_TIMEOUT_MS = 12 * 60 * 1000;
const AGENT_MIN_TIMEOUT_MS = 30 * 1000;
const MAX_RUNNING_AGENTS = 4;
const AGENT_START_DEBOUNCE_MS = 1200;
const AGENT_START_PENDING_TTL_MS = 15 * 1000;
const BACKEND_STATUS_TTL_MS = 3 * 1000;
const RENDERER_RECOVERY_DELAY_MS = 800;
const RENDERER_UNRESPONSIVE_RECOVERY_MS = 15 * 1000;
const RENDERER_RECOVERY_WINDOW_MS = 60 * 1000;
const MAX_RENDERER_RECOVERY_ATTEMPTS = 3;
const MAX_PROMPT_CHARS = 80_000;
const MIN_PROMPT_CHARS = 1_000;
const MAX_PROMPT_PREVIEW_CHARS = 240;
const SETTINGS_FILE_NAME = 'settings.json';
const AUTH_SESSION_FILE_NAME = 'auth-session.json';
const AUTH_IDENTITY_FILE_NAME = 'auth-identity.json';
const SECRETS_FILE_NAME = 'secrets.json';
const MODEL_PROFILES_FILE_NAME = 'model-profiles.json';
const SESSION_TRANSCRIPT_DIR_NAME = 'sessions';
const CRASH_SUMMARY_FILE_NAME = 'crash-summary.json';
const DIAGNOSTIC_EXPORT_DIR_NAME = 'EcoreX Diagnostics';
const PROJECT_STATE_FILE_NAME = '.ecorex-projects.json';
const PROJECT_METADATA_FILE_NAME = '.ecorex-project.json';
const PROJECT_MEMORY_DIR_NAME = '.ecorex-memory';
const PROJECT_MEMORY_FILE_NAME = 'project-memory.md';
const PROJECT_CONTEXT_FILE_NAME = 'project-context.json';
const LOG_FILE_NAME = 'ecorex-agent.log';
const MAX_LOG_LINES = 200;
const MAX_CRASH_EVENTS = 50;
const MAX_DIAGNOSTIC_LOG_LINES = 80;
const MAX_DIAGNOSTIC_SESSIONS = 12;
const MAX_COMMAND_OUTPUT_CHARS = 2 * 1024 * 1024;
const MAX_AGENT_LINE_BUFFER_CHARS = 2 * 1024 * 1024;
const MAX_TRANSCRIPT_EVENTS = 80;
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
const MAX_MANAGED_ITEMS = 500;
const MAX_SKILLS_PER_PLUGIN = 200;
const MAX_SECRET_VALUE_CHARS = 20_000;
const MAX_MODEL_PROFILES = 20;
const MAX_MODEL_PROFILE_TEXT_CHARS = 2048;
const MODEL_PROFILE_TEST_TIMEOUT_MS = 20 * 1000;
const LOCAL_AUTH_HASH_ITERATIONS = 210_000;
const LOCAL_AUTH_MIN_PASSWORD_CHARS = 8;
const AUTH_SESSION_TTL_MS = 12 * 60 * 60 * 1000;
const AUTH_SESSION_REFRESH_THRESHOLD_MS = 30 * 60 * 1000;
const MAX_PROJECTS = 100;
const MAX_PROJECT_STATS_ITEMS = 2000;
const FULL_ACCESS_PERMISSION_MODE = 'fullAccess';
const FULL_ACCESS_CLAUDE_FLAG = '--dangerously-skip-permissions';
const PUBLIC_PERMISSION_MODES = ['default', FULL_ACCESS_PERMISSION_MODE];
const WINDOW_CONTROL_ACTIONS = new Set(['minimize', 'maximize', 'close']);
const PERMISSION_POLICIES = Object.freeze({
  default: {
    value: 'default',
    accessMode: 'default',
    mode: 'default',
    permissionMode: 'default',
    cliMode: 'default',
    cliFlags: [],
    label: '默认权限',
    description: '使用 Claude Code 默认权限策略。',
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

const AGENT_SYSTEM_PROMPT = [
  '你是 EcoreX Agent，由 EcoreX 亦芯开发的具备自主思考能力的 AI Agent。',
  '你的主要任务是服务广告业务常用工作场景，包括但不限于品牌策略、投放计划、预算分配、素材创意、A/B 测试、数据归因、效果复盘、竞品洞察、客户简报、项目协同与自动化执行。',
  '你应主动拆解目标、识别缺失信息、给出可执行方案，并在需要调用工具、读写文件、运行命令或生成报告时清晰说明动作与结果。',
  '默认使用专业、可靠、面向业务结果的中文表达，输出要便于广告运营、市场、创意、数据分析与项目管理团队直接落地。'
].join('\n');

const ECOREX_AGENT_SYSTEM_PROMPT = [
  '你是 EcoreX Agent，由 EcoreX 亦芯开发的具备自主思考能力的 AI Agent。',
  '你的主要任务是服务广告业务常用工作场景，包括但不限于品牌策略、投放计划、预算分配、素材创意、A/B 测试、数据归因、效果复盘、竞品洞察、客户简报、项目协同与自动化执行。',
  '你应主动拆解目标、识别缺失信息、给出可执行方案，并在需要调用工具、读写文件、运行命令或生成报告时清晰说明动作与结果。',
  '默认使用专业、可靠、面向业务结果的中文表达，输出要便于广告运营、市场、创意、数据分析与项目管理团队直接落地。'
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

function isAllowedRendererUrl(url = '') {
  const value = String(url || '');
  if (app.isPackaged) return value === rendererEntryUrl();
  return value === 'http://127.0.0.1:5188/' || value.startsWith('http://127.0.0.1:5188/');
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
    .replace(/\bClaude Code CLI\b/gi, 'EcoreX execution engine')
    .replace(/\bClaude Code\b/gi, 'EcoreX execution engine')
    .replace(/\bclaude\s+(mcp|plugin|plugins)\b/gi, 'EcoreX data connection')
    .replace(/\bMCP servers?\b/gi, 'data connections')
    .replace(/\bMCP\b/gi, 'data connection')
    .replace(/\bplugin marketplace\b/gi, 'capability library')
    .replace(/\bplugins?\b/gi, 'capability pack')
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

function publicBridgeError(result = {}, fallback = 'Agent bridge operation failed.') {
  const text = safeOutputText(result.stderr || result.stdout || '', 4000)
    .replace(/claude\s+(mcp|plugin|plugins)\b/gi, 'the agent bridge')
    .replace(/Claude Code CLI/gi, 'agent bridge')
    .replace(/plugin(s)?/gi, 'skill pack$1');
  return text || fallback;
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
  return event;
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

function secretsPath() {
  return path.join(app.getPath('userData'), SECRETS_FILE_NAME);
}

function modelProfilesPath() {
  return path.join(app.getPath('userData'), MODEL_PROFILES_FILE_NAME);
}

function sessionTranscriptDir() {
  return path.join(app.getPath('userData'), SESSION_TRANSCRIPT_DIR_NAME);
}

function crashSummaryPath() {
  return path.join(app.getPath('userData'), CRASH_SUMMARY_FILE_NAME);
}

function atomicWriteJson(file, payload) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tempFile = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tempFile, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  fs.renameSync(tempFile, file);
}

function publicAuthSession(session, options = {}) {
  const identity = Object.prototype.hasOwnProperty.call(options, 'identity')
    ? options.identity
    : readAuthIdentity();
  const setupRequired = !identity;
  if (!session?.token) {
    return {
      ok: true,
      loggedIn: false,
      setupRequired,
      authMode: 'local-owner'
    };
  }
  const summary = {
    ok: true,
    loggedIn: true,
    email: session.email,
    user: { email: session.email },
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    expiresAt: session.expiresAt,
    setupRequired,
    authMode: 'local-owner'
  };
  if (identity?.email) summary.ownerEmail = identity.email;
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
      const identity = readAuthIdentity();
      if (!identity || session?.email !== identity.email) {
        clearAuthSession();
        return null;
      }
      return refreshAuthSessionIfNeeded(session, options);
    }
    const legacySession = validateAuthSession(raw);
    if (legacySession) {
      const identity = readAuthIdentity();
      if (!identity || legacySession.email !== identity.email) {
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

  let identity = readAuthIdentity();
  const password = normalizeLocalPassword(payload.password, { forSetup: !identity });
  let createdIdentity = false;

  if (!identity) {
    identity = createAuthIdentity(email, password);
    createdIdentity = true;
  } else if (identity.email !== email || !verifyLocalPassword(identity, password)) {
    writeLog('warn', 'Local auth login rejected', { email, reason: identity.email !== email ? 'email-mismatch' : 'password-mismatch' });
    throw new Error('Invalid email or password.');
  }

  const now = new Date().toISOString();
  const session = writeAuthSession({
    email: identity.email,
    token: crypto.randomBytes(32).toString('hex'),
    createdAt: now,
    updatedAt: now
  });
  writeLog('info', createdIdentity ? 'Local auth owner bound' : 'Auth session created', { email: identity.email });
  return publicAuthSession(session, { includeToken: true, identity });
}

function logoutAuth() {
  const session = readAuthSession();
  clearAuthSession();
  if (session?.email) writeLog('info', 'Auth session cleared', { email: session.email });
  return { ok: true, loggedIn: false };
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
  const session = readAuthSession({ refresh: true });
  const token = extractAuthToken(args);
  if (!session?.token || !token || token.length !== session.token.length) return false;
  const expected = Buffer.from(session.token, 'utf8');
  const provided = Buffer.from(token, 'utf8');
  if (provided.length !== expected.length) return false;
  return crypto.timingSafeEqual(expected, provided);
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
        imageModel: normalizeModelProfileText(rawProfile.imageModel || '', 'imageModel', {
          required: false,
          maxLength: 160
        }),
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
      imageModel: normalizeModelProfileText(profile.imageModel || '', 'imageModel', {
        required: false,
        maxLength: 160
      }),
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
    imageModel: profile.imageModel || '',
    imageModelName: profile.imageModel || '',
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

function modelCapabilityOptions() {
  const options = new Map(BUILTIN_MODEL_OPTIONS.map((model) => [model.value, model]));
  try {
    for (const profile of readModelProfilesFile().profiles) {
      if (!profile.model) continue;
      options.set(profile.model, {
        value: profile.model,
        label: profile.label ? `${profile.label} · ${profile.model}` : profile.model,
        profileName: profile.name,
        imageModel: profile.imageModel || '',
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
      ECOREX_IMAGE_MODEL: profile.imageModel || ''
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
      ? normalizeModelProfileText(imageModelValue || '', 'imageModel', { required: false, maxLength: 160 })
      : existing?.imageModel || '',
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
      ? normalizeModelProfileText(imageModelValue || '', 'imageModel', { required: false, maxLength: 160 })
      : stored?.imageModel || ''
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
    imageModel: profile.imageModel || DEFAULT_IMAGE_MODEL,
    imageModelName: profile.imageModel || DEFAULT_IMAGE_MODEL,
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
    model: normalizeModelProfileText(input.model || profile.imageModel || DEFAULT_IMAGE_MODEL, 'imageModel', {
      maxLength: 160
    }),
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
  const timeoutMs = Math.min(Math.max(Number(payload?.timeoutMs) || 30000, 1000), 60000);
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
    model: result.model || profile.imageModel || DEFAULT_IMAGE_MODEL,
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
    defaultModel: normalizeModelName(raw.defaultModel, 'sonnet'),
    accessMode,
    permissionMode: accessMode,
    defaultPermissionMode: accessMode,
    workspaceRoot,
    customWorkspaceRootConfirmed,
    maxPromptChars: clampPromptChars(raw.maxPromptChars),
    autoRefreshBackend: typeof raw.autoRefreshBackend === 'boolean' ? raw.autoRefreshBackend : true
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
  const workspace = readSettings().workspaceRoot;
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
    description: sanitizeProjectText(raw.description || raw.summary || raw.note || '', 'project description', 260)
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
    '',
    '## 长期记忆',
    '- '
  ];
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
  const next = {
    id: project.id,
    name: Object.prototype.hasOwnProperty.call(payload, 'name') ? sanitizeProjectDisplayName(payload.name) : project.name,
    status: Object.prototype.hasOwnProperty.call(payload, 'status') ? sanitizeProjectStatus(payload.status) : project.status,
    client: Object.prototype.hasOwnProperty.call(payload, 'client') ? payload.client : project.client,
    goal: Object.prototype.hasOwnProperty.call(payload, 'goal') ? payload.goal : project.goal,
    industry: Object.prototype.hasOwnProperty.call(payload, 'industry') ? payload.industry : project.industry,
    scenario: Object.prototype.hasOwnProperty.call(payload, 'scenario') ? payload.scenario : project.scenario,
    budget: Object.prototype.hasOwnProperty.call(payload, 'budget') ? payload.budget : project.budget,
    period: Object.prototype.hasOwnProperty.call(payload, 'period') ? payload.period : project.period,
    deliverables: Object.prototype.hasOwnProperty.call(payload, 'deliverables') ? payload.deliverables : project.deliverables,
    description: Object.prototype.hasOwnProperty.call(payload, 'description') ? payload.description : project.description,
    createdAt: project.createdAt,
    updatedAt: now
  };
  writeProjectMetadata(project.projectPath, next);
  const updated = readProjectMetadata(project.projectPath, project.dirName);
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

function sanitizePayload(payload = {}) {
  const settings = readSettings();
  const prompt = String(payload.prompt || '').trim().slice(0, settings.maxPromptChars);
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
  const model = normalizeModelName(payload.model, settings.defaultModel);
  const workspaceRoot = settings.workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const requestedProjectId = payload.projectId ? sanitizeProjectId(payload.projectId) : null;
  let projectContext = requestedProjectId
    ? projectContextFromProject(workspaceRoot, resolveProject({ id: requestedProjectId }, { allowArchived: false }).project)
    : activeProjectContext();
  let cwd = projectContext?.projectPath || workspaceRoot;
  if (payload.cwd || payload.pathLabel) {
    const requested = String(payload.cwd || payload.pathLabel || '').trim();
    const requestedCwd = requested.startsWith('workspace:/')
      ? path.resolve(workspaceRoot, sanitizeWorkspaceRelativePath(requested.replace(/^workspace:\//, '')))
      : path.resolve(requested);
    const cwdRoot = projectContext?.projectPath || workspaceRoot;
    if (fs.existsSync(requestedCwd) && fs.statSync(requestedCwd).isDirectory() && isPathInside(cwdRoot, requestedCwd)) {
      cwd = requestedCwd;
    } else if (projectContext) {
      throw new Error('Run cwd must stay inside the current project.');
    }
  }
  const plugins = Array.isArray(payload.plugins)
    ? payload.plugins
        .map((plugin) => String(plugin || '').trim())
        .filter((plugin) => /^[a-zA-Z0-9_.-]{1,80}$/.test(plugin))
        .slice(0, 12)
    : [];

  return {
    sessionId: sanitizeSessionId(payload.sessionId),
    prompt,
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
  if (!projectContext) return ECOREX_AGENT_SYSTEM_PROMPT;
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
    `项目目录：${projectContext.pathLabel}`,
    `项目记忆：${projectContext.memoryLabel}`
  ].filter(Boolean);
  const memoryPreview = readProjectMemoryPreview(projectContext);
  return [
    ECOREX_AGENT_SYSTEM_PROMPT,
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
    ECOREX_PROJECT_MEMORY_FILE: PROJECT_MEMORY_FILE_NAME,
    ECOREX_PROJECT_MEMORY_DIR: PROJECT_MEMORY_DIR_NAME,
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
  const fallback = { width: 1560, height: 980 };
  let workArea = null;
  try {
    workArea = screen.getPrimaryDisplay()?.workAreaSize || null;
  } catch {
    workArea = null;
  }
  if (!workArea?.width || !workArea?.height) return fallback;
  return {
    width: Math.min(fallback.width, Math.max(1180, workArea.width - 32)),
    height: Math.min(fallback.height, Math.max(780, workArea.height - 32))
  };
}

function createWindow() {
  const bounds = defaultWindowBounds();
  mainWindow = new BrowserWindow({
    width: bounds.width,
    height: bounds.height,
    minWidth: 1040,
    minHeight: 720,
    center: true,
    title: 'EcoreX Agent',
    backgroundColor: '#080d14',
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

  mainWindow.on('ready-to-show', () => {
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
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
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
    writeLog('info', 'Renderer finished loading', windowDiagnosticSnapshot(mainWindow));
  });

  writeLog('info', 'Main window created', windowDiagnosticSnapshot(mainWindow));

  if (app.isPackaged) {
    const target = rendererEntryPath();
    mainWindow.loadFile(target).catch((error) => {
      writeLog('error', 'mainWindow.loadFile failed', {
        ...windowDiagnosticSnapshot(mainWindow),
        target,
        error: error instanceof Error ? error.message : String(error)
      });
    });
  } else {
    const target = 'http://127.0.0.1:5188';
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
}

app.whenReady().then(createWindow);

app.whenReady().then(() => {
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          app.isPackaged
            ? "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
            : "default-src 'self' http://127.0.0.1:5188 ws://127.0.0.1:5188; img-src 'self' data: http://127.0.0.1:5188; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-eval'; connect-src 'self' http://127.0.0.1:5188 ws://127.0.0.1:5188"
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
  const taskName = taskType === 'tool' ? 'EcoreX capability' : publicProductText(rawTaskName);
  const taskId =
    payload.task?.id ||
    payload.taskId ||
    `${sessionId || 'agent'}:${taskType}:${String(taskName).toLowerCase().replace(/[^a-z0-9_.:-]+/g, '-').slice(0, 80)}`;
  const text = safeOutputText(payload.text || '', options.textLimit || MAX_AGENT_EVENT_TEXT_CHARS);
  const safeTools = Array.isArray(payload.tools)
    ? payload.tools.slice(0, 20).map((tool, index) => ({
        id: tool?.id ? publicStableId('capability', tool.id) : `capability-${index + 1}`,
        name: 'EcoreX capability',
        status: tool?.status,
        input: tool?.input ? safeJsonValue(tool.input, 4000) : undefined
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
      name: safeOutputText(taskType === 'tool' ? 'EcoreX capability' : taskName, 120),
      status,
      state
    },
    text
  };

  if (safeTools) event.tools = safeTools;
  if (event.toolName) event.toolName = 'EcoreX capability';
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
    const overflow = queue.events.length - HARD_MAX_AGENT_EVENT_QUEUE;
    queue.dropped += overflow;
    queue.events = [
      normalizeAgentEvent({
        sessionId,
        kind: 'status',
        status: 'backpressure',
        text: `EcoreX buffered ${overflow} older events under renderer backpressure. Session transcript retained the durable summary.`
      }, { includeRaw: false }),
      ...queue.events.slice(-HARD_MAX_AGENT_EVENT_QUEUE)
    ];
    writeLog('error', 'Agent event queue hard limit reached', { sessionId, overflow });
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

  if (runningAgents.has(payload.sessionId) || pendingAgentStarts.has(payload.sessionId)) {
    return {
      ok: false,
      sessionId: payload.sessionId,
      code: 'duplicate-session',
      status: 'running',
      state: 'running',
      error: 'Agent session is already running.',
      recoveryHint: agentRecoveryHint('duplicate-session')
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
    promptPreview: publicPromptPreview(payload.prompt),
    workspacePath: publicWorkspacePath(payload.cwd),
    model: payload.model,
    accessMode: payload.accessMode,
    permissionMode: payload.permissionMode,
    permissionCliMode: payload.permissionCliMode,
    permissionCliFlags: payload.permissionCliFlags,
    permissionLabel: payload.permissionLabel,
    permissionPolicy: payload.permissionPolicy,
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
  if (status === 'completed') return 'Agent task completed.';
  if (status === 'cancelled') return 'Agent task cancelled.';
  if (status === 'timeout') return `Agent task timed out${details.reason === 'idle-timeout' ? ' while idle' : ''}.`;
  if (details.code !== undefined && details.code !== null) return `Agent process exited with code ${details.code}.`;
  if (details.reason) return `Agent task stopped: ${details.reason}.`;
  return 'Agent task failed.';
}

function finalizeAgentSession(sessionId, entry, details = {}) {
  if (!entry || entry.finished) return false;
  entry.finished = true;
  runningAgents.delete(sessionId);
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
    return { ok: false, reason: 'not-found', status: 'not-found', error: 'Agent session was not found.', recoveryHint: agentRecoveryHint('not-found') };
  }
  killProcessTree(entry.child);
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

function commandOutput(command, args = [], options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd || ROOT_DIR,
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
  if (cachedClaude) return cachedClaude;

  for (const candidate of candidateClaudePaths()) {
    const looksLikePath = candidate.includes(path.sep) || candidate.endsWith('.cmd') || candidate.endsWith('.ps1');
    if (looksLikePath && !fs.existsSync(candidate)) continue;

    const invocation = claudeInvocation(candidate, ['--version']);
    const result = await commandOutput(invocation.command, invocation.args, { timeoutMs: 8000 });
    if (result.ok && result.stdout) {
      cachedClaude = {
        command: invocation.command,
        baseArgs: invocation.baseArgs,
        path: looksLikePath ? candidate : 'PATH: claude',
        version: result.stdout
      };
      return cachedClaude;
    }
  }

  cachedClaude = {
    command: null,
    baseArgs: [],
    path: null,
    version: null
  };
  return cachedClaude;
}

async function runClaudeCommand(args, options = {}) {
  const claude = await locateClaude();
  if (!claude.command) {
    return { ok: false, code: -1, stdout: '', stderr: 'Local agent bridge was not found.' };
  }
  return commandOutput(claude.command, [...claude.baseArgs, ...args], options);
}

function parsePluginInventory() {
  const repoRoot = backendPath('claude-code-main');
  const safeRepoRoot = path.resolve(repoRoot);
  const marketplacePath = path.join(repoRoot, '.claude-plugin', 'marketplace.json');
  let marketplace = { plugins: [] };
  try {
    marketplace = fs.existsSync(marketplacePath)
      ? JSON.parse(fs.readFileSync(marketplacePath, 'utf8'))
      : { plugins: [] };
  } catch {
    marketplace = { plugins: [] };
  }

  return (marketplace.plugins || []).map((plugin) => {
    const pluginName = String(plugin.name || '').trim();
    const fallbackSource = `plugins/${pluginName.replace(/[^a-zA-Z0-9_.-]/g, '')}`;
    const source = String(plugin.source || '').replace(/^\.\//, '') || fallbackSource;
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
  });
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
        tags: ['Data connection'],
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
  return attachDeveloperDiagnostics({
    ok: true,
    source: 'EcoreX data connections',
    refreshedAt: new Date().toISOString(),
    configured: false,
    services: [],
    servers: [],
    defaultEmpty: true,
    message: 'EcoreX starts with no data connections. Add project connections inside EcoreX.'
  }, {
    bridge: 'ecorex-managed',
    command: 'disabled: local Claude/Codex MCP inventory is intentionally not scanned'
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
    source: 'EcoreX data connections',
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
    source: 'EcoreX data connections',
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
  const skillsRoot = path.join(pluginRoot, 'skills');
  try {
    if (!fs.existsSync(skillsRoot)) return [];
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
    id: publicStableId('skillpack', plugin.id || plugin.name),
    name: plugin.name || 'Skill pack',
    version: plugin.version || null,
    scope: plugin.scope || null,
    enabled: plugin.enabled !== false,
    installed: Boolean(plugin.installed),
    status: plugin.enabled === false ? 'disabled' : plugin.installed ? 'enabled' : 'available',
    description: plugin.description ? safeOutputText(plugin.description, 500) : undefined,
    installCount: plugin.installCount
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
    error: result.ok ? undefined : publicBridgeError(result, 'Skill inventory refresh failed.')
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

async function collectSkillStatus(payload = {}) {
  return attachDeveloperDiagnostics({
    ok: true,
    source: 'EcoreX Skill Library',
    refreshedAt: new Date().toISOString(),
    unsupportedActions: ['install', 'enable', 'disable', 'update'],
    installedSkillPacks: [],
    availableSkillPacks: [],
    availableSkillPacksTruncated: false,
    skills: [],
    counts: {
      installedSkillPacks: 0,
      installedSkills: 0,
      bundledSkills: 0,
      totalSkills: 0
    },
    defaultEmpty: true,
    partial: false,
    message: 'EcoreX starts with no skills. Install EcoreX skills from the app-managed library later.'
  }, {
    bridge: 'ecorex-managed',
    command: 'disabled: local Claude/Codex skill inventory is intentionally not scanned',
    installedSkillPacks: [],
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

async function buildBackendStatus() {
  const claude = await locateClaude();
  const [auth, mcp, plugins] = await Promise.all([
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
      configured: Boolean(mcp?.stdout && !mcp.stdout.includes('No MCP servers configured')),
      services: parseMcpServices(mcp?.stdout || mcp?.stderr || '')
    },
    mcp: undefined,
    skillPacks: {
      summary: plugins?.ok ? 'Skill inventory is available.' : 'Skill inventory is unavailable.'
    },
    sourceMap: publicSourceMapStats()
  };
}

async function collectBackendStatus(_event, payload = {}) {
  const forceRefresh = Boolean(payload?.refresh || payload?.forceRefresh);
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
    status: entry.status || 'running',
    state: entry.state || 'running',
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

function recordSessionEvent(entry, event) {
  if (!entry) return;
  if (!Array.isArray(entry.transcript)) entry.transcript = [];
  const normalized = normalizeAgentEvent(event, { includeRaw: false, textLimit: 2000 });
  entry.status = normalized.status;
  entry.state = normalized.state;
  entry.lastActivityAt = Date.now();
  entry.lastEventAt = normalized.time;
  entry.transcript.push({
    time: normalized.time,
    kind: normalized.kind,
    status: normalized.status,
    state: normalized.state,
    detailStatus: normalized.detailStatus,
    task: normalized.task,
    textPreview: safeTranscriptTextPreview(normalized.text)
  });
  if (entry.transcript.length > MAX_TRANSCRIPT_EVENTS) {
    entry.transcript = entry.transcript.slice(-MAX_TRANSCRIPT_EVENTS);
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
      recent: recentSessionFiles(sessionLimit).map(safeSessionSummaryForDiagnostics)
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
    error: backendResult.reason instanceof Error ? backendResult.reason.message : String(backendResult.reason || 'Backend status failed.')
  };
  const capabilities = capabilitiesResult.status === 'fulfilled' ? capabilitiesResult.value : {
    ok: false,
    error: capabilitiesResult.reason instanceof Error ? capabilitiesResult.reason.message : String(capabilitiesResult.reason || 'Capability status failed.')
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
    runningSessions: getRunningSessionSummaries(),
    crashes: readCrashSummary(10),
    logs: {
      path: logPath(),
      recent: readRecentLogs(40)
    }
  };

  if (!backendStatus.ok) health.backend.error = safeOutputText(backendStatus.error || 'Backend status unavailable.', 1000);
  if (capabilities.ok === false) health.capabilities.error = safeOutputText(capabilities.error || 'Capability status unavailable.', 1000);

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
    logs: {
      path: logPath(),
      recent: readRecentLogs()
    },
    runningSessions: getRunningSessionSummaries(),
    recentSessionHistory: recentSessionFiles(),
    recentSessionFiles: recentSessionFiles(),
    crashes: readCrashSummary(),
    backend: {
      source: fileSummary(backendSource),
      sourceMap: fileSummary(backendMap),
      dist: fileSummary(devPath('dist', 'index.html')),
      packagedBackend: fileSummary(path.join(process.resourcesPath || '', 'backend')),
      nativeBinaryPackage: nativePackage
        ? fileSummary(devPath('node_modules', '@anthropic-ai', nativePackage))
        : { path: null, exists: false }
    },
    release: collectReleaseInstallers()
  };
}

async function collectCapabilities() {
  if (cachedCapabilities) return cachedCapabilities;
  const map = sourceMapStats();
  const capabilityPacks = [];
  const totals = { commands: 0, agents: 0, skills: 0, hooks: 0, bins: 0 };

  cachedCapabilities = {
    capabilityPacks,
    plugins: capabilityPacks,
    totals,
    sourceMap: publicSourceMapStats(map),
    permissionModes: publicPermissionPolicies(),
    models: modelCapabilityOptions(),
    builtIns: [
      'Read / Write / Edit / Grep / Glob',
      'Shell execution tools',
      'EcoreX data connections',
      'Plan / Auto / Full Access permission modes',
      'Background agents and multi-agent dispatch',
      'Lifecycle guards and approval controls',
      'App-managed EcoreX skills and data connections',
      'Structured streaming output'
    ]
  };
  return cachedCapabilities;
}

function normalizeClaudeEvent(sessionId, json) {
  const base = { sessionId, raw: json, time: new Date().toISOString() };
  if (json.type === 'assistant') {
    const content = json.message?.content || json.content || [];
    const tools = Array.isArray(content)
      ? content
          .filter((block) => block?.type === 'tool_use')
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
            if (block?.type === 'tool_use') return '\n[EcoreX capability running]\n';
            return '';
          })
          .join('')
      : '';
    if (tools.length && !text.trim()) {
      return {
        ...base,
        kind: 'tool',
        status: 'running',
        toolName: 'EcoreX capability',
        tools,
        text: 'EcoreX capability requested.'
      };
    }
    return { ...base, kind: 'assistant', tools, text };
  }

  if (json.type === 'user') {
    const content = json.message?.content || json.content || [];
    const toolResults = Array.isArray(content)
      ? content.filter((block) => block?.type === 'tool_result')
      : [];
    if (toolResults.length) {
      const failed = toolResults.some((block) => block.is_error);
      return {
        ...base,
        kind: 'tool',
        status: failed ? 'failed' : 'completed',
        toolUseId: toolResults[0].tool_use_id,
        text: toolResults
          .map((block) => {
            if (typeof block.content === 'string') return block.content;
            if (Array.isArray(block.content)) {
              return block.content.map((item) => item?.text || '').join('\n');
            }
            return '';
          })
          .filter(Boolean)
          .join('\n')
      };
    }
  }

  if (json.type === 'result') {
    return {
      ...base,
      kind: 'result',
      status: 'completed',
      text: json.result || '',
      costUsd: json.total_cost_usd,
      durationMs: json.duration_ms
    };
  }

  if (json.type === 'system') {
    return {
      ...base,
      kind: 'status',
      status: json.subtype || 'system',
      text: json.cwd || json.session_id || json.message || '会话已初始化'
    };
  }

  if (json.type?.includes('hook') || json.type?.includes('tool')) {
    return {
      ...base,
      kind: 'tool',
      status: json.type,
      text: json.message || 'EcoreX capability event'
    };
  }

  return { ...base, kind: 'debug', text: JSON.stringify(json) };
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
      resolve({ ok: false, sessionId, status: 'failed', error: '提示词为空。', recoveryHint: 'Enter a prompt before starting the task.' });
      return;
    }

    const startClaim = claimAgentStart(safePayload, options);
    if (!startClaim.ok) {
      resolve(startClaim);
      return;
    }

    const startLock = startClaim.lock;

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
        resolve({ ok: false, sessionId, status: 'failed', error: '本地执行引擎未就绪。', recoveryHint: 'Install or start the local runtime engine, then retry.' });
        return;
      }

      const {
        accessMode,
        permissionMode,
        permissionCliMode,
        permissionCliFlags,
        permissionLabel,
        permissionPolicy,
        model,
        cwd,
        projectContext
      } = safePayload;
    const selectedPlugins = safePayload.plugins;
    const repoRoot = backendPath('claude-code-main');
    const safeRepoRoot = path.resolve(repoRoot);
    const pluginInventory = parsePluginInventory();
    const pluginPathByName = new Map(
      pluginInventory
        .filter((plugin) => plugin.available)
        .map((plugin) => [plugin.name, path.resolve(repoRoot, plugin.source)])
    );
    const args = [
      ...claude.baseArgs,
      '--print',
      '--output-format',
      'stream-json',
      '--verbose',
      '--include-partial-messages',
      '--include-hook-events',
      '--model',
      model,
      '--append-system-prompt',
      agentSystemPromptForProject(projectContext),
      '--name',
      'EcoreX Desktop Agent'
    ];
    if (permissionCliMode) args.push('--permission-mode', permissionCliMode);
    for (const flag of permissionCliFlags || []) {
      args.push(flag);
    }

    for (const pluginName of selectedPlugins) {
      const pluginPath = pluginPathByName.get(pluginName);
      if (
        pluginPath &&
        isPathInside(safeRepoRoot, pluginPath) &&
        fs.existsSync(pluginPath)
      ) {
        args.push('--plugin-dir', pluginPath);
      }
    }

    const modelProfileEnv = modelProfileEnvForModel(model);
    const child = spawn(claude.command, args, {
      cwd,
      env: filteredAgentEnv({
        ...modelProfileEnv,
        ...projectEnvForAgent(projectContext),
        CLAUDE_CODE_NO_FLICKER: '1',
        CLAUDE_CODE_SIMPLE: safePayload.bare ? '1' : process.env.CLAUDE_CODE_SIMPLE || ''
      }),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      shell: false
    });
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
      cwd,
      model,
      accessMode,
      permissionMode,
      permissionLabel,
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
      accessMode,
      permissionMode,
      permissionCliMode,
      permissionCliFlags,
      permissionLabel,
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      text: '本地执行引擎会话已启动'
    };
    const entry = {
      child,
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
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      promptPreview: startLock.promptPreview,
      workspacePath: startLock.workspacePath,
      startedAt: Date.now(),
      lastActivityAt: Date.now(),
      status: 'starting',
      state: 'running',
      finished: false,
      transcript: [],
      transcriptWritten: false,
      flushBufferedOutput: null,
      totalTimer: null,
      idleTimer: null
    };
    entry.flushBufferedOutput = () => {
      if (!lineBuffer.trim()) return;
      const buffered = lineBuffer.trim();
      lineBuffer = '';
      try {
        const event = normalizeClaudeEvent(sessionId, JSON.parse(buffered));
        recordSessionEvent(entry, event);
        emitAgentEvent(event);
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
    releaseAgentStart(sessionId, startLock);
    armIdleTimer(sessionId);
    resolve({
      ok: true,
      sessionId,
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
      accessMode,
      permissionMode,
      permissionCliMode,
      permissionCliFlags,
      permissionLabel,
      projectId: projectContext?.id || null,
      projectName: projectContext?.name || '',
      projectPath: projectContext?.pathLabel || '',
      projectMemoryLabel: projectContext?.memoryLabel || '',
      text: '本地执行引擎会话已启动'
    });

    child.stdout.on('data', (chunk) => {
      const entry = runningAgents.get(sessionId);
      if (entry) entry.lastActivityAt = Date.now();
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
          recordSessionEvent(runningAgents.get(sessionId), event);
          emitAgentEvent(event);
        } catch {
          const event = { sessionId, kind: 'assistant', text: trimmed };
          recordSessionEvent(runningAgents.get(sessionId), event);
          emitAgentEvent(event);
        }
      }
    });

    child.stderr.on('data', (chunk) => {
      const entry = runningAgents.get(sessionId);
      if (entry) entry.lastActivityAt = Date.now();
      armIdleTimer(sessionId);
      const stderrText = safeOutputText(chunk.toString(), MAX_AGENT_EVENT_TEXT_CHARS);
      const event = {
        sessionId,
        kind: 'stderr',
        text: stderrText
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
      finalizeAgentSession(sessionId, entry, {
        status: code === 0 ? 'completed' : 'failed',
        code,
        signal
      });
      writeLog(code === 0 ? 'info' : 'error', 'Agent session closed', {
        sessionId,
        code,
        signal,
        durationMs: Date.now() - entry.startedAt
      });
    });
    } catch (error) {
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
      if (options.authRequired && !isAuthorized(args)) {
        writeLog('warn', 'Blocked unauthorized IPC invoke', { channel });
        return unauthorizedResponse();
      }
      return await handler(event, ...args);
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
handleSafe('auth:logout', logoutAuth);
handleSafe('auth:status', (_event, payload = {}) =>
  publicAuthSession(readAuthSession({ refresh: Boolean(payload?.refresh) }), {
    includeToken: Boolean(payload?.includeToken)
  })
);
handleSafe('secrets:status', (_event, payload) => {
  optionalObjectPayload(payload, 'secret payload');
  return secretsStatus();
}, { authRequired: true });
handleSafe('secrets:list', (_event, payload) => listSecrets(payload), { authRequired: true });
handleSafe('secrets:set', (_event, payload) => setSecret(payload), { authRequired: true });
handleSafe('secrets:delete', (_event, payload) => deleteSecret(payload), { authRequired: true });
handleSafe('listModelProfiles', (_event, payload) => listModelProfiles(payload), { authRequired: true });
handleSafe('saveModelProfile', (_event, payload) => saveModelProfile(payload), { authRequired: true });
handleSafe('deleteModelProfile', (_event, payload) => deleteModelProfile(payload), { authRequired: true });
handleSafe('activateModelProfile', (_event, payload) => activateModelProfile(payload), { authRequired: true });
handleSafe('testModelProfile', (_event, payload) => testModelProfile(payload), { authRequired: true });
handleSafe('modelAdapter:testProfile', (_event, payload) => testModelProfile(payload), { authRequired: true });
handleSafe('modelAdapter:generateImage', (_event, payload) => generateModelProfileImage(payload), { authRequired: true });
handleSafe('settings:get', () => ({ ok: true, settings: writeSettings(readSettings()) }), { authRequired: true });
handleSafe('settings:update', (_event, payload) => ({ ok: true, settings: updateSettings(payload) }), { authRequired: true });
handleSafe('startup:health', collectStartupHealth, { authRequired: true });
handleSafe('diagnostics:get', collectDiagnostics, { authRequired: true });
handleSafe('diagnostics:export', exportDiagnosticsPackage, { authRequired: true });
handleSafe('diagnostics:open-location', openDiagnosticsLocation, { authRequired: true });
handleSafe('diagnostics:crash-recovery', (_event, payload) => getCrashRecoveryStatus(payload), { authRequired: true });
handleSafe('workspace:select-directory', (event, payload) => selectWorkspaceDirectory(event, payload), { authRequired: true });
handleSafe('workspace:list', (_event, payload) => listWorkspace(payload), { authRequired: true });
handleSafe('workspace:ensure', (_event, payload) => ensureWorkspace(payload), { authRequired: true });
handleSafe('project:list', (_event, payload) => listProjects(payload), { authRequired: true });
handleSafe('project:create', (_event, payload) => createProject(payload), { authRequired: true });
handleSafe('project:switch', (_event, payload) => switchProject(payload), { authRequired: true });
handleSafe('project:update', (_event, payload) => updateProject(payload), { authRequired: true });
handleSafe('project:archive', (_event, payload) => archiveProject(payload), { authRequired: true });
handleSafe('project:status', (_event, payload) => projectStatus(payload), { authRequired: true });
handleSafe('mcp:list', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:status', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:refresh', (_event, payload) => collectMcpStatus(payload), { authRequired: true });
handleSafe('mcp:get', (_event, payload) => getMcpServer(payload), { authRequired: true });
handleSafe('mcp:update', (_event, payload) => updateMcpConfig(payload), { authRequired: true });
handleSafe('mcp:update-config', (_event, payload) => updateMcpConfig(payload), { authRequired: true });
handleSafe('mcp:enable', () => unsupportedMcpToggle('enable'), { authRequired: true });
handleSafe('mcp:disable', () => unsupportedMcpToggle('disable'), { authRequired: true });
handleSafe('skill:list', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:status', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:refresh', (_event, payload) => collectSkillStatus(payload), { authRequired: true });
handleSafe('skill:install', () => unsupportedSkillAction('install'), { authRequired: true });
handleSafe('skill:enable', () => unsupportedSkillAction('enable'), { authRequired: true });
handleSafe('skill:disable', () => unsupportedSkillAction('disable'), { authRequired: true });
handleSafe('skill:update', () => unsupportedSkillAction('update'), { authRequired: true });
handleSafe('agent:run', (event, payload) => runAgent(payload, { ownerId: event.sender.id }), { authRequired: true });
handleSafe('agent:stop', (_event, payload) => {
  const sessionId = typeof payload === 'object' && payload ? payload.sessionId : payload;
  return stopAgent(sessionId, 'cancelled');
}, { authRequired: true });
handleSafe('agent:sessions', () => {
  return { ok: true, sessions: getRunningSessionSummaries() };
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
      stdio: 'ignore',
      windowsHide: true
    }).unref();
  } else {
    spawn(claude.command, [...claude.baseArgs, 'auth', 'login'], {
      detached: true,
      stdio: 'ignore'
    }).unref();
  }
  return { ok: true };
}, { authRequired: true });

ipcMain.on('window:control', (event, action) => {
  const payload = action && typeof action === 'object' && !Array.isArray(action) ? action : { action };
  const unauthorized = assertAuthorizedIpc(event, 'window:control', [payload]);
  if (unauthorized) return;
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

const { app, BrowserWindow, ipcMain, session } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT_DIR = path.resolve(__dirname, '..');
const isWindows = process.platform === 'win32';
const runningAgents = new Map();
let mainWindow = null;
let cachedClaude = null;
let cachedCapabilities = null;
const AGENT_TIMEOUT_MS = 30 * 60 * 1000;
const AGENT_IDLE_TIMEOUT_MS = 5 * 60 * 1000;
const AGENT_MIN_TIMEOUT_MS = 30 * 1000;
const MAX_PROMPT_CHARS = 80_000;
const MIN_PROMPT_CHARS = 1_000;
const SETTINGS_FILE_NAME = 'settings.json';
const LOG_FILE_NAME = 'ecorex-agent.log';
const MAX_LOG_LINES = 200;
const MAX_COMMAND_OUTPUT_CHARS = 2 * 1024 * 1024;
const MAX_AGENT_LINE_BUFFER_CHARS = 2 * 1024 * 1024;
const ALLOWED_PERMISSION_MODES = new Set(['acceptEdits', 'auto', 'plan', 'default']);
const ALLOWED_MODELS = new Set(['sonnet', 'opus']);
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
  'ANTHROPIC_BASE_URL',
  'CLAUDE_CONFIG_DIR',
  'XDG_CONFIG_HOME'
]);

const AGENT_SYSTEM_PROMPT = [
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
  return path.join(ROOT_DIR, '终端源代码', ...segments);
}

function logPath() {
  return path.join(app.getPath('logs'), LOG_FILE_NAME);
}

function writeLog(level, message, meta = {}) {
  try {
    fs.mkdirSync(app.getPath('logs'), { recursive: true });
    const payload = {
      time: new Date().toISOString(),
      level,
      message,
      ...meta
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

function filteredAgentEnv(extra = {}) {
  const env = {};
  for (const key of AGENT_ENV_ALLOWLIST) {
    if (process.env[key]) env[key] = process.env[key];
  }
  return { ...env, ...extra };
}

function isTrustedSender(event) {
  const senderUrl = event?.senderFrame?.url || event?.sender?.getURL?.() || '';
  if (!senderUrl) return true;
  if (app.isPackaged) return senderUrl.startsWith('file://');
  return senderUrl.startsWith('http://127.0.0.1:5188') || senderUrl.startsWith('http://localhost:5188');
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

function defaultWorkspaceRoot() {
  const workspace = path.join(app.getPath('userData'), 'workspace');
  return workspace;
}

function settingsPath() {
  return path.join(app.getPath('userData'), SETTINGS_FILE_NAME);
}

function clampPromptChars(value) {
  const chars = Number(value);
  if (!Number.isFinite(chars)) return MAX_PROMPT_CHARS;
  return Math.min(Math.max(Math.floor(chars), MIN_PROMPT_CHARS), MAX_PROMPT_CHARS);
}

function normalizeWorkspaceRoot(value) {
  const fallback = defaultWorkspaceRoot();
  const raw = typeof value === 'string' && value.trim() ? value.trim() : fallback;
  const resolved = path.resolve(raw);
  if (resolved.length > 500) return fallback;
  return resolved;
}

function normalizeSettings(raw = {}) {
  return {
    defaultModel: ALLOWED_MODELS.has(raw.defaultModel) ? raw.defaultModel : 'sonnet',
    permissionMode: ALLOWED_PERMISSION_MODES.has(raw.permissionMode) ? raw.permissionMode : 'acceptEdits',
    workspaceRoot: normalizeWorkspaceRoot(raw.workspaceRoot),
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
    if (!ALLOWED_MODELS.has(payload.defaultModel)) throw new Error('Invalid defaultModel.');
    next.defaultModel = payload.defaultModel;
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'permissionMode')) {
    if (!ALLOWED_PERMISSION_MODES.has(payload.permissionMode)) throw new Error('Invalid permissionMode.');
    next.permissionMode = payload.permissionMode;
  }
  if (Object.prototype.hasOwnProperty.call(payload, 'workspaceRoot')) {
    if (typeof payload.workspaceRoot !== 'string' || !payload.workspaceRoot.trim()) {
      throw new Error('Invalid workspaceRoot.');
    }
    next.workspaceRoot = normalizeWorkspaceRoot(payload.workspaceRoot);
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
          const stat = fs.statSync(absolute);
          return {
            name: entry.name,
            path: path.relative(workspaceRoot, absolute).replace(/\\/g, '/'),
            type: entry.isDirectory() ? 'directory' : 'file',
            size: entry.isFile() ? stat.size : 0,
            modifiedAt: stat.mtime.toISOString()
          };
        })
    : [];
  return { ok: true, workspaceRoot, relativePath: relativePath.replace(/\\/g, '/'), entries };
}

function ensureWorkspace(payload = {}) {
  if (typeof payload?.workspaceRoot === 'string' && payload.workspaceRoot.trim()) {
    updateSettings({ workspaceRoot: payload.workspaceRoot });
  }
  const { workspaceRoot, target, relativePath } = resolveWorkspacePath(payload?.relativePath);
  fs.mkdirSync(target, { recursive: true });
  const stat = fs.statSync(target);
  if (!stat.isDirectory()) throw new Error('Workspace path is not a directory.');
  return {
    ok: true,
    workspaceRoot,
    path: target,
    relativePath: relativePath.replace(/\\/g, '/'),
    exists: true
  };
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
  const permissionMode = ALLOWED_PERMISSION_MODES.has(payload.permissionMode)
    ? payload.permissionMode
    : settings.permissionMode;
  const model = ALLOWED_MODELS.has(payload.model) ? payload.model : settings.defaultModel;
  const workspaceRoot = settings.workspaceRoot;
  fs.mkdirSync(workspaceRoot, { recursive: true });
  const requestedCwd = payload.cwd ? path.resolve(String(payload.cwd)) : workspaceRoot;
  const cwd = fs.existsSync(requestedCwd) && isPathInside(workspaceRoot, requestedCwd) ? requestedCwd : workspaceRoot;
  const plugins = Array.isArray(payload.plugins)
    ? payload.plugins
        .map((plugin) => String(plugin || '').trim())
        .filter((plugin) => /^[a-zA-Z0-9_.-]{1,80}$/.test(plugin))
        .slice(0, 12)
    : [];

  return {
    sessionId: sanitizeSessionId(payload.sessionId),
    prompt,
    permissionMode,
    model,
    cwd,
    plugins,
    timeoutMs: clampTimeout(payload.timeoutMs),
    bare: Boolean(payload.bare)
  };
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1180,
    minHeight: 760,
    title: 'EcoreX Agent',
    backgroundColor: '#080d14',
    icon: devPath('build', 'icon.ico'),
    frame: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  mainWindow.on('closed', () => {
    stopAllAgents('window-closed');
    mainWindow = null;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    writeLog('warn', 'Blocked window.open', { url });
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const allowed = app.isPackaged
      ? url.startsWith('file://')
      : url.startsWith('http://127.0.0.1:5188') || url.startsWith('http://localhost:5188');
    if (!allowed) {
      event.preventDefault();
      writeLog('warn', 'Blocked navigation', { url });
    }
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    writeLog('error', 'Renderer process gone', details);
    stopAllAgents('renderer-gone');
  });
  mainWindow.webContents.on('unresponsive', () => {
    writeLog('warn', 'Renderer became unresponsive');
  });

  if (app.isPackaged) {
    mainWindow.loadFile(path.join(ROOT_DIR, 'dist', 'index.html'));
  } else {
    mainWindow.loadURL('http://127.0.0.1:5188');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
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
  writeLog('error', 'Main process uncaughtException', {
    error: error?.message,
    stack: error?.stack
  });
});

process.on('unhandledRejection', (reason) => {
  writeLog('error', 'Main process unhandledRejection', {
    error: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : undefined
  });
});

function emitAgentEvent(payload) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('agent:event', payload);
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

function stopAgent(sessionId, reason = 'cancelled') {
  const entry = runningAgents.get(sessionId);
  if (!entry) return { ok: false, reason: 'not-found' };
  runningAgents.delete(sessionId);
  clearAgentTimers(entry);
  killProcessTree(entry.child);
  writeLog(reason === 'cancelled' ? 'info' : 'warn', 'Agent session stopped', {
    sessionId,
    reason,
    durationMs: Date.now() - entry.startedAt
  });
  emitAgentEvent({
    sessionId,
    kind: reason === 'cancelled' ? 'cancelled' : 'error',
    status: reason,
    text: reason === 'cancelled' ? '用户已取消当前 Agent 任务' : `Agent 任务已停止：${reason}`
  });
  return { ok: true, reason };
}

function stopAllAgents(reason = 'app-quit') {
  for (const sessionId of runningAgents.keys()) {
    stopAgent(sessionId, reason);
  }
}

function commandOutput(command, args = [], options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd || ROOT_DIR,
      env: filteredAgentEnv(options.env || {}),
      windowsHide: true,
      shell: false
    });

    let stdout = '';
    let stderr = '';
    let truncated = false;
    const timeout = setTimeout(() => {
      killProcessTree(child);
      resolve({ ok: false, code: -1, stdout, stderr: `${stderr}\nCommand timed out.`.trim() });
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
      clearTimeout(timeout);
      resolve({ ok: false, code: -1, stdout, stderr: error.message });
    });
    child.on('close', (code) => {
      clearTimeout(timeout);
      resolve({ ok: code === 0, code, stdout: stdout.trim(), stderr: stderr.trim(), truncated });
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
    return { ok: false, code: -1, stdout: '', stderr: 'Claude Code CLI was not found.' };
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

function parseMcpServices(raw = '') {
  if (!raw || /No MCP servers configured/i.test(raw)) return [];

  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/^name\s+/i.test(line) && !/^-{3,}$/.test(line))
    .map((line, index) => {
      const urlMatch = line.match(/https?:\/\/\S+/i);
      const nameMatch = line.match(/^([^\s:|]+)/);
      const status = /(disabled|offline|failed|error|disconnected)/i.test(line) ? 'offline' : 'online';
      return {
        id: nameMatch?.[1] || `mcp-${index + 1}`,
        name: nameMatch?.[1] || `MCP ${index + 1}`,
        url: urlMatch?.[0] || line.replace(/\s+/g, ' '),
        raw: line,
        tags: ['Claude MCP'],
        auth: /oauth/i.test(line) ? 'OAuth 2.0' : 'Local',
        authState: status === 'online' ? '已授权' : '需授权',
        status,
        ping: status === 'online' ? '已连接' : '-',
        sync: '刚刚',
        permissions: '读写'
      };
    });
}

async function collectBackendStatus() {
  const claude = await locateClaude();
  const [auth, mcp, plugins] = await Promise.all([
    claude.command ? runClaudeCommand(['auth', 'status'], { timeoutMs: 10000 }) : Promise.resolve(null),
    claude.command ? runClaudeCommand(['mcp', 'list'], { timeoutMs: 10000 }) : Promise.resolve(null),
    claude.command ? runClaudeCommand(['plugin', 'list'], { timeoutMs: 10000 }) : Promise.resolve(null)
  ]);

  let authStatus = null;
  if (auth?.stdout) {
    try {
      authStatus = JSON.parse(auth.stdout);
    } catch {
      authStatus = { loggedIn: false, raw: auth.stdout || auth.stderr };
    }
  }

  return {
    ok: Boolean(claude.command),
    claude,
    auth: authStatus || { loggedIn: false },
    mcp: {
      raw: mcp?.stdout || mcp?.stderr || '',
      configured: Boolean(mcp?.stdout && !mcp.stdout.includes('No MCP servers configured')),
      services: parseMcpServices(mcp?.stdout || mcp?.stderr || '')
    },
    installedPlugins: plugins?.stdout || plugins?.stderr || '',
    sourceMap: sourceMapStats()
  };
}

function getRunningSessionSummaries() {
  return Array.from(runningAgents.entries()).map(([sessionId, entry]) => ({
    sessionId,
    prompt: entry.prompt,
    cwd: entry.cwd,
    startedAt: entry.startedAt,
    lastActivityAt: entry.lastActivityAt
  }));
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
    installers: entries
  };
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
    settings,
    workspaceRoot: settings.workspaceRoot,
    claude: {
      path: claude.path,
      version: claude.version,
      command: claude.command,
      nativePackage
    },
    logs: {
      path: logPath(),
      recent: readRecentLogs()
    },
    runningSessions: getRunningSessionSummaries(),
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
  const plugins = parsePluginInventory();
  const map = sourceMapStats();
  const totals = plugins.reduce(
    (acc, plugin) => {
      acc.commands += plugin.commands;
      acc.agents += plugin.agents;
      acc.skills += plugin.skills;
      acc.hooks += plugin.hooks;
      acc.bins += plugin.bins;
      return acc;
    },
    { commands: 0, agents: 0, skills: 0, hooks: 0, bins: 0 }
  );

  cachedCapabilities = {
    plugins,
    totals,
    sourceMap: map,
    permissionModes: [
      { value: 'acceptEdits', label: 'Accept' },
      { value: 'auto', label: 'Auto' },
      { value: 'plan', label: 'Plan' },
      { value: 'default', label: 'Default' }
    ],
    models: [
      { value: 'sonnet', label: 'Sonnet' },
      { value: 'opus', label: 'Opus' }
    ],
    builtIns: [
      'Read / Write / Edit / Grep / Glob',
      'Bash 与 PowerShell 工具',
      'MCP servers 与远程连接器',
      'Plan / Auto / Accept Edits 权限模式',
      'Background agents 与多 Agent 派发',
      'Hooks 生命周期与安全拦截',
      'Skills、Slash commands、Plugin marketplace',
      'Stream JSON 输出、结构化 JSON Schema'
    ]
  };
  return cachedCapabilities;
}

function normalizeClaudeEvent(sessionId, json) {
  const base = { sessionId, raw: json, time: new Date().toISOString() };
  if (json.type === 'assistant') {
    const content = json.message?.content || json.content || [];
    const text = Array.isArray(content)
      ? content
          .map((block) => {
            if (typeof block === 'string') return block;
            if (block?.type === 'text') return block.text;
            if (block?.type === 'tool_use') return `\n[调用工具] ${block.name || 'tool'}\n`;
            return '';
          })
          .join('')
      : '';
    return { ...base, kind: 'assistant', text };
  }

  if (json.type === 'result') {
    return {
      ...base,
      kind: 'result',
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
      text: json.name || json.tool_name || json.message || '后端工具事件'
    };
  }

  return { ...base, kind: 'debug', text: JSON.stringify(json) };
}

function runAgent(payload = {}) {
  return new Promise(async (resolve) => {
    const safePayload = sanitizePayload(payload);
    const claude = await locateClaude();
    const sessionId = safePayload.sessionId;
    if (!claude.command) {
      resolve({ ok: false, sessionId, error: '未找到 Claude Code CLI。' });
      return;
    }

    const prompt = safePayload.prompt;
    if (!prompt) {
      resolve({ ok: false, sessionId, error: '提示词为空。' });
      return;
    }

    const { permissionMode, model, cwd } = safePayload;
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
      '--include-partial-messages',
      '--permission-mode',
      permissionMode,
      '--model',
      model,
      '--append-system-prompt',
      AGENT_SYSTEM_PROMPT,
      '--name',
      'EcoreX Desktop Agent'
    ];

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

    args.push(prompt);

    const child = spawn(claude.command, args, {
      cwd,
      env: filteredAgentEnv({
        CLAUDE_CODE_NO_FLICKER: '1',
        CLAUDE_CODE_SIMPLE: safePayload.bare ? '1' : process.env.CLAUDE_CODE_SIMPLE || ''
      }),
      windowsHide: true,
      shell: false
    });
    writeLog('info', 'Agent session started', { sessionId, cwd, model, permissionMode, plugins: selectedPlugins });

    const startedEvent = {
      sessionId,
      kind: 'status',
      status: 'started',
      text: 'Claude Code backend session started'
    };
    const entry = {
      child,
      prompt,
      cwd,
      startedAt: Date.now(),
      lastActivityAt: Date.now(),
      totalTimer: null,
      idleTimer: null
    };
    entry.totalTimer = setTimeout(() => {
      stopAgent(sessionId, 'timeout');
    }, safePayload.timeoutMs);
    runningAgents.set(sessionId, entry);
    armIdleTimer(sessionId);
    resolve({ ok: true, sessionId, initialEvent: startedEvent });

    emitAgentEvent({
      sessionId,
      kind: 'status',
      status: 'started',
      text: 'Claude Code 后端会话已启动'
    });

    let lineBuffer = '';
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
          emitAgentEvent(normalizeClaudeEvent(sessionId, json));
        } catch {
          emitAgentEvent({ sessionId, kind: 'assistant', text: trimmed });
        }
      }
    });

    child.stderr.on('data', (chunk) => {
      const entry = runningAgents.get(sessionId);
      if (entry) entry.lastActivityAt = Date.now();
      armIdleTimer(sessionId);
      emitAgentEvent({
        sessionId,
        kind: 'stderr',
        text: chunk.toString()
      });
      writeLog('warn', 'Agent stderr', { sessionId, text: chunk.toString().slice(0, 4000) });
    });

    child.on('error', (error) => {
      const entry = runningAgents.get(sessionId);
      if (!entry) return;
      runningAgents.delete(sessionId);
      clearAgentTimers(entry);
      writeLog('error', 'Agent process error', { sessionId, error: error.message });
      emitAgentEvent({ sessionId, kind: 'error', text: error.message });
    });

    child.on('close', (code) => {
      const entry = runningAgents.get(sessionId);
      if (!entry) return;
      runningAgents.delete(sessionId);
      clearAgentTimers(entry);
      if (lineBuffer.trim()) {
        try {
          emitAgentEvent(normalizeClaudeEvent(sessionId, JSON.parse(lineBuffer.trim())));
        } catch {
          emitAgentEvent({ sessionId, kind: 'assistant', text: lineBuffer.trim() });
        }
      }
      emitAgentEvent({
        sessionId,
        kind: code === 0 ? 'done' : 'error',
        status: code === 0 ? 'completed' : 'failed',
        text: code === 0 ? '后端执行完成' : `后端进程退出，代码 ${code}`
      });
      writeLog(code === 0 ? 'info' : 'error', 'Agent session closed', {
        sessionId,
        code,
        durationMs: Date.now() - entry.startedAt
      });
    });
  });
}

function handleSafe(channel, handler) {
  ipcMain.handle(channel, async (event, ...args) => {
    try {
      if (!isTrustedSender(event)) {
        writeLog('warn', 'Blocked untrusted IPC invoke', { channel, url: event.senderFrame?.url });
        return { ok: false, error: 'Untrusted renderer.' };
      }
      return await handler(event, ...args);
    } catch (error) {
      writeLog('error', 'IPC handler failed', {
        channel,
        error: error instanceof Error ? error.message : String(error)
      });
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error)
      };
    }
  });
}

function assertTrustedIpc(event, channel) {
  if (isTrustedSender(event)) return true;
  writeLog('warn', 'Blocked untrusted IPC message', { channel, url: event.senderFrame?.url });
  return false;
}

handleSafe('backend:status', collectBackendStatus);
handleSafe('backend:capabilities', collectCapabilities);
handleSafe('settings:get', () => ({ ok: true, settings: writeSettings(readSettings()) }));
handleSafe('settings:update', (_event, payload) => ({ ok: true, settings: updateSettings(payload) }));
handleSafe('diagnostics:get', collectDiagnostics);
handleSafe('workspace:list', (_event, payload) => listWorkspace(payload));
handleSafe('workspace:ensure', (_event, payload) => ensureWorkspace(payload));
ipcMain.handle('agent:run', (event, payload) => {
  if (!assertTrustedIpc(event, 'agent:run')) return { ok: false, error: 'Untrusted renderer.' };
  return runAgent(payload);
});
ipcMain.handle('agent:stop', (event, sessionId) => {
  if (!assertTrustedIpc(event, 'agent:stop')) return { ok: false, error: 'Untrusted renderer.' };
  return stopAgent(sessionId, 'cancelled');
});
ipcMain.handle('agent:sessions', (event) => {
  if (!assertTrustedIpc(event, 'agent:sessions')) return [];
  return getRunningSessionSummaries();
});
ipcMain.handle('backend:open-auth', async (event) => {
  if (!assertTrustedIpc(event, 'backend:open-auth')) return { ok: false, error: 'Untrusted renderer.' };
  const claude = await locateClaude();
  if (!claude.command) return { ok: false, error: '未找到 Claude Code CLI。' };
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
});

ipcMain.on('window:control', (event, action) => {
  if (!assertTrustedIpc(event, 'window:control')) return;
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window) return;
  if (action === 'minimize') window.minimize();
  if (action === 'maximize') {
    if (window.isMaximized()) window.unmaximize();
    else window.maximize();
  }
  if (action === 'close') window.close();
});

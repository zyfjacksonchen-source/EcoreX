const { app, BrowserWindow, ipcMain } = require('electron');
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
const ALLOWED_PERMISSION_MODES = new Set(['acceptEdits', 'auto', 'plan', 'default']);
const ALLOWED_MODELS = new Set(['sonnet', 'opus']);

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

function defaultAgentCwd() {
  if (!app.isPackaged) return ROOT_DIR;
  const workspace = path.join(app.getPath('userData'), 'workspace');
  fs.mkdirSync(workspace, { recursive: true });
  return workspace;
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
  const prompt = String(payload.prompt || '').trim().slice(0, MAX_PROMPT_CHARS);
  const permissionMode = ALLOWED_PERMISSION_MODES.has(payload.permissionMode)
    ? payload.permissionMode
    : 'acceptEdits';
  const model = ALLOWED_MODELS.has(payload.model) ? payload.model : 'sonnet';
  const workspaceRoot = defaultAgentCwd();
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

  if (app.isPackaged) {
    mainWindow.loadFile(path.join(ROOT_DIR, 'dist', 'index.html'));
  } else {
    mainWindow.loadURL('http://127.0.0.1:5188');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  stopAllAgents('app-quit');
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
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
      env: { ...process.env, ...(options.env || {}) },
      windowsHide: true,
      shell: false
    });

    let stdout = '';
    let stderr = '';
    const timeout = setTimeout(() => {
      killProcessTree(child);
      resolve({ ok: false, code: -1, stdout, stderr: `${stderr}\nCommand timed out.`.trim() });
    }, options.timeoutMs || 15000);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    child.on('error', (error) => {
      clearTimeout(timeout);
      resolve({ ok: false, code: -1, stdout, stderr: error.message });
    });
    child.on('close', (code) => {
      clearTimeout(timeout);
      resolve({ ok: code === 0, code, stdout: stdout.trim(), stderr: stderr.trim() });
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
      env: {
        ...process.env,
        CLAUDE_CODE_NO_FLICKER: '1',
        CLAUDE_CODE_SIMPLE: safePayload.bare ? '1' : process.env.CLAUDE_CODE_SIMPLE || ''
      },
      windowsHide: true,
      shell: false
    });

    const startedEvent = {
      sessionId,
      kind: 'status',
      status: 'started',
      text: 'Claude Code backend session started'
    };
    const entry = {
      child,
      prompt,
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
    });

    child.on('error', (error) => {
      const entry = runningAgents.get(sessionId);
      if (!entry) return;
      runningAgents.delete(sessionId);
      clearAgentTimers(entry);
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
    });
  });
}

ipcMain.handle('backend:status', collectBackendStatus);
ipcMain.handle('backend:capabilities', collectCapabilities);
ipcMain.handle('agent:run', (_event, payload) => runAgent(payload));
ipcMain.handle('agent:stop', (_event, sessionId) => {
  return stopAgent(sessionId, 'cancelled');
});
ipcMain.handle('agent:sessions', () => {
  return Array.from(runningAgents.entries()).map(([sessionId, entry]) => ({
    sessionId,
    prompt: entry.prompt,
    startedAt: entry.startedAt,
    lastActivityAt: entry.lastActivityAt
  }));
});
ipcMain.handle('backend:open-auth', async () => {
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
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window) return;
  if (action === 'minimize') window.minimize();
  if (action === 'maximize') {
    if (window.isMaximized()) window.unmaximize();
    else window.maximize();
  }
  if (action === 'close') window.close();
});

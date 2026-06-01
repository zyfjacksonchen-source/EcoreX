const { spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const argv = new Set(process.argv.slice(2));
const strictReleaseMode =
  argv.has('--strict') ||
  argv.has('--release') ||
  process.env.ECOREX_RELEASE_STRICT === '1' ||
  process.env.ECOREX_RELEASE === '1' ||
  process.env.CI_RELEASE === '1';
const requireSignedArtifact =
  argv.has('--require-signed-artifact') ||
  process.env.ECOREX_REQUIRE_SIGNED_ARTIFACT === '1';
const failures = [];
const warnings = [];
const passes = [];
const REQUIRED_LOCAL_STATE_EXCLUSIONS = [
  '!**/.env',
  '!**/.env.*',
  '!**/*.log',
  '!**/settings.json',
  '!**/secrets.json',
  '!**/auth-session.json',
  '!**/auth-identity.json',
  '!**/auth-users.json',
  '!**/enterprise-admin-journal.jsonl',
  '!**/session-bindings.json',
  '!**/skill-packs.json',
  '!**/skill-packs/**/*',
  '!**/model-profiles.json',
  '!**/.ecorex-project.json',
  '!**/.ecorex-projects.json',
  '!**/.ecorex-memory/**/*',
  '!**/.mcp.json',
  '!**/.claude/**/*',
  '!**/.codex/**/*',
  '!**/.agents/**/*',
  '!**/.lark/**/*',
  '!**/.feishu/**/*',
  '!**/.larksuite/**/*',
  '!**/superpowers/**/*',
  '!**/huashu-design/**/*',
  '!**/huashu_design/**/*'
];
const REQUIRED_BACKEND_FILTER_EXCLUSIONS = [
  ...REQUIRED_LOCAL_STATE_EXCLUSIONS,
  '!plugins/**/node_modules/**/*',
  '!plugins/**/.git/**/*',
  '!plugins/**/superpowers/**/*',
  '!plugins/**/huashu-design/**/*',
  '!plugins/**/huashu_design/**/*'
];
const REQUIRED_BACKEND_RUNTIME_PLUGINS = ['feature-dev', 'code-review', 'security-guidance', 'plugin-dev'];

function rel(...segments) {
  return path.join(rootDir, ...segments);
}

function toPosix(value) {
  return String(value || '').replace(/^[/\\]+/, '').replace(/[\\]+/g, '/');
}

function readText(relativePath) {
  return fs.readFileSync(rel(relativePath), 'utf8');
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function check(name, fn) {
  try {
    fn();
    passes.push(name);
  } catch (error) {
    failures.push({ name, message: error && error.message ? error.message : String(error) });
  }
}

function warn(message) {
  warnings.push(message);
}

function nodeCheck(relativePath) {
  const target = rel(relativePath);
  assert(fs.existsSync(target), `${relativePath} is missing.`);
  const result = spawnSync(process.execPath, ['--check', target], {
    cwd: rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  assert(
    result.status === 0,
    `${relativePath} failed node --check:\n${result.stderr || result.stdout || result.error?.message || ''}`
  );
}

function packageJson() {
  return JSON.parse(readText('package.json'));
}

function includesAll(text, values, label) {
  for (const value of values) {
    assert(text.includes(value), `${label} missing ${value}`);
  }
}

function assertMatches(text, pattern, message) {
  assert(pattern.test(text), message);
}

function assertNotMatches(text, pattern, message) {
  assert(!pattern.test(text), message);
}

function sha512Base64(file) {
  return crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64');
}

function shellQuotePowerShell(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function configuredSigningSources() {
  const pkg = packageJson();
  const win = (pkg.build && pkg.build.win) || {};
  const env = process.env;
  const sources = [];
  const missing = [];

  if (env.CSC_LINK || env.WIN_CSC_LINK) {
    const passwordName = env.CSC_LINK ? 'CSC_KEY_PASSWORD' : 'WIN_CSC_KEY_PASSWORD';
    if (env[passwordName]) sources.push(env.CSC_LINK ? 'CSC_LINK' : 'WIN_CSC_LINK');
    else missing.push(`${passwordName} for ${env.CSC_LINK ? 'CSC_LINK' : 'WIN_CSC_LINK'}`);
  }
  if (env.CSC_NAME || env.WIN_CSC_NAME) sources.push(env.CSC_NAME ? 'CSC_NAME' : 'WIN_CSC_NAME');
  if (env.WINDOWS_CERTIFICATE_FILE) {
    if (env.WINDOWS_CERTIFICATE_PASSWORD) sources.push('WINDOWS_CERTIFICATE_FILE');
    else missing.push('WINDOWS_CERTIFICATE_PASSWORD for WINDOWS_CERTIFICATE_FILE');
  }
  if (win.certificateSubjectName) sources.push('build.win.certificateSubjectName');
  if (win.certificateSha1) sources.push('build.win.certificateSha1');

  return { sources, missing };
}

function latestField(text, key) {
  const match = text.match(new RegExp(`^${key}:\\s*(.+)$`, 'm'));
  return match ? match[1].trim().replace(/^['"]|['"]$/g, '') : '';
}

function latestFileEntry(text) {
  const match = text.match(/files:\s*\r?\n\s*-\s*url:\s*(.+)\r?\n\s*sha512:\s*(.+)\r?\n\s*size:\s*(\d+)/m);
  if (!match) throw new Error('release/latest.yml files[0] entry must include url, sha512, and size.');
  return {
    url: match[1].trim().replace(/^['"]|['"]$/g, ''),
    sha512: match[2].trim().replace(/^['"]|['"]$/g, ''),
    size: Number(match[3])
  };
}

function assertPlainReleaseFileName(value, label) {
  assert(value, `release/latest.yml ${label} is empty.`);
  assert(!path.isAbsolute(value), `release/latest.yml ${label} must be a relative artifact name.`);
  assert(!/[\\/]/.test(value), `release/latest.yml ${label} must not contain a directory.`);
}

function authenticodeStatus(file) {
  if (process.platform !== 'win32') return { status: 'skipped', message: 'Authenticode check is Windows-only.' };
  const command = [
    '$sig = Get-AuthenticodeSignature -LiteralPath',
    shellQuotePowerShell(file),
    ';',
    '[Console]::Out.Write(($sig.Status.ToString()) + "|" + ($sig.StatusMessage -replace "\\r?\\n", " "))'
  ].join(' ');
  const result = spawnSync('powershell.exe', ['-NoProfile', '-Command', command], {
    cwd: rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.status !== 0) {
    return { status: 'error', message: result.stderr || result.stdout || result.error?.message || 'PowerShell signature check failed.' };
  }
  const [status, ...messageParts] = String(result.stdout || '').split('|');
  return { status: status.trim(), message: messageParts.join('|').trim() };
}

function assertNoSecretOrModelStoragePaths(files, label) {
  const forbiddenNames = [
    '.env',
    '.npmrc',
    '.yarnrc',
    'model-profiles.json',
    'secrets.json',
    'auth-session.json',
    'auth-identity.json',
    'auth-users.json',
    'enterprise-admin-journal.jsonl',
    'session-bindings.json',
    'settings.json',
    '.ecorex-projects.json',
    '.ecorex-project.json',
    '.ecorex-memory',
    'ecorex-agent.log'
  ];
  const forbiddenSegments = ['.claude', '.codex', '.agents', '.mcp', '.lark', '.feishu', '.larksuite', 'superpowers', 'huashu-design', 'huashu_design'];
  for (const file of files) {
    const normalized = toPosix(file).replace(/\/+$/, '').toLowerCase();
    if (!normalized || normalized.startsWith('!')) continue;
    const segments = normalized.split('/').filter(Boolean);
    const name = normalized.split('/').pop();
    assert(!forbiddenNames.includes(name), `${label} unexpectedly includes ${file}`);
    assert(!name.startsWith('.env.'), `${label} unexpectedly includes ${file}`);
    assert(!name.endsWith('.log'), `${label} unexpectedly includes ${file}`);
    for (const segment of segments) {
      assert(!forbiddenSegments.includes(segment), `${label} unexpectedly includes local runtime path ${file}`);
    }
  }
}

function listPackageTree(startDir) {
  if (!fs.existsSync(startDir)) return [];
  const result = [];
  const stack = [startDir];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const fullPath = path.join(current, entry.name);
      const relative = toPosix(path.relative(startDir, fullPath));
      result.push(entry.isDirectory() ? `${relative}/` : relative);
      if (entry.isDirectory()) stack.push(fullPath);
    }
  }
  return result.sort();
}

function assertIncludesPatterns(patterns, required, label) {
  assert(Array.isArray(patterns), `${label} must be an array.`);
  for (const pattern of required) {
    assert(patterns.includes(pattern), `${label} missing ${pattern}`);
  }
}

function assertNotIncludesPatterns(patterns, forbidden, label) {
  assert(Array.isArray(patterns), `${label} must be an array.`);
  for (const pattern of forbidden) {
    assert(!patterns.includes(pattern), `${label} must not include ${pattern}`);
  }
}

function assertRepoRelativePath(value, label) {
  const normalized = toPosix(value);
  assert(normalized, `${label} is empty.`);
  assert(!path.isAbsolute(value), `${label} must be relative to the repository.`);
  assert(!normalized.startsWith('../') && !normalized.includes('/../'), `${label} must stay inside the repository.`);
  assert(!/^~(?:\/|$)/.test(normalized), `${label} must not reference a user home directory.`);
  assert(!/%(?:USERPROFILE|APPDATA|LOCALAPPDATA|HOME)%/i.test(value), `${label} must not reference a user profile environment variable.`);
  assert(!/\$\{?(?:HOME|USERPROFILE|APPDATA|LOCALAPPDATA|CLAUDE_CONFIG_DIR|CODEX_HOME)\}?/i.test(value), `${label} must not reference a user profile environment variable.`);
}

function assertExistingFile(file, label, minBytes = 1) {
  assert(fs.existsSync(file), `${label} is missing.`);
  const stat = fs.statSync(file);
  assert(stat.isFile(), `${label} must be a file.`);
  assert(stat.size >= minBytes, `${label} is unexpectedly small (${stat.size} bytes).`);
}

function assertExistingDirectory(file, label) {
  assert(fs.existsSync(file), `${label} is missing.`);
  assert(fs.statSync(file).isDirectory(), `${label} must be a directory.`);
}

function isPathInside(parent, child) {
  const relative = path.relative(parent, child);
  return Boolean(relative) && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function parseMarketplace(file) {
  try {
    const marketplace = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert(Array.isArray(marketplace.plugins), `${file} must contain a plugins array.`);
    return marketplace;
  } catch (error) {
    throw new Error(`${file} is not a readable marketplace JSON file: ${error.message}`);
  }
}

function firstPartyTextFiles() {
  const roots = ['electron', 'src', 'dist', 'scripts'];
  const result = ['package.json', 'index.html', 'vite.config.js'].filter((file) => fs.existsSync(rel(file)));
  for (const root of roots) {
    const start = rel(root);
    if (!fs.existsSync(start)) continue;
    const stack = [start];
    while (stack.length) {
      const current = stack.pop();
      for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
        const fullPath = path.join(current, entry.name);
        if (entry.isDirectory()) {
          stack.push(fullPath);
          continue;
        }
        if (!entry.isFile()) continue;
        if (!/\.(cjs|mjs|js|jsx|json|html|css|txt|yml|yaml)$/i.test(entry.name)) continue;
        result.push(path.relative(rootDir, fullPath));
      }
    }
  }
  return result;
}

function scanTextForSecretsAndHardcodedModelUrls(items, readItem, label) {
  const providerUrls = [
    'api.anthropic.com',
    'api.openai.com',
    'openrouter.ai/api',
    'dashscope.aliyuncs.com',
    'api.deepseek.com',
    'generativelanguage.googleapis.com',
    'api.siliconflow.cn'
  ];
  const secretPatterns = [
    /(?:^|[^A-Za-z0-9_-])(sk-ant-[A-Za-z0-9_-]{20,})/g,
    /(?:^|[^A-Za-z0-9_-])(sk-proj-[A-Za-z0-9_-]{20,})/g,
    /(?:^|[^A-Za-z0-9_-])(sk-[A-Za-z0-9_-]{24,})/g,
    /(?:^|[^A-Za-z0-9_-])(ghp_[A-Za-z0-9_]{20,})/g,
    /Bearer\s+[A-Za-z0-9._-]{20,}/g
  ];
  const envLiteralPatterns = [
    /\bANTHROPIC_BASE_URL\s*[:=]\s*['"]https?:\/\//,
    /\bOPENAI_BASE_URL\s*[:=]\s*['"]https?:\/\//,
    /\b[A-Z0-9_]*(?:API_KEY|AUTH_TOKEN|SECRET)\b\s*[:=]\s*['"][^'"]{8,}['"]/
  ];

  for (const item of items) {
    const text = readItem(item);
    if (typeof text !== 'string') continue;
    const lower = text.toLowerCase();
    const isVerificationScript = toPosix(item) === 'scripts/verify-production.cjs';
    if (!isVerificationScript) {
      for (const url of providerUrls) {
        assert(!lower.includes(url), `${label} ${item} hardcodes provider base URL ${url}`);
      }
    }
    for (const pattern of secretPatterns) {
      pattern.lastIndex = 0;
      const match = pattern.exec(text);
      assert(!match, `${label} ${item} appears to contain a real secret-like token.`);
    }
    if (!isVerificationScript) {
      for (const pattern of envLiteralPatterns) {
        assert(!pattern.test(text), `${label} ${item} appears to hardcode a model secret/base URL.`);
      }
    }
  }
}

function findAsarFiles() {
  const releaseDir = rel('release');
  if (!fs.existsSync(releaseDir)) return [];
  const result = [];
  const stack = [releaseDir];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(fullPath);
      } else if (entry.isFile() && entry.name === 'app.asar') {
        result.push(fullPath);
      }
    }
  }
  return result;
}

check('main process syntax', () => {
  nodeCheck('electron/main.cjs');
});

check('preload syntax', () => {
  nodeCheck('electron/preload.cjs');
});

check('model adapter syntax', () => {
  nodeCheck('electron/model-adapter.cjs');
  nodeCheck('scripts/test-model-adapter.cjs');
});

check('agent runtime smoke syntax', () => {
  nodeCheck('scripts/agent-runtime-smoke.cjs');
  nodeCheck('scripts/packaged-runtime-smoke.cjs');
  nodeCheck('scripts/lint-smoke.cjs');
  nodeCheck('scripts/prepare-vue-office.cjs');
  nodeCheck('scripts/verify-vue-office-vendor.cjs');
});

check('preload exposes model profile IPC', () => {
  const preload = readText('electron/preload.cjs');
  assert(preload.includes("contextBridge.exposeInMainWorld('ecorex'"), 'ecorex bridge is not exposed.');
  const expected = {
    listModelProfiles: 'listModelProfiles',
    saveModelProfile: 'saveModelProfile',
    deleteModelProfile: 'deleteModelProfile',
    activateModelProfile: 'activateModelProfile',
    testModelProfile: 'testModelProfile',
    testModelAdapterProfile: 'modelAdapter:testProfile',
    generateModelImage: 'modelAdapter:generateImage'
  };
  for (const [method, channel] of Object.entries(expected)) {
    assert(new RegExp(`\\b${method}\\s*:`).test(preload), `preload method ${method} is missing.`);
    assert(preload.includes(`ipcRenderer.invoke('${channel}'`), `preload method ${method} does not invoke ${channel}.`);
  }
  assert(/listModelProfiles:\s*\(payload\)\s*=>\s*ipcRenderer\.invoke\('listModelProfiles',\s*withAuth\(payload\)\)/.test(preload), 'listModelProfiles must pass auth payload.');
  assert(/saveModelProfile:\s*\(payload\)\s*=>\s*ipcRenderer\.invoke\('saveModelProfile',\s*withAuth\(payload\)\)/.test(preload), 'saveModelProfile must pass auth payload.');
});

check('renderer exposes model configuration entry and image IPC', () => {
  const app = readText('src/App.jsx');
  const preload = readText('electron/preload.cjs');
  const main = readText('electron/main.cjs');
  includesAll(
    app,
    [
      'modelConfigOpen',
      'setModelConfigOpen(true)',
      '<ModelConfigModal',
      'loadModelProfiles',
      'saveModelProfile',
      'activateModelProfile',
      'testModelProfile',
      'testModelAdapterProfile',
      "callEcorex(['testModelAdapterProfile']",
      'generateModelImage'
    ],
    'renderer model configuration entry'
  );
  assert(
    app.includes('generateModelImage') || preload.includes('generateModelImage'),
    'renderer/preload generateModelImage IPC surface is missing.'
  );
  assert(preload.includes("generateModelImage: (payload) => ipcRenderer.invoke('modelAdapter:generateImage', withAuth(payload))"), 'generateModelImage must invoke authenticated modelAdapter:generateImage.');
  assert(main.includes("handleSafe('modelAdapter:generateImage'") && main.includes('generateModelProfileImage(payload)'), 'main process must register generate image handler.');
});

check('main process registers model profile handlers', () => {
  const main = readText('electron/main.cjs');
  const channels = [
    'listModelProfiles',
    'saveModelProfile',
    'deleteModelProfile',
    'activateModelProfile',
    'testModelProfile',
    'modelAdapter:testProfile',
    'modelAdapter:generateImage'
  ];
  for (const channel of channels) {
    const pattern = new RegExp(`handleSafe\\('${channel}'[\\s\\S]*?authRequired:\\s*true`);
    assert(pattern.test(main), `${channel} handler must be authRequired.`);
  }
});

check('local desktop auth is first-run bound and encrypted', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      "const AUTH_IDENTITY_FILE_NAME = 'auth-identity.json'",
      "const AUTH_USERS_FILE_NAME = 'auth-users.json'",
      "const PROVISIONING_FILE_NAME = 'ecorex-provisioning.json'",
      'const LOCAL_AUTH_HASH_ITERATIONS =',
      'function authIdentityPath',
      'function authUsersPath',
      'function readProvisioningFile',
      'function readAuthIdentity',
      'function readAuthUsers',
      'function writeAuthUsers',
      'function provisionAuthUsersIfNeeded',
      'function writeAuthIdentity',
      'function createAuthIdentity',
      'function createUserRecord',
      'function listAuthUsers',
      'function verifyLocalPassword',
      'crypto.pbkdf2Sync',
      'crypto.timingSafeEqual',
      'setupRequired',
      'authMode: \'local-owner\''
    ],
    'local auth binding'
  );
  assertMatches(main, /if \(!fs\.existsSync\(file\)\) \{[\s\S]*?const provisionedUsers = provisionAuthUsersIfNeeded\(\);[\s\S]*?if \(provisionedUsers\.length\) return provisionedUsers;/, 'managed installer auth provisioning must run before first-login owner binding.');
  assertMatches(main, /if \(!users\.length\) \{[\s\S]*?role:\s*'super_admin'[\s\S]*?users = writeAuthUsers\(\[user\]\);[\s\S]*?createdIdentity = true;/, 'first login must bind a local super administrator.');
  assertMatches(main, /else if \(!user \|\| user\.active === false \|\| !verifyLocalPassword\(user, password\)\)/, 'subsequent login must verify bound user, active state, and password.');
  assertMatches(main, /if \(loginType === 'code' \|\| payload\.code\)/, 'local mode must not accept arbitrary verification codes.');
  assertMatches(main, /encoding:\s*'safeStorage\/v1'[\s\S]*encryptLocalPayload\(safeIdentity\)/, 'auth identity must be encrypted with safeStorage.');
  assertMatches(main, /function writeAuthUsers[\s\S]*encoding:\s*'safeStorage\/v1'[\s\S]*encryptLocalPayload\(envelope\)/, 'auth users must be encrypted with safeStorage.');
  assertMatches(main, /handleSafe\('auth:user:create'[\s\S]*requiredPermission:\s*'users:manage'/, 'user creation must require user management permission.');
  assertMatches(main, /handleSafe\('enterprise:action'[\s\S]*requiredPermission:\s*'enterprise:manage'/, 'enterprise actions must require enterprise management permission.');
  assertMatches(main, /super_admin:[\s\S]*'xin:query'[\s\S]*admin:[\s\S]*'xin:query'[\s\S]*user:\s*\['profile:update',\s*'agent:operate'\]/, 'Xin Assistant CLI access must be limited to super administrators and administrators.');
  assertMatches(main, /handleSafe\('xin-agent:natural-query'[\s\S]*requiredPermission:\s*'xin:query'/, 'Xin Assistant natural-language CLI queries must require xin:query permission.');
  includesAll(main, ["'project detail'", "'task list'", "'user list'", "'sync state'", "'sync changes'"], 'Xin Assistant extended command whitelist');
  includesAll(main, ['function xinAgentKeywordReportQueryFromText', "workflow: 'account-keyword-report'", 'runXinAgentKeywordReportWorkflow', 'Xin Agent keyword report workflow completed'], 'Xin Assistant keyword spend workflow');
  assertMatches(main, /function runXinAgentQuery[\s\S]*readCachedXinAgentQuery[\s\S]*joined in-flight[\s\S]*writeCachedXinAgentQuery/, 'Xin Assistant account-list queries must use cache and in-flight de-duplication.');
  assertMatches(main, /function runXinAgentQueryUncached[\s\S]*const startedAt = Date\.now\(\)[\s\S]*durationMs: Date\.now\(\) - startedAt/, 'Xin Assistant direct CLI calls must log duration without raw JSON payloads.');
  assertMatches(main, /function xinAgentLooksLikeMetaQuestion[\s\S]*耗时[\s\S]*不返回[\s\S]*日志/, 'Xin Assistant meta/debug questions must not trigger data queries.');
});

check('agent session bindings are persisted and project isolated', () => {
  const main = readText('electron/main.cjs');
  const pkg = JSON.parse(readText('package.json'));
  includesAll(
    main,
    [
      "const CLAUDE_SESSION_BINDINGS_FILE_NAME = 'session-bindings.json'",
      'function claudeSessionBindingsPath',
      'function ensureClaudeSessionBindingCacheLoaded',
      'function persistClaudeSessionBindings',
      'function claudeSessionBindingConflict',
      'String(left.conversationId || \'\') === String(right.conversationId || \'\')',
      'session-context-conflict'
    ],
    'persistent agent session binding'
  );
  assertMatches(main, /rememberClaudeSessionBinding[\s\S]*persistClaudeSessionBindings\(\)/, 'session bindings must be persisted after a runtime starts.');
  assertMatches(main, /claimAgentStart[\s\S]*claudeSessionBindingConflict\(payload\)[\s\S]*session-context-conflict/, 'agent start must reject cross-context session reuse.');
  assertIncludesPatterns(pkg.build?.files || [], ['!**/session-bindings.json'], 'build.files');
});

check('renderer routes agent events by owning conversation', () => {
  const app = readText('src/App.jsx');
  includesAll(
    app,
    [
	      'function sessionOwnerForSession',
	      'function hasRunningSessionForConversation',
	      'function runningSessionIdForConversation',
	      'function interruptRunningSessionForConversation',
	      'function syncCurrentSessionForVisibleConversation',
	      'storedEventsByConversation',
	      'loadConversationState(ownerConversationId)',
	      'saveConversationState(ownerConversationId',
	      'hasRunningSessionForConversation(activeConversationId)',
	      'queuedConversationId',
	      'activeConversationRunning'
    ],
    'owner-aware renderer session routing'
  );
  assertMatches(app, /updateTimelineForConversation\(activeConversationId[\s\S]*appendTimelineItems\(items, initialDisclosureTimeline\)/, 'new run disclosure timeline must target the owning conversation.');
  assertMatches(app, /updateMessagesForConversation\(activeConversationId[\s\S]*item\.id === assistantId[\s\S]*streaming:\s*false/, 'run failure updates must target the owning conversation.');
  assertMatches(app, /running=\{activeConversationRunning\}/, 'composer stop controls must be scoped to the visible conversation.');
  assertMatches(app, /syncCurrentSessionForVisibleConversation\(conversationIdRef\.current\)/, 'switching conversations must not keep a hidden session as the current stop target.');
  assertMatches(app, /function clearMessageRecoveryState[\s\S]*recovery:\s*null/, 'retrying a recoverable task must hide the stale recovery prompt.');
  assertMatches(app, /function touchRecentChatFromConversationState[\s\S]*storeRecentChatItems\(upsertRecentChatItem[\s\S]*ecorex:recent-chats-changed/, 'background conversation persistence must keep the recent-chat index recoverable.');
  includesAll(app, ['DELETED_CONVERSATION_STORAGE_KEY', 'rememberDeletedConversationId', 'isDeletedConversationId(safeId)', 'deleteStoredRecentChatItem(id)'], 'deleted conversations must be tombstoned so background persistence cannot resurrect them.');
  assertMatches(app, /function updateMessagesForConversation[\s\S]*saveConversationState\(ownerConversationId,\s*nextState\)[\s\S]*touchRecentChatFromConversationState\(ownerConversationId,\s*nextState\)/, 'background message updates must be saved and surfaced in recent chats.');
  assertMatches(app, /if \(!ownerMessages\.some[\s\S]*isAgentSessionFinalEvent\(event\)[\s\S]*appendRecoveredSessionMessage\(ownerConversationId/, 'late terminal events must recreate a stored assistant message instead of dropping hidden-session output.');
  assertMatches(app, /if \(event\.kind === 'done'\) \{[\s\S]*pendingStructuredAction[\s\S]*statusFromAgentUserAction\(pendingStructuredAction\)[\s\S]*continue;/, 'terminal done events must not overwrite a pending permission or authorization card.');
  assertMatches(app, /function RecoveryStateNotice[\s\S]*\['recoverable', 'retryable', 'stopped'\]\.includes\(recovery\.state\)[\s\S]*return null/, 'restored or timed-out background sessions must not require a manual recovery click.');
});

check('auth token exposure and session lifecycle are bounded', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  includesAll(
    main,
    [
      'const AUTH_SESSION_TTL_MS =',
      'const AUTH_SESSION_REFRESH_THRESHOLD_MS =',
      'function refreshAuthSessionIfNeeded',
      'expiresAt: session.expiresAt',
      "writeLog('info', 'Auth session expired'",
      'readAuthSession({ refresh: true })'
    ],
    'auth session lifecycle'
  );
  assertMatches(main, /handleSafe\('auth:login'[\s\S]*?loginAuth\(payload\)\)/, 'auth login handler must remain the token issuing path.');
  assertMatches(main, /handleSafe\('auth:status'[\s\S]*?includeToken:\s*Boolean\(payload\?\.includeToken\)/, 'auth status must only include token when explicitly requested.');
  assertNotMatches(main, /handleSafe\('auth:status'[\s\S]{0,200}includeToken:\s*true/, 'auth status must not always expose the session token.');
  assertMatches(preload, /getAuthStatus:\s*\(\)\s*=>\s*safeInvoke\('auth:status',\s*\{\s*includeToken:\s*!authToken,\s*refresh:\s*true\s*\}\)\.then\(rememberAuth\)/, 'preload must request the token only while its isolated auth cache is empty.');
  assertMatches(preload, /const \{ token, authToken: _authToken, sessionToken: _sessionToken,[\s\S]*?\.\.\.safeResult \} = result;/, 'preload must strip auth tokens from renderer-visible auth results.');
});

check('high privilege IPC requires trusted renderer and auth', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  for (const channel of ['startup:health', 'diagnostics:get', 'diagnostics:export', 'diagnostics:open-location', 'diagnostics:crash-recovery', 'workspace:select-directory', 'shell:open-external', 'backend:open-auth', 'agent:run', 'agent:stop']) {
    const pattern = new RegExp(`handleSafe\\('${channel}'[\\s\\S]*?authRequired:\\s*true`);
    assert(pattern.test(main), `${channel} handler must be authRequired.`);
  }
  includesAll(
    main,
    [
      'function isTrustedSender(event)',
      'event?.sender !== mainWindow.webContents',
      'event.senderFrame !== event.sender.mainFrame',
      'return isAllowedRendererUrl(senderUrl)',
      "const WINDOW_CONTROL_ACTIONS = new Set(['minimize', 'maximize', 'close'])",
      "if (!assertTrustedIpc(event, 'window:control')) return",
      'if (!WINDOW_CONTROL_ACTIONS.has(payload.action))'
    ],
    'IPC trust and window control safety'
  );
  assertMatches(preload, /windowControl:\s*\(action\)\s*=>\s*ipcRenderer\.send\('window:control',\s*withAuth\(\{\s*action\s*\}\)\)/, 'window control should keep the isolated auth token when available.');
  includesAll(main, ['function normalizeExternalUrl', "Only http, https and mailto links can be opened.", "shell.openExternal(url, { activate: true })"], 'external link opening safety');
  assertMatches(preload, /openExternalUrl:\s*\(payload\)\s*=>\s*safeInvoke\('shell:open-external',\s*withAuth/, 'preload must expose authenticated external-link opening IPC.');
});

check('crash recovery and diagnostics package are production-safe', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  includesAll(
    main,
    [
      "safeStorage, dialog, screen, shell",
      "const CRASH_SUMMARY_FILE_NAME = 'crash-summary.json'",
      "const DIAGNOSTIC_EXPORT_DIR_NAME = 'EcoreX Diagnostics'",
      'const MAX_RENDERER_RECOVERY_ATTEMPTS =',
      'function crashSummaryPath',
      'function recordCrashEvent',
      'function readCrashSummary',
      'function recoverRendererAfterCrash',
      'function claimRendererRecoverySlot',
      'function revealDiagnosticsPackage',
      'function openDiagnosticsLocation',
      "recordCrashEvent('renderer-gone'",
      "recordCrashEvent('renderer-unresponsive'",
      "recordCrashEvent('renderer-responsive'",
      "recordCrashEvent('renderer-recovery-reload'",
      "recordCrashEvent('renderer-recovery-suppressed'",
      "recordCrashEvent('main-uncaught-exception'",
      "recordCrashEvent('main-unhandled-rejection'",
      "recoverRendererAfterCrash(details)",
      'crashes: readCrashSummary(10)',
      'function buildDiagnosticsPackage',
      'function exportDiagnosticsPackage',
      'function safeDiagnosticValue',
      'function redactLocalPaths',
      'function safeLogSummary',
      'function safeSessionSummaryForDiagnostics',
      'function releaseArtifactSummary',
      "schema: 'ecorex.diagnostics.v1'",
      'includesApiKeys: false',
      'includesPromptFullText: false',
      'includesLocalPathBodies: false',
      "const exportDir = diagnosticsExportDir()",
      "fs.writeFileSync(savedPath, json, 'utf8')",
      "shell.showItemInFolder(target)",
      'openedLocation: Boolean(revealResult.opened)',
      "handleSafe('diagnostics:export', exportDiagnosticsPackage, { authRequired: true })",
      "handleSafe('diagnostics:open-location', openDiagnosticsLocation, { authRequired: true })"
    ],
    'crash recovery and diagnostics export'
  );
  assertMatches(preload, /exportDiagnosticsPackage:\s*\(payload\)\s*=>\s*safeInvoke\('diagnostics:export',\s*withAuth\(payload\)\)/, 'preload must expose diagnostics export through authenticated IPC.');
  assertMatches(preload, /openDiagnosticsLocation:\s*\(payload\)\s*=>\s*safeInvoke\('diagnostics:open-location',\s*withAuth\(payload\)\)/, 'preload must expose diagnostics open-location through authenticated IPC.');
  assertMatches(preload, /getCrashRecoveryStatus:\s*\(payload\)\s*=>\s*safeInvoke\('diagnostics:crash-recovery',\s*withAuth\(payload\)\)/, 'preload must expose crash recovery through authenticated IPC.');
  assertNotMatches(preload, /exportDiagnosticsPackage[\s\S]{0,200}token\s*:/, 'diagnostics export preload surface must not expose a token field.');
  assertMatches(main, /safeSessionSummaryForDiagnostics[\s\S]*promptFingerprint[\s\S]*slice\(0,\s*12\)/, 'diagnostics sessions must use a short prompt fingerprint, not prompt text.');
  assertNotMatches(main, /buildDiagnosticsPackage[\s\S]{0,2500}promptPreview/, 'diagnostics package must not include prompt previews.');
  assertMatches(main, /PRIVATE KEY[\s\S]*REDACTED_PRIVATE_KEY/, 'diagnostics redaction must scrub private-key blocks.');
  assertMatches(main, /Bearer\\s\+\[A-Za-z0-9._~\+\/=-\]\{16,\}[\s\S]*Bearer \[REDACTED\]/, 'diagnostics redaction must scrub bearer tokens.');
});

check('safe local file preview bridge is bounded and redacted', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  includesAll(
    main,
    [
      'const FILE_PREVIEW_MAX_BYTES = 512 * 1024',
      'const FILE_PREVIEW_IMAGE_MAX_BYTES = 768 * 1024',
      'const FILE_PREVIEW_TEXT_EXTENSIONS = new Set',
      'const FILE_PREVIEW_IMAGE_EXTENSIONS = new Set',
      'const FILE_PREVIEW_DOCUMENT_EXTENSIONS = new Set',
      'const AGENT_ARTIFACT_ACCESS_TTL_MS = 24 * 60 * 60 * 1000',
      'function resolveFilePreviewTarget',
      'function extractPreviewArtifactTargets',
      'function registerAgentArtifactsFromEvent',
      'function isRegisteredAgentArtifact',
      'function previewImageFile',
      'function previewMetadataOnly',
      'function previewFile',
      'function openSelectedAttachmentFile',
      'redactSensitiveText(rawText)',
      "reason: 'too-large'",
      "reason: 'unsupported-type'",
      "reason: 'binary'",
      "renderMode: filePreviewRenderMode(file.extension)",
      "renderMode: 'image'",
      "renderMode: 'metadata'",
      'dataUrl',
      "handleSafe('file:preview', (_event, payload) => previewFile(payload), { authRequired: true })",
      "handleSafe('attachment:open-file', (_event, payload) => openSelectedAttachmentFile(payload), { authRequired: true })"
    ],
    'safe local file preview bridge'
  );
  assertMatches(
    preload,
    /previewFile:\s*\(payload\)\s*=>\s*safeInvoke\('file:preview',\s*withAuth\(typeof payload === 'string' \? \{ path: payload \} : payload\)\)/,
    'preload must expose authenticated file preview IPC.'
  );
  assertMatches(main, /candidatePreviewPath[\s\S]*fileURLToPath\(raw\)[\s\S]*File preview only supports local files/, 'file preview must normalize local paths and reject remote URLs.');
  assertMatches(main, /resolveFilePreviewTarget[\s\S]*isPathInside\(entry\.root,\s*target\)[\s\S]*pathContainsSymlink\(root\.root,\s*target\)/, 'file preview paths must stay inside allowed roots and reject symlink traversal.');
  assertMatches(main, /previewFile[\s\S]*stat\.size > FILE_PREVIEW_MAX_BYTES[\s\S]*previewable:\s*false[\s\S]*reason:\s*'too-large'/, 'oversized files must return metadata with a non-previewable reason.');
  assertMatches(main, /previewFile[\s\S]*looksLikeBinaryBuffer\(buffer\)[\s\S]*reason:\s*'binary'/, 'binary files must return metadata with a non-previewable reason.');
  assertMatches(main, /previewFile[\s\S]*const content = redactSensitiveText\(rawText\)[\s\S]*text:\s*content/, 'preview text must be redacted before crossing IPC.');
  assertMatches(main, /previewImageFile[\s\S]*stat\.size > FILE_PREVIEW_IMAGE_MAX_BYTES[\s\S]*reason:\s*'too-large'[\s\S]*dataUrl/, 'small image preview must be bounded and returned as dataUrl.');
  assertMatches(main, /VUE_OFFICE_DOCUMENT_EXTENSIONS\.has\(file\.extension\)[\s\S]*previewWithVueOffice\(target,\s*file,\s*stat\)[\s\S]*previewOpenXmlOfficeFile/, 'supported PDF and Office files must try vue-office before metadata or text fallbacks.');
  assertMatches(main, /isDocumentMetadataPreviewExtension\(file\.extension\)[\s\S]*previewMetadataOnly\(file,\s*'metadata-only'\)/, 'unsupported document files must return metadata-only previews.');
  assertMatches(main, /resolveFilePreviewTarget[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)/, 'file preview must also support explicit selected-file grants.');
  assertMatches(main, /extractPreviewArtifactTargets[\s\S]*cleanPreviewArtifactPath[\s\S]*\[\^\\\\r\\\\n<>/, 'AI artifact path extraction must tolerate generated paths with spaces while staying line bounded.');
  assertMatches(main, /function agentArtifactsFromEvidenceText[\s\S]*extractPreviewArtifactTargets\(text,\s*workspaceRoot\)[\s\S]*safeAgentArtifact/, 'AI artifact events must expose structured artifact references.');
  assertMatches(main, /const rootPriority = \{ 'agent-artifact': 0,\s*session: 1,\s*project: 2,\s*workspace: 3 \}/, 'relative AI artifact preview must prefer the session cwd before workspace fallbacks.');
  assertMatches(main, /extractPreviewArtifactTargets[\s\S]*const bareFilePattern[\s\S]*addTarget\(token\)/, 'AI artifact path extraction must include final bare filenames relative to the run roots.');
  assertMatches(main, /function eventLooksLikeArtifactWrite[\s\S]*\['result', 'assistant'\][\s\S]*hasPreviewTarget/, 'final assistant/result messages that list generated files must authorize artifact preview.');
  assertMatches(main, /runtimeCwd:\s*entry\.cwd \|\| undefined/, 'session transcripts must retain the private runtime cwd for persisted relative artifact authorization.');
  assertMatches(main, /function candidatePreviewPaths[\s\S]*preferredRoots[\s\S]*\['session', 'project', 'workspace', 'agent-artifact'\]/, 'relative preview requests must resolve against session, project, and workspace roots before failing.');
  assertMatches(main, /resolveFilePreviewTarget[\s\S]*isRegisteredAgentArtifact\((?:target|candidate),\s*input,\s*workspaceRoot\)[\s\S]*kind:\s*'agent-artifact'/, 'same-session AI artifacts must be previewable without opening arbitrary local files.');
  assertMatches(main, /filePreviewPathLabel[\s\S]*kind === 'agent-artifact'[\s\S]*artifact:\//, 'AI artifact preview labels must not expose full local paths.');
  assertMatches(main, /recordSessionEvent[\s\S]*registerAgentArtifactsFromEvent\(entry,\s*normalized\)/, 'tool events must register generated artifact grants for the current session.');
  assertMatches(main, /if \(!requestedId \|\| !entry\.id \|\| requestedId !== entry\.id\) return false;/, 'selected attachment grants must require the generated attachment id.');
  assertMatches(main, /openSelectedAttachmentFile[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)[\s\S]*openPathSafely\(target\)/, 'uploaded attachments may only be opened locally after a selected-file grant.');
  assertMatches(preload, /openAttachmentFile:\s*\(payload\)\s*=>\s*safeInvoke\('attachment:open-file',\s*withAuth/, 'preload must expose authenticated attachment open IPC.');
  assertNotMatches(main, /function previewFile[\s\S]{0,2400}(shell\.openPath|BrowserWindow|loadURL|executeJavaScript|spawn\()/, 'file preview must not open windows, execute, open, or spawn local artifacts.');
  assertNotMatches(main, /function previewImageFile[\s\S]{0,2500}(shell\.openPath|BrowserWindow|loadURL|executeJavaScript|spawn\()/, 'image preview must stay inside safe read-only IPC.');
});

check('vue-office static preview engine is local and bounded', () => {
  const main = readText('electron/main.cjs');
  const app = readText('src/App.jsx');
  const pkg = packageJson();
  includesAll(
    main,
    [
      "const VUE_OFFICE_VENDOR_DIR_NAME = 'vue-office'",
      'const VUE_OFFICE_PREVIEW_MAX_BYTES = 100 * 1024 * 1024',
      'function locateVueOfficeResource',
      'function ensureVueOfficePreviewServer',
      'function handleVueOfficePreviewRequest',
      'function serveVueOfficePreviewFile',
      'function previewWithVueOffice',
      "renderMode: 'vue-office'",
      "listen(0, '127.0.0.1'",
      "'Cache-Control': 'no-store'",
      "stopVueOfficePreviewServer('app-quit')"
    ],
    'vue-office static preview engine'
  );
  assertMatches(main, /createVueOfficeSourceUrl[\s\S]*crypto\.randomBytes\(24\)[\s\S]*expiresAt:\s*Date\.now\(\) \+ VUE_OFFICE_GRANT_TTL_MS/, 'vue-office source URLs must use expiring random grants.');
  assertMatches(main, /serveVueOfficePreviewFile[\s\S]*preview-file\\\/\(\[a-f0-9\]\{32,64\}\)[\s\S]*'Cache-Control': 'no-store'/, 'vue-office file bridge must only serve tokenized preview-file requests with no-store caching.');
  assertMatches(main, /function vueOfficeResourceRoots\(\)[\s\S]*if \(app\.isPackaged\)[\s\S]*process\.resourcesPath[\s\S]*\} else \{[\s\S]*process\.env\.ECOREX_VUE_OFFICE_HOME/, 'packaged vue-office must use bundled resources while dev may override the static root.');
  assertNotMatches(main, /ensureVueOfficePreviewServer[\s\S]{0,2600}\bspawn\(/, 'vue-office preview must not spawn a sidecar process.');
  assertMatches(main, /previewFile[\s\S]*previewWithVueOffice\(target,\s*file,\s*stat\)[\s\S]*previewOpenXmlOfficeFile/, 'Office preview must try vue-office before falling back to OpenXML text extraction.');
  assertMatches(main, /Content-Security-Policy[\s\S]*img-src 'self' data: https: http:\/\/127\.0\.0\.1:\*[\s\S]*media-src 'self' data: blob: https: http:\/\/127\.0\.0\.1:\*[\s\S]*frame-src 'self' http:\/\/127\.0\.0\.1:\*/, 'renderer CSP must allow local preview frames and bounded rich chat media.');
  includesAll(
    app,
    [
      "preview.renderMode === 'vue-office'",
      'artifact-vue-office-frame',
      'sandbox="allow-scripts allow-same-origin allow-forms"',
      "payload.type !== 'ecorex-preview-selection'"
    ],
    'vue-office renderer preview branch'
  );
  const extraResources = Array.isArray(pkg.build?.extraResources) ? pkg.build.extraResources : [];
  const vueOfficeResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'vue-office');
  assert(vueOfficeResource, 'vue-office extraResources entry must be configured.');
  assert(vueOfficeResource.from === 'vendor/vue-office', 'vue-office extraResources source must be vendor/vue-office.');
  assertIncludesPatterns(vueOfficeResource.filter, ['index.html', 'manifest.json', 'js-preview-lib/**/*'], 'vue-office extraResources.filter');
  assertExistingDirectory(rel('vendor/vue-office'), 'vue-office vendor directory');
  assertExistingFile(rel('vendor/vue-office/index.html'), 'vue-office viewer');
  assertExistingFile(rel('vendor/vue-office/js-preview-lib/pdf.umd.js'), 'vue-office PDF runtime');
  assertExistingFile(rel('vendor/vue-office/js-preview-lib/pptx-preview.umd.js'), 'vue-office PPTX runtime');
});

check('agent attachments, tool ledger and run journal are production-safe', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  includesAll(
    main,
    [
      'const ATTACHMENT_TEXT_MAX_BYTES = 512 * 1024',
      'const ATTACHMENT_IMAGE_MAX_BYTES = 768 * 1024',
      'const MAX_AGENT_ATTACHMENTS = 12',
      'const RUN_JOURNAL_FILE_NAME =',
      'function resolveAttachmentTarget',
      'function ingestAgentAttachments',
      'function composePromptWithAttachmentContext',
      'const attachmentContext = ingestAgentAttachments(payload, {',
      'includePromptText: true',
      'prompt,',
      'userPrompt,',
      'attachmentContext,',
      'function toolLedgerStartEvent',
      'function toolLedgerFinishEvent',
      'function safeToolLedger',
      'function safeToolLedgerValue',
      'const finishLedgers = toolResults.map',
      'function appendRunJournalEntry',
      'function recentUnfinishedRunJournals',
      "handleSafe('attachment:ingest', (_event, payload) => ingestAttachmentsForPreview(payload), { authRequired: true })",
      'unfinishedRuns: recentUnfinishedRunJournals()'
    ],
    'attachment ingestion, ledger and durable run journal'
  );
  assertMatches(main, /resolveAttachmentTarget[\s\S]*isPathInside\(entry\.root,\s*target\)[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)[\s\S]*pathContainsSymlink\(root\.root,\s*target\)/, 'attachment ingestion must stay inside project/workspace or selected-file grants and reject symlink traversal.');
  assertMatches(main, /ingestAttachmentFromPath[\s\S]*fs\.readFileSync\(target\)[\s\S]*textAttachmentContextFromBuffer|ingestAttachmentFromPath[\s\S]*textAttachmentContextFromBuffer\(buffer,\s*metadata\)/, 'attachment ingestion must read bounded text payloads for the agent prompt.');
  assertMatches(main, /imageAttachmentContextFromBuffer[\s\S]*stageImageAttachmentForAgent[\s\S]*visionPathLabel[\s\S]*buildAttachmentPromptBlock[\s\S]*local image file for visual\/OCR inspection/, 'image attachment ingestion must hand a real staged image path to the agent for visual/OCR inspection.');
  assertMatches(main, /function ingestAttachmentsForPreview[\s\S]*ingestAgentAttachments\(input,\s*\{\s*cwd,\s*projectContext,\s*includePromptText:\s*false\s*\}\)/, 'renderer attachment preview ingestion must not return internal promptText or staged vision paths.');
  assertMatches(main, /function finalizeAgentSession[\s\S]*cleanupAgentAttachmentStagingFiles\(entry\.attachmentContext\?\.stagedFiles/, 'agent image attachment staging files must be cleaned after terminal runs.');
  assertMatches(main, /appendRunJournalEntry\(sessionId,\s*entry,\s*'running',\s*\{\s*event:\s*'start'\s*\}\)/, 'run journal must record session start.');
  assertMatches(main, /appendRunJournalEntry\(sessionId,\s*entry,\s*status,[\s\S]*event:\s*'finish'/, 'run journal must record session finish.');
  assertMatches(preload, /ingestAttachments:\s*\(payload\)\s*=>\s*safeInvoke\('attachment:ingest',\s*withAuth\(payload\)\)/, 'preload must expose only authenticated attachment ingestion IPC.');
  assertNotMatches(preload, /readFile|writeFile|openPath|BrowserWindow|shell\./, 'preload must not expose arbitrary filesystem or shell APIs.');
});

check('window default, preload and minimum size guardrails', () => {
  const main = readText('electron/main.cjs');
  includesAll(main, ['function defaultWindowBounds', 'screen.getPrimaryDisplay()', 'width: bounds.width', 'height: bounds.height', 'minWidth: 800', 'minHeight: 600', "backgroundColor: '#070c12'", 'show: true', 'function revealStartupWindow', "revealStartupWindow('created-dark-shell')", "revealStartupWindow('startup-splash-loaded')", 'mainWindow.maximize()', 'function startupBrandIconPath', 'function startupSplashUrlForWindow', 'function startupSplashDataUrl', 'function startStartupPreload', 'function waitForStartupPreload', "startStartupPreload('native-loading')", 'collectBackendStatus(null, { refresh: false, lightweight: true })'], 'desktop window sizing and startup preload guardrails');
  includesAll(main, ['locateClaude()', 'Promise.resolve().then(() => collectCapabilities())', 'publicAuthSession(readAuthSession({ refresh: true }))', 'await waitForStartupPreload(startupPreload)', 'loadRendererEntry()'], 'native splash preload must finish or time out before renderer entry');
  assertMatches(
    main,
    /startupSplashUrl = startupSplashUrlForWindow\(\);[\s\S]*?mainWindow\.loadURL\(startupSplashUrl\)[\s\S]*?revealStartupWindow\('startup-splash-loaded'\);[\s\S]*?const startupPreload = startStartupPreload\('native-loading'\);/,
    'native startup splash must be visible before main-process preload starts.'
  );
  const css = readText('src/styles.css');
  includesAll(css, ['min-width: 800px'], 'renderer minimum width CSS');
  const renderer = readText('src/main.jsx');
  const app = readText('src/App.jsx');
  includesAll(renderer, ['window.__ecorexFinishStartup', 'finishStartupLoader', '15000'], 'renderer startup loader completion fallback');
  includesAll(app, ['startupReadyRef', 'const startupWork = refreshAuthStatus()', '.then(() => Promise.allSettled', 'refreshBackend()', 'preloadStartupState()', 'withStartupTimeout(startupWork)', 'window.__ecorexFinishStartup?.()'], 'renderer startup loader waits for auth/backend preload');
  const authFirstStartupFinally = /const startupWork = refreshAuthStatus\(\)[\s\S]*?\.then\(\(\) => Promise\.allSettled\(\[[\s\S]*?refreshBackend\(\),[\s\S]*?preloadStartupState\(\)[\s\S]*?\]\)\);[\s\S]*?withStartupTimeout\(startupWork\)\.finally\(\(\) => \{[\s\S]*?window\.__ecorexFinishStartup\?\.\(\);/.test(app);
  assert(
    authFirstStartupFinally,
    'renderer startup loader must recover auth before backend/preload and finish after those promises settle.'
  );
  assertMatches(
    main,
    /mainWindow\.loadURL\(startupSplashUrl\)[\s\S]*?await waitForStartupPreload\(startupPreload\);[\s\S]*?loadRendererEntry\(\);/,
    'native startup splash must wait for main-process preload before loading the renderer.'
  );
});

check('packaged renderer trust boundary is fixed to app entry', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      'const { pathToFileURL } = require(\'url\')',
      'function rendererEntryPath()',
      'function rendererEntryUrl()',
      'function isAllowedRendererUrl',
      'return value === rendererEntryUrl()',
      'return isAllowedRendererUrl(senderUrl)',
      'if (!isAllowedRendererUrl(url))',
      'const target = rendererEntryPath()'
    ],
    'packaged renderer trust boundary'
  );
  assertNotMatches(main, /app\.isPackaged\)\s*return\s+senderUrl\.startsWith\('file:\/\/'\)/, 'packaged IPC must not trust arbitrary file:// renderers.');
  assertNotMatches(main, /app\.isPackaged\s*\?\s*url\.startsWith\('file:\/\/'\)/, 'packaged navigation must not allow arbitrary file:// URLs.');
});

check('agent runtime production guardrails', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      'const MAX_RUNNING_AGENTS =',
      "code: 'too-many-sessions'",
      'MAX_RUNNING_AGENTS',
      'child.stdin.end(`${prompt}\\n`)',
      "'--verbose'",
      'const running = getRunningSessionSummaries();',
      'sessions: [...running, ...recoverable]',
      'unfinishedRuns: recentUnfinishedRunJournals()',
      "if (status === 'timeout') return 'timeout'",
      'ECOREX_AGENT_SYSTEM_PROMPT',
      'function agentRecoveryHint',
      'recoveryHint: agentRecoveryHint',
      'function defaultCommandCwd',
      'process.resourcesPath',
      'function isPackagedClaudeWrapperStub',
      'function stableClaudeSessionUuid',
      'sanitizeClaudeSessionId',
      'function claudeSessionTranscriptExists',
      'function refreshClaudeSessionTranscriptSeen',
      'const ECOREX_AGENT_CONFIG_DIR_NAME',
      'const BLOCKED_LOCAL_SKILL_NAMES',
      'function isolatedAgentRuntimeEnv',
      'function isBlockedLocalSkillName',
      "'--session-id'",
      "'--resume'",
      'claudeSessionId',
      'contextManagement',
      'const ECOREX_BUILTIN_PLUGIN_ALLOWLIST',
      'const ECOREX_GENERAL_WORKSPACE_DIR_NAME',
      'const BUNDLED_MANAGED_SKILL_PACKS_DIR_NAME',
      'const BUNDLED_MANAGED_TOOLS_DIR_NAME',
      'const LARK_CLI_SKILL_PACK_NAME',
      'const OFFICE_CLI_SKILL_PACK_NAME',
      'const OPENCLI_SKILL_PACK_NAME',
      'const RETIRED_MANAGED_SKILL_PACK_NAMES',
      'ECOREX_GENERAL_CHAT_ISOLATION_PROMPT',
      'function generalAgentWorkspaceDir',
      'function seedBundledManagedSkillPacks',
      'function packagedNodeModulesDir',
      'function nodeToolPathEntries',
      'function managedToolPathEntries',
      'function larkCliConfigDir',
      'function allowedRuntimePluginNames',
      "'--tools'",
      'nativeToolMode'
    ],
    'agent runtime guardrails'
  );
  assertMatches(main, /function publicSessionSummary[\s\S]*autoRecover:\s*true[\s\S]*recoveryMode:\s*'reattach'/, 'live running sessions must advertise automatic reattach without requiring a manual recovery click.');
  assertMatches(main, /'--output-format',\s*'stream-json',\s*'--verbose'/, 'stream-json output must always be paired with --verbose.');
  includesAll(
    main,
    [
      'const AGENT_STRUCTURED_EVENT_PROTOCOL',
      'const AGENT_ASSISTANT_DELTA_FLUSH_MS',
      'const MAX_AGENT_ASSISTANT_BUFFER_CHARS',
      'function runtimeAuthorizationRequestFromValue',
      'function runtimePermissionRequestFromValue',
      'function structuredUserActionFromPayload',
      'function safeAgentRecovery'
    ],
    'structured runtime event protocol'
  );
  assertMatches(main, /function normalizeAgentEvent[\s\S]*protocol:\s*payload\.protocol \|\| AGENT_STRUCTURED_EVENT_PROTOCOL[\s\S]*const userAction = structuredUserActionFromPayload\(payload\)/, 'agent events must carry the structured protocol and use native userAction payloads.');
  assertMatches(main, /function normalizeAgentEvent[\s\S]*const text = publicAgentText\(payload\.text \|\| '', options\.textLimit \|\| MAX_AGENT_EVENT_TEXT_CHARS\)/, 'normalized agent events must pass through the same public text sanitizer before renderer IPC.');
  assertNotMatches(main, /const userAction = safeAgentUserAction\(payload\.userAction\) \|\| userActionFromAgentText\(text\)/, 'backend must not infer authorization or permission cards from arbitrary public assistant text.');
  assertMatches(main, /function runtimePermissionRequestFromValue[\s\S]*typeof value === 'string'[\s\S]*EcoreX auto mode classifier[\s\S]*ecorex\.user-action\.permission\.v1/, 'raw local permission denials must become structured user-action events instead of chat text.');
  assertMatches(main, /function agentSessionHasUnresolvedAuthorization[\s\S]*authorizationStateFromStructuredEvent\(event\) === 'waiting'[\s\S]*function agentSessionHasUnresolvedUserBlocker/, 'authorization-incomplete final status must be driven by structured waiting events, not prompt text.');
  assertMatches(main, /let assistantOutputBuffer = ''[\s\S]*const flushAssistantOutput = [\s\S]*AGENT_ASSISTANT_DELTA_FLUSH_MS[\s\S]*handleRuntimeEvent\(normalizeClaudeEvent\(sessionId,\s*json,\s*\{ cwd: entry\?\.cwd \|\| cwd \}\)\)/, 'assistant token deltas must be buffered before transcript and renderer IPC.');
  assertMatches(main, /'--print'[\s\S]*'--output-format'[\s\S]*'stream-json'[\s\S]*'--plugin-dir'/, 'agent runtime must stream through the full isolated CLI tool surface while explicitly loading bundled plugins.');
  assertNotMatches(main, /'--bare'/, 'agent runtime must not pass --bare because it can hide MCP, Skill, WebSearch, Todo and PowerShell tools.');
  assertMatches(main, /const runtimeEnv = \{[\s\S]*isolatedAgentRuntimeEnv\(options\.authContext \|\| null\)[\s\S]*CLAUDE_CODE_NO_FLICKER:\s*'1'/, 'agent runtime must isolate config while preserving the full default tool surface.');
  assertMatches(main, /process\.platform === 'win32' \? \{ CLAUDE_CODE_USE_POWERSHELL_TOOL:\s*'1' \}/, 'Windows agent runs must enable the PowerShell tool surface.');
  assertNotMatches(main, /CLAUDE_CODE_SIMPLE:\s*'1'/, 'agent runtime must not force simple mode because it hides WebSearch, Skill, Todo and PowerShell tools.');
  assertMatches(main, /function isolatedAgentRuntimeEnv\(authContext = null\)[\s\S]*HOME:\s*configDir[\s\S]*USERPROFILE:\s*configDir[\s\S]*APPDATA:\s*appDataDir[\s\S]*LOCALAPPDATA:\s*localAppDataDir[\s\S]*LARKSUITE_CLI_CONFIG_DIR:\s*larkConfigDir/, 'agent runtime must isolate home/appdata and Feishu auth config so local skills and MCP state are not inherited.');
  assertMatches(main, /PATH:\s*managedToolPath[\s\S]*Path:\s*managedToolPath/, 'agent runtime PATH must include EcoreX managed tools.');
  assertMatches(main, /if \(nodeModulesDir\) \{[\s\S]*env\.NODE_PATH = nodeModulesDir[\s\S]*env\.ECOREX_NODE_MODULES_DIR = nodeModulesDir/, 'agent runtime must expose packaged Node modules for Playwright and local JS tools.');
  assertMatches(main, /handleSafe\('agent:run',\s*\(event,\s*payload,\s*authContext\) => runAgent\(payload,\s*\{ ownerId:\s*event\.sender\.id,\s*authContext \}\)/, 'agent runs must receive auth context for per-user managed tool auth.');
  assertMatches(main, /function collectManagedSkillInventory[\s\S]*seedBundledManagedSkillPacks\(\)/, 'managed skill inventory must seed bundled EcoreX skill packs.');
  assertMatches(main, /const projectContextDisabled = payload\.disableProjectContext === true \|\| !payload\.projectId/, 'general chat runs must not inherit the last active project.');
  assertNotMatches(main, /projectContextDisabled \? null : activeProjectContext\(\)/, 'agent runs must not fall back to the active project when no project id is provided.');
  assertMatches(main, /const defaultRunRoot = projectContext\?\.projectPath \|\| generalAgentWorkspaceDir\(\)/, 'general chat runs must use the isolated general workspace.');
  assertMatches(main, /function agentSystemPromptForProject[\s\S]*ECOREX_GENERAL_CHAT_ISOLATION_PROMPT/, 'general chat prompt must explicitly forbid project memory/file inspection.');
  includesAll(main, ['待授权或待继续', 'For Feishu/Lark setup', '任务才算完成'], 'agent system prompt must prevent finite tasks from hanging on background commands or missing final conclusions.');
  assertMatches(main, /function claudeProjectsRoot\(\) \{[\s\S]*agentRuntimeConfigDir\(\)[\s\S]*'projects'/, 'Claude transcript discovery must only use the EcoreX runtime project root.');
  assertNotMatches(main, /function claudeProjectsRoot\(\) \{[\s\S]*process\.env\.USERPROFILE[\s\S]*\.claude[\s\S]*projects/, 'Claude transcript discovery must not scan the user global ~/.claude project history.');
  assertNotMatches(main, /function claudeSessionTranscriptExists[\s\S]*claudeSessionWasLaunched/, 'Claude resume must require a real transcript, not only a previous launch marker.');
  assertMatches(main, /isPackagedClaudeWrapperStub\(candidate\)[\s\S]*continue;/, 'Claude locator must skip packaged Windows wrapper stubs and prefer the native runtime binary.');
  assertMatches(main, /function runClaudeCommand[\s\S]*env:\s*\{[\s\S]*isolatedAgentRuntimeEnv\(\)/, 'auxiliary backend CLI commands must also use the EcoreX isolated config.');
  assertMatches(main, /const plugins = Array\.isArray\(payload\.plugins\)[\s\S]*isBlockedLocalSkillName\(plugin\)/, 'payload plugin names must reject local user skill packs.');
  assertMatches(main, /parsePluginInventory[\s\S]*isBlockedLocalSkillName\(pluginName\)[\s\S]*isBlockedLocalSkillName\(source\)/, 'backend plugin inventory must exclude blocked local skill pack names.');
  assertMatches(main, /const selectedPlugins = allowedRuntimePluginNames\(safePayload\.plugins \|\| \[\]\)/, 'runtime plugins must be intersected with the EcoreX builtin and managed allowlist.');
  assertMatches(main, /function runtimePluginPathAllowed[\s\S]*isPathInside\(managedSkillPacksDir\(\),\s*resolved\)[\s\S]*isPathInside\(bundledManagedSkillPacksRoot\(\),\s*resolved\)/, 'managed runtime plugins must be loaded only from EcoreX managed or bundled skill pack roots.');
  assertMatches(main, /installPath:\s*source[\s\S]*sourceKind:\s*mcpConfig \? 'mcp-wrapper' : 'bundled-skill-collection'/, 'bundled skill packs must mount from packaged resources without copying large trees on startup.');
  assertMatches(main, /function runtimePluginPathAllowed[\s\S]*ECOREX_BUILTIN_PLUGIN_ALLOWLIST\.has\(name\)[\s\S]*isPathInside\(backendRoot,\s*resolved\)/, 'backend plugin directories must be allowlisted by plugin name.');
  assertMatches(main, /runtimePluginPathAllowed\(pluginPath,\s*safeRepoRoot,\s*pluginName\)/, 'plugin path validation must include the selected plugin name.');
  assertMatches(main, /stdio:\s*\['pipe',\s*'pipe',\s*'pipe'\]/, 'agent child process must keep stdin piped.');
  assertMatches(main, /child\.stdin\.end\(`\$\{prompt\}\\n`\)/, 'agent prompt must be written to stdin.');
  assertNotMatches(main, /args\.push\(\s*prompt\s*\)|spawn\([^)]*prompt/s, 'agent prompt must not be passed through process argv.');
  assert(/const profile = store\.profiles\.find\(\(item\) => item\.model === normalizedModel\) \|\| null/.test(main), 'model profile env must only match the selected model exactly.');
	  assertMatches(main, /default:\s*\{[\s\S]*?permissionMode:\s*'auto'[\s\S]*?cliMode:\s*'auto'/, 'default permission mode must use auto mode while preserving the full native tool surface.');
	  includesAll(main, ['ECOREX_DEVELOPER_WORKFLOW_PROMPT', 'frontend-design skill as the default design layer', 'frontend-design 报告级排版', 'test/lint/build/dev-server scripts', 'git status/diff', '测试、lint、build、dev server'], 'agent prompt and capability inventory must advertise the full developer workflow surface.');
	  assertNotMatches(main, /CLAUDE_AUTO_ALLOWED_TOOL_SET|'--allowedTools'|"--allowedTools"/, 'agent runtime must not narrow the native tool surface with an allowedTools whitelist.');
  assertMatches(main, /sanitizeClaudeSessionId\(payload\.claudeSessionId \|\| payload\.conversationId,\s*payload\.sessionId\)/, 'Claude session id must be stable for frontend conversations with session fallback.');
  assertMatches(main, /const claudeResumeExistingSession = claudeSessionTranscriptExists\(claudeSessionId\)/, 'Claude session reuse must detect existing CLI transcripts.');
  assertMatches(main, /if \(claudeResumeExistingSession\) \{\s*args\.push\('--resume', claudeSessionId\);\s*\} else \{\s*args\.push\('--session-id', claudeSessionId\);/s, 'Claude CLI must resume existing sessions and only create new sessions with --session-id.');
  assertMatches(main, /entry\.claudeSessionId === requestedClaudeSessionId/, 'parallel starts for the same Claude session must be blocked before spawning.');
  assertMatches(main, /function refreshClaudeSessionTranscriptSeen[\s\S]*findClaudeSessionTranscript\(sessionId\)[\s\S]*claudeTranscriptExistenceCache\.delete\(sessionId\)/, 'Claude resume cache must verify transcript files and clear stale resume state.');
  assertMatches(main, /let finalStatus = code === 0 && !entry\.claudeResultFailed \? 'completed' : 'failed'[\s\S]*const incompleteResult = finalStatus === 'completed' && !agentSessionHasSubstantiveResult\(entry\)[\s\S]*const authorizationIncomplete = finalStatus === 'completed' && agentSessionHasUnresolvedAuthorization\(entry\)[\s\S]*const unresolvedUserBlocker = finalStatus === 'completed' && agentSessionHasUnresolvedUserBlocker\(entry\)[\s\S]*if \(authorizationIncomplete\) \{[\s\S]*finalStatus = 'authorization-incomplete'[\s\S]*\} else if \(unresolvedUserBlocker\) \{[\s\S]*finalStatus = 'user-action-required'[\s\S]*\} else if \(incompleteResult\) \{[\s\S]*finalStatus = 'failed'/, 'Claude sessions must not report completed when no substantive result, pending authorization, or unresolved user blocker remains.');
  assertMatches(main, /reason:\s*authorizationIncomplete \? 'authorization-incomplete' : unresolvedUserBlocker \? 'user-action-required' : incompleteResult \? 'incomplete-result'/, 'unfinished user-action blockers must be reported as resumable failures instead of completed tasks.');
  assertMatches(main, /function normalizeAgentState[\s\S]*authorization-incomplete\|authorization-required\|user-action-required[\s\S]*return 'waiting'/, 'pending authorization and user-action events must never normalize to completed state.');
  assertMatches(main, /const userAction = structuredUserActionFromPayload\(payload\)[\s\S]*event\.requiresUserAction = true[\s\S]*event\.status = pendingStatus[\s\S]*event\.task\.status = pendingStatus/, 'backend event protocol must mark structured permission and external authorization waits as pending user action.');
  assertMatches(main, /function permissionRequestFromAgentText[\s\S]*mcp__\|mcp\[_\\s-\]\|tool\|browser\|playwright/, 'backend permission detector must catch MCP/tool/browser permission requests.');
  assertMatches(main, /function externalAuthorizationRequestFromText[\s\S]*device\[_ -\]\?code[\s\S]*qrText/, 'backend authorization detector must expose links, device codes, and QR payload text.');
  assertMatches(main, /function substantiveAgentResultText[\s\S]*function agentSessionHasSubstantiveResult[\s\S]*function agentSessionHasUnresolvedAuthorization[\s\S]*function agentSessionHasUnresolvedUserBlocker[\s\S]*function incompleteAgentResultText/, 'agent completion must distinguish process exit from usable final output, unfinished authorization, and unresolved user blockers.');
  assertMatches(main, /function agentSessionHasUnresolvedUserBlocker[\s\S]*entry\.lastUserAction[\s\S]*permission-required[\s\S]*\\u6743\\u9650\\u786e\\u8ba4\\u5361/, 'pending permission actions and Chinese permission-card blockers must keep the agent session waiting.');
  assertMatches(main, /recordSessionEvent[\s\S]*pendingUserActionResult[\s\S]*const resultText = pendingUserActionResult \? '' : substantiveAgentResultText/, 'pending user-action result text must not be counted as a substantive final answer.');
  assertMatches(main, /const finalUserAction = \['authorization-incomplete', 'user-action-required'\]\.includes\(status\)[\s\S]*userAction:\s*finalUserAction/, 'final lifecycle events must not reattach stale user-action cards after successful completion.');
  assertMatches(main, /authorizationState === 'completed'[\s\S]*delete entry\.lastUserAction[\s\S]*normalized\.userAction[\s\S]*userActionCompleted[\s\S]*delete entry\.lastUserAction/, 'completed authorization or permission events must clear stale pending user-action state.');
  assertMatches(main, /recordSessionEvent[\s\S]*normalized\.kind === 'result'[\s\S]*entry\.hasSubstantiveResult = true/, 'result events must mark whether the task produced a substantive final answer.');
  assertNotMatches(main, /child\.on\('close'[\s\S]{0,600}markClaudeSessionTranscriptSeen\(entry\.claudeSessionId\)/, 'agent close must not blindly mark failed or missing Claude transcripts as resumable.');
  assertMatches(main, /streamType === 'error'[\s\S]*claudeResultStatus:\s*'failed'/, 'stream-json error events must be surfaced as failed agent events.');
  assertMatches(main, /json\.type === 'result'[\s\S]*const resultFailed = Boolean[\s\S]*claudeResultStatus: resultFailed \? 'failed' : 'completed'/, 'Claude result subtypes must drive success or failure status.');
  assertMatches(main, /const retainedEventCount = HARD_MAX_AGENT_EVENT_QUEUE - 1[\s\S]*queue\.events\.slice\(-retainedEventCount\)/, 'event backpressure compression must stay within the hard queue limit including the notice event.');
});

check('agent transcript history is public-safe', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      'const MAX_TRANSCRIPT_TEXT_PREVIEW_CHARS =',
      'function publicPromptPreview',
      'promptPreview: entry.promptPreview ||',
      'promptHash: entry.promptHash || undefined',
      'textPreview: safeTranscriptTextPreview(normalized.text)',
      'workspacePath: entry.workspacePath || publicWorkspacePath(entry.cwd)',
      'function sessionTranscriptSummaryFromFile',
      'function getSessionHistorySummary',
      'recentSessionHistory: recent',
      "handleSafe('agent:session-history'"
    ],
    'public-safe transcript history'
  );
  const transcriptWriter = main.match(/function writeSessionTranscript[\s\S]*?function publicTranscriptEventSummary/);
  assert(transcriptWriter, 'writeSessionTranscript block must be present.');
  assertNotMatches(transcriptWriter[0], /prompt:\s*entry\.prompt|cwd:\s*entry\.cwd/, 'transcript must not save full prompt or absolute cwd.');
  assertNotMatches(transcriptWriter[0], /\btext:\s*safeTranscriptText\(normalized\.text\)/, 'transcript must store textPreview instead of full event text.');
});

check('chat disclosure stays public-safe', () => {
  const app = readText('src/App.jsx');
  includesAll(
    app,
    [
      'function isPublicTraceItem',
      "if (kind === 'debug' || kind === 'assistant') return false",
      'function publicTraceItems',
      'slice(-6)',
      'function InlineAgentTrace',
      'function AssistantProcessDisclosure',
      'function StreamingAssistantText',
      'function AssistantReportText',
      'function isContextCompactEvent',
      'contextSummary',
      'claude-cli-compact'
    ],
    'chat disclosure filtering'
  );
  assertNotMatches(app, /costUsd|total_cost_usd/, 'renderer chat must not render model runtime cost fields.');
});

check('fullAccess permission parameter chain', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      "const FULL_ACCESS_PERMISSION_MODE = 'fullAccess'",
      "const FULL_ACCESS_CLAUDE_FLAG = '--dangerously-skip-permissions'",
      'cliFlags: [FULL_ACCESS_CLAUDE_FLAG]',
      'requiresConfirmation: true',
      'function hasFullAccessConfirmation',
      'payload?.fullAccessConfirmed === true',
      'payload?.confirmFullAccess === true',
      'payload?.fullAccessConfirmation === FULL_ACCESS_PERMISSION_MODE',
      'Full access permission requires explicit confirmation.',
      'permissionCliFlags: permissionPolicy.cliFlags'
    ],
    'fullAccess chain'
  );
  assert(/if \(requestedMode === FULL_ACCESS_PERMISSION_MODE && !hasFullAccessConfirmation\(payload\)\)/.test(main), 'settings update must require fullAccess confirmation.');
  assert(/if \(permissionPolicy\.fullAccess && permissionModeFromPayload && !hasFullAccessConfirmation\(payload\)\)/.test(main), 'agent payload must require fullAccess confirmation.');
  assert(/for \(const flag of permissionCliFlags \|\| \[\]\) \{\s*args\.push\(flag\);\s*\}/.test(main), 'agent args must include permissionCliFlags.');
  assert(/if \(permissionPolicy\?\.fullAccess\) \{\s*writeLog\('warn'/.test(main), 'fullAccess agent starts must be logged as warnings.');
});

check('model profile storage is local, encrypted, and public-safe', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      "const MODEL_PROFILES_FILE_NAME = 'model-profiles.json'",
      "return path.join(app.getPath('userData'), MODEL_PROFILES_FILE_NAME)",
      "encoding: 'safeStorage/v1'",
      'encryptSecretValue(normalizeSecretValue(apiKey))',
      "apiKey: ''",
      'apiKeyConfigured',
      'apiKeyMasked',
      'pathLabel: `userData:/${MODEL_PROFILES_FILE_NAME}`',
      'function provisionModelProfilesIfNeeded',
      "return provisionModelProfilesIfNeeded() || fallback",
      'env.ANTHROPIC_BASE_URL = profile.baseUrl',
      'env.OPENAI_BASE_URL = profile.baseUrl',
      'env.ANTHROPIC_API_KEY = apiKey',
      'env.OPENAI_API_KEY = apiKey'
    ],
    'model profile storage'
  );
});

check('model profile tests cannot redirect stored credentials', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      'function normalizeModelBaseUrlForUse',
      'function hasPrivateModelBaseUrlConfirmation',
      'function isPrivateModelHost',
      'Private or local model baseUrl requires explicit confirmation.',
      'const hasPayloadApiKey = apiKeyFromPayload !== null',
      'const hasPayloadBaseUrl = hasOwnValue(input, \'baseUrl\')',
      'Saved model credentials cannot be tested against a temporary baseUrl.',
      'apiKey: hasPayloadApiKey ? apiKeyFromPayload : decryptModelProfileApiKey(stored)'
    ],
    'model credential redirect guard'
  );
  assertMatches(
    main,
    /stored && hasPayloadBaseUrl && !hasPayloadApiKey && payloadBaseUrl !== stored\.baseUrl/,
    'stored model credentials must not be paired with an arbitrary payload baseUrl.'
  );
  assertNotMatches(
    main,
    /baseUrl:\s*hasOwnValue\(input,\s*'baseUrl'\)\s*\?\s*normalizeModelBaseUrl\(input\.baseUrl\)\s*:\s*stored\?\.baseUrl/,
    'model profile test payload must not directly override stored baseUrl.'
  );
});

check('workspace root changes require confirmation and reject protected roots', () => {
  const main = readText('electron/main.cjs');
  includesAll(
    main,
    [
      'function hasCustomWorkspaceRootConfirmation',
      'payload?.confirmCustomWorkspaceRoot === true',
      'function isProtectedWorkspaceRoot',
      'function pathContainsSymlink',
      'function safeWorkspaceDialogDefaultPath',
      'path.parse(resolved)',
      'Custom workspaceRoot requires explicit confirmation.',
      'Workspace root cannot be a disk root, home folder, app data folder, or protected system directory.',
      'Workspace path crosses a symbolic link.',
      'customWorkspaceRootConfirmed',
      'next.workspaceRoot = defaultWorkspaceRoot()',
      'allowCurrentRoot || hasCustomWorkspaceRootConfirmation(payload)'
    ],
    'workspace root safety guard'
  );
  assertMatches(
    main,
    /next\.workspaceRoot = normalizeWorkspaceRoot\(payload\.workspaceRoot,\s*\{[\s\S]*allowCustomWorkspaceRoot:\s*hasCustomWorkspaceRootConfirmation\(payload\)/,
    'settings update must require explicit confirmation for custom workspaceRoot.'
  );
  assertMatches(
    main,
    /function ensureWorkspace\(payload = \{\}\)[\s\S]*const current = readSettings\(\)[\s\S]*allowCurrentRoot \|\| hasCustomWorkspaceRootConfirmation\(payload\)/,
    'workspace ensure must not silently persist arbitrary custom roots.'
  );
  assertNotMatches(main, /updateSettings\(\{\s*workspaceRoot:\s*payload\.workspaceRoot\s*\}\)/, 'workspace ensure must not silently save payload workspaceRoot.');
  assertMatches(main, /resolveWorkspacePath[\s\S]*pathContainsSymlink\(workspaceRoot,\s*target\)/, 'workspace relative paths must reject symlink traversal.');
});

check('model adapter defaults and smoke tests are offline-safe', () => {
  const adapter = readText('electron/model-adapter.cjs');
  const main = readText('electron/main.cjs');
  const smoke = readText('scripts/test-model-adapter.cjs');
  includesAll(
    adapter,
    [
      "const DEFAULT_IMAGE_MODEL = 'gpt-image-2'",
      'const MAX_TIMEOUT_MS = 2 * 60 * 1000',
      'model: normalizeImageModelName(body.model || profile.imageModel || profile.imageModelName || DEFAULT_IMAGE_MODEL)',
      'DEFAULT_IMAGE_MODEL',
      'function normalizeImageModelName',
      'function extractOpenAIText',
      'function parseOpenAIStream',
      'function normalizeOpenAIResponse',
      'text: responseOk ? normalizedText :',
      'stream: Boolean(normalized.stream)'
    ],
    'model adapter defaults and OpenAI-compatible text/stream transform'
  );
  includesAll(
    main,
    [
      'const IMAGE_GENERATION_TIMEOUT_MS = 2 * 60 * 1000',
      'IMAGE_GENERATION_TIMEOUT_MS'
    ],
    'image generation timeout guard'
  );
  assertMatches(adapter, /const responseOk = Boolean\(response\.ok && !normalized\.errorMessage\)/, 'model adapter must treat OpenAI stream error chunks as failed responses.');
  assertMatches(adapter, /parseServerSentEventData[\s\S]*trimmed === '\[DONE\]'[\s\S]*extractOpenAIStreamText/, 'model adapter must parse OpenAI-compatible SSE stream text.');
  assertMatches(smoke, /createModelAdapter\(\{\s*fetchImpl:/, 'model adapter smoke tests must inject fake fetchImpl.');
  assertMatches(smoke, /assert\.equal\(imageCalls\[0\]\.model,\s*DEFAULT_IMAGE_MODEL\)/, 'model adapter smoke tests must assert default image model.');
  assertNotMatches(smoke, /\bfetch\(\s*['"`]https?:\/\//, 'model adapter smoke tests must not call real network URLs.');
});

check('MCP plugin and CLI exposure is sanitized', () => {
  const main = readText('electron/main.cjs');
  const app = readText('src/App.jsx');
  const pkg = packageJson();
  includesAll(
    main,
    [
      'function publicProductText',
      'function publicAgentText',
      '.replace(/\\bClaude Code CLI\\b/gi,',
      ".replace(/\\bClaude\\b/gi, 'EcoreX')",
      '.replace(/\\bMCP servers?\\b/gi,',
      '.replace(/\\bplugins?\\b/gi,',
      'function safeOutputText',
      'const text = publicProductText(String(value || \'\'))',
      'publicStableId',
      'publicSkillPackRecord'
    ],
    'main public product redaction'
  );
  includesAll(
    main,
    [
      "command: 'managed: local Claude/Codex MCP inventory is intentionally not scanned'",
      "command: 'managed: local Claude/Codex skill inventory is intentionally not scanned'",
      'defaultEmpty: services.length === 0',
      'services,',
      'function installManagedSkillPack',
      'function updateManagedSkillEnabled',
      'function resetManagedSkillPacks',
      'function runtimeManagedSkillPlugins',
      'const pluginInventory = [...parsePluginInventory(), ...runtimeManagedSkillPlugins()]',
      "args.push('--plugin-dir', pluginPath)"
    ],
    'default hidden local MCP inventory with managed SKILLS and bundled backend plugin activation'
  );
  includesAll(
    app,
    [
      'function sanitizeDisplayText',
      '.replace(/--dangerously-skip-permissions/gi,',
      '.replace(/\\bCLI\\b/g,',
      '.replace(/\\bMCP\\b/g,',
      '.replace(/\\bplugins\\b/gi,',
      '.replace(/\\bplugin\\b/gi,'
    ],
    'renderer public product redaction'
  );
  includesAll(
    app,
    [
      'capabilities?.capabilityPacks',
      'function canManageSkillsFromAuth',
      'data-testid="skill-source-path-input"',
      'data-testid="skill-reset-button"',
      "'feature-dev', 'code-review', 'security-guidance', 'plugin-dev', ...managedPlugins",
      'plugins: selectedPlugins',
      'setSkills([])',
      'setServices([])'
    ],
    'renderer must hide default MCP/Skill lists while keeping backend plugins active'
  );
  const extraResources = Array.isArray(pkg.build?.extraResources) ? pkg.build.extraResources : [];
  const backendResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/claude-code-main');
  const backendMapResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/cli.js.map');
  assert(!backendResource, 'build config must not package the terminal source tree.');
  assert(!backendMapResource, 'build config must not package terminal source maps.');
  assert(!JSON.stringify(pkg.build).includes('%USERPROFILE%') && !JSON.stringify(pkg.build).includes('${HOME}'), 'build config must not package user home MCP/Skill state.');
  assert(!JSON.stringify(pkg.build).includes('终端源代码'), 'build config must not reference the local terminal source directory.');
  assertMatches(main, /attachDeveloperDiagnostics\([\s\S]*?if \(payload\?\.includeDiagnostics\)/, 'developer diagnostics must only be attached on explicit request.');
  assertMatches(main, /raw:\s*safeOutputText\(/, 'raw bridge output must be sanitized before diagnostics.');
  assertMatches(main, /handleSafe\('skill:reset'[\s\S]*requiredPermission:\s*'skills:manage'/, 'skill reset must require managed skill permissions.');
  assertMatches(main, /handleSafe\('skill:install'[\s\S]*requiredPermission:\s*'skills:manage'/, 'skill install must require managed skill permissions.');
  assertMatches(app, /const canManageSkills = canManageSkillsFromAuth\(authStatus\)[\s\S]*const actionDisabled = !canManageSkills/, 'renderer skill controls must be gated to admins with skill permissions.');
});

check('diagnostics health check UI is complete and static-safe', () => {
  const app = readText('src/App.jsx');
  const css = readText('src/styles.css');
  const main = readText('electron/main.cjs');
  includesAll(
    app,
    [
      'function SystemSettingsView',
      "systemSettingsTabFromPage(page)",
      "data-testid=\"system-settings-page\"",
      "data-testid={`system-settings-tab-${value}`}",
      '系统设置',
      'MCP',
      'SKILLS',
      'EvaluationView',
      '<DiagnosticsView',
      "callEcorex(['getDiagnostics', 'diagnostics.get'])",
      'function releasePackageSummary',
      'function signatureStatusSummary',
      'function runtimeEngineSummary',
      'function modelConfigSummary',
      'function localBuildSummary',
      'function permissionModeSummary',
      'function projectDirectorySummary',
      'function recentTaskSummary',
      'function validateModelConnectionDraft',
      'function normalizeRecentHistoryItem',
      'const healthItems = [',
      '可发布前检查',
      '本地构建',
      'Agent 引擎',
      '模型配置',
      '权限模式',
      '项目目录',
      '最近任务',
      '签名状态',
      '证书签名不作为本轮目标',
      'health-check-panel',
      'health-check-grid',
      'recentSessionHistory',
      'recentSessionFiles'
    ],
    'diagnostics health check renderer'
  );
  assertMatches(app, /<nav className="side-nav">[\s\S]*?setPage\('projects'\)[\s\S]*?<\/nav>/, 'sidebar must keep project navigation available.');
  assertNotMatches(app, /<nav className="side-nav">[\s\S]*?setPage\('(mcp|skills|diagnostics|settings)'\)[\s\S]*?<\/nav>/, 'MCP, SKILLS, and diagnostics/settings must stay out of the left sidebar.');
  assertMatches(app, /profile-menu-grid[\s\S]*setPage\('settings'\)[\s\S]*系统设置/, 'system settings must remain reachable from the personal profile menu.');
  assertMatches(app, /const tabs = \[[\s\S]*\['mcp', 'MCP'[\s\S]*\['skills', 'SKILLS'[\s\S]*\['diagnostics'[\s\S]*tabs\.splice\(3,\s*0,\s*\['evaluations'/, 'system settings must expose MCP, SKILLS, evaluations, and diagnostics as tabs.');
  assertMatches(app, /async function refreshModelHealth\([\s\S]*?loadModelProfiles\(settings\.defaultModel \|\| DEFAULT_AGENT_MODEL_NAME\)/, 'diagnostics model health must use stored profile metadata only.');
  assertNotMatches(app, /refreshModelHealth[\s\S]{0,500}testModel(Profile|AdapterProfile)|refreshModelHealth[\s\S]{0,500}generateModelImage/, 'diagnostics model health must not run model tests or image generation.');
  assertMatches(app, /function validateModelConnectionDraft\([\s\S]*?未发起模型调用/, 'model tests must locally report missing key/baseUrl/model without calling providers.');
  assertMatches(app, /async function handleTest\([\s\S]*?validateModelConnectionDraft\(draft,\s*selectedProfile\)[\s\S]*?return;[\s\S]*?testModelAdapterProfile/, 'model speed test must validate required fields before IPC.');
  assertMatches(app, /async function handleImageTest\([\s\S]*?validateModelConnectionDraft\(draft,\s*selectedProfile\)[\s\S]*?return;[\s\S]*?generateModelImagePreview/, 'image model test must validate required fields before IPC.');
  assertNotMatches(app, /<strong>\{secret\.key\}<\/strong>|<em>\{secret\.key\}<\/em>/, 'diagnostics must not render raw secret keys.');
  includesAll(
    css,
    [
      '.diagnostics-page',
      'overflow: hidden',
      '.diagnostics-content',
      'min-height: 0',
      '.settings-panel',
      'overflow: auto',
      '.health-check-grid',
      'max-height: min(250px, 34vh)',
      '.health-check-item',
      '.recent-session-file em',
      '.workspace-entry-list',
      'flex: 1 1 auto',
      '.model-config-notice'
    ],
    'diagnostics health check layout'
  );
});

check('enterprise evaluation framework is wired and memory-safe', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  const app = readText('src/App.jsx');
  const evalFramework = readText('electron/evaluation-framework.cjs');
  includesAll(
    main,
    [
      "require('./evaluation-framework.cjs')",
      "const EVALUATION_REPORT_FILE_NAME = 'evaluation-report.json'",
      "handleSafe('evaluation:list'",
      "handleSafe('evaluation:run'",
      "requiredPermission: 'enterprise:manage'"
    ],
    'evaluation IPC'
  );
  includesAll(preload, ['listEvaluations', 'runEvaluations'], 'evaluation preload bridge');
  includesAll(app, ['function EvaluationView', 'evaluation-page', "activeTab === 'evaluations'"], 'evaluation renderer');
  const sampleCount = (evalFramework.match(/sample\('/g) || []).length;
  assert(sampleCount === 50, 'evaluation framework must contain exactly 50 named samples.');
  includesAll(
    evalFramework,
    [
      'exponential-backoff-with-jitter',
      'factuality',
      'structure',
      'toolUse',
      'latency',
      'evaluation-only; do-not-store-in-chat-or-project-memory'
    ],
    'evaluation policies'
  );
});

check('chat state tree and critical front-end affordances', () => {
  const app = readText('src/App.jsx');
  const css = readText('src/styles.css');
  const main = readText('electron/main.cjs');
  includesAll(
    app,
    [
      'const messageStates = {',
      "cancelled: { label: '已取消'",
      "timeout: { label: '已超时'",
      "const AGENT_EVENT_TERMINAL_KINDS = new Set(['result', 'done', 'error', 'cancelled', 'timeout'])",
      "const AGENT_SESSION_FINAL_KINDS = new Set(['done', 'error', 'cancelled', 'timeout'])",
      'function isAgentSessionFinalEvent',
      'function textLooksPendingUserAction',
	      'function recoverDuplicateRunWithoutQueue',
      'function agentRunPolicySection',
      '有限任务不要启动长期后台命令',
      "status: 'cancelled'",
      "status: 'timeout'",
      'function agentRecoveryText',
      'function agentRunFailureMessage',
      "result.code === 'too-many-sessions'",
      'timelineItemFromAgentEvent',
      '<MessageStatus',
      "chat-layout ${focusArtifact ? 'preview-focus' : 'chat-only'}",
      '<ArtifactFocusPanel',
      'function RichMessageText',
      'function ChatExternalLink',
      'function ChatInlineMedia',
      'function isInternalAgentOutputLine',
      'function cleanAgentDisplayLine',
      'function publicRunningSessionPrompt',
      'function publicRunningSessionTitle',
      'TaskUpdate|正在整理工具参数|开始生成回复|正在同步输出|回复生成完成',
      'REMINDER:',
      'Updated|Created|Deleted',
      'page/cli',
      'chatMediaKind(safeUrl)',
      'openExternalUrlWithBridge(safeUrl)',
      'function stageTransferredInput',
      'function transferText',
      'function filesFromDataTransfer',
      'function ChatPermissionOverlay',
      'function InlinePermissionRequest',
      'data-testid="permission-confirmation-card"',
      "import QRCode from 'qrcode'",
      'QRCode.toDataURL(qrValue',
      'EcoreX 聊天框内权限确认卡',
      'onDrop={(event) => stageTransferredInput(event, event.dataTransfer)}'
    ],
    'chat state tree and artifact focus layout'
  );
  includesAll(app, ["replace(/\\bClaude\\s*Code\\s*CLI\\b/gi, 'EcoreX')", "replace(/\\bClaude\\b/gi, 'EcoreX')"], 'assistant-visible product naming sanitizer');
  assertNotMatches(main, /replace\(\s*\/\\bCLI\\b\/gi,\s*['"]execution bridge['"]\s*\)/, 'backend product sanitizer must not rewrite generic CLI inside authorization URLs.');
  includesAll(main, [
    'function cleanAgentUrlToken',
    ".replace(/\\/page\\/execution[_\\s-]*bridge(?=\\?)/gi, '/page/cli')",
    ".replace(/from=execution[_\\s-]*bridge\\b/gi, 'from=cli')"
  ], 'backend URL cleaner must repair legacy Feishu execution-bridge URLs back to page/cli.');
  assertMatches(main, /function runtimeAuthorizationRequestFromValue[\s\S]*verification_uri_complete[\s\S]*verificationUriComplete[\s\S]*page\\\/\(\?:cli\|execution/, 'backend structured authorization parser must keep lark-cli verification_uri_complete/page/cli links.');
  includesAll(app, [
    'function cleanChatUrlToken',
    ".replace(/\\/page\\/execution[_\\s-]*bridge(?=\\?)/gi, '/page/cli')",
    ".replace(/from=execution[_\\s-]*bridge\\b/gi, 'from=cli')"
  ], 'chat URL cleaner must repair legacy Feishu execution-bridge URLs back to page/cli.');
  assertMatches(app, /function authorizationRequestFromText[\s\S]*open\\\.feishu\\\.cn\\\/page\\\/\(\?:cli\|execution/, 'chat authorization parser must recognize Feishu page/cli links.');
  assertNotMatches(app, /<RunningSessionStrip\b/, 'running session strip must stay hidden from the main chat UI.');
  assertMatches(app, /if \(event\.kind === 'result'\)[\s\S]*streaming:\s*true[\s\S]*status:\s*'generating'/, 'stream-json result content must not release the running session before the lifecycle final event.');
  assertMatches(app, /relevantEvents[\s\S]*\.filter\(\(event\) => isAgentSessionFinalEvent\(event\)\)/, 'front-end running sessions must finish only on lifecycle final events.');
  assertMatches(app, /terminalPendingUserActionStatus\(event,\s*combinedText\)/, 'authorization or browser handoff prompts must not be shown as completed.');
  assertMatches(app, /function timelineItemFromAgentEvent[\s\S]*const pendingStatus = pendingUserActionStatusFromEvent\(event\)/, 'timeline rows must not infer waiting authorization or confirmation from ordinary assistant result text.');
  assertMatches(app, /function terminalPendingUserActionStatus[\s\S]*textLooksTerminalPendingUserAction\(existingText\)[\s\S]*permissionPromptStatusFromText\(existingText\)/, 'terminal events must only become pending user-action states from explicit actionable prompts.');
  assertMatches(app, /function visibleAssistantTextFromUserActionEvent[\s\S]*action\.type === 'authorization' \? action\.description : ''[\s\S]*return text;/, 'structured permission cards must not swallow substantive result text into the overlay.');
  assertMatches(app, /const structuredUserAction = normalizeAgentUserAction\(event\.userAction \|\| event\.authorization \|\| event\.permissionRequest\)[\s\S]*statusFromAgentUserAction\(structuredUserAction\)[\s\S]*userAction: structuredUserAction[\s\S]*continue;/, 'structured backend user-action events must render as pending cards before text-based inference or terminal merging.');
  assertMatches(app, /function AuthorizationChatContent[\s\S]*QRCode\.toDataURL\(qrValue[\s\S]*openExternalUrlWithBridge\(safeUrl\)/, 'external authorization QR codes and open-link actions must render in assistant chat content.');
  assertNotMatches(app, /function InlinePermissionRequest[\s\S]*QRCode\.toDataURL\(qrValue/, 'composer permission overlay must stay compact and must not embed QR-rich authorization content.');
  assertMatches(app, /function normalizeAgentUserAction[\s\S]*userCode:[\s\S]*deviceCode:[\s\S]*expiresIn:/, 'authorization cards must retain device-code fallback fields from structured backend events.');
  assertMatches(app, /function AuthorizationChatContent[\s\S]*fallbackCode[\s\S]*<code>\{fallbackCode\}<\/code>/, 'authorization chat content must show a manual device/user code when a browser QR flow is not enough.');
  assertMatches(app, /<ChatPermissionOverlay[\s\S]*request=\{pendingPermissionRequest\?\.request\}/, 'permission confirmation must render from the composer overlay instead of being embedded in assistant chat content.');
  assertNotMatches(app, /<InlinePermissionRequest[\s\S]*onPermissionReply\?\.\(message,\s*action\)/, 'assistant messages must not embed permission confirmation cards after the overlay owns the flow.');
  assertMatches(app, /const continuationMessageId = message\.id/, 'permission continuation must reuse the original assistant message instead of creating a new relay chat bubble.');
	  assertMatches(app, /if \(pendingMessageForPrompt && !fromQueue && !forceRun\)[\s\S]*continueFromPermission\(pendingMessageForPrompt[\s\S]*markPendingUserActionSuperseded\(pendingMessageForPrompt, activeConversationId\)/, 'done/authorized replies must resume pending user-action tasks directly, while new input supersedes stale waiting cards.');
	  assertMatches(app, /if \(hasRunningSessionForConversation\(activeConversationId\) && !forceRun\)[\s\S]*continueFromPermission\(pendingMessage[\s\S]*interruptRunningSessionForConversation\(activeConversationId\)/, 'new user input during a running task must interrupt the current run instead of entering a follow-up queue.');
	  assertMatches(app, /function recoverDuplicateRunWithoutQueue[\s\S]*status:\s*'read'[\s\S]*运行已接入现有任务/, 'duplicate running-session races must not create queued/manual guide messages.');
	  assertNotMatches(app, /<button className="queued-message-action"/, 'user messages must not expose manual guide/queue buttons.');
  assertMatches(app, /function cleanAgentDisplayLine[\s\S]*numberTokens\.length >= 3[\s\S]*open\\\.feishu\\\.cn[\s\S]*\/page\/cli/, 'chat renderer must strip line-number noise while preserving Feishu page/cli links.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*Updated\|Created\|Deleted[\s\S]*task\\s\+#\?\\d\+/, 'chat renderer must suppress raw task-list mutation noise.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*UNDICI-EHPA[\s\S]*name:\\s\*lark-shared/, 'chat renderer must suppress OpenCLI warnings and skill metadata noise.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*lark-execution\\s\+bridge\\s\+auth\\s\+login[\s\S]*base_token/, 'chat renderer must suppress Feishu authorization harness and raw token JSON noise.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*REMINDER:\\s\*You MUST include the sources above/, 'chat renderer must suppress internal web-search citation reminders.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*WEB_SEARCH_OK[\s\S]*No links found[\s\S]*搜索工具返回[\s\S]*开始生成回复[\s\S]*正在同步输出/, 'chat renderer must suppress web-search harness status noise.');
  assertMatches(app, /function isInternalAgentOutputLine[\s\S]*Browser Bridge extension[\s\S]*unknown command[\s\S]*opencli[\s\S]*main\\\.js/, 'chat renderer must suppress OpenCLI browser-bridge diagnostic noise.');
  assertMatches(app, /function publicRunningSessionPrompt[\s\S]*textLooksCorrupted\(clean\)[\s\S]*return fallback/, 'running session strip must hide corrupted or raw internal prompt previews.');
  assertMatches(app, /function publicRunningSessionTitle[\s\S]*textLooksCorrupted\(clean\)[\s\S]*return fallback/, 'running session strip must hide corrupted recovered titles.');
  assertMatches(app, /function splitAssistantDisplayMessages[\s\S]*assistant-message-thread[\s\S]*renderAssistantExtras/, 'long assistant output must render as separate visible message rows with status controls on the final row.');
  assertMatches(app, /function looksLikeXinAgentNaturalQuery[\s\S]*hasSpendQuestion[\s\S]*hasSpendQuestion/, 'business spend questions must use the direct Xin Assistant query path instead of a generic agent run.');
  assertMatches(app, /function looksLikeXinAgentNaturalQuery[\s\S]*isXinMetaQuestion[\s\S]*return false/, 'Xin Assistant meta/debug questions must stay in the normal chat path.');
  includesAll(app, ['account_keyword_report', 'xinAgentWorkflowReportSummary', '匹配账户'], 'Xin Assistant keyword report rendering');
  includesAll(css, ['.assistant-message-thread', '.assistant-card-split', '.assistant-row-spacer'], 'split assistant message row styles');
  assertMatches(app, /const messageStates = \{[\s\S]*interrupted:[\s\S]*authorization-incomplete[\s\S]*user-action-required/, 'message status badges must not render recoverable failures as completed.');
  assertMatches(app, /trackSession\(requestedSessionId[\s\S]*prompt:\s*cleanPrompt \|\| '正在执行任务'/, 'running session strip must use the user task summary instead of the full internal agent prompt.');
  assertMatches(main, /function cleanAgentDisplayLine[\s\S]*numberTokens\.length >= 3[\s\S]*open\\\.feishu\\\.cn[\s\S]*\/page\/cli/, 'backend event sanitizer must strip line-number noise while preserving Feishu page/cli links.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*Updated\|Created\|Deleted[\s\S]*task\\s\+#\?\\d\+/, 'backend event sanitizer must suppress raw task-list mutation noise.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*UNDICI-EHPA[\s\S]*name:\\s\*lark-shared/, 'backend event sanitizer must suppress OpenCLI warnings and skill metadata noise.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*lark-execution\\s\+bridge\\s\+auth\\s\+login[\s\S]*base_token/, 'backend event sanitizer must suppress Feishu authorization harness and raw token JSON noise.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*REMINDER:\\s\*You MUST include the sources above/, 'backend event sanitizer must suppress internal web-search citation reminders.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*WEB_SEARCH_OK[\s\S]*No links found[\s\S]*搜索工具返回[\s\S]*开始生成回复[\s\S]*正在同步输出/, 'backend event sanitizer must suppress web-search harness status noise.');
  assertMatches(main, /function isInternalAgentOutputLine[\s\S]*Browser Bridge extension[\s\S]*unknown command[\s\S]*opencli[\s\S]*main\\\.js/, 'backend event sanitizer must suppress OpenCLI browser-bridge diagnostic noise.');
  assertMatches(main, /function agentOutputLooksUnresolvedAuthorization[\s\S]*open\\\.feishu\\\.cn[\s\S]*not configured/, 'backend must keep an internal auth-wait signal before hiding raw Feishu JSON noise.');
  assertMatches(main, /entry\.authorizationCompleted = true[\s\S]*entry\.authorizationHandoffStarted = true/, 'agent transcripts must track Feishu authorization handoff state before public filtering.');
  assertMatches(main, /function agentSessionHasSubstantiveResult[\s\S]*event\?\.kind === 'assistant'[\s\S]*agentOutputLooksProcessOnly/, 'backend must count substantive assistant output so final answers are not swallowed by empty result events.');
  assertMatches(main, /function agentSessionHasUnresolvedUserBlocker[\s\S]*agentSessionHasSubstantiveResult\(entry\)[\s\S]*agentSessionPublicText/, 'backend must not reclassify substantive final answers as waiting user-action from broad text heuristics.');
  assertMatches(main, /function stopAgent[\s\S]*agentSessionHasUnresolvedAuthorization\(entry\)[\s\S]*authorizationIncompleteAgentResultText\(\)[\s\S]*agentSessionHasUnresolvedUserBlocker\(entry\)/, 'stopping during external authorization must preserve a waiting-auth status instead of a completed or generic stopped state.');
  assertMatches(main, /function safeTranscriptText[\s\S]*stripInternalAgentOutput\(redactSensitiveText\(value\)\)/, 'session transcript previews must use the same internal noise filter as live chat output.');
  assertMatches(main, /input_json_delta'\) return null/, 'backend stream parser must not emit raw tool argument delta events into public chat.');
  assertMatches(app, /function cleanPublicAgentText[\s\S]*isInternalAgentOutputLine/, 'chat renderer must suppress internal launch/progress noise from assistant text.');
  assertNotMatches(app, /PermissionConfirmationModal|permission-confirmation-modal|permission-confirmation-backdrop/, 'permission confirmation must render as an inline chat card, not a full-screen modal.');
  includesAll(css, ['.chat-permission-overlay', '.chat-permission-card', '.chat-authorization-message'], 'inline permission confirmation and chat authorization styles');
  assertNotMatches(css, /\.inline-authorization-panel/, 'authorization QR styles must not live in the composer permission overlay.');
  assertNotMatches(css, /\.permission-confirmation-backdrop/, 'permission confirmation CSS must not include the removed full-screen backdrop.');
  assert(!app.includes('setRailExpanded((next) => !next)'), 'chat main must not keep the removed quick project right rail toggle.');
  assert(!app.includes('<aside className={`right-rail'), 'chat main must not render the removed quick project right rail.');
  includesAll(app, ['function finalArtifactsFromText', "source: 'assistant-final'", 'finalArtifacts: mergeArtifactReferences', 'message.finalArtifacts || []', 'function isExplicitLocalArtifactPathToken'], 'final deliverable artifact extraction');
  assertMatches(app, /function parseArtifactPathToken\(rawValue = '', options = \{\}\)[\s\S]*options\.allowBare[\s\S]*const bareFilePattern[\s\S]*add\(token, \{ allowBare: true \}\)/, 'final deliverable extraction must turn bare generated filenames into previewable artifact cards.');
  assertMatches(app, /function artifactReferencesFromEvent[\s\S]*event\.artifacts[\s\S]*agent-structured[\s\S]*artifactReferencesFromEvent\(event\)[\s\S]*finalArtifactsFromText\(combinedText\)/, 'structured backend artifact events must create final preview cards even when the answer text omits a path.');
  assertMatches(app, /const candidateArtifactReferences = useMemo\([\s\S]{0,620}message\.finalArtifacts \|\| \[\]/, 'artifact preview shelf must use final result artifacts only.');
  assertMatches(app, /const artifactReferences = useMemo\([\s\S]{0,220}candidateArtifactReferences/, 'artifact preview shelf must render final local artifact cards before opening preview.');
  assertMatches(app, /<ArtifactPreviewShelf[\s\S]{0,420}artifacts=\{artifactReferences\}/, 'artifact preview shelf must render only the computed final artifact list.');
  assertNotMatches(app, /const candidateArtifactReferences = useMemo\([\s\S]{0,900}extractArtifactReferences\(rawText\)/, 'streamed/intermediate assistant text must not create artifact preview cards.');
  assertNotMatches(app, /const candidateArtifactReferences = useMemo\([\s\S]{0,900}artifactsFromLedger\(message\.ledger/, 'tool ledger and intermediate files must not create artifact preview cards.');
  assertNotMatches(app, /message\.role === 'user'[\s\S]{0,900}<ArtifactPreviewShelf/, 'user-uploaded local files must not open the AI artifact preview flow.');
  includesAll(app, ['onOpenAttachment={openUserAttachment}', '<AttachmentPreviewList attachments={message.attachments} compact onOpen={onOpenAttachment} />'], 'uploaded file local-open UX');
  assertMatches(app, /function Composer\([\s\S]*?const maxHeight = 112;[\s\S]*?overflowY = textarea\.scrollHeight > maxHeight \? 'auto' : 'hidden'/, 'composer must clamp textarea height and enable overflow scrolling.');
  includesAll(
    css,
    [
      '.composer textarea',
      'max-height: 128px',
      '.chat-rich-link',
      '.chat-rich-media video',
      '.chat-layout.preview-focus',
      'grid-template-columns: minmax(0, 1fr);',
      '.sidebar-project-session.active',
      'background: transparent;'
    ],
    'front-end layout CSS guardrails'
  );
});

check('project workspaces isolate advertising context and memory', () => {
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  const app = readText('src/App.jsx');
  const css = readText('src/styles.css');
  includesAll(
    main,
    [
      "const PROJECT_MEMORY_DIR_NAME = '.ecorex-memory'",
      'function normalizeProjectBusinessFields',
      'function ensureProjectMemory',
      'function agentSystemPromptForProject',
      'function projectEnvForAgent',
      'function generalAgentWorkspaceDir',
      '有限任务不要启动长期后台命令',
      "handleSafe('project:update'",
      "handleSafe('project:archive'",
      "handleSafe('project:delete'",
      'Run cwd must stay inside the current project.',
      'Run cwd must stay inside the general workspace.'
    ],
    'project workspace backend'
  );
  assertMatches(main, /sanitizePayload\([\s\S]*?projectContext[\s\S]*?cwdRoot[\s\S]*?isPathInside\(cwdRoot,\s*requestedCwd\)/, 'runPrompt cwd must be bounded by the active project when one is selected.');
  assertMatches(main, /runAgent\([\s\S]*?agentSystemPromptForProject\(projectContext\)[\s\S]*?projectEnvForAgent\(projectContext\)/, 'agent run must receive project prompt context and isolated project env.');
  includesAll(
    preload,
    [
      "updateProject: (payload) => safeInvoke('project:update'",
      "archiveProject: (payload) => safeInvoke('project:archive'",
      "deleteProject: (payload) => safeInvoke('project:delete'"
    ],
    'project workspace preload bridge'
  );
  includesAll(
    app,
    [
      'function ProjectsView',
      'const PROJECT_STATUS_OPTIONS',
      'function projectPayloadFromDraft',
      'async function archiveManagedProject',
      'async function deleteManagedProject',
      "page === 'projects'",
      "title=\"项目\"",
      'project-memory.md',
      'onOpenProjects'
    ],
    'project workspace renderer'
  );
  includesAll(
    css,
    [
      '.projects-page',
      '.projects-grid',
      '.project-list-entry',
      '.project-edit-form',
      '.project-create-form',
      '.project-meta-mini'
    ],
    'project workspace layout'
  );
});

check('renderer default copy is advertising-focused', () => {
  const app = readText('src/App.jsx');
  const rendererFiles = firstPartyTextFiles().filter((file) => {
    const normalized = toPosix(file);
    return normalized === 'src/App.jsx' || normalized.startsWith('dist/');
  });
  const forbiddenLegacyDomainPattern = /双碳|碳排|碳核算|减排|能耗|排放|绿色电力|供应链 Scope|碳资产|ESG|tCO2e/;
  includesAll(
    app,
    [
      '广告投放',
      '素材创意',
      '预算优化',
      '归因分析',
      '投放数据分析',
      '生成客户周报',
      '素材审核',
      '广告投放与项目分析上下文',
      '你可以问我任何问题'
    ],
    'advertising-focused renderer copy'
  );
  for (const file of rendererFiles) {
    assertNotMatches(readText(file), forbiddenLegacyDomainPattern, `${file} must not contain legacy carbon/ESG default copy.`);
  }
});

check('macOS packaging, release policy, and telemetry guardrails', () => {
  const pkg = packageJson();
  const main = readText('electron/main.cjs');
  const preload = readText('electron/preload.cjs');
  const app = readText('src/App.jsx');
  for (const scriptName of [
    'dist:mac',
    'verify:mac',
    'assets:mac-icon',
    'release:policy',
    'verify:install-matrix',
    'test:real-agent',
    'audit:security'
  ]) {
    assert(pkg.scripts?.[scriptName], `package script ${scriptName} is missing.`);
  }
  includesAll(
    pkg.scripts['dist:mac'],
    ['npm run assets:mac-icon', 'electron-builder --mac --x64 --arm64', 'npm run release:policy', 'npm run verify:mac'],
    'mac dist script'
  );
  const build = pkg.build || {};
  assert(build.mac?.hardenedRuntime === true, 'mac hardened runtime must be enabled.');
  assert(build.mac?.entitlements === 'build/entitlements.mac.plist', 'mac entitlements file must be configured.');
  assert(build.mac?.entitlementsInherit === 'build/entitlements.mac.inherit.plist', 'mac inherited entitlements file must be configured.');
  assertExistingFile(rel('build/entitlements.mac.plist'), 'mac entitlements');
  assertExistingFile(rel('build/entitlements.mac.inherit.plist'), 'mac inherited entitlements');
  assertIncludesPatterns(build.files, [
    'node_modules/@anthropic-ai/claude-code-darwin-arm64/**/*',
    'node_modules/@anthropic-ai/claude-code-darwin-x64/**/*'
  ], 'build.files mac native packages');
  assertIncludesPatterns(build.asarUnpack, [
    'node_modules/@anthropic-ai/claude-code-darwin-arm64/**/*',
    'node_modules/@anthropic-ai/claude-code-darwin-x64/**/*'
  ], 'build.asarUnpack mac native packages');
  for (const file of [
    'scripts/verify-macos-release-env.cjs',
    'scripts/create-mac-icon.cjs',
    'scripts/prepare-release-policy.cjs',
    'scripts/verify-install-matrix.cjs',
    'scripts/real-agent-stress.cjs',
    'scripts/security-audit.cjs',
    'docs/production-qa-checklist.md'
  ]) {
    assertExistingFile(rel(file), file);
  }
  includesAll(
    main,
    [
      "const TELEMETRY_QUEUE_FILE_NAME = 'telemetry-queue.json'",
      'anonymousTelemetryEnabled',
      'function publicTelemetryStatus',
      'function enqueueAnonymousTelemetry',
      "handleSafe('telemetry:status'",
      "handleSafe('telemetry:flush'"
    ],
    'anonymous telemetry main guardrails'
  );
  includesAll(
    preload,
    ['getTelemetryStatus', 'flushTelemetry'],
    'anonymous telemetry preload bridge'
  );
  includesAll(
    app,
    ['匿名诊断上报', '诊断上报端点', 'anonymousTelemetryEnabled', 'telemetryEndpoint'],
    'anonymous telemetry renderer settings'
  );
  assertMatches(main, /anonymousTelemetryEnabled:\s*raw\.anonymousTelemetryEnabled\s*===\s*true/, 'anonymous telemetry must default off.');
  assertMatches(main, /includesPrompts:\s*false[\s\S]*includesApiKeys:\s*false[\s\S]*includesLocalPathBodies:\s*false/, 'telemetry privacy summary must exclude prompts, keys, and local path bodies.');
});

check('package build config excludes local model/secrets storage', () => {
  const pkg = packageJson();
  assert(pkg.main === 'electron/main.cjs', 'package main must point to electron/main.cjs.');
  assert(pkg.scripts && pkg.scripts['verify:production'], 'verify:production script is missing.');
  assert(
    pkg.scripts['verify:production'].includes('npm run test:agent-runtime') &&
      pkg.scripts['verify:production'].includes('node scripts/verify-production.cjs') &&
      pkg.scripts['verify:production'].includes('npm run test:model-adapter') &&
      pkg.scripts['verify:production'].includes('npm run verify:release-clean'),
    'verify:production must be the P0/P1 entry and include agent runtime, model adapter, and release cleanliness smoke tests.'
  );
  assert(
    pkg.scripts['test:agent-runtime'] === 'node scripts/agent-runtime-smoke.cjs',
    'test:agent-runtime must run the offline agent runtime smoke script.'
  );
	  assert(
	    pkg.scripts['test:model-adapter'] === 'node scripts/test-model-adapter.cjs',
	    'test:model-adapter must run the offline model adapter smoke script.'
	  );
	  assert(
	    pkg.scripts.lint === 'node scripts/lint-smoke.cjs',
	    'lint must provide a first-class project lint entry for local developer workflow checks.'
	  );
  assert(
    pkg.scripts['release:fix-metadata'] === 'node scripts/fix-release-metadata.cjs',
    'release:fix-metadata must run the release metadata repair script.'
  );
  assert(
    pkg.scripts['prepare:managed-feishu'] === 'node scripts/prepare-managed-feishu-skill.cjs',
    'prepare:managed-feishu must build the bundled managed skill/runtime resources.'
  );
  assert(
    pkg.scripts['release:clean'] === 'node scripts/clean-release-artifacts.cjs',
    'release:clean must run the release cleanup script.'
  );
  assert(
    pkg.scripts['verify:release-clean'] === 'node scripts/verify-release-clean.cjs',
    'verify:release-clean must run the release cleanliness verifier.'
  );
  assert(
    pkg.scripts['verify:production:strict'] === 'npm run test:agent-runtime && node scripts/verify-production.cjs --strict && npm run test:model-adapter && npm run verify:release-clean',
    'verify:production:strict must enable strict release signing gates while staying offline.'
  );
  assert(
    pkg.scripts['verify:production:release'] === 'npm run test:agent-runtime && node scripts/verify-production.cjs --strict --require-signed-artifact && npm run test:model-adapter && npm run verify:release-clean',
    'verify:production:release must require signed release artifacts after packaging.'
  );
  for (const scriptName of ['pack', 'dist', 'dist:release']) {
    const script = pkg.scripts[scriptName] || '';
    const builderIndex = script.indexOf('electron-builder');
    const fixIndex = script.indexOf('npm run release:fix-metadata');
    const cleanIndex = script.indexOf('npm run release:clean');
    const feishuPrepareIndex = script.indexOf('npm run prepare:managed-feishu');
    const forbiddenReleaseActions = /\bgit\s+(?:push|tag|commit|add)|\bgh\s+release|\bnpm\s+publish|\belectron-builder\s+.*\s--publish\b/i;
    assert(!forbiddenReleaseActions.test(script), `${scriptName} must not upload, publish, tag, or commit artifacts.`);
    assert(cleanIndex > -1 && cleanIndex < builderIndex, `${scriptName} must clean old release/local artifacts before electron-builder.`);
    assert(
      feishuPrepareIndex > cleanIndex && feishuPrepareIndex < builderIndex,
      `${scriptName} must prepare bundled Feishu resources before electron-builder.`
    );
    assert(
      builderIndex > -1 && script.indexOf('npm run verify:production') > -1 && script.indexOf('npm run verify:production') < builderIndex,
      `${scriptName} must run verify:production before electron-builder.`
    );
    assert(
      fixIndex > builderIndex,
      `${scriptName} must fix release metadata after electron-builder.`
    );
  }
  assert(
    (pkg.scripts.dist || '').includes('npm run verify:production') &&
      !(pkg.scripts.dist || '').includes('verify:production:strict') &&
      !(pkg.scripts.dist || '').includes('verify:production:release'),
    'dist must remain usable for unsigned local installer builds.'
  );
  assert(
    (pkg.scripts['dist:release'] || '').includes('npm run verify:production:strict') &&
      (pkg.scripts['dist:release'] || '').includes('npm run verify:production:release'),
    'dist:release must use strict signing gates before packaging and signed-artifact gates after metadata repair.'
  );
  assert(
    (pkg.scripts['dist:mac'] || '').includes('npm run release:clean') &&
      (pkg.scripts['dist:mac'] || '').includes('npm run verify:release-clean'),
    'dist:mac must clean old release artifacts and verify release cleanliness.'
  );
  assert(
    (pkg.scripts.pack || '').lastIndexOf('npm run verify:production') > (pkg.scripts.pack || '').indexOf('npm run release:fix-metadata'),
    'pack must run ordinary production verification after metadata repair.'
  );
  const build = pkg.build || {};
  assert(build.artifactName === '${productName} Setup ${version}.${ext}', 'build.artifactName must keep installer and latest.yml names aligned.');
  assert(Array.isArray(build.files), 'build.files must be an array.');
  assert(build.files.includes('dist/**/*'), 'build.files must include dist.');
  assert(build.files.includes('electron/**/*'), 'build.files must include electron.');
  assert(build.files.includes('node_modules/@anthropic-ai/claude-code/**/*'), 'build.files must include the packaged agent runtime.');
  assertIncludesPatterns(build.files, ['node_modules/@playwright/test/**/*', 'node_modules/playwright/**/*', 'node_modules/playwright-core/**/*'], 'build.files Playwright runtime');
  assert(build.win?.icon === 'build/icon.ico', 'Windows app icon must use the EcoreX brand icon.');
  assertExistingFile(rel('build/icon.ico'), 'Windows EcoreX icon');
  assertIncludesPatterns(build.files, REQUIRED_LOCAL_STATE_EXCLUSIONS, 'build.files');
  assert(Array.isArray(build.asarUnpack), 'build.asarUnpack must be an array.');
  assert(build.asarUnpack.includes('node_modules/@anthropic-ai/claude-code/bin/**/*'), 'build.asarUnpack must unpack the agent runtime executable.');
  assertIncludesPatterns(build.asarUnpack, ['node_modules/@playwright/test/**/*', 'node_modules/playwright/**/*', 'node_modules/playwright-core/**/*'], 'build.asarUnpack Playwright runtime');
  assertNoSecretOrModelStoragePaths([...(build.files || []), ...(build.extraResources || []).map((item) => item.from || item.to || '')], 'build config');
  assert(build.nsis?.oneClick === false, 'NSIS installer must be assisted so users can choose install options.');
  assert(build.nsis?.include === 'build/installer.nsh', 'NSIS installer must include custom uninstall shortcut script.');
  assert(build.nsis?.uninstallDisplayName === 'EcoreX Agent', 'NSIS installer must set a stable uninstall display name.');
  assert(build.nsis?.allowToChangeInstallationDirectory === true, 'NSIS installer must expose the install-directory/options flow.');
  assert(build.nsis?.createDesktopShortcut === false, 'Desktop shortcut creation must be controlled by the custom installer checkbox.');
  const installerNsh = readText('build/installer.nsh');
  includesAll(
    installerNsh,
    [
      '!macro customPageAfterChangeDir',
      '安装选项',
      '创建桌面快捷方式',
      '开机自动启动 EcoreX Agent',
      'EcoreXDesktopShortcutCheckbox',
      'EcoreXStartupShortcutCheckbox',
      'CreateShortCut "$DESKTOP\\${SHORTCUT_NAME}.lnk"',
      'CreateShortCut "$SMSTARTUP\\${SHORTCUT_NAME}.lnk"',
      '!macro customInstall',
      'Uninstall ${SHORTCUT_NAME}.lnk',
      '${UNINSTALL_FILENAME}',
      '!macro customUnInstall'
    ],
    'custom uninstall shortcut script'
  );
  const feishuPrepareScript = readText('scripts/prepare-managed-feishu-skill.cjs');
  includesAll(
    feishuPrepareScript,
    [
      '.lark',
      '.feishu',
      '.larksuite',
      'auth-identity\\.json',
      'auth-users\\.json',
      'enterprise-admin-journal\\.jsonl',
      'session-bindings\\.json',
      'model-profiles\\.json',
      'patchFeishuSharedSkill',
      'ecorex-auth-handoff-v2',
      'prepareFrontendDesignSkill',
      'EcoreX report-grade-layout-v1'
    ],
    'managed Feishu skill preparation denylist'
  );

  const extraResources = Array.isArray(build.extraResources) ? build.extraResources : [];
  const mapResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/cli.js.map');
  const backendResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/claude-code-main');
  assert(!mapResource, 'build config must not package terminal source maps.');
  assert(!backendResource, 'build config must not package the terminal source tree.');
  assert(!JSON.stringify(extraResources).includes('终端源代码'), 'extraResources must not reference the local terminal source directory.');
  const provisioningResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'provisioning');
  assert(provisioningResource, 'managed installer provisioning resource must be configured.');
  assert(provisioningResource.from === 'build/provisioning', 'managed installer provisioning must come from build/provisioning.');
  assertIncludesPatterns(provisioningResource.filter, ['ecorex-provisioning.json', '!**/.env', '!**/.env.*', '!**/*.log'], 'provisioning extraResources.filter');
  const managedSkillResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'managed-skill-packs');
  assert(managedSkillResource, 'bundled managed skill-packs resource must be configured.');
  assert(managedSkillResource.from === 'build/managed-skill-packs', 'bundled managed skill-packs must come from build/managed-skill-packs.');
  assertIncludesPatterns(managedSkillResource.filter, ['agent-skill-creator/**/*', 'frontend-design/**/*', 'lark-cli/**/*', 'officecli/**/*', 'opencli/**/*', 'xin-agent/**/*', '!**/.env', '!**/.env.*', '!**/*.log', '!**/auth-session.json', '!**/auth-identity.json', '!**/auth-users.json', '!**/enterprise-admin-journal.jsonl', '!**/session-bindings.json', '!**/model-profiles.json', '!**/.lark/**/*', '!**/.feishu/**/*', '!**/.larksuite/**/*'], 'managed skill-packs extraResources.filter');
  assertNotIncludesPatterns(managedSkillResource.filter, ['ppt-master/**/*', 'excel-mcp-server/**/*'], 'managed skill-packs retired filters');
  const managedToolResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'managed-tools');
  assert(managedToolResource, 'bundled managed tools resource must be configured.');
  assert(managedToolResource.from === 'build/managed-tools', 'bundled managed tools must come from build/managed-tools.');
  assertIncludesPatterns(managedToolResource.filter, ['lark-cli/**/*', 'officecli/**/*', 'opencli/**/*', 'xin-agent/**/*', '!**/.env', '!**/.env.*', '!**/*.log', '!**/auth-session.json', '!**/auth-identity.json', '!**/auth-users.json', '!**/enterprise-admin-journal.jsonl', '!**/session-bindings.json', '!**/model-profiles.json', '!**/.lark/**/*', '!**/.feishu/**/*', '!**/.larksuite/**/*'], 'managed tools extraResources.filter');
  const playwrightBinResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'app.asar.unpacked/node_modules/.bin');
  assert(playwrightBinResource, 'Playwright CLI wrappers must be copied into unpacked node_modules .bin.');
  assert(playwrightBinResource.from === 'node_modules/.bin', 'Playwright CLI wrappers must come from node_modules/.bin.');
  assertIncludesPatterns(playwrightBinResource.filter, ['playwright*', '!**/.env', '!**/.env.*', '!**/*.log'], 'Playwright CLI wrapper extraResources.filter');
  assertExistingFile(rel('build/managed-skill-packs/lark-cli/.claude-plugin/plugin.json'), 'prepared Feishu skill manifest');
  assertExistingFile(rel('build/managed-skill-packs/lark-cli/skills/lark-shared/SKILL.md'), 'prepared Feishu shared skill');
  includesAll(readText('build/managed-skill-packs/lark-cli/skills/lark-shared/SKILL.md'), ['ecorex-auth-handoff-v2', 'verification_url', 'auth qrcode', '至少 600 秒', 'raw JSON token'], 'prepared Feishu shared skill must include EcoreX authorization handoff rules.');
  assertExistingFile(rel('build/managed-skill-packs/agent-skill-creator/.claude-plugin/plugin.json'), 'prepared Agent Skill Creator manifest');
  assertExistingFile(rel('build/managed-skill-packs/agent-skill-creator/skills/agent-skill-creator/SKILL.md'), 'prepared Agent Skill Creator skill');
  assertExistingFile(rel('build/managed-skill-packs/frontend-design/.claude-plugin/plugin.json'), 'prepared Frontend Design manifest');
  assertExistingFile(rel('build/managed-skill-packs/frontend-design/skills/frontend-design/SKILL.md'), 'prepared Frontend Design skill');
  includesAll(readText('build/managed-skill-packs/frontend-design/skills/frontend-design/SKILL.md'), ['report-grade layouts', 'PPT', 'webpage', 'EcoreX report-grade-layout-v1'], 'prepared Frontend Design skill must include report-grade default routing.');
  assertExistingFile(rel('build/managed-skill-packs/officecli/.claude-plugin/plugin.json'), 'prepared OfficeCLI manifest');
  assertExistingFile(rel('build/managed-skill-packs/officecli/skills/officecli/SKILL.md'), 'prepared OfficeCLI skill');
  assertExistingFile(rel('build/managed-skill-packs/opencli/.claude-plugin/plugin.json'), 'prepared OpenCLI manifest');
  assertExistingFile(rel('build/managed-skill-packs/opencli/skills/opencli-browser/SKILL.md'), 'prepared OpenCLI browser skill');
  assertExistingFile(rel('build/managed-skill-packs/xin-agent/.claude-plugin/plugin.json'), 'prepared Xin Agent manifest');
  assertExistingFile(rel('build/managed-skill-packs/xin-agent/skills/xin-agent/SKILL.md'), 'prepared Xin Agent skill');
  assertExistingFile(rel('build/managed-tools/lark-cli/lark-cli.exe'), 'prepared Feishu CLI executable', 1024 * 1024);
  assertExistingFile(rel('build/managed-tools/officecli/officecli.exe'), 'prepared OfficeCLI executable', 1024 * 1024);
  assertExistingFile(rel('build/managed-tools/opencli/bin/opencli.cmd'), 'prepared OpenCLI command wrapper');
  assertExistingFile(rel('build/managed-tools/opencli/package/dist/src/main.js'), 'prepared OpenCLI runtime entry');
  assertExistingFile(rel('build/managed-tools/opencli/extension/manifest.json'), 'prepared OpenCLI extension manifest');
  assertExistingFile(rel('build/managed-tools/xin-agent/xin-agent-query.cmd'), 'prepared Xin Agent command wrapper');
  assertExistingFile(rel('build/managed-tools/xin-agent/xin-agent-query.js'), 'prepared Xin Agent runtime entry');
});

check('first-party source has no hardcoded model secrets/base URLs', () => {
  const files = firstPartyTextFiles();
  scanTextForSecretsAndHardcodedModelUrls(files, (file) => readText(file), 'source');
});

check('code signing release gate', () => {
  const signing = configuredSigningSources();
  if (!strictReleaseMode) {
    if (!signing.sources.length) {
      warn('code signing certificate is not configured; ordinary local verification allows unsigned builds.');
    }
    return;
  }

  assert(
    signing.sources.length > 0,
    [
      'Strict/release mode requires a Windows code signing certificate configuration.',
      'Configure one of: CSC_LINK + CSC_KEY_PASSWORD, WIN_CSC_LINK + WIN_CSC_KEY_PASSWORD, CSC_NAME/WIN_CSC_NAME, WINDOWS_CERTIFICATE_FILE + WINDOWS_CERTIFICATE_PASSWORD, or build.win.certificateSubjectName/certificateSha1.',
      'No certificate material is required for ordinary npm run verify:production.'
    ].join(' ')
  );
  assert(signing.missing.length === 0, `Strict/release signing configuration is incomplete: ${signing.missing.join(', ')}.`);
});

check('renderer build output exists', () => {
  assert(fs.existsSync(rel('dist', 'index.html')), 'dist/index.html is missing. Run npm run build before production verification.');
  const assetsDir = rel('dist', 'assets');
  assert(fs.existsSync(assetsDir), 'dist/assets is missing. Run npm run build before production verification.');
  const assets = fs.readdirSync(assetsDir);
  assert(assets.some((name) => /\.js$/i.test(name)), 'dist/assets has no JS bundle.');
  assert(assets.some((name) => /\.css$/i.test(name)), 'dist/assets has no CSS bundle.');
});

check('download page points at current installer version', () => {
  const pkg = packageJson();
  const index = readText('landing/ecorex-download/index.html');
  const expected = `EcoreX-Agent-Setup-${pkg.version}.exe`;
  assert(index.includes(expected), `download page must link to current installer ${expected}.`);
});

check('release artifacts are coherent when present', () => {
  const releaseDir = rel('release');
  if (!fs.existsSync(releaseDir)) {
    assert(!requireSignedArtifact, 'release/ is missing; signed release artifact verification cannot run.');
    warn('release/ is absent; installer artifact checks skipped.');
    return;
  }
  const installers = fs
    .readdirSync(releaseDir)
    .filter((name) => /^EcoreX(?: Agent|-Agent)? Setup .+\.exe$/i.test(name) || /^EcoreX-Agent-Setup-.+\.exe$/i.test(name))
    .map((name) => {
      const file = rel('release', name);
      return { name, file, stat: fs.statSync(file) };
    })
    .sort((left, right) => right.stat.mtimeMs - left.stat.mtimeMs || right.stat.size - left.stat.size);
  const installer = installers[0];
  if (!installer) {
    assert(!requireSignedArtifact, 'No release installer exe found under release/; signed release artifact verification cannot run.');
    warn('No release installer exe found under release/.');
  }
  const latestYml = rel('release', 'latest.yml');
  if (!fs.existsSync(latestYml)) {
    assert(!requireSignedArtifact, 'release/latest.yml is missing; signed release artifact verification cannot run.');
    warn('release/latest.yml is absent.');
  } else {
    const latest = fs.readFileSync(latestYml, 'utf8');
    const pkg = packageJson();
    const entry = latestFileEntry(latest);
    const latestPathValue = latestField(latest, 'path');
    const latestSha512 = latestField(latest, 'sha512');
    const latestVersion = latestField(latest, 'version');
    assert(latestVersion === pkg.version, `release/latest.yml version ${latestVersion || '(missing)'} does not match package version ${pkg.version}.`);
    assertPlainReleaseFileName(entry.url, 'files[0].url');
    assertPlainReleaseFileName(latestPathValue, 'path');
    assert(entry.url === latestPathValue, `release/latest.yml files[0].url (${entry.url}) must match path (${latestPathValue}).`);
    assert(installer && installer.name === latestPathValue, `release/latest.yml path points to ${latestPathValue}, but latest installer is ${installer ? installer.name : 'missing'}.`);
    const installerFile = rel('release', latestPathValue);
    assert(fs.existsSync(installerFile), `release/latest.yml path points to missing ${latestPathValue}.`);
    const stat = fs.statSync(installerFile);
    const actualSha512 = sha512Base64(installerFile);
    assert(entry.size === stat.size, `release/latest.yml files[0].size ${entry.size} does not match installer size ${stat.size}.`);
    assert(entry.sha512 === actualSha512, 'release/latest.yml files[0].sha512 does not match installer SHA-512.');
    assert(latestSha512 === actualSha512, 'release/latest.yml top-level sha512 does not match installer SHA-512.');
    const blockmapName = `${latestPathValue}.blockmap`;
    const blockmapFile = rel('release', blockmapName);
    assert(fs.existsSync(blockmapFile), `release blockmap ${blockmapName} is missing.`);
    assert(fs.statSync(blockmapFile).size > 0, `release blockmap ${blockmapName} is empty.`);
    if (requireSignedArtifact) {
      const signature = authenticodeStatus(installerFile);
      assert(signature.status === 'Valid', `release installer must be Authenticode signed in release mode; status=${signature.status || 'unknown'} ${signature.message || ''}`.trim());
    }
  }
  const unpackedExe = rel('release', 'win-unpacked', 'EcoreX Agent.exe');
  const unpackedAsar = rel('release', 'win-unpacked', 'resources', 'app.asar');
  if (fs.existsSync(rel('release', 'win-unpacked'))) {
    assert(fs.existsSync(unpackedExe), 'release/win-unpacked/EcoreX Agent.exe is missing.');
    assert(fs.existsSync(unpackedAsar), 'release/win-unpacked/resources/app.asar is missing.');
    const resourcesDir = rel('release', 'win-unpacked', 'resources');
    const backendRoot = path.join(resourcesDir, 'backend');
    assert(!fs.existsSync(backendRoot), 'release resources must not include terminal source backend files.');
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'lark-cli', '.claude-plugin', 'plugin.json'),
      'release bundled Feishu skill manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'lark-cli', 'skills', 'lark-shared', 'SKILL.md'),
      'release bundled Feishu shared skill'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'agent-skill-creator', '.claude-plugin', 'plugin.json'),
      'release bundled Agent Skill Creator manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'agent-skill-creator', 'skills', 'agent-skill-creator', 'SKILL.md'),
      'release bundled Agent Skill Creator skill'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'frontend-design', '.claude-plugin', 'plugin.json'),
      'release bundled Frontend Design manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'frontend-design', 'skills', 'frontend-design', 'SKILL.md'),
      'release bundled Frontend Design skill'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'officecli', '.claude-plugin', 'plugin.json'),
      'release bundled OfficeCLI manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'officecli', 'skills', 'officecli', 'SKILL.md'),
      'release bundled OfficeCLI skill'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'opencli', '.claude-plugin', 'plugin.json'),
      'release bundled OpenCLI manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-skill-packs', 'opencli', 'skills', 'opencli-browser', 'SKILL.md'),
      'release bundled OpenCLI browser skill'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-tools', 'lark-cli', 'lark-cli.exe'),
      'release bundled Feishu CLI executable',
      1024 * 1024
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-tools', 'officecli', 'officecli.exe'),
      'release bundled OfficeCLI executable',
      1024 * 1024
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-tools', 'opencli', 'bin', 'opencli.cmd'),
      'release bundled OpenCLI command wrapper'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-tools', 'opencli', 'package', 'dist', 'src', 'main.js'),
      'release bundled OpenCLI runtime entry'
    );
    assertExistingFile(
      path.join(resourcesDir, 'managed-tools', 'opencli', 'extension', 'manifest.json'),
      'release bundled OpenCLI extension manifest'
    );
    assertExistingFile(
      path.join(resourcesDir, 'app.asar.unpacked', 'node_modules', 'playwright', 'package.json'),
      'release unpacked Playwright runtime'
    );
    assertExistingFile(
      path.join(resourcesDir, 'app.asar.unpacked', 'node_modules', 'playwright', 'node_modules', 'playwright-core', 'package.json'),
      'release unpacked Playwright core runtime'
    );
    assertExistingFile(
      path.join(resourcesDir, 'app.asar.unpacked', 'node_modules', '.bin', process.platform === 'win32' ? 'playwright.cmd' : 'playwright'),
      'release unpacked Playwright CLI wrapper'
    );
    const unpackedClaudeWrapper = path.join(
      resourcesDir,
      'app.asar.unpacked',
      'node_modules',
      '@anthropic-ai',
      'claude-code',
      'bin',
      'claude.exe'
    );
    assertExistingFile(unpackedClaudeWrapper, 'release unpacked agent runtime wrapper');
    const nativeRuntimeCandidates = [
      path.join(resourcesDir, 'app.asar.unpacked', 'node_modules', '@anthropic-ai', 'claude-code-win32-x64', 'claude.exe'),
      path.join(
        resourcesDir,
        'app.asar.unpacked',
        'node_modules',
        '@anthropic-ai',
        'claude-code',
        'node_modules',
        '@anthropic-ai',
        'claude-code-win32-x64',
        'claude.exe'
      )
    ];
    const nativeRuntime = nativeRuntimeCandidates.find((candidate) => fs.existsSync(candidate));
    assert(nativeRuntime, 'release unpacked agent native runtime executable is missing.');
    assertExistingFile(nativeRuntime, 'release unpacked agent native runtime executable', 1024 * 1024);
  }
});

check('asar package contents when present', () => {
  const asarFiles = findAsarFiles();
  if (!asarFiles.length) {
    warn('No app.asar found under release/; asar content checks skipped.');
    return;
  }
  let asar;
  try {
    asar = require('@electron/asar');
  } catch (error) {
    throw new Error('@electron/asar is unavailable, cannot inspect app.asar.');
  }

  for (const asarFile of asarFiles) {
    const rawEntries = asar.listPackage(asarFile);
    const entryPathByPosix = new Map(
      rawEntries.map((entry) => [toPosix(entry), String(entry).replace(/^[/\\]+/, '')])
    );
    const entries = Array.from(entryPathByPosix.keys());
    for (const required of ['dist/index.html', 'electron/main.cjs', 'electron/preload.cjs', 'package.json']) {
      assert(entries.includes(required), `${path.relative(rootDir, asarFile)} missing ${required}`);
    }
    assertNoSecretOrModelStoragePaths(entries, path.relative(rootDir, asarFile));
    const textEntries = entries.filter((entry) =>
      /^(package\.json|electron\/.*\.cjs|dist\/.*\.(html|js|css))$/i.test(entry)
    );
    scanTextForSecretsAndHardcodedModelUrls(
      textEntries,
      (entry) => asar.extractFile(asarFile, entryPathByPosix.get(entry)).toString('utf8'),
      path.relative(rootDir, asarFile)
    );
  }
});

console.log('\nProduction verification');
for (const name of passes) console.log(`  ok   ${name}`);
for (const message of warnings) console.log(`  skip ${message}`);

if (failures.length) {
  console.error('\nFailures');
  for (const failure of failures) {
    console.error(`  fail ${failure.name}`);
    console.error(`       ${failure.message.replace(/\n/g, '\n       ')}`);
  }
  process.exit(1);
}

console.log(`\nAll ${passes.length} production checks passed.`);

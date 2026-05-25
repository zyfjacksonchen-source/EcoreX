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
  '!**/model-profiles.json',
  '!**/.ecorex-project.json',
  '!**/.ecorex-projects.json',
  '!**/.ecorex-memory/**/*',
  '!**/.mcp.json',
  '!**/.claude/**/*',
  '!**/.codex/**/*',
  '!**/.agents/**/*',
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
    'settings.json',
    '.ecorex-projects.json',
    '.ecorex-project.json',
    '.ecorex-memory',
    'ecorex-agent.log'
  ];
  const forbiddenSegments = ['.claude', '.codex', '.agents', '.mcp', 'superpowers', 'huashu-design', 'huashu_design'];
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
  nodeCheck('scripts/prepare-kkfileview.cjs');
  nodeCheck('scripts/verify-kkfileview-vendor.cjs');
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
      'const LOCAL_AUTH_HASH_ITERATIONS =',
      'function authIdentityPath',
      'function readAuthIdentity',
      'function writeAuthIdentity',
      'function createAuthIdentity',
      'function verifyLocalPassword',
      'crypto.pbkdf2Sync',
      'crypto.timingSafeEqual',
      'setupRequired',
      'authMode: \'local-owner\''
    ],
    'local auth binding'
  );
  assertMatches(main, /if \(!identity\) \{[\s\S]*?identity = createAuthIdentity\(email, password\);[\s\S]*?createdIdentity = true;/, 'first login must bind a local owner identity.');
  assertMatches(main, /else if \(identity\.email !== email \|\| !verifyLocalPassword\(identity, password\)\)/, 'subsequent login must verify bound email and password.');
  assertMatches(main, /if \(loginType === 'code' \|\| payload\.code\)/, 'local mode must not accept arbitrary verification codes.');
  assertMatches(main, /encoding:\s*'safeStorage\/v1'[\s\S]*encryptLocalPayload\(safeIdentity\)/, 'auth identity must be encrypted with safeStorage.');
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
  assertMatches(main, /isDocumentMetadataPreviewExtension\(file\.extension\)[\s\S]*previewMetadataOnly\(file,\s*'metadata-only'\)/, 'PDF and Office files must return metadata-only previews.');
  assertMatches(main, /resolveFilePreviewTarget[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)/, 'file preview must also support explicit selected-file grants.');
  assertMatches(main, /extractPreviewArtifactTargets[\s\S]*cleanPreviewArtifactPath[\s\S]*\[\^\\\\r\\\\n<>/, 'AI artifact path extraction must tolerate generated paths with spaces while staying line bounded.');
  assertMatches(main, /resolveFilePreviewTarget[\s\S]*isRegisteredAgentArtifact\(target,\s*input,\s*workspaceRoot\)[\s\S]*kind:\s*'agent-artifact'/, 'same-session AI artifacts must be previewable without opening arbitrary local files.');
  assertMatches(main, /filePreviewPathLabel[\s\S]*kind === 'agent-artifact'[\s\S]*artifact:\//, 'AI artifact preview labels must not expose full local paths.');
  assertMatches(main, /recordSessionEvent[\s\S]*registerAgentArtifactsFromEvent\(entry,\s*normalized\)/, 'tool events must register generated artifact grants for the current session.');
  assertMatches(main, /if \(!requestedId \|\| !entry\.id \|\| requestedId !== entry\.id\) return false;/, 'selected attachment grants must require the generated attachment id.');
  assertMatches(main, /openSelectedAttachmentFile[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)[\s\S]*openPathSafely\(target\)/, 'uploaded attachments may only be opened locally after a selected-file grant.');
  assertMatches(preload, /openAttachmentFile:\s*\(payload\)\s*=>\s*safeInvoke\('attachment:open-file',\s*withAuth/, 'preload must expose authenticated attachment open IPC.');
  assertNotMatches(main, /function previewFile[\s\S]{0,2400}(shell\.openPath|BrowserWindow|loadURL|executeJavaScript|spawn\()/, 'file preview must not open windows, execute, open, or spawn local artifacts.');
  assertNotMatches(main, /function previewImageFile[\s\S]{0,2500}(shell\.openPath|BrowserWindow|loadURL|executeJavaScript|spawn\()/, 'image preview must stay inside safe read-only IPC.');
});

check('kkFileView sidecar preview engine is local and bounded', () => {
  const main = readText('electron/main.cjs');
  const app = readText('src/App.jsx');
  const pkg = packageJson();
  includesAll(
    main,
    [
      "const KKFILEVIEW_VENDOR_DIR_NAME = 'kkfileview'",
      'const KKFILEVIEW_PREVIEW_MAX_BYTES = 100 * 1024 * 1024',
      'function locateKkFileViewResource',
      'function ensureKkFileViewEngine',
      'function ensureKkFileViewBridgeServer',
      'function handleKkFileViewBridgeRequest',
      'function previewWithKkFileView',
      "renderMode: 'kkfileview'",
      "listen(0, '127.0.0.1'",
      '--server.address=127.0.0.1',
      '--file.upload.disable=true',
      '--pdf.download.disable=true',
      '--media.convert.disable=true',
      "stopKkFileViewEngine('app-quit')"
    ],
    'kkFileView sidecar preview engine'
  );
  assertMatches(main, /createKkFileViewSourceUrl[\s\S]*crypto\.randomBytes\(24\)[\s\S]*expiresAt:\s*Date\.now\(\) \+ KKFILEVIEW_GRANT_TTL_MS/, 'kkFileView source URLs must use expiring random grants.');
  assertMatches(main, /handleKkFileViewBridgeRequest[\s\S]*preview-file\\\/\(\[a-f0-9\]\{32,64\}\)[\s\S]*'Cache-Control': 'no-store'/, 'kkFileView bridge must only serve tokenized preview-file requests with no-store caching.');
  assertMatches(main, /function kkFileViewResourceRoots\(\)[\s\S]*if \(app\.isPackaged\)[\s\S]*process\.resourcesPath[\s\S]*\} else \{[\s\S]*process\.env\.ECOREX_KKFILEVIEW_HOME/, 'packaged kkFileView must use bundled resources instead of env-injected runtime roots.');
  assertMatches(main, /function isKkFileViewRuntimeEnabled\(\)[\s\S]*app\.isPackaged[\s\S]*ECOREX_ENABLE_DEV_KKFILEVIEW/, 'kkFileView runtime must be packaged-only unless dev explicitly opts in.');
  assertMatches(main, /function canUseKkFileViewPreview[\s\S]*isKkFileViewRuntimeEnabled\(\)/, 'document preview must not start kkFileView when the runtime is disabled.');
  assertMatches(main, /safeKkFileViewOutputText[\s\S]*'\/preview-file\/\[REDACTED\]'/, 'kkFileView logs must redact temporary preview tokens.');
  assertMatches(main, /previewFile[\s\S]*previewWithKkFileView\(target,\s*file,\s*stat\)[\s\S]*previewOpenXmlOfficeFile/, 'Office preview must try kkFileView before falling back to OpenXML text extraction.');
  assertMatches(main, /Content-Security-Policy[\s\S]*img-src 'self' data: https: http:\/\/127\.0\.0\.1:\*[\s\S]*media-src 'self' data: blob: https: http:\/\/127\.0\.0\.1:\*[\s\S]*frame-src 'self' http:\/\/127\.0\.0\.1:\*/, 'renderer CSP must allow local preview frames and bounded rich chat media.');
  includesAll(
    app,
    [
      "preview.renderMode === 'kkfileview'",
      'artifact-kkfileview-frame',
      'sandbox="allow-scripts allow-same-origin allow-forms"',
      "payload.type !== 'ecorex-preview-selection'"
    ],
    'kkFileView renderer preview branch'
  );
  const extraResources = Array.isArray(pkg.build?.extraResources) ? pkg.build.extraResources : [];
  const kkResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'kkfileview');
  assert(kkResource, 'kkFileView extraResources entry must be configured.');
  assert(kkResource.from === 'vendor/kkfileview', 'kkFileView extraResources source must be vendor/kkfileview.');
  assertIncludesPatterns(kkResource.filter, ['**/*', '!**/*.log', '!**/tmp/**/*'], 'kkFileView extraResources.filter');
  assertExistingDirectory(rel('vendor/kkfileview'), 'kkFileView vendor placeholder');
  assertExistingFile(rel('vendor/kkfileview/README.md'), 'kkFileView vendor README');
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
      'const attachmentContext = ingestAgentAttachments(payload, { cwd, projectContext })',
      'prompt,',
      'userPrompt,',
      'attachmentContext,',
      'function toolLedgerStartEvent',
      'function toolLedgerFinishEvent',
      'function safeToolLedger',
      'ledger: toolLedgerFinishEvent',
      'function appendRunJournalEntry',
      'function recentUnfinishedRunJournals',
      "handleSafe('attachment:ingest', (_event, payload) => ingestAttachmentsForPreview(payload), { authRequired: true })",
      'unfinishedRuns: recentUnfinishedRunJournals()'
    ],
    'attachment ingestion, ledger and durable run journal'
  );
  assertMatches(main, /resolveAttachmentTarget[\s\S]*isPathInside\(entry\.root,\s*target\)[\s\S]*isRegisteredSelectedAttachment\(target,\s*input\)[\s\S]*pathContainsSymlink\(root\.root,\s*target\)/, 'attachment ingestion must stay inside project/workspace or selected-file grants and reject symlink traversal.');
  assertMatches(main, /ingestAttachmentFromPath[\s\S]*fs\.readFileSync\(target\)[\s\S]*textAttachmentContextFromBuffer|ingestAttachmentFromPath[\s\S]*textAttachmentContextFromBuffer\(buffer,\s*metadata\)/, 'attachment ingestion must read bounded text payloads for the agent prompt.');
  assertMatches(main, /imageAttachmentContextFromBuffer[\s\S]*base64Sample[\s\S]*ATTACHMENT_IMAGE_BASE64_SAMPLE_CHARS/, 'image attachment ingestion must include only bounded base64 summaries.');
  assertMatches(main, /appendRunJournalEntry\(sessionId,\s*entry,\s*'running',\s*\{\s*event:\s*'start'\s*\}\)/, 'run journal must record session start.');
  assertMatches(main, /appendRunJournalEntry\(sessionId,\s*entry,\s*status,[\s\S]*event:\s*'finish'/, 'run journal must record session finish.');
  assertMatches(preload, /ingestAttachments:\s*\(payload\)\s*=>\s*safeInvoke\('attachment:ingest',\s*withAuth\(payload\)\)/, 'preload must expose only authenticated attachment ingestion IPC.');
  assertNotMatches(preload, /readFile|writeFile|openPath|BrowserWindow|shell\./, 'preload must not expose arbitrary filesystem or shell APIs.');
});

check('window default, preload and minimum size guardrails', () => {
  const main = readText('electron/main.cjs');
  includesAll(main, ['function defaultWindowBounds', 'screen.getPrimaryDisplay()', 'width: bounds.width', 'height: bounds.height', 'minWidth: 800', 'minHeight: 600', "backgroundColor: '#070c12'", 'show: true', 'function revealStartupWindow', "revealStartupWindow('created-dark-shell')", "revealStartupWindow('startup-splash-loaded')", 'mainWindow.maximize()', 'function startupBrandIconPath', 'function startupSplashUrlForWindow', 'function startupSplashDataUrl', 'function startStartupPreload', 'function waitForStartupPreload', "startStartupPreload('native-loading')", 'collectBackendStatus(null, { refresh: false })'], 'desktop window sizing and startup preload guardrails');
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
  includesAll(app, ['startupReadyRef', 'const startupWork = Promise.allSettled', 'refreshAuthStatus()', 'refreshBackend()', 'withStartupTimeout(startupWork)', 'window.__ecorexFinishStartup?.()'], 'renderer startup loader waits for auth/backend preload');
  const directStartupFinally = /const startupWork = Promise\.allSettled\(\[[\s\S]*?refreshAuthStatus\(\),[\s\S]*?refreshBackend\(\)[\s\S]*?\]\)\.finally\(\(\) => \{[\s\S]*?window\.__ecorexFinishStartup\?\.\(\);/.test(app);
  const wrappedStartupFinally = /const startupWork = Promise\.allSettled\(\[[\s\S]*?refreshAuthStatus\(\),[\s\S]*?refreshBackend\(\)[\s\S]*?\]\);[\s\S]*?withStartupTimeout\(startupWork\)\.finally\(\(\) => \{[\s\S]*?window\.__ecorexFinishStartup\?\.\(\);/.test(app);
  assert(
    directStartupFinally || wrappedStartupFinally,
    'renderer startup loader must finish after the auth/backend preload promise settles.'
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
      'sessions: getRunningSessionSummaries()',
      'unfinishedRuns: recentUnfinishedRunJournals()',
      "if (status === 'timeout') return 'timeout'",
      'ECOREX_AGENT_SYSTEM_PROMPT',
      'function agentRecoveryHint',
      'recoveryHint: agentRecoveryHint',
      'function defaultCommandCwd',
      'process.resourcesPath',
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
      "'--bare'",
      'claudeSessionId',
      'contextManagement',
      'const CLAUDE_AUTO_ALLOWED_TOOL_SET',
      "'--tools'",
      "'--allowedTools'",
      'CLAUDE_AUTO_ALLOWED_TOOL_SET'
    ],
    'agent runtime guardrails'
  );
  assertMatches(main, /'--output-format',\s*'stream-json',\s*'--verbose'/, 'stream-json output must always be paired with --verbose.');
  assertMatches(main, /'--bare'[\s\S]*'--print'[\s\S]*'--plugin-dir'/, 'agent runtime must run in bare isolated mode while explicitly loading bundled plugins.');
  assertMatches(main, /env:\s*filteredAgentEnv\(\{[\s\S]*isolatedAgentRuntimeEnv\(\)[\s\S]*CLAUDE_CODE_SIMPLE:\s*'1'/, 'agent runtime must isolate config and disable inherited user skills/memory.');
  assertMatches(main, /function runClaudeCommand[\s\S]*env:\s*\{[\s\S]*isolatedAgentRuntimeEnv\(\)/, 'auxiliary backend CLI commands must also use the EcoreX isolated config.');
  assertMatches(main, /const plugins = Array\.isArray\(payload\.plugins\)[\s\S]*isBlockedLocalSkillName\(plugin\)/, 'payload plugin names must reject local user skill packs.');
  assertMatches(main, /parsePluginInventory[\s\S]*isBlockedLocalSkillName\(pluginName\)[\s\S]*isBlockedLocalSkillName\(source\)/, 'backend plugin inventory must exclude blocked local skill pack names.');
  assertMatches(main, /stdio:\s*\['pipe',\s*'pipe',\s*'pipe'\]/, 'agent child process must keep stdin piped.');
  assertMatches(main, /child\.stdin\.end\(`\$\{prompt\}\\n`\)/, 'agent prompt must be written to stdin.');
  assertNotMatches(main, /args\.push\(\s*prompt\s*\)|spawn\([^)]*prompt/s, 'agent prompt must not be passed through process argv.');
  assert(/const profile = store\.profiles\.find\(\(item\) => item\.model === normalizedModel\) \|\| null/.test(main), 'model profile env must only match the selected model exactly.');
  assertMatches(main, /default:\s*\{[\s\S]*?permissionMode:\s*'auto'[\s\S]*?cliMode:\s*'auto'/, 'default permission mode must use Claude auto mode for low-risk tool calls.');
  assertMatches(main, /const CLAUDE_AUTO_ALLOWED_TOOL_SET = 'WebFetch,WebSearch,Task,TodoRead,TodoWrite,mcp__\*'/, 'auto allowed tools must be limited to web and low-risk tools.');
  const autoAllowedTools = (main.match(/const CLAUDE_AUTO_ALLOWED_TOOL_SET = '([^']*)'/)?.[1] || '').split(',').map((tool) => tool.trim()).filter(Boolean);
  for (const forbiddenTool of ['Bash', 'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookRead', 'NotebookEdit', 'Glob', 'Grep', 'LS']) {
    assert(!autoAllowedTools.includes(forbiddenTool), `file and command tool ${forbiddenTool} must not be auto-allowed.`);
  }
  assertMatches(main, /sanitizeClaudeSessionId\(payload\.claudeSessionId \|\| payload\.conversationId,\s*payload\.sessionId\)/, 'Claude session id must be stable for frontend conversations with session fallback.');
  assertMatches(main, /const claudeResumeExistingSession = claudeSessionTranscriptExists\(claudeSessionId\)/, 'Claude session reuse must detect existing CLI transcripts.');
  assertMatches(main, /if \(claudeResumeExistingSession\) \{\s*args\.push\('--resume', claudeSessionId\);\s*\} else \{\s*args\.push\('--session-id', claudeSessionId\);/s, 'Claude CLI must resume existing sessions and only create new sessions with --session-id.');
  assertMatches(main, /entry\.claudeSessionId === requestedClaudeSessionId/, 'parallel starts for the same Claude session must be blocked before spawning.');
  assertMatches(main, /function refreshClaudeSessionTranscriptSeen[\s\S]*findClaudeSessionTranscript\(sessionId\)[\s\S]*claudeTranscriptExistenceCache\.delete\(sessionId\)/, 'Claude resume cache must verify transcript files and clear stale resume state.');
  assertMatches(main, /const finalStatus = code === 0 && !entry\.claudeResultFailed \? 'completed' : 'failed'/, 'Claude result error events must keep the final session status failed even when the process exits cleanly.');
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
  const smoke = readText('scripts/test-model-adapter.cjs');
  includesAll(
    adapter,
    [
      "const DEFAULT_IMAGE_MODEL = 'gpt-image-2'",
      'model: body.model || profile.imageModel || DEFAULT_IMAGE_MODEL',
      'DEFAULT_IMAGE_MODEL',
      'function extractOpenAIText',
      'function parseOpenAIStream',
      'function normalizeOpenAIResponse',
      'text: responseOk ? normalizedText :',
      'stream: Boolean(normalized.stream)'
    ],
    'model adapter defaults and OpenAI-compatible text/stream transform'
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
      "command: 'disabled: local Claude/Codex MCP inventory is intentionally not scanned'",
      "command: 'disabled: local Claude/Codex skill inventory is intentionally not scanned'",
      'defaultEmpty: true',
      'services: []',
      'skills: []',
      'const pluginInventory = parsePluginInventory()',
      "args.push('--plugin-dir', pluginPath)"
    ],
    'default hidden local MCP/skill inventory with bundled backend plugin activation'
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
      "return ['feature-dev', 'code-review', 'security-guidance', 'plugin-dev'];",
      'plugins: selectedPlugins',
      'setSkills([])',
      'setServices([])'
    ],
    'renderer must hide default MCP/Skill lists while keeping backend plugins active'
  );
  const extraResources = Array.isArray(pkg.build?.extraResources) ? pkg.build.extraResources : [];
  const backendResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/claude-code-main');
  assert(backendResource, 'backend claude-code-main resource must be configured.');
  const backendFilter = Array.isArray(backendResource.filter) ? backendResource.filter : [];
  for (const required of ['.claude-plugin/marketplace.json', 'plugins/**/*', ...REQUIRED_BACKEND_FILTER_EXCLUSIONS]) {
    assert(backendFilter.includes(required), `backend plugin packaging filter must include ${required}`);
  }
  assert(!backendFilter.includes('examples/**/*'), 'backend examples must not be packaged.');
  assert(!JSON.stringify(pkg.build).includes('%USERPROFILE%') && !JSON.stringify(pkg.build).includes('${HOME}'), 'build config must not package user home MCP/Skill state.');
  assertMatches(main, /attachDeveloperDiagnostics\([\s\S]*?if \(payload\?\.includeDiagnostics\)/, 'developer diagnostics must only be attached on explicit request.');
  assertMatches(main, /raw:\s*safeOutputText\(/, 'raw bridge output must be sanitized before diagnostics.');
});

check('diagnostics health check UI is complete and static-safe', () => {
  const app = readText('src/App.jsx');
  const css = readText('src/styles.css');
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
  assertMatches(app, /const tabs = \[[\s\S]*\['mcp', 'MCP'[\s\S]*\['skills', 'SKILLS'[\s\S]*\['diagnostics'/, 'system settings must expose MCP, SKILLS, and diagnostics as tabs.');
  assertMatches(app, /async function refreshModelHealth\([\s\S]*?loadModelProfiles\(settings\.defaultModel \|\| 'sonnet'\)/, 'diagnostics model health must use stored profile metadata only.');
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

check('chat state tree and critical front-end affordances', () => {
  const app = readText('src/App.jsx');
  const css = readText('src/styles.css');
  includesAll(
    app,
    [
      'const messageStates = {',
      "cancelled: { label: '已取消'",
      "timeout: { label: '已超时'",
      "const AGENT_EVENT_TERMINAL_KINDS = new Set(['result', 'done', 'error', 'cancelled', 'timeout'])",
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
      'chatMediaKind(safeUrl)',
      'openExternalUrlWithBridge(safeUrl)',
      'function stageTransferredInput',
      'function transferText',
      'function filesFromDataTransfer',
      'onDrop={(event) => stageTransferredInput(event, event.dataTransfer)}'
    ],
    'chat state tree and artifact focus layout'
  );
  includesAll(app, ["replace(/\\bClaude\\s*Code\\s*CLI\\b/gi, 'EcoreX')", "replace(/\\bClaude\\b/gi, 'EcoreX')"], 'assistant-visible product naming sanitizer');
  assert(!app.includes('setRailExpanded((next) => !next)'), 'chat main must not keep the removed quick project right rail toggle.');
  assert(!app.includes('<aside className={`right-rail'), 'chat main must not render the removed quick project right rail.');
  includesAll(app, ['function finalArtifactsFromText', "source: 'assistant-final'", 'finalArtifacts: mergeArtifactReferences', 'message.finalArtifacts || []', 'function isExplicitLocalArtifactPathToken', 'function validateArtifactAvailabilityWithBridge'], 'final deliverable artifact extraction');
  assertMatches(app, /const candidateArtifactReferences = useMemo\([\s\S]{0,620}message\.finalArtifacts \|\| \[\]/, 'artifact preview shelf must use final result artifacts only.');
  assertMatches(app, /validateArtifactAvailabilityWithBridge\(artifact\)[\s\S]{0,320}setAvailableArtifactIds/, 'artifact preview shelf must hide non-local or unavailable artifact references before rendering cards.');
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
      "handleSafe('project:update'",
      "handleSafe('project:archive'",
      "handleSafe('project:delete'",
      'Run cwd must stay inside the current project.'
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
      pkg.scripts['verify:production'].includes('npm run test:model-adapter'),
    'verify:production must be the P0/P1 entry and include agent runtime and model adapter smoke tests.'
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
    pkg.scripts['release:fix-metadata'] === 'node scripts/fix-release-metadata.cjs',
    'release:fix-metadata must run the release metadata repair script.'
  );
  assert(
    pkg.scripts['verify:production:strict'] === 'npm run test:agent-runtime && node scripts/verify-production.cjs --strict && npm run test:model-adapter',
    'verify:production:strict must enable strict release signing gates while staying offline.'
  );
  assert(
    pkg.scripts['verify:production:release'] === 'npm run test:agent-runtime && node scripts/verify-production.cjs --strict --require-signed-artifact && npm run test:model-adapter',
    'verify:production:release must require signed release artifacts after packaging.'
  );
  for (const scriptName of ['pack', 'dist', 'dist:release']) {
    const script = pkg.scripts[scriptName] || '';
    const builderIndex = script.indexOf('electron-builder');
    const fixIndex = script.indexOf('npm run release:fix-metadata');
    const forbiddenReleaseActions = /\bgit\s+(?:push|tag|commit|add)|\bgh\s+release|\bnpm\s+publish|\belectron-builder\s+.*\s--publish\b/i;
    assert(!forbiddenReleaseActions.test(script), `${scriptName} must not upload, publish, tag, or commit artifacts.`);
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
    (pkg.scripts.pack || '').lastIndexOf('npm run verify:production') > (pkg.scripts.pack || '').indexOf('npm run release:fix-metadata'),
    'pack must run ordinary production verification after metadata repair.'
  );
  const build = pkg.build || {};
  assert(build.artifactName === '${productName} Setup ${version}.${ext}', 'build.artifactName must keep installer and latest.yml names aligned.');
  assert(Array.isArray(build.files), 'build.files must be an array.');
  assert(build.files.includes('dist/**/*'), 'build.files must include dist.');
  assert(build.files.includes('electron/**/*'), 'build.files must include electron.');
  assert(build.files.includes('node_modules/@anthropic-ai/claude-code/**/*'), 'build.files must include the packaged agent runtime.');
  assertIncludesPatterns(build.files, REQUIRED_LOCAL_STATE_EXCLUSIONS, 'build.files');
  assert(Array.isArray(build.asarUnpack), 'build.asarUnpack must be an array.');
  assert(build.asarUnpack.includes('node_modules/@anthropic-ai/claude-code/bin/**/*'), 'build.asarUnpack must unpack the agent runtime executable.');
  assertNoSecretOrModelStoragePaths([...(build.files || []), ...(build.extraResources || []).map((item) => item.from || item.to || '')], 'build config');

  const extraResources = Array.isArray(build.extraResources) ? build.extraResources : [];
  const mapResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/cli.js.map');
  assert(mapResource, 'backend cli.js.map resource must be configured.');
  assertRepoRelativePath(mapResource.from, 'backend cli.js.map source');
  assertExistingFile(rel(mapResource.from), 'backend cli.js.map source', 1024 * 1024);

  const backendResource = extraResources.find((item) => String(item.to || '').replace(/\\/g, '/') === 'backend/claude-code-main');
  assert(backendResource, 'backend claude-code-main resource must be configured.');
  assertRepoRelativePath(backendResource.from, 'backend claude-code-main source');
  const backendSource = rel(backendResource.from);
  assertExistingDirectory(backendSource, 'backend claude-code-main source');
  assertIncludesPatterns(backendResource.filter, REQUIRED_BACKEND_FILTER_EXCLUSIONS, 'backend extraResources.filter');
  const marketplacePath = path.join(backendSource, '.claude-plugin', 'marketplace.json');
  assertExistingFile(marketplacePath, 'backend marketplace source');
  const marketplace = parseMarketplace(marketplacePath);
  const pluginByName = new Map(marketplace.plugins.map((plugin) => [plugin.name, plugin]));
  for (const pluginName of REQUIRED_BACKEND_RUNTIME_PLUGINS) {
    const plugin = pluginByName.get(pluginName);
    assert(plugin, `backend marketplace must include ${pluginName}.`);
    const pluginSource = toPosix(plugin.source || `plugins/${pluginName}`).replace(/^\.\//, '');
    const pluginDir = path.resolve(backendSource, pluginSource);
    assert(isPathInside(backendSource, pluginDir), `backend plugin ${pluginName} source must stay inside claude-code-main.`);
    assertExistingDirectory(pluginDir, `backend plugin ${pluginName} source`);
  }
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
    const backendMap = path.join(resourcesDir, 'backend', 'cli.js.map');
    const backendRoot = path.join(resourcesDir, 'backend', 'claude-code-main');
    const backendMarketplace = path.join(backendRoot, '.claude-plugin', 'marketplace.json');
    assertExistingFile(backendMap, 'release backend cli.js.map', 1024 * 1024);
    assertExistingDirectory(backendRoot, 'release backend claude-code-main');
    assertExistingFile(backendMarketplace, 'release backend marketplace');
    const marketplace = parseMarketplace(backendMarketplace);
    const packedPluginByName = new Map(marketplace.plugins.map((plugin) => [plugin.name, plugin]));
    for (const pluginName of REQUIRED_BACKEND_RUNTIME_PLUGINS) {
      const plugin = packedPluginByName.get(pluginName);
      assert(plugin, `release backend marketplace must include ${pluginName}.`);
      const pluginSource = toPosix(plugin.source || `plugins/${pluginName}`).replace(/^\.\//, '');
      const pluginDir = path.resolve(backendRoot, pluginSource);
      assert(isPathInside(backendRoot, pluginDir), `release backend plugin ${pluginName} source must stay inside backend resource.`);
      assertExistingDirectory(pluginDir, `release backend plugin ${pluginName}`);
    }
    const backendTree = listPackageTree(backendRoot);
    assertNoSecretOrModelStoragePaths(backendTree, 'release backend resource');
    assert(!backendTree.some((entry) => /(^|\/)node_modules\//i.test(entry)), 'release backend resource must not include plugin node_modules.');
    assert(!backendTree.some((entry) => /(^|\/)\.git\//i.test(entry)), 'release backend resource must not include .git directories.');
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

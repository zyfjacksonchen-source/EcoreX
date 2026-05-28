const base = require('@playwright/test');
const electronPath = require('electron');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { closeEcorex, launchEcorex, login, repoRoot } = require('./helpers/electron-app.cjs');

const { expect } = base;
const { _electron: electron } = base;

const appUrlPattern = /^http:\/\/127\.0\.0\.1:5188(?:\/|$)/;
const secretEnvKeys = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY',
  'ANTHROPIC_BASE_URL',
  'OPENAI_BASE_URL',
  'ECOREX_REAL_MODEL_API_KEY'
];

const test = base.test.extend({
  ecorex: async ({}, use) => {
    const instance = await launchEcorex();
    try {
      await use(instance);
    } finally {
      await closeEcorex(instance);
    }
  }
});

function relaunchEnv(paths) {
  const noProxy = [...new Set([
    ...String(process.env.NO_PROXY || process.env.no_proxy || '').split(',').map((item) => item.trim()).filter(Boolean),
    '127.0.0.1',
    'localhost'
  ])].join(',');
  const env = {
    ...process.env,
    APPDATA: paths.appData,
    LOCALAPPDATA: paths.localAppData,
    TEMP: paths.temp,
    TMP: paths.temp,
    ECOREX_E2E: '1',
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
    NO_PROXY: noProxy,
    no_proxy: noProxy
  };
  delete env.ELECTRON_RUN_AS_NODE;
  for (const key of secretEnvKeys) delete env[key];
  return env;
}

async function waitForRelaunchedAppWindow(electronApp, timings = {}) {
  const deadline = Date.now() + 30 * 1000;
  let lastUrls = [];

  while (Date.now() < deadline) {
    const windows = electronApp.windows();
    lastUrls = windows.map((page) => page.url());
    for (const page of windows) {
      if (!appUrlPattern.test(page.url())) continue;
      await page.waitForLoadState('domcontentloaded');
      await page.locator('.app-frame').waitFor({ state: 'visible' });
      page.setDefaultTimeout(10 * 1000);
      timings.rendererReadyAt = Date.now();
      if (timings.launchStartedAt) {
        timings.rendererReadyMs = timings.rendererReadyAt - timings.launchStartedAt;
      }
      return page;
    }

    await Promise.race([
      electronApp.waitForEvent('window', { timeout: 500 }).catch(() => null),
      new Promise((resolve) => setTimeout(resolve, 250))
    ]);
  }

  throw new Error(`Timed out waiting for relaunched EcoreX renderer window. Seen windows: ${lastUrls.join(', ') || 'none'}`);
}

async function relaunchEcorexWithPaths(paths) {
  const timings = { launchStartedAt: Date.now() };
  const electronApp = await electron.launch({
    executablePath: electronPath,
    args: ['--no-sandbox', `--user-data-dir=${paths.userData}`, '.'],
    cwd: repoRoot,
    env: relaunchEnv(paths),
    timeout: 45 * 1000
  });
  timings.electronLaunchMs = Date.now() - timings.launchStartedAt;
  const page = await waitForRelaunchedAppWindow(electronApp, timings);
  return { electronApp, page, paths, timings };
}

async function logoutFromProfile(page) {
  await page.locator('.profile-trigger').click();
  await page.locator('.profile-logout').click();
  await expect(page.locator('[data-testid="login-form"], form.login-panel').first()).toBeVisible({ timeout: 10_000 });
}

async function ensureLoggedIn(page, credentials) {
  const shell = page.locator('[data-testid="app-shell"], .app-shell').first();
  const loginForm = page.locator('[data-testid="login-form"], form.login-panel').first();
  const state = await Promise.race([
    shell.waitFor({ state: 'visible', timeout: 20_000 }).then(() => 'shell').catch(() => null),
    loginForm.waitFor({ state: 'visible', timeout: 20_000 }).then(() => 'login').catch(() => null)
  ]);
  if (state === 'login') {
    await login(page, credentials);
    return;
  }
  await expect(shell).toBeVisible({ timeout: 20_000 });
}

async function openDiagnostics(page) {
  await page.locator('.profile-trigger').click();
  await page.locator('.profile-menu-grid').getByRole('button', { name: /系统设置/ }).click();
  await expect(page.locator('[data-testid="system-settings-page"]')).toBeVisible();
  await page.locator('[data-testid="system-settings-tab-diagnostics"]').click();
  await expect(page.locator('[data-testid="diagnostics-page"], .diagnostics-page').first()).toBeVisible();
}

function budgetMs(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

async function measureInteraction(action) {
  const startedAt = Date.now();
  await action();
  return Date.now() - startedAt;
}

async function setAppWindowSize(electronApp, width, height) {
  return electronApp.evaluate(({ BrowserWindow }, size) => {
    const appWindow = BrowserWindow.getAllWindows()
      .find((window) => window.webContents.getURL().startsWith('http://127.0.0.1:5188'));
    if (!appWindow) throw new Error('EcoreX app window was not found.');
    appWindow.setSize(size.width, size.height);
    return appWindow.getBounds();
  }, { width, height });
}

async function appUserDataPath(electronApp) {
  return electronApp.evaluate(({ app }) => app.getPath('userData'));
}

async function writeCrashSummary(electronApp, events) {
  const userData = await appUserDataPath(electronApp);
  fs.mkdirSync(userData, { recursive: true });
  const crashFile = path.join(userData, 'crash-summary.json');
  fs.writeFileSync(
    crashFile,
    `${JSON.stringify({ version: 1, updatedAt: new Date().toISOString(), events }, null, 2)}\n`,
    'utf8'
  );
  return crashFile;
}

async function writeManagedSkillStateWithBom(electronApp, packs) {
  const userData = await appUserDataPath(electronApp);
  const configDir = path.join(userData, 'agent-runtime-config');
  const packRoot = path.join(configDir, 'skill-packs');
  fs.mkdirSync(packRoot, { recursive: true });
  const rows = packs.map((pack) => {
    const installPath = path.join(packRoot, pack.name);
    fs.mkdirSync(installPath, { recursive: true });
    return {
      id: `skillpack-${pack.name}`,
      name: pack.name,
      title: pack.title || pack.name,
      version: pack.version || '1.0.0',
      description: pack.description || `${pack.name} E2E capability`,
      category: pack.category || 'EcoreX',
      enabled: pack.enabled !== false,
      installed: true,
      installPath,
      sourcePath: pack.sourcePath || installPath,
      sourceKind: pack.sourceKind || 'plugin',
      generatedWrapper: Boolean(pack.generatedWrapper),
      mcpConfig: pack.mcpConfig || null,
      installedAt: '2026-05-26T00:00:00.000Z',
      lastUpdated: '2026-05-26T00:00:00.000Z'
    };
  });
  fs.writeFileSync(
    path.join(configDir, 'skill-packs.json'),
    Buffer.concat([
      Buffer.from([0xef, 0xbb, 0xbf]),
      Buffer.from(`${JSON.stringify(rows, null, 2)}\n`, 'utf8')
    ])
  );
  return { userData, rows };
}

async function startChatMediaServer() {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lS5nWQAAAABJRU5ErkJggg==', 'base64');
  const server = http.createServer((request, response) => {
    if (request.url === '/creative.png') {
      response.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'no-store' });
      response.end(png);
      return;
    }
    if (request.url === '/demo.mp4') {
      response.writeHead(200, { 'content-type': 'video/mp4', 'cache-control': 'no-store' });
      response.end(Buffer.from([0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109, 0, 0, 2, 0]));
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    server,
    baseUrl: `http://127.0.0.1:${server.address().port}`
  };
}

const crc32Table = new Uint32Array(256);
for (let index = 0; index < 256; index += 1) {
  let value = index;
  for (let bit = 0; bit < 8; bit += 1) {
    value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
  }
  crc32Table[index] = value >>> 0;
}

function crc32(buffer) {
  let value = 0xffffffff;
  for (const byte of buffer) value = crc32Table[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function zipStored(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  const dosTime = 0;
  const dosDate = ((2026 - 1980) << 9) | (1 << 5) | 1;

  for (const entry of entries) {
    const nameBuffer = Buffer.from(entry.name, 'utf8');
    const dataBuffer = Buffer.isBuffer(entry.data) ? entry.data : Buffer.from(String(entry.data || ''), 'utf8');
    const checksum = crc32(dataBuffer);

    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(0x04034b50, 0);
    localHeader.writeUInt16LE(20, 4);
    localHeader.writeUInt16LE(0, 6);
    localHeader.writeUInt16LE(0, 8);
    localHeader.writeUInt16LE(dosTime, 10);
    localHeader.writeUInt16LE(dosDate, 12);
    localHeader.writeUInt32LE(checksum, 14);
    localHeader.writeUInt32LE(dataBuffer.length, 18);
    localHeader.writeUInt32LE(dataBuffer.length, 22);
    localHeader.writeUInt16LE(nameBuffer.length, 26);
    localHeader.writeUInt16LE(0, 28);
    localParts.push(localHeader, nameBuffer, dataBuffer);

    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(0x02014b50, 0);
    centralHeader.writeUInt16LE(20, 4);
    centralHeader.writeUInt16LE(20, 6);
    centralHeader.writeUInt16LE(0, 8);
    centralHeader.writeUInt16LE(0, 10);
    centralHeader.writeUInt16LE(dosTime, 12);
    centralHeader.writeUInt16LE(dosDate, 14);
    centralHeader.writeUInt32LE(checksum, 16);
    centralHeader.writeUInt32LE(dataBuffer.length, 20);
    centralHeader.writeUInt32LE(dataBuffer.length, 24);
    centralHeader.writeUInt16LE(nameBuffer.length, 28);
    centralHeader.writeUInt16LE(0, 30);
    centralHeader.writeUInt16LE(0, 32);
    centralHeader.writeUInt16LE(0, 34);
    centralHeader.writeUInt16LE(0, 36);
    centralHeader.writeUInt32LE(0, 38);
    centralHeader.writeUInt32LE(offset, 42);
    centralParts.push(centralHeader, nameBuffer);

    offset += localHeader.length + nameBuffer.length + dataBuffer.length;
  }

  const centralOffset = offset;
  const centralSize = centralParts.reduce((total, part) => total + part.length, 0);
  const endRecord = Buffer.alloc(22);
  endRecord.writeUInt32LE(0x06054b50, 0);
  endRecord.writeUInt16LE(0, 4);
  endRecord.writeUInt16LE(0, 6);
  endRecord.writeUInt16LE(entries.length, 8);
  endRecord.writeUInt16LE(entries.length, 10);
  endRecord.writeUInt32LE(centralSize, 12);
  endRecord.writeUInt32LE(centralOffset, 16);
  endRecord.writeUInt16LE(0, 20);
  return Buffer.concat([...localParts, ...centralParts, endRecord]);
}

function createDocxFixture() {
  return zipStored([
    {
      name: '[Content_Types].xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    },
    {
      name: '_rels/.rels',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    },
    {
      name: 'word/document.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>EcoreX docx preview fixture</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>'
    }
  ]);
}

function createXlsxFixture() {
  return zipStored([
    {
      name: '[Content_Types].xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'
    },
    {
      name: '_rels/.rels',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    },
    {
      name: 'xl/workbook.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Forecast" sheetId="1" r:id="rId1"/></sheets></workbook>'
    },
    {
      name: 'xl/_rels/workbook.xml.rels',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'
    },
    {
      name: 'xl/styles.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    },
    {
      name: 'xl/worksheets/sheet1.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:C3"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Channel</t></is></c><c r="B1" t="inlineStr"><is><t>Revenue</t></is></c><c r="C1" t="inlineStr"><is><t>Confidence</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Search</t></is></c><c r="B2"><v>128000</v></c><c r="C2"><v>0.91</v></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Social</t></is></c><c r="B3"><v>96000</v></c><c r="C3"><v>0.86</v></c></row></sheetData></worksheet>'
    }
  ]);
}

function createPptxFixture() {
  return zipStored([
    {
      name: '[Content_Types].xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>'
    },
    {
      name: '_rels/.rels',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>'
    },
    {
      name: 'ppt/presentation.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="5143500"/></p:presentation>'
    },
    {
      name: 'ppt/_rels/presentation.xml.rels',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>'
    },
    {
      name: 'ppt/slides/slide1.xml',
      data: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp><p:nvSpPr/><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>EcoreX pptx preview fixture</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    }
  ]);
}

function writeSupportedPreviewFixtures(root) {
  fs.mkdirSync(root, { recursive: true });
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lS5nWQAAAABJRU5ErkJggg==', 'base64');
  const files = [
    ['sample.txt', 'EcoreX text preview fixture\n'],
    ['sample.md', '# EcoreX markdown preview fixture\n'],
    ['sample.html', '<main><h1>EcoreX html preview fixture</h1></main>'],
    ['sample.json', JSON.stringify({ fixture: 'EcoreX json preview fixture', ok: true }, null, 2)],
    ['sample.csv', 'channel,revenue\nsearch,128000\nsocial,96000\n'],
    ['sample.png', png],
    ['sample.svg', '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><rect width="12" height="12" fill="#ff5a00"/></svg>'],
    ['sample.pdf', '%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n'],
    ['sample.docx', createDocxFixture()],
    ['sample.xlsx', createXlsxFixture()],
    ['sample.pptx', createPptxFixture()]
  ];
  return files.map(([name, data]) => {
    const target = path.join(root, name);
    fs.writeFileSync(target, data);
    return { name, path: target.replace(/\\/g, '/') };
  });
}

function samePath(left, right) {
  const resolvedLeft = path.resolve(left);
  const resolvedRight = path.resolve(right);
  if (process.platform === 'win32') {
    return resolvedLeft.toLowerCase() === resolvedRight.toLowerCase();
  }
  return resolvedLeft === resolvedRight;
}

function installStartupLoaderProbe() {
  const probe = {
    authStarted: false,
    authDone: false,
    backendStarted: false,
    backendDone: false,
    authWrapped: false,
    backendWrapped: false,
    finishCalls: 0,
    finishBeforeBothSettled: false,
    loaderVisibleDuringPending: false,
    loaderVisibleAtFinish: false,
    readyDatasetAtFinish: false,
    finishAt: 0,
    patchErrors: []
  };
  window.__ecorexStartupProbe = probe;

  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const startupLoaderVisible = () => {
    const loader = document.getElementById('startup-loader');
    if (!loader) return false;
    const style = window.getComputedStyle(loader);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };

  let assignedFinishStartup;
  Object.defineProperty(window, '__ecorexFinishStartup', {
    configurable: true,
    get() {
      return assignedFinishStartup;
    },
    set(value) {
      if (typeof value !== 'function') {
        assignedFinishStartup = value;
        return;
      }
      assignedFinishStartup = function wrappedFinishStartup(...args) {
        probe.finishCalls += 1;
        if (probe.authWrapped && probe.backendWrapped) {
          probe.finishBeforeBothSettled = probe.finishBeforeBothSettled || !(probe.authDone && probe.backendDone);
        }
        probe.loaderVisibleAtFinish = startupLoaderVisible();
        probe.readyDatasetAtFinish = document.documentElement.dataset.ecorexReady === 'true';
        probe.finishAt = Date.now();
        return value.apply(this, args);
      };
    }
  });

  function wrapStartupMethod(name, startedKey, doneKey, delayMs) {
    const original = window.ecorex?.[name];
    if (typeof original !== 'function') return;
    try {
      window.ecorex[name] = async function wrappedStartupMethod(...args) {
        probe[startedKey] = true;
        probe.loaderVisibleDuringPending = probe.loaderVisibleDuringPending || startupLoaderVisible();
        await delay(delayMs);
        probe.loaderVisibleDuringPending = probe.loaderVisibleDuringPending || startupLoaderVisible();
        try {
          return await original.apply(this, args);
        } finally {
          probe[doneKey] = true;
        }
      };
      probe[`${name === 'getAuthStatus' ? 'auth' : 'backend'}Wrapped`] = window.ecorex[name] !== original;
    } catch (error) {
      probe.patchErrors.push(`${name}: ${error?.message || error}`);
    }
  }

  wrapStartupMethod('getAuthStatus', 'authStarted', 'authDone', 260);
  wrapStartupMethod('getBackendStatus', 'backendStarted', 'backendDone', 520);
}

test.describe('EcoreX Agent Electron E2E', () => {
  test('starts the Electron app and shows the login form @responsive', async ({ ecorex }) => {
    const { electronApp, page, timings } = ecorex;

    await expect(page.locator('.app-frame')).toBeVisible();
    expect(timings.rendererReadyMs).toBeLessThan(budgetMs('ECOREX_E2E_STARTUP_BUDGET_MS', 30_000));
    await expect(page.locator('.titlebar-title')).toHaveText('EcoreX 亦芯');
    await expect(page.locator('[data-testid="login-form"], form.login-panel').first()).toBeVisible();
    await expect(page.getByRole('button', { name: '密码登录' })).toBeVisible();
    await expect(page.locator('[data-testid="login-email-input"]').first()).toBeVisible();
    await expect(page.locator('[data-testid="login-secret-input"]').first()).toBeVisible();

    const windowTitle = await electronApp.evaluate(({ BrowserWindow }) => {
      const appWindow = BrowserWindow.getAllWindows()
        .find((window) => window.webContents.getURL().startsWith('http://127.0.0.1:5188'));
      return appWindow?.getTitle() || '';
    });
    expect(windowTitle).toContain('EcoreX');

    const isMaximized = await electronApp.evaluate(({ BrowserWindow }) => {
      const appWindow = BrowserWindow.getAllWindows()
        .find((window) => window.webContents.getURL().startsWith('http://127.0.0.1:5188'));
      return appWindow?.isMaximized() || false;
    });
    expect(isMaximized).toBe(true);
  });

  test('keeps the startup loader until auth and backend preload settle', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;

    await electronApp.evaluate(({ ipcMain }) => {
      global.__ecorexStartupIpcProbe = {
        authStartedAt: 0,
        authDoneAt: 0,
        backendStartedAt: 0,
        backendDoneAt: 0
      };
      const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      ipcMain.removeHandler('auth:status');
      ipcMain.handle('auth:status', async () => {
        global.__ecorexStartupIpcProbe.authStartedAt = Date.now();
        await delay(260);
        global.__ecorexStartupIpcProbe.authDoneAt = Date.now();
        return { ok: true, loggedIn: false, setupRequired: true, authMode: 'local-owner' };
      });
      ipcMain.removeHandler('backend:status');
      ipcMain.handle('backend:status', async () => {
        global.__ecorexStartupIpcProbe.backendStartedAt = Date.now();
        await delay(520);
        global.__ecorexStartupIpcProbe.backendDoneAt = Date.now();
        return { ok: true, ready: true, mode: 'e2e-startup-probe' };
      });
    });

    await page.addInitScript(installStartupLoaderProbe);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('.app-frame')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('[data-testid="login-form"], form.login-panel').first()).toBeVisible({ timeout: 20_000 });
    await expect.poll(() => page.evaluate(() => window.__ecorexStartupProbe?.finishCalls || 0)).toBeGreaterThan(0);
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.ecorexReady)).toBe('true');

    const probe = await page.evaluate(() => window.__ecorexStartupProbe);
    const ipcProbe = await electronApp.evaluate(() => global.__ecorexStartupIpcProbe);
    expect(probe.patchErrors).toEqual([]);
    expect(ipcProbe.authStartedAt).toBeGreaterThan(0);
    expect(ipcProbe.backendStartedAt).toBeGreaterThan(0);
    expect(ipcProbe.authDoneAt).toBeGreaterThanOrEqual(ipcProbe.authStartedAt);
    expect(ipcProbe.backendDoneAt).toBeGreaterThanOrEqual(ipcProbe.backendStartedAt);
    expect(probe.finishAt).toBeGreaterThanOrEqual(ipcProbe.authDoneAt);
    expect(probe.finishAt).toBeGreaterThanOrEqual(ipcProbe.backendDoneAt);
    expect(probe.loaderVisibleAtFinish).toBe(true);
    expect(probe.readyDatasetAtFinish).toBe(false);
    expect(probe.finishBeforeBothSettled).toBe(false);
  });

  test('opens diagnostics from personal system settings and shows the health check area', async ({ ecorex }) => {
    const { page } = ecorex;
    await login(page);

    await expect(page.locator('.side-nav').getByText(/MCP|SKILLS|系统设置|诊断/)).toHaveCount(0);
    await openDiagnostics(page);

    await expect(page.getByRole('heading', { name: /系统设置/ })).toBeVisible();
    const mcpTab = page.locator('[data-testid="system-settings-tab-mcp"]');
    const skillsTab = page.locator('[data-testid="system-settings-tab-skills"]');
    const diagnosticsTab = page.locator('[data-testid="system-settings-tab-diagnostics"]');
    await expect(mcpTab).toContainText('MCP');
    await expect(skillsTab).toContainText('SKILLS');
    await expect(diagnosticsTab).toContainText(/诊断/);
    await mcpTab.click();
    await expect(mcpTab).toHaveAttribute('aria-selected', 'true');
    await skillsTab.click();
    await expect(skillsTab).toHaveAttribute('aria-selected', 'true');
    await diagnosticsTab.click();
    await expect(diagnosticsTab).toHaveAttribute('aria-selected', 'true');
    const healthPanel = page.locator('.health-check-panel');
    await expect(healthPanel).toBeVisible();
    await expect(healthPanel.getByText('可发布前检查')).toBeVisible();
    await expect(healthPanel.getByText('本地构建', { exact: true })).toBeVisible();
    await expect(healthPanel.getByText('Agent 引擎', { exact: true })).toBeVisible();
    await expect(healthPanel.getByText('模型配置', { exact: true })).toBeVisible();
  });

  test('manages local users, roles and enterprise actions from system settings', async ({ ecorex }) => {
    const { page } = ecorex;
    await login(page);

    await page.locator('.profile-trigger').click();
    await page.locator('.profile-menu-grid').getByRole('button', { name: /系统设置/ }).click();
    await page.locator('[data-testid="system-settings-tab-users"]').click();
    await expect(page.locator('[data-testid="enterprise-users-page"]')).toBeVisible();
    await expect(page.locator('[data-testid="users-admin-panel"]')).toBeVisible();

    const ownerResult = await page.evaluate(async () => {
      const listBefore = await window.ecorex.listUsers();
      const profile = await window.ecorex.updateProfile({ displayName: 'E2E Owner', title: '超级管理员', team: 'QA' });
      const admin = await window.ecorex.createUser({
        email: 'e2e.admin@ecorex.local',
        displayName: 'E2E Admin',
        role: 'admin',
        password: 'EcoreX123!'
      });
      const member = await window.ecorex.createUser({
        email: 'e2e.member@ecorex.local',
        displayName: 'E2E Member',
        role: 'user',
        password: 'EcoreX123!'
      });
      const enterprise = await window.ecorex.runEnterpriseAction({ action: 'syncMcp', summary: 'E2E sync' });
      const listAfter = await window.ecorex.listUsers();
      return { listBefore, profile, admin, member, enterprise, listAfter };
    });

    expect(ownerResult.listBefore.users.some((user) => user.role === 'super_admin')).toBe(true);
    expect(ownerResult.profile.ok).toBe(true);
    expect(ownerResult.admin.ok).toBe(true);
    expect(ownerResult.member.ok).toBe(true);
    expect(ownerResult.enterprise.ok).toBe(true);
    expect(ownerResult.listAfter.canManageUsers).toBe(true);
    expect(ownerResult.listAfter.users.some((user) => user.email === 'e2e.admin@ecorex.local' && user.role === 'admin')).toBe(true);

    await page.evaluate(async () => {
      await window.ecorex.authLogout();
      window.dispatchEvent(new Event('ecorex:auth-updated'));
    });
    await expect(page.locator('[data-testid="login-form"], form.login-panel').first()).toBeVisible({ timeout: 10_000 });
    await login(page, { email: 'e2e.member@ecorex.local', password: 'EcoreX123!' });

    const memberResult = await page.evaluate(async () => {
      const listed = await window.ecorex.listUsers();
      const ownProfile = await window.ecorex.updateProfile({ displayName: 'Member Self Update' });
      const forbiddenUser = await window.ecorex.createUser({
        email: 'blocked@ecorex.local',
        role: 'admin',
        password: 'EcoreX123!'
      });
      const forbiddenEnterprise = await window.ecorex.runEnterpriseAction({ action: 'pushSkill', summary: 'blocked' });
      return { listed, ownProfile, forbiddenUser, forbiddenEnterprise };
    });

    expect(memberResult.listed.ok).toBe(true);
    expect(memberResult.listed.canManageUsers).toBe(false);
    expect(memberResult.listed.users).toHaveLength(1);
    expect(memberResult.listed.users[0].email).toBe('e2e.member@ecorex.local');
    expect(memberResult.ownProfile.ok).toBe(true);
    expect(memberResult.forbiddenUser.forbidden || memberResult.forbiddenUser.ok === false).toBeTruthy();
    expect(memberResult.forbiddenEnterprise.forbidden || memberResult.forbiddenEnterprise.ok === false).toBeTruthy();
  });

  test('allows a created local account to reopen persisted chats and artifacts after relaunch', async ({ ecorex }) => {
    let { electronApp, page, paths } = ecorex;
    await login(page);

    const credentials = {
      email: `e2e.persist.${Date.now()}@ecorex.local`,
      password: 'EcoreX123!'
    };
    const created = await page.evaluate(async ({ email, password }) => window.ecorex.createUser({
      email,
      displayName: 'E2E Persisted Member',
      role: 'user',
      password
    }), credentials);
    expect(created.ok).toBe(true);
    expect(created.user.email).toBe(credentials.email);

    await logoutFromProfile(page);
    await login(page, credentials);
    const memberStatus = await page.evaluate(async () => window.ecorex.getAuthStatus());
    expect(memberStatus.loggedIn).toBe(true);
    expect(memberStatus.email || memberStatus.user?.email).toBe(credentials.email);
    expect(memberStatus.role || memberStatus.user?.role).toBe('user');

    const userData = await appUserDataPath(electronApp);
    const artifactName = 'persisted-report.md';
    const artifactPath = path.join(userData, 'workspace', 'persistent-artifacts', artifactName);
    fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
    fs.writeFileSync(artifactPath, '# Persistent artifact\nsaved after relaunch\n', 'utf8');
    const normalizedArtifactPath = artifactPath.replace(/\\/g, '/');
    const prompt = 'persist artifact relaunch';
    const assistantText = 'Persistent artifact result';

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, payload) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, runPayload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: runPayload.sessionId,
            events: [
              {
                sessionId: runPayload.sessionId,
                kind: 'result',
                status: 'completed',
                text: `${payload.assistantText}\n- [${payload.artifactName}](${payload.artifactPath})`
              },
              { sessionId: runPayload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: runPayload.sessionId,
          initialEvent: { sessionId: runPayload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    }, { artifactName, artifactPath: normalizedArtifactPath, assistantText });

    await page.locator('[data-testid="chat-input"]').fill(prompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: prompt }).first()).toBeVisible();
    await expect(page.locator('.assistant-card').filter({ hasText: assistantText }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="artifact-file-card"], .artifact-thumb-card').filter({ hasText: artifactName }).first()).toBeVisible({ timeout: 15_000 });

    await expect.poll(async () => page.evaluate(({ prompt, assistantText, artifactName }) => {
      const recent = JSON.parse(localStorage.getItem('ecorex-recent-chats') || '[]');
      const conversations = JSON.parse(localStorage.getItem('ecorex-chat-conversations') || '{}');
      const row = recent.find((item) => String(item.title || '').includes(prompt));
      const state = row ? conversations[row.id] : null;
      return Boolean(
        row
        && state
        && (state.messages || []).some((message) => message.role === 'user' && String(message.text || '').includes(prompt))
        && (state.messages || []).some((message) =>
          message.role === 'assistant'
          && String(message.text || '').includes(assistantText)
          && (message.finalArtifacts || []).some((artifact) => String(artifact.name || '').includes(artifactName))
        )
      );
    }, { prompt, assistantText, artifactName }), { timeout: 10_000 }).toBe(true);

    await ecorex.electronApp.close();
    const relaunched = await relaunchEcorexWithPaths(paths);
    ecorex.electronApp = relaunched.electronApp;
    ecorex.page = relaunched.page;
    electronApp = relaunched.electronApp;
    page = relaunched.page;

    await ensureLoggedIn(page, credentials);
    const restartedStatus = await page.evaluate(async () => window.ecorex.getAuthStatus());
    expect(restartedStatus.loggedIn).toBe(true);
    expect(restartedStatus.email || restartedStatus.user?.email).toBe(credentials.email);

    const recentRow = page.locator('.recent-row').filter({ hasText: prompt }).first();
    await expect(recentRow).toBeVisible({ timeout: 15_000 });
    await recentRow.locator('.recent-open').click();
    await expect(page.locator('.user-bubble').filter({ hasText: prompt }).first()).toBeVisible();
    await expect(page.locator('.assistant-card').filter({ hasText: assistantText }).first()).toBeVisible();

    const restoredArtifact = page.locator('[data-testid="artifact-file-card"], .artifact-thumb-card').filter({ hasText: artifactName }).first();
    await expect(restoredArtifact).toBeVisible({ timeout: 15_000 });
    await restoredArtifact.locator('[data-testid="artifact-file-open"], .artifact-thumb-open').first().click();
    await expect(page.locator('.chat-layout')).toHaveClass(/preview-focus/);
    await expect(page.locator('.artifact-focus-panel')).toContainText(artifactName);
    await expect(page.locator('.artifact-focus-panel .artifact-text-preview')).toContainText('saved after relaunch');
  });

  test('installs and toggles managed SKILLS through super administrator push', async ({ ecorex }) => {
    const { page, paths } = ecorex;
    await login(page);

    const skillSource = path.join(paths.root, 'e2e-managed-skill-source');
    const skillDir = path.join(skillSource, 'skills', 'e2e-managed-skill');
    fs.mkdirSync(path.join(skillSource, '.claude-plugin'), { recursive: true });
    fs.mkdirSync(skillDir, { recursive: true });
    fs.writeFileSync(path.join(skillSource, '.claude-plugin', 'plugin.json'), JSON.stringify({
      name: 'e2e-managed-skill-pack',
      description: 'E2E managed skill pack',
      version: '1.0.0',
      skills: './skills'
    }, null, 2), 'utf8');
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), [
      '---',
      'name: e2e-managed-skill',
      'description: E2E managed skill.',
      '---',
      '',
      '# E2E Managed Skill',
      '',
      'Use this skill to verify EcoreX managed skill push.'
    ].join('\n'), 'utf8');

    const result = await page.evaluate(async (sourcePath) => {
      const pushed = await window.ecorex.runEnterpriseAction({
        action: 'pushSkill',
        sourcePaths: [sourcePath],
        summary: 'E2E push managed skill'
      });
      const listedAfterPush = await window.ecorex.listSkills({ refresh: true });
      const skill = listedAfterPush.skills.find((item) => item.name === 'e2e-managed-skill-pack');
      const childSkill = listedAfterPush.childSkills.find((item) => item.name === 'e2e-managed-skill');
      const disabled = await window.ecorex.disableSkill({ id: skill?.id, name: skill?.name });
      const listedAfterDisable = await window.ecorex.listSkills({ refresh: true });
      const enabled = await window.ecorex.enableSkill({ id: skill?.id, name: skill?.name });
      const listedAfterEnable = await window.ecorex.listSkills({ refresh: true });
      return { pushed, listedAfterPush, skill, childSkill, disabled, listedAfterDisable, enabled, listedAfterEnable };
    }, skillSource);

    expect(result.pushed.ok).toBe(true);
    expect(result.pushed.installedSkills.some((item) => item.name === 'e2e-managed-skill-pack')).toBe(true);
    expect(result.skill?.name).toBe('e2e-managed-skill-pack');
    expect(result.skill?.installed).toBe(true);
    expect(result.skill?.enabled).toBe(true);
    expect(result.skill?.skillCount).toBe(1);
    expect(result.skill?.childSkillCount).toBe(1);
    expect(result.childSkill?.name).toBe('e2e-managed-skill');
    expect(result.disabled.ok).toBe(true);
    expect(result.listedAfterDisable.skills.find((item) => item.name === 'e2e-managed-skill-pack')?.enabled).toBe(false);
    expect(result.enabled.ok).toBe(true);
    expect(result.listedAfterEnable.skills.find((item) => item.name === 'e2e-managed-skill-pack')?.enabled).toBe(true);
  });

  test('installs skill collection directories as one managed SKILLS package', async ({ ecorex }) => {
    const { page, paths } = ecorex;
    await login(page);

    const collectionSource = path.join(paths.root, 'e2e-skill-collection-source');
    const firstSkillDir = path.join(collectionSource, 'skills', 'e2e-collection-alpha');
    const secondSkillDir = path.join(collectionSource, 'skills', 'e2e-collection-beta');
    fs.mkdirSync(firstSkillDir, { recursive: true });
    fs.mkdirSync(secondSkillDir, { recursive: true });
    fs.writeFileSync(path.join(collectionSource, 'package.json'), JSON.stringify({
      name: '@ecorex/e2e-collection',
      version: '1.2.3',
      description: 'E2E managed skill collection'
    }, null, 2), 'utf8');
    fs.writeFileSync(path.join(firstSkillDir, 'SKILL.md'), [
      '---',
      'name: e2e-collection-alpha',
      'description: E2E collection alpha skill.',
      '---',
      '',
      '# E2E Collection Alpha'
    ].join('\n'), 'utf8');
    fs.writeFileSync(path.join(secondSkillDir, 'SKILL.md'), [
      '---',
      'name: e2e-collection-beta',
      'description: E2E collection beta skill.',
      '---',
      '',
      '# E2E Collection Beta'
    ].join('\n'), 'utf8');

    const result = await page.evaluate(async (sourcePath) => {
      const pushed = await window.ecorex.runEnterpriseAction({
        action: 'pushSkill',
        sourcePaths: [sourcePath],
        summary: 'E2E push managed skill collection'
      });
      const listedAfterPush = await window.ecorex.listSkills({ refresh: true });
      const collection = listedAfterPush.skills.find((item) => item.name === 'ecorex-e2e-collection');
      const alpha = listedAfterPush.childSkills.find((item) => item.name === 'e2e-collection-alpha');
      const beta = listedAfterPush.childSkills.find((item) => item.name === 'e2e-collection-beta');
      const disabled = await window.ecorex.disableSkill({ id: pushed.installedSkills?.[0]?.id });
      const listedAfterDisable = await window.ecorex.listSkills({ refresh: true });
      const enabled = await window.ecorex.enableSkill({ id: pushed.installedSkills?.[0]?.id });
      const listedAfterEnable = await window.ecorex.listSkills({ refresh: true });
      return { pushed, listedAfterPush, collection, alpha, beta, disabled, listedAfterDisable, enabled, listedAfterEnable };
    }, collectionSource);

    expect(result.pushed.ok).toBe(true);
    expect(result.pushed.installedSkills.some((item) => item.name === 'ecorex-e2e-collection')).toBe(true);
    expect(result.collection?.installed).toBe(true);
    expect(result.collection?.skillCount).toBe(1);
    expect(result.collection?.childSkillCount).toBe(2);
    expect(result.alpha?.installed).toBe(true);
    expect(result.beta?.installed).toBe(true);
    expect(result.disabled.ok).toBe(true);
    expect(result.listedAfterDisable.skills.find((item) => item.name === 'ecorex-e2e-collection')?.enabled).toBe(false);
    expect(result.enabled.ok).toBe(true);
    expect(result.listedAfterEnable.skills.find((item) => item.name === 'ecorex-e2e-collection')?.enabled).toBe(true);
  });

  test('surfaces MCP-backed managed skills in the MCP manager', async ({ ecorex }) => {
    const { page, paths } = ecorex;
    await login(page);

    const mcpSource = path.join(paths.root, 'e2e-mcp-backed-skill');
    fs.mkdirSync(mcpSource, { recursive: true });
    fs.writeFileSync(path.join(mcpSource, 'manifest.json'), JSON.stringify({
      name: 'e2e-excel-mcp',
      version: '1.0.0',
      description: 'E2E Excel MCP server',
      server: {
        mcp_config: {
          command: 'uvx',
          args: ['e2e-excel-mcp', 'stdio']
        }
      },
      tools: [{ name: 'workbook_read' }, { name: 'workbook_write' }]
    }, null, 2), 'utf8');

    const result = await page.evaluate(async (sourcePath) => {
      const pushed = await window.ecorex.runEnterpriseAction({
        action: 'pushSkill',
        sourcePaths: [sourcePath],
        summary: 'E2E push MCP-backed managed skill'
      });
      const skills = await window.ecorex.listSkills({ refresh: true });
      const mcp = await window.ecorex.listMcpServers({ refresh: true });
      return { pushed, skills, mcp };
    }, mcpSource);

    expect(result.pushed.ok).toBe(true);
    expect(result.skills.skills.find((item) => item.name === 'e2e-excel-mcp')?.sourceKind).toBe('mcp-wrapper');
    expect(result.mcp.ok).toBe(true);
    expect(result.mcp.services.find((item) => item.packageName === 'e2e-excel-mcp')?.enabled).toBe(true);
  });

  test('loads BOM encoded managed skill state in settings pages', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await writeManagedSkillStateWithBom(electronApp, [
      {
        name: 'ppt-master',
        title: 'ppt-master',
        sourceKind: 'plugin',
        category: 'productivity',
        description: 'Generate natively editable PPTX from PDF, DOCX, URL, or Markdown.'
      },
      {
        name: 'excel-mcp-server',
        title: 'excel-mcp-server',
        sourceKind: 'mcp-wrapper',
        category: 'MCP',
        description: 'A Model Context Protocol server for Excel file manipulation',
        generatedWrapper: true,
        mcpConfig: { command: 'uvx', args: ['excel-mcp-server', 'stdio'] }
      },
      {
        name: 'lark-cli',
        title: 'lark-cli',
        sourceKind: 'skill-collection',
        category: 'EcoreX',
        description: 'Feishu/Lark CLI skill collection.'
      }
    ]);

    const direct = await page.evaluate(async () => {
      const skills = await window.ecorex.listSkills({ refresh: true });
      const mcp = await window.ecorex.listMcpServers({ refresh: true });
      return { skills, mcp };
    });
    expect(direct.skills.ok).toBe(true);
    expect(direct.skills.skills.map((item) => item.name).sort()).toEqual(['excel-mcp-server', 'lark-cli', 'ppt-master']);
    expect(direct.skills.counts.totalSkills).toBe(3);
    expect(direct.mcp.ok).toBe(true);
    expect(direct.mcp.services.map((item) => item.packageName)).toContain('excel-mcp-server');

    await page.locator('.profile-trigger').click();
    await page.locator('.profile-menu-grid').getByRole('button', { name: /系统设置|settings/i }).click();
    await expect(page.locator('[data-testid="system-settings-page"]')).toBeVisible();
    await page.locator('[data-testid="system-settings-tab-skills"]').click();
    await expect(page.locator('[data-testid="skills-page"]')).toContainText('ppt-master');
    await expect(page.locator('[data-testid="skills-page"]')).toContainText(/excel[-\s]?MCP[-\s]?server/i);
    await expect(page.locator('[data-testid="skills-page"]')).toContainText(/lark|飞书|execution bridge/i);
    await page.locator('[data-testid="system-settings-tab-mcp"]').click();
    await expect(page.locator('[data-testid="mcp-page"]')).toContainText(/excel[-\s]?mcp[-\s]?server/i);
  });

  test('keeps model test validation local when required fields are missing', async ({ ecorex }) => {
    const { page } = ecorex;
    await login(page);

    await page.locator('.profile-trigger').click();
    await page.locator('.profile-menu-grid').getByRole('button', { name: /模型配置/ }).click();

    const dialog = page.getByRole('dialog', { name: '模型配置' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: '新建配置' }).click();
    await dialog.getByRole('textbox', { name: '模型名称', exact: true }).fill('');
    await dialog.getByRole('button', { name: '模型测速' }).click();

    await expect(dialog.locator('.model-config-notice')).toHaveText('缺少Base URL、模型名称、API Key，未发起模型调用。');
    await expect(dialog.getByText('测试中')).toHaveCount(0);
  });

  test('enforces the BrowserWindow minimum size guard', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;

    let result;
    let lastError;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        result = await electronApp.evaluate(({ BrowserWindow }) => {
          const appWindow = BrowserWindow.getAllWindows()
            .find((window) => window.webContents.getURL().startsWith('http://127.0.0.1:5188'));
          if (!appWindow) throw new Error('EcoreX app window was not found.');

          appWindow.setSize(320, 240);
          return {
            minimumSize: appWindow.getMinimumSize(),
            bounds: appWindow.getBounds(),
            contentBounds: appWindow.getContentBounds()
          };
        });
        break;
      } catch (error) {
        lastError = error;
        await page.waitForTimeout(500);
      }
    }
    if (!result) throw lastError;

    expect(result.minimumSize[0]).toBeGreaterThanOrEqual(800);
    expect(result.minimumSize[1]).toBeGreaterThanOrEqual(600);
    expect(result.bounds.width).toBeGreaterThanOrEqual(result.minimumSize[0]);
    expect(result.bounds.height).toBeGreaterThanOrEqual(result.minimumSize[1]);
    expect(result.contentBounds.width).toBeGreaterThan(0);
    expect(result.contentBounds.height).toBeGreaterThan(0);
  });

  test('keeps login and chat composer responsive without overflowing at minimum size @responsive', async ({ ecorex }) => {
    const { electronApp, page, timings } = ecorex;

    expect(timings.rendererReadyMs).toBeLessThan(budgetMs('ECOREX_E2E_STARTUP_BUDGET_MS', 30_000));
    const loginResult = await login(page);
    expect(loginResult.loginMs).toBeLessThan(budgetMs('ECOREX_E2E_LOGIN_BUDGET_MS', 15_000));

    await setAppWindowSize(electronApp, 800, 600);
    await page.waitForTimeout(150);

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible();
    const longPrompt = Array.from({ length: 48 }, (_, index) =>
      `responsive-smoke-${index + 1}: Scope-3 supplier energy variance review with very-long-token-${'x'.repeat(72)}`
    ).join('\n');
    const fillMs = await measureInteraction(async () => {
      await input.fill(longPrompt);
      await page.waitForFunction(() => {
        const textarea = document.querySelector('[data-testid="chat-input"]');
        return textarea && textarea.value.length > 1000 && textarea.scrollHeight > textarea.clientHeight;
      });
    });
    expect(fillMs).toBeLessThan(budgetMs('ECOREX_E2E_CHAT_FILL_BUDGET_MS', 2_000));

    const layout = await page.evaluate(() => {
      const composer = document.querySelector('[data-testid="chat-composer"]');
      const textarea = document.querySelector('[data-testid="chat-input"]');
      const chatMain = document.querySelector('.chat-main');
      const composerRect = composer.getBoundingClientRect();
      const textareaRect = textarea.getBoundingClientRect();
      const chatMainRect = chatMain.getBoundingClientRect();
      const textareaStyle = window.getComputedStyle(textarea);
      return {
        composerLeft: composerRect.left,
        composerRight: composerRect.right,
        chatMainLeft: chatMainRect.left,
        chatMainRight: chatMainRect.right,
        textareaHeight: textareaRect.height,
        textareaClientHeight: textarea.clientHeight,
        textareaScrollHeight: textarea.scrollHeight,
        textareaOverflowY: textareaStyle.overflowY,
        textareaClientWidth: textarea.clientWidth,
        textareaScrollWidth: textarea.scrollWidth,
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth
      };
    });

    expect(layout.composerLeft).toBeGreaterThanOrEqual(layout.chatMainLeft - 1);
    expect(layout.composerRight).toBeLessThanOrEqual(layout.chatMainRight + 1);
    expect(layout.textareaHeight).toBeLessThanOrEqual(114);
    expect(layout.textareaScrollHeight).toBeGreaterThan(layout.textareaClientHeight);
    expect(['auto', 'scroll']).toContain(layout.textareaOverflowY);
    expect(layout.textareaScrollWidth).toBeLessThanOrEqual(layout.textareaClientWidth + 2);
    expect(layout.documentScrollWidth).toBeLessThanOrEqual(layout.documentClientWidth + 2);
  });

  test('handles core chat interactions without trapping the user', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);
    await page.addStyleTag({ content: '.messages { max-height: 220px !important; }' });

    await electronApp.evaluate(({ ipcMain }) => {
      global.__ecorexRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (_event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        return {
          ok: false,
          sessionId: payload.sessionId,
          status: 'cancelled',
          error: 'e2e stubbed run'
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('回车发送烟测');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: '回车发送烟测' }).first()).toBeVisible();
    await expect(page.locator('.recent-row').first()).toContainText('回车发送烟测');
    const recentCountAfterFirstSend = await page.locator('.recent-row').count();
    await page.locator('[data-testid="chat-send-button"]').click();
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });

    await page.locator('[data-testid="chat-input"]').fill('同一会话第二轮');
    await page.locator('.messages').evaluate((node) => {
      node.scrollTop = 0;
    });
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: '同一会话第二轮' }).first()).toBeVisible();
    await expect.poll(async () => page.locator('.messages').evaluate((node) => (
      Math.ceil(node.scrollTop + node.clientHeight) >= node.scrollHeight - 3
    ))).toBeTruthy();
    const runPayloads = await electronApp.evaluate(() => global.__ecorexRunPayloads || []);
    expect(runPayloads.length).toBeGreaterThanOrEqual(2);
    expect(runPayloads[1].conversationId).toBe(runPayloads[0].conversationId);
    expect(runPayloads[0].claudeSessionId).toBe(runPayloads[0].conversationId);
    expect(runPayloads[1].claudeSessionId).toBe(runPayloads[0].claudeSessionId);
    expect(runPayloads[1].prompt).not.toContain('当前会话上下文');
    expect(runPayloads[1].prompt).not.toContain('用户：回车发送烟测');
    expect(runPayloads[1].prompt).toContain('用户当前输入：同一会话第二轮');
    await expect(page.locator('.recent-row')).toHaveCount(recentCountAfterFirstSend);
    await expect(page.locator('.recent-row').first()).toContainText('同一会话第二轮');
    await expect(page.locator('.permission-inline-note')).toHaveCount(0);
    await page.locator('[data-testid="chat-send-button"]').click();
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });

    await page.locator('.side-nav button').first().click();
    await page.locator('.recent-row').first().locator('.recent-open').click();
    await expect(page.locator('.user-bubble').filter({ hasText: '同一会话第二轮' }).first()).toBeVisible();

    await page.locator('[data-testid="chat-input"]').fill('这条会被新会话清空');
    await page.locator('.new-chat').click();
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
    await expect(page.locator('.assistant-card').first()).toContainText('接下来我们做些什么');
    await expect(page.locator('.recent-row').first()).toContainText('新会话');

    const recentRows = page.locator('.recent-row');
    const beforeDelete = await recentRows.count();
    await recentRows.first().hover();
    await recentRows.first().locator('.recent-delete').click();
    await expect(recentRows).toHaveCount(beforeDelete - 1);

    const runCountBeforePassiveInput = await electronApp.evaluate(() => (global.__ecorexRunPayloads || []).length);
    await page.locator('[data-testid="chat-input"]').evaluate((textarea) => {
      const base64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lS5nWQAAAABJRU5ErkJggg==';
      const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
      const file = new File([bytes], 'pasted-creative.png', { type: 'image/png' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      const event = new ClipboardEvent('paste', {
        clipboardData: transfer,
        bubbles: true,
        cancelable: true
      });
      textarea.dispatchEvent(event);
    });
    await expect(page.locator('.attachment-chip').filter({ hasText: 'pasted-creative.png' }).first()).toBeVisible();
    await expect(page.locator('.attachment-chip img').first()).toHaveAttribute('src', /^data:image\/png/);
    await page.locator('[data-testid="chat-input"]').evaluate((textarea) => {
      const file = new File([new Uint8Array([0, 1, 2, 3])], 'pasted-demo.webm', { type: 'video/webm' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      const event = new ClipboardEvent('paste', {
        clipboardData: transfer,
        bubbles: true,
        cancelable: true
      });
      textarea.dispatchEvent(event);
    });
    const videoAttachment = page.locator('.attachment-chip.video').filter({ hasText: 'pasted-demo.webm' }).first();
    await expect(videoAttachment).toBeVisible();
    await expect(videoAttachment.locator('img')).toHaveCount(0);
    await page.locator('[data-testid="chat-input"]').evaluate((textarea) => {
      const transfer = new DataTransfer();
      transfer.setData('text/uri-list', 'https://www.bing.com/search?q=ecorex');
      transfer.setData('text/plain', 'https://www.bing.com/search?q=ecorex');
      const event = new ClipboardEvent('paste', {
        clipboardData: transfer,
        bubbles: true,
        cancelable: true
      });
      textarea.dispatchEvent(event);
    });
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue(/https:\/\/www\.bing\.com\/search\?q=ecorex/);
    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexRunPayloads || []).length)).toBe(runCountBeforePassiveInput);
  });

  test('renders rich chat links, images and videos without navigating the app', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    const media = await startChatMediaServer();
    try {
      await login(page);
      await electronApp.evaluate(({ ipcMain, shell }, baseUrl) => {
        global.__ecorexExternalUrls = [];
        const originalOpenExternal = shell.openExternal.bind(shell);
        shell.openExternal = async (url) => {
          global.__ecorexExternalUrls.push(url);
          return '';
        };
        global.__ecorexRestoreOpenExternal = () => {
          shell.openExternal = originalOpenExternal;
        };
        ipcMain.removeHandler('agent:run');
        ipcMain.handle('agent:run', (event, payload) => {
          setTimeout(() => {
            event.sender.send('agent:events', {
              events: [
                {
                  sessionId: payload.sessionId,
                  kind: 'result',
                  status: 'completed',
                  text: [
                    'Rich response with [landing](https://example.com/campaign).',
                    `![creative](${baseUrl}/creative.png)`,
                    `Video: ${baseUrl}/demo.mp4`
                  ].join('\n')
                }
              ]
            });
          }, 30);
          return { ok: true, sessionId: payload.sessionId, status: 'running' };
        });
      }, media.baseUrl);

      await page.locator('[data-testid="chat-input"]').fill('render rich media');
      await page.locator('[data-testid="chat-input"]').press('Enter');
      const assistant = page.locator('.assistant-card').filter({ hasText: 'Rich response' }).first();
      await expect(assistant).toBeVisible({ timeout: 15_000 });
      await expect(assistant.locator('.chat-rich-link').filter({ hasText: 'landing' })).toBeVisible();
      const escapedBaseUrl = media.baseUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      await expect(assistant.locator('.chat-rich-media.image img')).toHaveAttribute('src', new RegExp(`${escapedBaseUrl}/creative\\.png`));
      await expect(assistant.locator('.chat-rich-media.video video')).toHaveAttribute('src', new RegExp(`${escapedBaseUrl}/demo\\.mp4`));

      await assistant.locator('.chat-rich-link').filter({ hasText: 'landing' }).click();
      await expect.poll(() => electronApp.evaluate(() => global.__ecorexExternalUrls || [])).toContain('https://example.com/campaign');
      await expect(page.locator('.app-frame')).toBeVisible();
    } finally {
      await electronApp.evaluate(() => global.__ecorexRestoreOpenExternal?.()).catch(() => {});
      await new Promise((resolve) => media.server.close(resolve));
    }
  });

  test('keeps backend runtime naming private in assistant-visible output', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: '`/clear` is a Claude Code built-in command. Please run it in the Claude Code input.'
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 30);
        return { ok: true, sessionId: payload.sessionId, status: 'running' };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('clear context');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    const assistant = page.locator('.assistant-card').filter({ hasText: '/clear' }).first();
    await expect(assistant).toBeVisible({ timeout: 10_000 });
    await expect(assistant).toContainText('EcoreX');
    await expect(assistant).not.toContainText(/Claude Code|Claude CLI|Anthropic CLI/i);
  });

  test('preserves streamed assistant artifacts when final result is a short status', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'assistant',
                status: 'streaming',
                text: '小红书笔记正文\n标题：装修设计近三天热门话题\n正文：这是已经生成好的笔记正文，不应该被最终状态覆盖。'
              },
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: '当前会话没有可用的文生图模型接口，因此无法直接生成图片文件。'
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'Agent task completed.' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'E2E stub started.' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('生成一篇小红书装修设计笔记和封面提示词');
    await page.locator('[data-testid="chat-input"]').press('Enter');

    const assistant = page.locator('.assistant-card').filter({ hasText: '小红书笔记正文' }).first();
    await expect(assistant).toBeVisible({ timeout: 15_000 });
    await expect(assistant).toContainText('这是已经生成好的笔记正文');
    await expect(assistant).toContainText('当前会话没有可用的文生图模型接口');
    await expect(page.locator('body')).not.toContainText('Agent task completed.');
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });
  });

  test('deduplicates repeated assistant safety fallback sentences', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        const prefix = '刚才执行被安全策略拦截，原因是命令里包含不必要的 ExecutionPolicy Bypass；我将改用不绕过执行策略的方式';
        const suffix = '不绕过执行策略的方式创建同样的 4 个测试文件。';
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'assistant', status: 'streaming', text: prefix },
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: `${suffix}${suffix}` },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'Agent task completed.' }
            ]
          });
        }, 30);
        return { ok: true, sessionId: payload.sessionId, status: 'running' };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('创建测试文件');
    await page.locator('[data-testid="chat-input"]').press('Enter');

    const assistant = page.locator('.assistant-card').filter({ hasText: 'ExecutionPolicy Bypass' }).first();
    await expect(assistant).toBeVisible({ timeout: 10_000 });
    const text = await assistant.textContent();
    expect((text.match(/刚才执行被安全策略拦截/g) || []).length).toBe(1);
    expect((text.match(/不绕过执行策略的方式/g) || []).length).toBe(1);
    await expect(page.locator('body')).not.toContainText('Agent task completed.');
  });

  test('shows recent empty state after deleting the last conversation and starts the next chat as a new session', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        const runIndex = global.__ecorexRunPayloads.length;
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: `E2E stub completed ${runIndex}`
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'Agent task completed.' }
            ]
          });
        }, 30);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'E2E stub started.' }
        };
      });
    });

    await expect(page.locator('.recent-row')).toHaveCount(0);
    await expect(page.locator('.recent-empty')).toBeVisible();
    await expect(page.locator('.recent-empty')).toContainText(/暂无(最近|历史)对话/);

    const firstPrompt = '最近对话删除到空状态';
    await page.locator('[data-testid="chat-input"]').fill(firstPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: firstPrompt }).first()).toBeVisible();
    await expect(page.locator('.recent-row')).toHaveCount(1);
    await expect(page.locator('.recent-row').first()).toContainText(firstPrompt);
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });

    const firstRunPayload = await electronApp.evaluate(() => global.__ecorexRunPayloads?.[0]);
    expect(firstRunPayload?.conversationId).toBeTruthy();
    const recentRows = page.locator('.recent-row');
    await recentRows.first().hover();
    await recentRows.first().locator('.recent-delete').click();

    await expect(recentRows).toHaveCount(0);
    await expect(page.locator('.recent-empty')).toBeVisible();
    await expect(page.locator('.recent-empty')).toContainText(/暂无(最近|历史)对话/);
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
    await expect(page.locator('.assistant-card').first()).toContainText('接下来我们做些什么');
    await expect(page.locator('.user-bubble').filter({ hasText: firstPrompt })).toHaveCount(0);

    const nextPrompt = '空状态后的新会话';
    await page.locator('[data-testid="chat-input"]').fill(nextPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: nextPrompt }).first()).toBeVisible();
    await expect(page.locator('.recent-row')).toHaveCount(1);
    await expect(page.locator('.recent-row').first()).toContainText(nextPrompt);
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });

    const runPayloads = await electronApp.evaluate(() => global.__ecorexRunPayloads || []);
    expect(runPayloads).toHaveLength(2);
    expect(runPayloads[1].permissionContinuation).not.toBe(true);
    expect(runPayloads[1].conversationId).toBeTruthy();
    expect(runPayloads[1].conversationId).not.toBe(firstRunPayload.conversationId);
    expect(runPayloads[1].claudeSessionId).toBe(runPayloads[1].conversationId);
  });

  test('keeps parallel agent runs isolated across conversations', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexParallelRuns = [];
      global.__ecorexParallelStops = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        const sessionId = payload.sessionId || `parallel-session-${global.__ecorexParallelRuns.length + 1}`;
        global.__ecorexParallelRuns.push({
          rawPrompt: payload.rawPrompt || payload.userPrompt || '',
          sessionId,
          conversationId: payload.conversationId,
          claudeSessionId: payload.claudeSessionId,
          windowId: win?.id || 0,
          completed: false
        });
        return {
          ok: true,
          sessionId,
          initialEvent: { sessionId, kind: 'status', status: 'started', text: `started ${payload.rawPrompt || payload.userPrompt || ''}` }
        };
      });
      ipcMain.removeHandler('agent:stop');
      ipcMain.handle('agent:stop', (_event, payload) => {
        global.__ecorexParallelStops.push(payload);
        return { ok: true, sessionId: payload.sessionId, status: 'cancelled' };
      });
    });

    const alphaPrompt = 'parallel alpha prompt';
    const betaPrompt = 'parallel beta prompt';
    const alphaResult = 'alpha result isolated';
    const betaResult = 'beta result isolated';

    await page.locator('[data-testid="chat-input"]').fill(alphaPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: alphaPrompt }).first()).toBeVisible();
    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexParallelRuns || []).length), { timeout: 10_000 }).toBe(1);

    await page.locator('.new-chat').click();
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
    await expect(page.locator('[data-testid="chat-stop-button"]')).toHaveCount(0);
    await expect(page.getByTestId('running-session-strip')).toHaveCount(0);
    await expect(page.getByTestId('running-session-pill').filter({ hasText: alphaPrompt })).toHaveCount(0);
    await page.locator('[data-testid="chat-input"]').fill(betaPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: betaPrompt }).first()).toBeVisible();

    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexParallelRuns || []).length), { timeout: 10_000 }).toBe(2);
    const runPayloads = await electronApp.evaluate(() => global.__ecorexParallelRuns || []);
    expect(runPayloads[0].sessionId).not.toBe(runPayloads[1].sessionId);
    expect(runPayloads[0].conversationId).not.toBe(runPayloads[1].conversationId);
    expect(runPayloads[0].claudeSessionId).toBe(runPayloads[0].conversationId);
    expect(runPayloads[1].claudeSessionId).toBe(runPayloads[1].conversationId);
    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexParallelStops || []).length)).toBe(0);
    await expect(page.getByTestId('running-session-strip')).toHaveCount(0);
    await expect(page.getByTestId('running-session-pill')).toHaveCount(0);
    await expect(page.locator('.user-bubble').filter({ hasText: betaPrompt }).first()).toBeVisible();

    async function completeParallelRun(runKey, text) {
      return electronApp.evaluate(({ BrowserWindow }, payload) => {
        const key = String(payload.runKey || '');
        const run = (global.__ecorexParallelRuns || []).find((item) => (
          !item.completed
          && [item.sessionId, item.conversationId, item.claudeSessionId, item.rawPrompt].map((value) => String(value || '')).includes(key)
        ));
        if (!run) return null;
        run.completed = true;
        const win = BrowserWindow.fromId(run.windowId);
        if (!win || win.isDestroyed()) return { ...run, sent: false };
        win.webContents.send('agent:events', {
          sessionId: run.sessionId,
          events: [
            { sessionId: run.sessionId, kind: 'result', status: 'completed', text: payload.text },
            { sessionId: run.sessionId, kind: 'done', status: 'completed', text: 'done' }
          ]
        });
        return { ...run, sent: true };
      }, { runKey, text });
    }

    const completedBeta = await completeParallelRun(runPayloads[1].sessionId, betaResult);
    expect(completedBeta?.sent).toBe(true);
    await expect(page.locator('.assistant-card').filter({ hasText: betaResult }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.assistant-card').filter({ hasText: alphaResult })).toHaveCount(0);

    const alphaRow = page.locator('.recent-row').filter({ hasText: alphaPrompt }).first();
    await expect(alphaRow).toBeVisible();
    await alphaRow.locator('.recent-open').click();
    await expect(page.locator('.user-bubble').filter({ hasText: alphaPrompt }).first()).toBeVisible();
    await expect(page.locator('.assistant-card').filter({ hasText: betaResult })).toHaveCount(0);

    const completedAlpha = await completeParallelRun(runPayloads[0].sessionId, alphaResult);
    expect(completedAlpha?.sent).toBe(true);
    await expect(page.locator('.assistant-card').filter({ hasText: alphaResult }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.assistant-card').filter({ hasText: betaResult })).toHaveCount(0);

    const betaRow = page.locator('.recent-row').filter({ hasText: betaPrompt }).first();
    await expect(betaRow).toBeVisible();
    await betaRow.locator('.recent-open').click();
    await expect(page.locator('.user-bubble').filter({ hasText: betaPrompt }).first()).toBeVisible();
    await expect(page.locator('.assistant-card').filter({ hasText: betaResult }).first()).toBeVisible();
    await expect(page.locator('.assistant-card').filter({ hasText: alphaResult })).toHaveCount(0);
    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexParallelRuns || []).every((item) => item.completed)), { timeout: 10_000 }).toBe(true);

    await expect.poll(async () => page.evaluate(({ alphaPrompt, betaPrompt, alphaResult, betaResult }) => {
      const conversations = Object.values(JSON.parse(localStorage.getItem('ecorex-chat-conversations') || '{}'));
      const findByPrompt = (prompt) => conversations.find((conversation) => (
        conversation
        && Array.isArray(conversation.messages)
        && conversation.messages.some((message) => message.role === 'user' && String(message.text || '').includes(prompt))
      ));
      const alpha = findByPrompt(alphaPrompt);
      const beta = findByPrompt(betaPrompt);
      const textFor = (conversation) => (conversation?.messages || []).map((message) => String(message.text || '')).join('\n');
      const alphaText = textFor(alpha);
      const betaText = textFor(beta);
      return {
        alphaHasAlpha: alphaText.includes(alphaResult),
        alphaHasBeta: alphaText.includes(betaResult),
        betaHasBeta: betaText.includes(betaResult),
        betaHasAlpha: betaText.includes(alphaResult)
      };
    }, { alphaPrompt, betaPrompt, alphaResult, betaResult }), { timeout: 10_000 }).toEqual({
      alphaHasAlpha: true,
      alphaHasBeta: false,
      betaHasBeta: true,
      betaHasAlpha: false
    });
  });

  test('keeps recovered running-session previews hidden from the main chat UI', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await electronApp.evaluate(({ ipcMain }) => {
      ipcMain.removeHandler('agent:sessions');
      ipcMain.handle('agent:sessions', () => ({
        ok: true,
        sessions: [{
          sessionId: 'corrupt-running-session',
          conversationId: 'corrupt-running-conversation',
          claudeSessionId: 'corrupt-running-conversation',
          messageId: 'assistant-corrupt-running-session',
          status: 'running',
          state: 'running',
          title: '姝ｅ湪鎵ц浠诲姟',
          promptPreview: '???????????????????? 1. ??????? 2. ???????',
          updatedAt: Date.now()
        }]
      }));
    });

    await login(page);
    await expect(page.getByTestId('running-session-strip')).toHaveCount(0);
    await expect(page.locator('.running-session-pill')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('????????');
    await expect(page.locator('body')).not.toContainText('姝ｅ湪');
  });

  test('marks incomplete agent terminal results as failed instead of complete', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    const incompleteText = 'Task stopped before a usable final result was returned.';
    await electronApp.evaluate(({ ipcMain, BrowserWindow }, text) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'status',
                status: 'running',
                text: 'Configuring Feishu CLI'
              },
              {
                sessionId: payload.sessionId,
                kind: 'error',
                status: 'failed',
                reason: 'incomplete-result',
                text,
                recoveryHint: 'Retry with a smaller task and ask for a concrete final result.'
              }
            ]
          });
        }, 60);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: {
            sessionId: payload.sessionId,
            kind: 'status',
            status: 'started',
            text: 'started'
          }
        };
      });
    }, incompleteText);

    await page.locator('[data-testid="chat-input"]').fill('configure Feishu CLI for this local project and report every final verification result');
    await page.locator('[data-testid="chat-input"]').press('Enter');

    const assistant = page.locator('.assistant-card').filter({ hasText: incompleteText }).first();
    await expect(assistant).toBeVisible({ timeout: 10_000 });
    await expect(assistant.locator('.message-status.error')).toBeVisible();
    await expect(assistant.locator('.message-status.complete')).toHaveCount(0);
    await expect(assistant.locator('.message-retry-link')).toBeVisible();
    await expect(assistant.locator('.agent-trace-node.danger')).toBeVisible();
    await expect(page.locator('[data-testid="chat-stop-button"]')).toHaveCount(0);
  });

  test('continues permission confirmations as a hidden rerun without adding a user prompt', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        const text = payload.permissionContinuation
          ? '已执行确认后的本地只读操作。'
          : '我需要执行一条只读的 PowerShell 命令来查看本机磁盘剩余空间，不会修改任何文件或系统设置。请确认是否允许我执行？';
        setTimeout(() => {
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'Agent task completed.' }
            ]
          });
        }, 50);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: '本地执行引擎会话已启动' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('查看本机磁盘剩余空间');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble')).toHaveCount(1);
    await expect(page.locator('.user-bubble').filter({ hasText: '查看本机磁盘剩余空间' })).toHaveCount(1);
    await expect(page.getByTestId('permission-confirmation-card')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.permission-confirmation-backdrop')).toHaveCount(0);
    await expect(page.locator('.assistant-card').filter({ hasText: '请确认是否允许' })).toHaveCount(0);
    await expect(page.locator('.inline-permission-actions button').filter({ hasText: '允许一次' })).toBeVisible();
    await page.locator('.inline-permission-actions button').filter({ hasText: '允许一次' }).click();
    await expect(page.getByTestId('permission-confirmation-card')).toHaveCount(0);
    await expect(page.locator('.user-bubble')).toHaveCount(1);
    await expect(page.locator('.user-bubble').filter({ hasText: '允许一次' })).toHaveCount(0);
    await expect(page.locator('.user-bubble').filter({ hasText: '权限确认回执' })).toHaveCount(0);
    await expect(page.locator('.assistant-card').filter({ hasText: '已执行确认后的本地只读操作' }).first()).toBeVisible({ timeout: 15_000 });

    const runPayloads = await electronApp.evaluate(() => global.__ecorexRunPayloads || []);
    expect(runPayloads).toHaveLength(2);
    expect(runPayloads[1].sessionId).not.toBe(runPayloads[0].sessionId);
    expect(runPayloads[1].permissionContinuation).toBe(true);
    expect(runPayloads[1].accessMode).toBe('fullAccess');
    expect(runPayloads[1].permissionMode).toBe('fullAccess');
    expect(runPayloads[1].defaultPermissionMode).toBe('fullAccess');
    expect(runPayloads[1].prompt).toContain('权限确认回执');
    expect(runPayloads[1].prompt).not.toContain('用户当前输入');
    expect(runPayloads[1].conversationId).toBe(runPayloads[0].conversationId);
    expect(runPayloads[1].claudeSessionId).toBe(runPayloads[0].claudeSessionId);
    await expect(page.locator('.user-bubble').filter({ hasText: '允许一次' })).toHaveCount(0);
  });

  test('applies default and full access permission switches to the next run immediately', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexPermissionRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexPermissionRunPayloads.push({
          rawPrompt: payload.rawPrompt,
          accessMode: payload.accessMode,
          permissionMode: payload.permissionMode,
          defaultPermissionMode: payload.defaultPermissionMode,
          fullAccessConfirmed: payload.fullAccessConfirmed,
          fullAccessConfirmation: payload.fullAccessConfirmation
        });
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: `permission-smoke-result ${global.__ecorexPermissionRunPayloads.length}` },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'Agent task completed.' }
            ]
          });
        }, 30);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: {
            sessionId: payload.sessionId,
            kind: 'status',
            status: 'started',
            text: 'permission mode smoke started'
          }
        };
      });
    });

    const sendAndWait = async (text, expectedCount) => {
      await page.locator('[data-testid="chat-input"]').fill(text);
      await page.locator('[data-testid="chat-input"]').press('Enter');
      await expect.poll(() => electronApp.evaluate(() => global.__ecorexPermissionRunPayloads?.length || 0)).toBe(expectedCount);
      await expect(page.locator('[data-testid="chat-stop-button"]')).toHaveCount(0, { timeout: 10_000 });
    };

    const permissionSelect = page.locator('.permission-select').first();
    await expect(permissionSelect).toBeVisible();
    if (!(await permissionSelect.evaluate((node) => node.classList.contains('default')))) {
      await permissionSelect.locator('> button').click();
      await page.locator('.permission-select-menu [role="menuitemradio"]').first().click();
      await expect(permissionSelect).toHaveClass(/default/);
    }

    await sendAndWait('permission default smoke 1', 1);
    let payloads = await electronApp.evaluate(() => global.__ecorexPermissionRunPayloads || []);
    expect(payloads[0]).toMatchObject({
      rawPrompt: 'permission default smoke 1',
      accessMode: 'default',
      permissionMode: 'default',
      defaultPermissionMode: 'default'
    });
    expect(payloads[0].fullAccessConfirmed).not.toBe(true);

    await permissionSelect.locator('> button').click();
    page.once('dialog', async (dialog) => {
      await dialog.accept();
    });
    await page.locator('.permission-select-menu [role="menuitemradio"]').nth(1).click();
    await expect(permissionSelect).toHaveClass(/full/);

    await sendAndWait('permission full access smoke 2', 2);
    payloads = await electronApp.evaluate(() => global.__ecorexPermissionRunPayloads || []);
    expect(payloads[1]).toMatchObject({
      rawPrompt: 'permission full access smoke 2',
      accessMode: 'fullAccess',
      permissionMode: 'fullAccess',
      defaultPermissionMode: 'fullAccess',
      fullAccessConfirmed: true,
      fullAccessConfirmation: 'fullAccess'
    });

    await permissionSelect.locator('> button').click();
    await page.locator('.permission-select-menu [role="menuitemradio"]').first().click();
    await expect(permissionSelect).toHaveClass(/default/);

    await sendAndWait('permission default smoke 3', 3);
    payloads = await electronApp.evaluate(() => global.__ecorexPermissionRunPayloads || []);
    expect(payloads[2]).toMatchObject({
      rawPrompt: 'permission default smoke 3',
      accessMode: 'default',
      permissionMode: 'default',
      defaultPermissionMode: 'default'
    });
    expect(payloads[2].fullAccessConfirmed).not.toBe(true);
  });

  test('creates and switches projects from diagnostics workspace @responsive', async ({ ecorex }) => {
    const { page } = ecorex;
    await login(page);

    const diagnosticsMs = await measureInteraction(async () => openDiagnostics(page));
    expect(diagnosticsMs).toBeLessThan(budgetMs('ECOREX_E2E_DIAGNOSTICS_NAV_BUDGET_MS', 8_000));

    const projectPanel = page.locator('[data-testid="project-workspace-panel"]');
    const createInput = page.locator('[data-testid="project-create-input"]');
    const createButton = page.locator('[data-testid="project-create-button"]');
    const currentSummary = page.locator('[data-testid="project-current-summary"]');
    await expect(projectPanel).toBeVisible();
    await expect(createInput).toBeEnabled({ timeout: 15_000 });

    const suffix = Date.now();
    const firstProject = `E2E Carbon Alpha ${suffix}`;
    const secondProject = `E2E Carbon Beta ${suffix}`;

    async function createProject(name) {
      await createInput.fill(name);
      await expect(createButton).toBeEnabled();
      await createButton.click();
      await expect(currentSummary).toContainText(name, { timeout: 10_000 });
      await expect(page.locator('[data-testid="project-workspace-entry"]').filter({ hasText: name }).first()).toBeVisible();
    }

    const firstCreateMs = await measureInteraction(async () => createProject(firstProject));
    expect(firstCreateMs).toBeLessThan(budgetMs('ECOREX_E2E_PROJECT_CREATE_BUDGET_MS', 10_000));
    const secondCreateMs = await measureInteraction(async () => createProject(secondProject));
    expect(secondCreateMs).toBeLessThan(budgetMs('ECOREX_E2E_PROJECT_CREATE_BUDGET_MS', 10_000));

    const firstEntry = page.locator('[data-testid="project-workspace-entry"]').filter({ hasText: firstProject }).first();
    const switchMs = await measureInteraction(async () => {
      await firstEntry.locator('[data-testid="project-switch-button"]').click();
      await expect(currentSummary).toContainText(firstProject, { timeout: 10_000 });
    });
    expect(switchMs).toBeLessThan(budgetMs('ECOREX_E2E_PROJECT_SWITCH_BUDGET_MS', 8_000));

    const status = await page.evaluate(async () => window.ecorex.getProjectStatus());
    expect(status.ok).toBe(true);
    expect(status.activeProject?.name || status.currentProject?.name).toBe(firstProject);
  });

  test('exports a redacted diagnostics package and surfaces crash recovery state', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    const crashAt = new Date(Date.now() - 60 * 1000).toISOString();
    await writeCrashSummary(electronApp, [
      {
        id: 'e2e-renderer-crash',
        time: crashAt,
        kind: 'renderer-crash',
        process: 'renderer',
        severity: 'error',
        details: {
          reason: 'e2e simulated renderer crash',
          prompt: 'this prompt text must not be exported'
        }
      }
    ]);
    await login(page);

    const result = await page.evaluate(async () => {
      const exported = await window.ecorex.exportDiagnosticsPackage({
        saveToFile: false,
        logLimit: 5,
        sessionLimit: 5
      });
      const crashStatus = await window.ecorex.getCrashRecoveryStatus({ limit: 5 });
      return { exported, crashStatus };
    });

    expect(result.exported.ok).toBe(true);
    expect(result.exported.saved).toBe(false);
    expect(result.exported.path).toBe('');
    expect(result.exported.fileName).toMatch(/^ecorex-diagnostics-.+\.json$/);
    expect(result.exported.diagnosticsPackage.schema).toBe('ecorex.diagnostics.v1');
    expect(result.exported.diagnosticsPackage.privacy.includesApiKeys).not.toBe(true);
    expect(result.exported.diagnosticsPackage.privacy.includesPromptFullText).not.toBe(true);
    expect(result.exported.diagnosticsPackage.privacy.includesLocalPathBodies).not.toBe(true);
    expect(result.exported.diagnosticsPackage.crashes.total).toBeGreaterThanOrEqual(1);
    expect(result.exported.diagnosticsPackage.crashes.recent.some((event) => event.kind === 'renderer-crash')).toBe(true);
    expect(result.exported.json).not.toContain('this prompt text must not be exported');
    expect(result.exported.json).not.toMatch(/authToken|sessionToken|EcoreX123!/i);
    expect(result.crashStatus.ok).toBe(true);
    expect(result.crashStatus.status).toBe('attention');
    expect(result.crashStatus.recentCrashes.some((event) => event.kind === 'renderer-crash')).toBe(true);

    await openDiagnostics(page);
    await expect(page.locator('.crash-recovery-status.warn')).toBeVisible();
    expect(await page.locator('.crash-recovery-entry').count()).toBeGreaterThan(0);
  });

  test('keeps permission and workspace changes behind explicit confirmation', async ({ ecorex }) => {
    const { page, paths } = ecorex;
    const customWorkspace = path.join(paths.root, 'Trusted Workspace');
    fs.mkdirSync(customWorkspace, { recursive: true });

    await login(page);
    await openDiagnostics(page);

    const permissionSelect = page.locator('.permission-setting-block select');
    await expect(permissionSelect).toHaveValue('default');

    let dismissedConfirmation = '';
    page.once('dialog', async (dialog) => {
      dismissedConfirmation = dialog.message();
      await dialog.dismiss();
    });
    await permissionSelect.selectOption('fullAccess');
    expect(dismissedConfirmation).toBeTruthy();
    await expect(page.locator('.permission-status-strip.ok')).toBeVisible();
    await expect.poll(() => page.evaluate(async () => {
      const result = await window.ecorex.getSettings();
      return result.settings?.accessMode;
    })).toBe('default');

    let acceptedConfirmation = '';
    page.once('dialog', async (dialog) => {
      acceptedConfirmation = dialog.message();
      await dialog.accept();
    });
    await permissionSelect.selectOption('fullAccess');
    expect(acceptedConfirmation).toBeTruthy();
    await expect(page.locator('.permission-status-strip.warn')).toBeVisible();
    await expect.poll(() => page.evaluate(async () => {
      const result = await window.ecorex.getSettings();
      return result.settings?.accessMode;
    })).toBe('fullAccess');

    const revokedPermission = await page.evaluate(async () => window.ecorex.updateSettings({
      accessMode: 'default',
      permissionMode: 'default',
      defaultPermissionMode: 'default'
    }));
    expect(revokedPermission.ok).toBe(true);
    await expect.poll(() => page.evaluate(async () => {
      const result = await window.ecorex.getSettings();
      return result.settings?.accessMode;
    })).toBe('default');

    const workspaceResult = await page.evaluate(async ({ customWorkspace, protectedRoot }) => {
      const rejectedCustom = await window.ecorex.updateSettings({ workspaceRoot: customWorkspace });
      const acceptedCustom = await window.ecorex.updateSettings({
        workspaceRoot: customWorkspace,
        confirmCustomWorkspaceRoot: true
      });
      const ensured = await window.ecorex.ensureWorkspace({ workspaceRoot: customWorkspace });
      const rejectedProtected = await window.ecorex.updateSettings({
        workspaceRoot: protectedRoot,
        confirmCustomWorkspaceRoot: true
      });
      const restored = await window.ecorex.updateSettings({
        workspaceRoot: '',
        workspaceRootConfirmed: false,
        customWorkspaceConfirmed: false
      });
      return { rejectedCustom, acceptedCustom, ensured, rejectedProtected, restored };
    }, { customWorkspace, protectedRoot: path.parse(paths.root).root });

    expect(workspaceResult.rejectedCustom.ok).toBe(false);
    expect(workspaceResult.rejectedCustom.error).toContain('Custom workspaceRoot requires explicit confirmation.');
    expect(workspaceResult.acceptedCustom.ok).toBe(true);
    expect(samePath(workspaceResult.acceptedCustom.settings.workspaceRoot, customWorkspace)).toBe(true);
    expect(workspaceResult.ensured.ok).toBe(true);
    expect(workspaceResult.ensured.workspace.pathLabel).toBe('workspace:/');
    expect(workspaceResult.rejectedProtected.ok).toBe(false);
    expect(workspaceResult.rejectedProtected.error).toContain('Workspace root cannot be');
    expect(workspaceResult.restored.ok).toBe(true);
    expect(workspaceResult.restored.settings.customWorkspaceRootConfirmed).toBe(false);
  });

  test('manages advertising projects with metadata, rename, archive and delete state', async ({ ecorex }) => {
    const { page, paths } = ecorex;
    const projectWorkspace = path.join(paths.root, 'Ad Projects');
    fs.mkdirSync(projectWorkspace, { recursive: true });
    await login(page);

    const result = await page.evaluate(async ({ projectWorkspace }) => {
      const settings = await window.ecorex.updateSettings({
        workspaceRoot: projectWorkspace,
        confirmCustomWorkspaceRoot: true
      });
      const created = await window.ecorex.createProject({
        name: '618 短视频投放',
        client: '星河饮品',
        goal: '提升新品线索转化',
        industry: '快消',
        scenario: '短视频信息流',
        budget: '50 万 / 月',
        period: '2026 Q2',
        deliverables: ['投放计划', '素材脚本', '复盘报告']
      });
      const status = await window.ecorex.getProjectStatus();
      const updated = await window.ecorex.updateProject({
        id: created.project.id,
        name: '618 Rebrand Project',
        goal: '提升新品线索转化并沉淀 A/B 测试结论',
        status: 'paused'
      });
      const archived = await window.ecorex.archiveProject({ id: created.project.id });
      const deleted = await window.ecorex.deleteProject({
        id: created.project.id,
        confirmDelete: true,
        deleteFilesConfirmed: true
      });
      const listed = await window.ecorex.listProjects();
      return { settings, created, status, updated, archived, deleted, listed };
    }, { projectWorkspace });

    expect(result.settings.ok).toBe(true);
    expect(result.created.ok).toBe(true);
    expect(result.created.project.client).toBe('星河饮品');
    expect(result.created.project.memoryLabel).toContain('.ecorex-memory');
    expect(result.status.activeProject.id).toBe(result.created.project.id);
    expect(result.updated.project.name).toBe('618 Rebrand Project');
    expect(result.updated.project.pathLabel).not.toBe(result.created.project.pathLabel);
    expect(result.updated.project.status).toBe('paused');
    expect(result.archived.project.status).toBe('archived');
    expect(result.deleted.ok).toBe(true);
    expect(result.listed.projects.some((project) => project.id === result.created.project.id)).toBe(false);
    const oldDir = path.join(projectWorkspace, result.created.project.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    const renamedDir = path.join(projectWorkspace, result.updated.project.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    expect(fs.existsSync(oldDir)).toBe(false);
    expect(fs.existsSync(renamedDir)).toBe(false);
  });

  test('manages projects from the project management UI and syncs local directories @responsive', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    const projectWorkspace = path.join(paths.root, 'UI Projects');
    fs.mkdirSync(projectWorkspace, { recursive: true });
    await login(page);

    const settings = await page.evaluate(async ({ projectWorkspace }) => window.ecorex.updateSettings({
      workspaceRoot: projectWorkspace,
      confirmCustomWorkspaceRoot: true
    }), { projectWorkspace });
    expect(settings.ok).toBe(true);

    await page.locator('[data-testid="sidebar-projects-nav"]').click();
    await expect(page.locator('[data-testid="projects-list-panel"]')).toBeVisible();
    await expect(page.locator('[data-testid="projects-new-button"]')).toBeEnabled({ timeout: 15_000 });

    const suffix = Date.now();
    const originalName = `UI Project ${suffix}`;
    const renamedName = `UI Project Renamed ${suffix}`;

    await page.locator('[data-testid="projects-new-button"]').click();
    await expect(page.locator('[data-testid="projects-create-name"]')).toBeEnabled({ timeout: 15_000 });
    await page.locator('[data-testid="projects-create-name"]').fill(originalName);
    await expect(page.locator('[data-testid="projects-create-submit"]')).toBeEnabled();
    await page.locator('[data-testid="projects-create-submit"]').click();
    await expect(page.locator('[data-testid="project-detail-panel"]')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="project-edit-name"]')).toHaveValue(originalName, { timeout: 15_000 });
    await page.locator('[data-testid="project-prompt-input"]').fill('请整理这个项目的投放复盘任务');
    await expect(page.locator('[data-testid="project-prompt-input"]')).toHaveValue('请整理这个项目的投放复盘任务');
    await page.locator('[data-testid="project-prompt-input"]').clear();
    await page.locator('[data-testid="project-detail-edit-button"]').click();
    await expect(page.locator('[data-testid="project-edit-client"]')).toBeVisible({ timeout: 10_000 });
    await page.locator('[data-testid="project-edit-client"]').fill('EcoreX 测试品牌');
    await page.locator('[data-testid="project-edit-goal"]').fill('验证项目弹窗填写');
    await page.locator('[data-testid="project-detail-modal-save"]').click();
    await expect(page.locator('.project-context-summary')).toContainText('EcoreX 测试品牌');

    const createdProject = await page.evaluate(async (name) => {
      const listed = await window.ecorex.listProjects();
      return listed.projects.find((project) => project.name === name);
    }, originalName);
    expect(createdProject).toBeTruthy();
    const createdDir = path.join(projectWorkspace, createdProject.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    expect(fs.existsSync(createdDir)).toBe(true);
    const projectUploadPath = path.join(projectWorkspace, `uploaded-project-file-${suffix}.txt`);
    fs.writeFileSync(projectUploadPath, 'project file tree smoke', 'utf8');
    const addFileResult = await page.evaluate(async ({ projectId, projectUploadPath }) => (
      window.ecorex.addProjectFiles({ id: projectId, projectId, files: [projectUploadPath] })
    ), { projectId: createdProject.id, projectUploadPath });
    expect(addFileResult.ok).toBe(true);
    await page.locator('.project-back-button').click();
    await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: originalName }).first().click();
    await expect(page.locator('[data-testid="project-file-tree"]')).toBeVisible({ timeout: 15_000 });
    const uploadedFileEntry = page.locator('[data-testid="project-file-entry"]').filter({ hasText: path.basename(projectUploadPath) }).first();
    if (!(await uploadedFileEntry.isVisible().catch(() => false))) {
      await page.locator('.project-file-tree-root').click();
    }
    await expect(page.locator('.project-file-tree-children')).toContainText(path.basename(projectUploadPath));

    await page.locator('[data-testid="project-edit-name"]').fill(renamedName);
    await expect(page.locator('[data-testid="project-detail-save"]')).toBeEnabled();
    await page.locator('[data-testid="project-detail-save"]').click();
    await expect(page.locator('[data-testid="project-edit-name"]')).toHaveValue(renamedName, { timeout: 15_000 });
    await page.locator('.project-back-button').click();
    await expect(page.locator('[data-testid="projects-list-panel"]')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName }).first()).toBeVisible({ timeout: 15_000 });

    const renamedProject = await page.evaluate(async (name) => {
      const listed = await window.ecorex.listProjects();
      return listed.projects.find((project) => project.name === name);
    }, renamedName);
    expect(renamedProject).toBeTruthy();
    expect(renamedProject.pathLabel).not.toBe(createdProject.pathLabel);
    const renamedDir = path.join(projectWorkspace, renamedProject.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    expect(fs.existsSync(createdDir)).toBe(false);
    expect(fs.existsSync(renamedDir)).toBe(true);

    const sessionId = `project-session-${suffix}`;
    await page.evaluate(({ sessionId, projectId, projectName }) => {
      const items = JSON.parse(localStorage.getItem('ecorex-recent-chats') || '[]');
      localStorage.setItem('ecorex-recent-chats', JSON.stringify([
        {
          id: sessionId,
          claudeSessionId: sessionId,
          title: 'Project Session Draft',
          time: '10:30',
          updatedAt: Date.now(),
          projectId,
          projectName
        },
        ...items
      ]));
      window.dispatchEvent(new CustomEvent('ecorex:recent-chats-changed'));
    }, { sessionId, projectId: renamedProject.id, projectName: renamedName });
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Draft' })).toBeVisible();
    await page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Draft' }).locator('[data-testid="sidebar-project-session-rename"]').click({ force: true });
    await expect(page.locator('[data-testid="sidebar-project-session-rename-input"]')).toBeVisible({ timeout: 10_000 });
    await page.locator('[data-testid="sidebar-project-session-rename-input"]').fill('Project Session Renamed');
    await expect(page.locator('[data-testid="sidebar-project-session-rename-save"]')).toBeVisible();
    await page.locator('[data-testid="sidebar-project-session-rename-save"]').click({ force: true });
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' })).toBeVisible();
    const renamedSession = await page.evaluate((sessionId) => {
      const items = JSON.parse(localStorage.getItem('ecorex-recent-chats') || '[]');
      return items.find((item) => item.id === sessionId);
    }, sessionId);
    expect(renamedSession.title).toBe('Project Session Renamed');
    await page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' }).locator('[data-testid="sidebar-project-session-delete"]').click({ force: true });
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' })).toHaveCount(0);

    await electronApp.evaluate(({ shell }) => {
      global.__ecorexProjectOpenPathCalls = [];
      const originalOpenPath = shell.openPath.bind(shell);
      shell.openPath = async (...args) => {
        global.__ecorexProjectOpenPathCalls.push(args);
        return '';
      };
      global.__ecorexRestoreProjectOpenPath = () => {
        shell.openPath = originalOpenPath;
      };
    });
    await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName }).first().click();
    await page.locator('[data-testid="project-detail-open-folder"]').click();
    await expect.poll(() => electronApp.evaluate(() => global.__ecorexProjectOpenPathCalls?.length || 0), { timeout: 10_000 }).toBe(1);
    const openCalls = await electronApp.evaluate(({ shell }) => {
      try {
        global.__ecorexRestoreProjectOpenPath?.();
      } catch {
        // Best-effort cleanup for the shell method stub.
      }
      return global.__ecorexProjectOpenPathCalls || [];
    });
    expect(samePath(openCalls[0][0], renamedDir)).toBe(true);

    page.once('dialog', async (dialog) => {
      expect(dialog.message()).toContain(renamedName);
      await dialog.accept();
    });
    await page.locator('[data-testid="project-detail-delete"]').click();
    await expect(page.locator('[data-testid="projects-list-panel"]')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName })).toHaveCount(0, { timeout: 15_000 });
    const listedAfterDelete = await page.evaluate(async () => window.ecorex.listProjects());
    expect(listedAfterDelete.projects.some((project) => project.id === renamedProject.id)).toBe(false);
    expect(fs.existsSync(renamedDir)).toBe(false);
  });

  test('quick-renames sidebar projects and references uploaded project files from project chat', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    const projectWorkspace = path.join(paths.root, 'Project Quick Rename Files');
    fs.mkdirSync(projectWorkspace, { recursive: true });
    await login(page);

    const settings = await page.evaluate(async ({ projectWorkspace }) => window.ecorex.updateSettings({
      workspaceRoot: projectWorkspace,
      confirmCustomWorkspaceRoot: true
    }), { projectWorkspace });
    expect(settings.ok).toBe(true);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'project file reference received' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          projectId: payload.projectId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    const suffix = Date.now();
    const originalName = `Sidebar Project ${suffix}`;
    const quickName = `Sidebar Project Renamed ${suffix}`;
    const project = await page.evaluate(async (name) => {
      const result = await window.ecorex.createProject({ name, client: 'E2E Brand', goal: 'Project file mentions' });
      window.dispatchEvent(new CustomEvent('ecorex:projects-changed'));
      return result.project;
    }, originalName);
    expect(project).toBeTruthy();

    await page.locator('[data-testid="sidebar-projects-nav"]').click();
    await expect(page.locator('[data-testid="projects-list-entry"]').filter({ hasText: originalName }).first()).toBeVisible({ timeout: 15_000 });
    const sidebarProject = page.locator('.sidebar-project').filter({ hasText: originalName }).first();
    await expect(sidebarProject).toBeVisible({ timeout: 15_000 });
    await sidebarProject.locator('[data-testid="sidebar-project-rename"]').click();
    await page.locator('[data-testid="sidebar-project-rename-input"]').fill(quickName);
    await page.locator('[data-testid="sidebar-project-rename-save"]').click();
    await expect(page.locator('.sidebar-project').filter({ hasText: quickName }).first()).toBeVisible({ timeout: 15_000 });
    const renamed = await page.evaluate(async (name) => {
      const listed = await window.ecorex.listProjects();
      return listed.projects.find((item) => item.name === name);
    }, quickName);
    expect(renamed?.id).toBe(project.id);

    const textPath = path.join(projectWorkspace, `project-brief-${suffix}.md`);
    const imagePath = path.join(projectWorkspace, `project-thumb-${suffix}.png`);
    fs.writeFileSync(textPath, '# Project Brief\nAudience: project file mention smoke.\n', 'utf8');
    fs.writeFileSync(imagePath, Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
      'base64'
    ));
    const addFileResult = await page.evaluate(async ({ projectId, textPath, imagePath }) => (
      window.ecorex.addProjectFiles({ id: projectId, projectId, files: [textPath, imagePath] })
    ), { projectId: project.id, textPath, imagePath });
    expect(addFileResult.ok).toBe(true);
    expect(addFileResult.files).toHaveLength(2);

    await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: quickName }).first().click();
    await expect(page.locator('[data-testid="project-file-tree"]')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="project-file-entry"]').filter({ hasText: path.basename(textPath) }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('[data-testid="project-file-entry"].image img').first()).toBeVisible({ timeout: 15_000 });

    await page.locator('[data-testid="project-prompt-input"]').fill('@project-brief');
    await expect(page.locator('[data-testid="project-file-mention-menu"]')).toBeVisible({ timeout: 10_000 });
    await page.locator('[data-testid="project-file-mention-option"]').filter({ hasText: path.basename(textPath) }).first().click();
    await expect(page.locator('.project-chat-composer .attachment-chip').filter({ hasText: path.basename(textPath) })).toBeVisible({ timeout: 10_000 });
    await page.locator('.project-send-button').click();
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue(new RegExp(path.basename(textPath).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), { timeout: 10_000 });
    await expect(page.locator('.composer .attachment-chip').filter({ hasText: path.basename(textPath) })).toBeVisible({ timeout: 10_000 });
    await page.locator('[data-testid="chat-send-button"]').click();
    await expect(page.locator('.assistant-card').filter({ hasText: 'project file reference received' }).first()).toBeVisible({ timeout: 10_000 });

    const payload = await electronApp.evaluate(() => global.__ecorexRunPayloads?.[0]);
    expect(payload).toBeTruthy();
    expect(payload.projectId).toBe(project.id);
    expect(payload.attachments).toEqual(expect.arrayContaining([
      expect.objectContaining({
        name: path.basename(textPath),
        source: 'project'
      })
    ]));
    expect(payload.attachments[0].path).toContain(path.basename(textPath));
  });

  test('keeps general chats outside active project files and memory', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const projectWorkspace = path.join(paths.root, 'General Chat Project Isolation');
    fs.mkdirSync(projectWorkspace, { recursive: true });
    const settings = await page.evaluate(async ({ projectWorkspace }) => window.ecorex.updateSettings({
      workspaceRoot: projectWorkspace,
      confirmCustomWorkspaceRoot: true
    }), { projectWorkspace });
    expect(settings.ok).toBe(true);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexGeneralIsolationPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexGeneralIsolationPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'general isolation result' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          projectId: payload.projectId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    const suffix = Date.now();
    const projectOnlyFile = `project-only-${suffix}.txt`;
    const project = await page.evaluate(async ({ name, projectOnlyFile }) => {
      const result = await window.ecorex.createProject({ name, client: 'Hidden Brand', goal: 'General isolation check' });
      window.dispatchEvent(new CustomEvent('ecorex:project-context', { detail: { project: result.project } }));
      window.dispatchEvent(new CustomEvent('ecorex:projects-changed'));
      return { ...result.project, projectOnlyFile };
    }, { name: `General Isolation ${suffix}`, projectOnlyFile });
    expect(project?.id).toBeTruthy();

    const projectDir = path.join(projectWorkspace, project.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    const filePath = path.join(projectDir, 'files', projectOnlyFile);
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, 'project-only material must not appear in general chat\n', 'utf8');

    await page.evaluate(() => {
      const conversationId = `general-chat-${Date.now()}`;
      window.dispatchEvent(new CustomEvent('ecorex:new-chat', {
        detail: { id: conversationId, claudeSessionId: conversationId, title: 'General chat' }
      }));
    });

    const prompt = `general chat should not inspect project files ${suffix}`;
    await page.locator('[data-testid="chat-input"]').fill(prompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'general isolation result' }).first()).toBeVisible({ timeout: 10_000 });

    const payload = await electronApp.evaluate(() => global.__ecorexGeneralIsolationPayloads?.[0]);
    expect(payload).toBeTruthy();
    expect(payload.projectId).toBe(null);
    expect(payload.disableProjectContext).toBe(true);
    expect(payload.projectName || '').toBe('');
    expect(payload.attachments || []).toHaveLength(0);
    expect(payload.prompt).not.toContain(projectOnlyFile);
    expect(payload.prompt).not.toContain('project-memory.md');
  });

  test('keeps resumed project runs and dirty project history out of public chat', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const projectWorkspace = path.join(paths.root, 'Project Isolation Recovery');
    fs.mkdirSync(projectWorkspace, { recursive: true });
    const settings = await page.evaluate(async ({ projectWorkspace }) => window.ecorex.updateSettings({
      workspaceRoot: projectWorkspace,
      confirmCustomWorkspaceRoot: true
    }), { projectWorkspace });
    expect(settings.ok).toBe(true);

    await electronApp.evaluate(({ ipcMain }) => {
      global.__ecorexResumeRun = null;
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (_event, payload) => {
        global.__ecorexResumeRun = { ...payload };
        return {
          ok: true,
          sessionId: payload.sessionId,
          conversationId: payload.conversationId,
          claudeSessionId: payload.claudeSessionId,
          messageId: payload.messageId,
          projectId: payload.projectId,
          projectName: payload.projectName,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
      ipcMain.removeHandler('agent:sessions');
      ipcMain.handle('agent:sessions', () => {
        const run = global.__ecorexResumeRun;
        return {
          ok: true,
          sessions: run ? [{
            sessionId: run.sessionId,
            conversationId: run.conversationId,
            claudeSessionId: run.claudeSessionId,
            messageId: run.messageId,
            projectId: run.projectId,
            projectName: run.projectName,
            status: 'running',
            state: 'running',
            promptPreview: run.rawPrompt || run.userPrompt || ''
          }] : []
        };
      });
    });

    const suffix = Date.now();
    const project = await page.evaluate(async (name) => {
      const result = await window.ecorex.createProject({ name, client: '隔离品牌', goal: '恢复路由验证' });
      window.dispatchEvent(new CustomEvent('ecorex:projects-changed'));
      const conversationId = `project-recovery-${Date.now()}`;
      window.dispatchEvent(new CustomEvent('ecorex:new-chat', {
        detail: {
          id: conversationId,
          claudeSessionId: conversationId,
          title: '项目恢复会话',
          projectId: result.project.id,
          projectName: result.project.name
        }
      }));
      window.dispatchEvent(new CustomEvent('ecorex:project-context', { detail: { project: result.project } }));
      return result.project;
    }, `Isolation Project ${suffix}`);
    expect(project?.id).toBeTruthy();

    const dirtyTitle = `Dirty Project History ${suffix}`;
    await page.evaluate(({ dirtyTitle, project }) => {
      const recent = JSON.parse(localStorage.getItem('ecorex-recent-chats') || '[]');
      const conversations = JSON.parse(localStorage.getItem('ecorex-chat-conversations') || '{}');
      const dirtyId = `dirty-project-${Date.now()}`;
      localStorage.setItem('ecorex-recent-chats', JSON.stringify([
        {
          id: dirtyId,
          claudeSessionId: dirtyId,
          title: dirtyTitle,
          time: '16:20',
          updatedAt: Date.now(),
          projectId: '',
          projectName: ''
        },
        ...recent
      ]));
      conversations[dirtyId] = {
        id: dirtyId,
        claudeSessionId: dirtyId,
        projectId: project.id,
        projectName: project.name,
        updatedAt: Date.now(),
        messages: [
          {
            id: `user-${dirtyId}`,
            role: 'user',
            text: dirtyTitle,
            attachments: [{
              id: 'project-file:dirty',
              name: 'test-project-only.txt',
              source: 'project',
              projectId: project.id,
              projectName: project.name,
              relativePath: 'test-project-only.txt',
              path: 'workspace:/project/files/test-project-only.txt'
            }]
          }
        ]
      };
      localStorage.setItem('ecorex-chat-conversations', JSON.stringify(conversations));
      window.dispatchEvent(new CustomEvent('ecorex:recent-chats-changed'));
    }, { dirtyTitle, project });
    await expect(page.locator('.recent-row').filter({ hasText: dirtyTitle })).toHaveCount(0);
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: dirtyTitle }).first()).toBeVisible({ timeout: 15_000 });

    const prompt = `project resumed artifact ${suffix}`;
    const resultText = `project resumed result ${suffix}`;
    const artifactName = `project-resumed-${suffix}.md`;
    const projectDir = path.join(projectWorkspace, project.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    const artifactPath = path.join(projectDir, 'files', artifactName);
    fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
    fs.writeFileSync(artifactPath, '# Project recovered artifact\nrestored output\n', 'utf8');

    await page.locator('[data-testid="chat-input"]').fill(prompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect.poll(async () => electronApp.evaluate(() => Boolean(global.__ecorexResumeRun?.messageId)), { timeout: 10_000 }).toBe(true);

    await page.locator('.new-chat').click();
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('');
    await page.reload();
    await ensureLoggedIn(page);
    await expect.poll(async () => electronApp.evaluate(() => Boolean(global.__ecorexResumeRun?.sessionId)), { timeout: 10_000 }).toBe(true);

    await electronApp.evaluate(({ BrowserWindow }, payload) => {
      const run = global.__ecorexResumeRun;
      const win = BrowserWindow.getAllWindows()[0];
      if (!run || !win || win.isDestroyed()) return false;
      win.webContents.send('agent:events', {
        sessionId: run.sessionId,
        events: [
          {
            sessionId: run.sessionId,
            kind: 'result',
            status: 'completed',
            text: `${payload.resultText}\n- [${payload.artifactName}](${payload.artifactPath})`
          },
          { sessionId: run.sessionId, kind: 'done', status: 'completed', text: 'done' }
        ]
      });
      return true;
    }, { resultText, artifactName, artifactPath: artifactPath.replace(/\\/g, '/') });

    await expect(page.locator('.assistant-card').filter({ hasText: resultText })).toHaveCount(0);
    await expect(page.locator('.recent-row').filter({ hasText: prompt })).toHaveCount(0);
    const projectSession = page.locator('.sidebar-project-session-row').filter({ hasText: prompt.slice(0, 28) }).first();
    await expect(projectSession).toBeVisible({ timeout: 15_000 });
    await projectSession.locator('.sidebar-project-session').click();
    await expect(page.locator('.assistant-card').filter({ hasText: resultText }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: artifactName }).first()).toBeVisible({ timeout: 15_000 });
  });

  test('queues follow-up input during a running agent task and resumes in the same conversation', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexRunPayloads = [];
      global.__ecorexStopPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        const runIndex = global.__ecorexRunPayloads.length;
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'tool',
                status: 'running',
                toolName: runIndex === 1 ? 'WebSearch' : 'TodoWrite',
                tools: [{ name: runIndex === 1 ? 'WebSearch' : 'TodoWrite', input: { runIndex } }],
                text: `tool event ${runIndex}`
              }
            ]
          });
        }, 80);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: `queued-run-${runIndex}-completed`
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, runIndex === 1 ? 700 : 80);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
      ipcMain.removeHandler('agent:stop');
      ipcMain.handle('agent:stop', (_event, payload) => {
        global.__ecorexStopPayloads.push(payload);
        return { ok: true, sessionId: payload.sessionId, status: 'cancelled' };
      });
    });

    const firstPrompt = 'start a long campaign analysis';
    const followUpPrompt = 'also compare last week pacing';
    await page.locator('[data-testid="chat-input"]').fill(firstPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect.poll(() => electronApp.evaluate(() => global.__ecorexRunPayloads?.length || 0)).toBe(1);
    await expect(page.locator('[data-testid="chat-stop-button"]')).toBeVisible({ timeout: 10_000 });

    await page.locator('[data-testid="chat-input"]').fill(followUpPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: followUpPrompt }).first()).toBeVisible();
    await expect(page.locator('.user-bubble').filter({ hasText: followUpPrompt }).first()).toContainText(/排队|queued/i);

    await expect.poll(() => electronApp.evaluate(() => global.__ecorexRunPayloads?.length || 0), { timeout: 15_000 }).toBe(2);
    await expect(page.locator('.assistant-card').filter({ hasText: 'queued-run-2-completed' }).first()).toBeVisible({ timeout: 10_000 });

    const { runPayloads, stopPayloads } = await electronApp.evaluate(() => ({
      runPayloads: global.__ecorexRunPayloads || [],
      stopPayloads: global.__ecorexStopPayloads || []
    }));
    expect(stopPayloads).toHaveLength(0);
    expect(runPayloads).toHaveLength(2);
    expect(runPayloads[1].permissionContinuation).not.toBe(true);
    expect(runPayloads[1].conversationId).toBe(runPayloads[0].conversationId);
    expect(runPayloads[1].claudeSessionId).toBe(runPayloads[0].claudeSessionId);
    expect(runPayloads[1].prompt).toContain(followUpPrompt);
  });

  test('keeps handoff result running until lifecycle final and queues the next user input', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      global.__ecorexRunPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        const runIndex = global.__ecorexRunPayloads.length;
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: runIndex === 1
                  ? 'Open this Feishu authorization link, finish the browser step, then tell me done: https://open.feishu.cn/page/execution_bridge?user_code=TEST'
                  : 'queued handoff completed'
              }
            ]
          });
        }, 80);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, runIndex === 1 ? 900 : 120);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    const firstPrompt = 'start Feishu authorization handoff';
    const followUpPrompt = 'give me a clickable configuration link';
    await page.locator('[data-testid="chat-input"]').fill(firstPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    const firstAssistant = page.locator('.assistant-card').filter({ hasText: 'Feishu authorization link' }).first();
    await expect(firstAssistant).toBeVisible({ timeout: 10_000 });
    await expect(firstAssistant.locator('.message-status.complete')).toHaveCount(0);

    await page.locator('[data-testid="chat-input"]').fill(followUpPrompt);
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.user-bubble').filter({ hasText: followUpPrompt }).first()).toContainText(/排队|鎺掗槦|queued/i);
    await expect(page.locator('.assistant-card').filter({ hasText: 'This conversation already has a task running' })).toHaveCount(0);
    await expect.poll(() => electronApp.evaluate(() => global.__ecorexRunPayloads?.length || 0), { timeout: 15_000 }).toBe(2);
    await expect(page.locator('.assistant-card').filter({ hasText: 'queued handoff completed' }).first()).toBeVisible({ timeout: 10_000 });
  });

  test('hides stale recovery prompts immediately after retry starts', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain }) => {
      global.__ecorexRecoveryRetryPayloads = [];
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (_event, payload) => {
        global.__ecorexRecoveryRetryPayloads.push(payload);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    const retryPrompt = `retry stale recovery ${Date.now()}`;
    await page.evaluate(({ retryPrompt }) => {
      const conversationId = `recovery-retry-${Date.now()}`;
      const conversations = JSON.parse(localStorage.getItem('ecorex-chat-conversations') || '{}');
      conversations[conversationId] = {
        id: conversationId,
        claudeSessionId: conversationId,
        projectId: '',
        projectName: '',
        updatedAt: Date.now(),
        messages: [
          {
            id: 'assistant-recoverable',
            role: 'assistant',
            text: '',
            time: '17:00',
            status: 'interrupted',
            originalPrompt: retryPrompt,
            recovery: {
              state: 'recoverable',
              label: '可恢复',
              tone: 'running',
              detail: '上次任务被中断，可重试。'
            }
          }
        ],
        timeline: []
      };
      localStorage.setItem('ecorex-chat-conversations', JSON.stringify(conversations));
      localStorage.setItem('ecorex-recent-chats', JSON.stringify([{
        id: conversationId,
        claudeSessionId: conversationId,
        title: retryPrompt,
        time: '17:00',
        updatedAt: Date.now(),
        projectId: '',
        projectName: ''
      }]));
      window.dispatchEvent(new CustomEvent('ecorex:open-chat', { detail: { id: conversationId } }));
    }, { retryPrompt });

    await expect(page.locator('.message-recovery-state')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.assistant-card').filter({ hasText: '未完成 / 可继续' }).first()).toBeVisible();
    await page.locator('.message-recovery-state button').click();
    await expect(page.locator('.message-recovery-state')).toHaveCount(0);
    await expect.poll(async () => electronApp.evaluate(() => global.__ecorexRecoveryRetryPayloads?.length || 0)).toBe(1);
    const payload = await electronApp.evaluate(() => global.__ecorexRecoveryRetryPayloads?.[0]);
    expect(payload.prompt).toContain(retryPrompt);
  });

  test('passes selected attachments as structured agent payloads instead of prompt-only filenames', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const attachmentPath = path.join(paths.root, 'creative-brief.md');
    fs.writeFileSync(attachmentPath, '# Creative brief\nAudience: enterprise ESG buyers.\n', 'utf8');

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, filePath) => {
      global.__ecorexRunPayloads = [];
      global.__ecorexOpenedAttachments = [];
      ipcMain.removeHandler('attachment:select-files');
      ipcMain.handle('attachment:select-files', () => ({
        ok: true,
        files: [
          {
            id: 'e2e-brief-attachment',
            name: 'creative-brief.md',
            path: filePath,
            type: 'text/markdown',
            sizeBytes: 49,
            source: 'upload'
          }
        ]
      }));
      ipcMain.removeHandler('attachment:open-file');
      ipcMain.handle('attachment:open-file', (_event, payload = {}) => {
        global.__ecorexOpenedAttachments.push(payload);
        return { ok: true, opened: true };
      });
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        global.__ecorexRunPayloads.push(payload);
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'attachment payload received' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    }, attachmentPath);

    await page.locator('.composer-file-button').click();
    await expect(page.locator('.attachment-chip').filter({ hasText: 'creative-brief.md' }).first()).toBeVisible();
    await expect.poll(async () => electronApp.evaluate(() => (global.__ecorexRunPayloads || []).length)).toBe(0);
    await page.locator('[data-testid="chat-input"]').fill('summarize the attachment');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'attachment payload received' }).first()).toBeVisible({ timeout: 10_000 });

    const payload = await electronApp.evaluate(() => global.__ecorexRunPayloads?.[0]);
    expect(payload).toBeTruthy();
    expect(payload.prompt).toContain('creative-brief.md');
    expect(Array.isArray(payload.attachments)).toBe(true);
    expect(payload.attachments).toHaveLength(1);
    expect(payload.attachments[0]).toEqual(expect.objectContaining({
      name: 'creative-brief.md',
      path: attachmentPath,
      type: 'text/markdown',
      sizeBytes: 49
    }));

    const userAttachment = page.locator('.user-bubble .attachment-chip').filter({ hasText: 'creative-brief.md' }).first();
    await expect(userAttachment).toBeVisible();
    await expect(userAttachment.locator('.attachment-thumb.document')).toBeVisible();
    await expect(page.locator('.user-bubble .artifact-preview-shelf')).toHaveCount(0);
    await userAttachment.click();
    const openedAttachments = await electronApp.evaluate(() => global.__ecorexOpenedAttachments || []);
    expect(openedAttachments).toHaveLength(1);
    expect(openedAttachments[0]).toEqual(expect.objectContaining({
      path: attachmentPath
    }));
  });

  test('shows tool ledger in chat as visible but collapsed by default', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        const tools = ['WebSearch', 'WebFetch', 'Read', 'Bash', 'TodoWrite', 'Grep'];
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              ...tools.map((name, index) => ({
                sessionId: payload.sessionId,
                kind: 'tool',
                status: index === tools.length - 1 ? 'completed' : 'running',
                toolName: name,
                tools: [{ name, input: { index } }],
                text: `${name} completed`
              })),
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'ledger result' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 60);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('run tools and show ledger with enough detail to inspect files and web sources');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'ledger result' }).first()).toBeVisible({ timeout: 10_000 });

    const trace = page.locator('.agent-trace').first();
    await expect(trace).toBeVisible();
    await expect(trace).toHaveClass(/compact/);
    await expect(trace.locator('.agent-trace-summary')).toContainText('已完成');
    await expect(page.locator('.agent-trace-list')).toHaveCount(0);

    await trace.locator('.agent-trace-summary').click();
    await expect(trace).toHaveClass(/expanded/);
    await expect(trace.locator('.agent-trace-row')).toHaveCount(6);
  });

  test('marks task update trace complete when the run finishes', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              { sessionId: payload.sessionId, kind: 'tool', status: 'running', toolName: 'TaskUpdate', text: 'TaskUpdate' },
              { sessionId: payload.sessionId, kind: 'tool', status: 'running', toolName: 'TaskUpdate', text: 'TaskUpdate' },
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'trace completion result' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 60);
        return { ok: true, sessionId: payload.sessionId, status: 'running' };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('finish a task update trace');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'trace completion result' }).first()).toBeVisible({ timeout: 10_000 });
    const trace = page.locator('.agent-trace').first();
    await expect(trace.locator('.agent-trace-summary')).toContainText('已完成');
    await expect(trace.locator('.agent-trace-summary')).not.toContainText('进行中');
    await trace.locator('.agent-trace-summary').click();
    await expect(trace.locator('.agent-trace-row').filter({ hasText: '更新任务清单' }).first()).toContainText('已完成');
  });

  test('places readable tool return values into the assistant message', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'tool',
                status: 'completed',
                toolName: 'WebSearch',
                toolUseId: 'weather-result',
                text: 'Shanghai: Light Rain Shower, Mist, +27°C, feels like +33°C, humidity 94%, wind 10km/h.'
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 50);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('查询下上海今天的天气');
    await page.locator('[data-testid="chat-input"]').press('Enter');

    const assistant = page.locator('.assistant-card').filter({ hasText: 'Shanghai: Light Rain Shower' }).first();
    await expect(assistant).toBeVisible({ timeout: 10_000 });
    await expect(assistant.locator('[data-testid="tool-result-inline"]')).toContainText('humidity 94%');
    await expect(assistant.locator('.artifact-thumb-card')).toHaveCount(0);
  });

  test('suppresses task update noise and execution bridge line numbers', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  '24 25 Open this link: 26 27 https://open.feishu.cn/page/execution bridgeuser_code=TEST&from=execution bridge 28 29',
                  'Updated task #1 description, status',
                  'Finish authorization in the browser, then return here.'
                ].join('\n')
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 50);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('return a Feishu authorization link');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    const assistant = page.locator('.assistant-card').filter({ hasText: 'open.feishu.cn' }).first();
    await expect(assistant).toBeVisible({ timeout: 10_000 });
    await expect(assistant).toContainText('https://open.feishu.cn/page/execution_bridge?user_code=TEST&from=execution_bridge');
    await expect(assistant).not.toContainText('24 25');
    await expect(assistant).not.toContainText('26 27');
    await expect(assistant).not.toContainText('Updated task #1');
    await expect(assistant).not.toContainText('TaskUpdate');
  });

  test('keeps composer responsive after large assistant output and folded ledger @responsive', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    await electronApp.evaluate(({ ipcMain, BrowserWindow }) => {
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        const tools = Array.from({ length: 24 }, (_, index) => ({
          sessionId: payload.sessionId,
          kind: 'tool',
          status: index === 23 ? 'completed' : 'running',
          toolName: index % 2 ? 'WebFetch' : 'WebSearch',
          tools: [{ name: index % 2 ? 'WebFetch' : 'WebSearch', input: { index, query: `large-ledger-${index}` } }],
          text: `large ledger tool ${index}`
        }));
        const largeText = Array.from({ length: 320 }, (_, index) => `Large output line ${index}: campaign insight ${index % 7}.`).join('\n');
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              ...tools,
              { sessionId: payload.sessionId, kind: 'assistant', status: 'running', text: largeText },
              { sessionId: payload.sessionId, kind: 'result', status: 'completed', text: 'large ledger final result' },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 50);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    });

    await page.locator('[data-testid="chat-input"]').fill('produce a large ledger response with enough detail to keep the trace visible');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    const largeCard = page.locator('.assistant-card').filter({ hasText: 'Large output line 0' }).first();
    await expect(largeCard).toBeVisible({ timeout: 15_000 });
    const expandLongText = largeCard.locator('.text-expand');
    if (await expandLongText.isVisible().catch(() => false)) {
      await expandLongText.click();
      await expect(largeCard).toContainText('large ledger final result', { timeout: 10_000 });
    }
    await expect(page.locator('.agent-trace').first()).toHaveClass(/compact/);
    await expect(page.locator('.agent-trace-list')).toHaveCount(0);

    const inputMs = await measureInteraction(async () => {
      await page.locator('[data-testid="chat-input"]').fill('quick follow-up after large ledger');
    });
    expect(inputMs).toBeLessThan(budgetMs('ECOREX_E2E_LARGE_LEDGER_INPUT_BUDGET_MS', 1500));
    await expect(page.locator('[data-testid="chat-input"]')).toHaveValue('quick follow-up after large ledger');
  });

  test('previews generated files inside EcoreX and backfills precise file references @responsive', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'artifacts');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const artifactPaths = {
      text: path.join(artifactRoot, 'preview-report.md').replace(/\\/g, '/'),
      html: path.join(artifactRoot, 'preview-page.html').replace(/\\/g, '/'),
      image: path.join(artifactRoot, 'creative.png').replace(/\\/g, '/'),
      pdf: path.join(artifactRoot, 'media-plan.pdf').replace(/\\/g, '/'),
      pptx: path.join(artifactRoot, 'strategy-deck.pptx').replace(/\\/g, '/')
    };

    await electronApp.evaluate(({ ipcMain, BrowserWindow, shell }, artifactPaths) => {
      global.__ecorexPreviewRequests = [];
      global.__ecorexExternalOpenCalls = [];
      const originalOpenExternal = shell.openExternal.bind(shell);
      const originalOpenPath = shell.openPath.bind(shell);
      shell.openExternal = async (...args) => {
        global.__ecorexExternalOpenCalls.push({ method: 'openExternal', args });
        return '';
      };
      shell.openPath = async (...args) => {
        global.__ecorexExternalOpenCalls.push({ method: 'openPath', args });
        return '';
      };
      global.__ecorexRestoreShellOpen = () => {
        shell.openExternal = originalOpenExternal;
        shell.openPath = originalOpenPath;
      };

      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, payload = {}) => {
        const targetPath = String(payload.path || payload.filePath || '').replace(/\\/g, '/');
        global.__ecorexPreviewRequests.push({ path: targetPath, payload });
        if (targetPath.endsWith('preview-report.md')) {
          return {
            ok: true,
            path: targetPath,
            mimeType: 'text/markdown',
            language: 'markdown',
            startLine: 1,
            content: [
              '# Report',
              'line two overview',
              'line three precise edit target',
              'line four summary'
            ].join('\n')
          };
        }
        if (targetPath.endsWith('preview-page.html')) {
          return {
            ok: true,
            path: targetPath,
            mimeType: 'text/html',
            language: 'html',
            content: '<main><h1>Sandbox HTML Preview</h1><script>window.parent.__ecorexPreviewEscaped = true;</script></main>'
          };
        }
        if (targetPath.endsWith('creative.png')) {
          return {
            ok: false,
            unsupported: true,
            kind: 'image',
            path: targetPath,
            name: 'creative.png',
            mimeType: 'image/png',
            sizeBytes: 4096,
            metadata: { width: 1024, height: 768, colorMode: 'RGBA' },
            reason: 'Image metadata: 1024x768 RGBA. Preview remains inside EcoreX.'
          };
        }
        if (targetPath.endsWith('media-plan.pdf')) {
          return {
            ok: true,
            previewable: true,
            kind: 'pdf',
            renderMode: 'vue-office',
            path: targetPath,
            name: 'media-plan.pdf',
            mimeType: 'application/pdf',
            sizeBytes: 8192,
            previewUrl: 'http://127.0.0.1:65535/index.html?type=pdf&src=stub',
            metadata: { pages: 7, title: 'Media Plan', previewEngine: 'vue-office' }
          };
        }
        if (targetPath.endsWith('strategy-deck.pptx')) {
          return {
            ok: true,
            previewable: true,
            kind: 'office',
            renderMode: 'vue-office',
            path: targetPath,
            name: 'strategy-deck.pptx',
            mimeType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            sizeBytes: 16384,
            previewUrl: 'http://127.0.0.1:65535/index.html?type=pptx&src=stub',
            metadata: { slides: 12, title: 'Strategy Deck', previewEngine: 'vue-office' }
          };
        }
        return { ok: false, error: 'unknown preview target' };
      });

      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  'Generated artifacts:',
                  `- [preview-report.md](${artifactPaths.text}#L2)`,
                  `- [preview-page.html](${artifactPaths.html})`,
                  `- [creative.png](${artifactPaths.image})`,
                  `- [media-plan.pdf](${artifactPaths.pdf})`,
                  `- [strategy-deck.pptx](${artifactPaths.pptx})`
                ].join('\n')
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    }, artifactPaths);

    const initialWindowCount = await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
    await page.locator('[data-testid="chat-input"]').fill('show generated artifacts');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'Generated artifacts' }).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.artifact-preview-shelf')).toBeVisible();
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'preview-report.md' }).first()).toBeVisible();
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: '创建时间' }).first()).toBeVisible();

    async function openArtifact(name) {
      await page.locator('.artifact-thumb-card').filter({ hasText: name }).locator('.artifact-thumb-open').first().click();
      await expect(page.locator('.chat-layout')).toHaveClass(/preview-focus/);
      const card = page.locator('.artifact-focus-panel .artifact-preview-card');
      await expect(card).toBeVisible({ timeout: 10_000 });
      await expect(page.locator('.artifact-focus-head')).toContainText(name);
      await expect(card.locator('header')).toHaveCount(0);
      return card;
    }

    const textCard = await openArtifact('preview-report.md');
    await expect(textCard.locator('.artifact-text-preview')).toContainText('line three precise edit target');
    await textCard.locator('.artifact-line').filter({ hasText: 'line three precise edit target' }).click();
    await expect(page.locator('.composer-reference-chip')).toContainText('line three precise edit target');
    await expect(page.locator('.composer-reference-chip')).toContainText('preview-report.md:3');

    const htmlCard = await openArtifact('preview-page.html');
    const frame = htmlCard.locator('iframe.artifact-html-frame');
    await expect(frame).toBeVisible();
    await expect(frame).toHaveAttribute('sandbox', '');
    await expect(frame).toHaveAttribute('srcdoc', /Sandbox HTML Preview/);
    expect(await page.evaluate(() => window.__ecorexPreviewEscaped)).toBeUndefined();

    const imageCard = await openArtifact('creative.png');
    await expect(imageCard.locator('.artifact-meta-preview')).toBeVisible();
    await expect(imageCard).toContainText('image/png');
    await expect(imageCard).toContainText('1024x768');

    const pdfCard = await openArtifact('media-plan.pdf');
    const vueOfficeFrame = pdfCard.locator('iframe.artifact-vue-office-frame');
    await expect(vueOfficeFrame).toBeVisible();
    await expect(vueOfficeFrame).toHaveAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
    await expect(vueOfficeFrame).toHaveAttribute('src', /127\.0\.0\.1:65535\/index\.html/);

    const pptxCard = await openArtifact('strategy-deck.pptx');
    const pptxFrame = pptxCard.locator('iframe.artifact-vue-office-frame');
    await expect(pptxFrame).toBeVisible();
    await expect(pptxFrame).toHaveAttribute('src', /type=pptx/);

    await page.locator('.artifact-thumb-card').filter({ hasText: 'creative.png' }).locator('.artifact-thumb-remove').first().click();
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'creative.png' })).toHaveCount(0);

    const { finalWindowCount, previewRequests, externalOpenCalls } = await electronApp.evaluate(({ BrowserWindow }) => {
      try {
        global.__ecorexRestoreShellOpen?.();
      } catch {
        // Best-effort cleanup for the shell method stubs.
      }
      return {
        finalWindowCount: BrowserWindow.getAllWindows().length,
        previewRequests: global.__ecorexPreviewRequests || [],
        externalOpenCalls: global.__ecorexExternalOpenCalls || []
      };
    });
    expect(finalWindowCount).toBe(initialWindowCount);
    expect([...new Set(previewRequests.map((item) => path.basename(item.path)))].sort()).toEqual([
      'creative.png',
      'media-plan.pdf',
      'preview-page.html',
      'preview-report.md',
      'strategy-deck.pptx'
    ]);
    expect(externalOpenCalls).toEqual([]);
  });

  test('previews locally generated supported file formats through the real preview bridge', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    const userData = await appUserDataPath(electronApp);
    const fixtures = writeSupportedPreviewFixtures(path.join(userData, 'sessions', 'preview-fixtures'));
    const results = await page.evaluate(async (items) => {
      const previews = {};
      for (const item of items) {
        previews[item.name] = await window.ecorex.previewFile({
          path: item.path,
          source: 'assistant-artifact'
        });
      }
      return previews;
    }, fixtures);

    for (const fixture of fixtures) {
      expect.soft(results[fixture.name]?.ok, `${fixture.name} preview ok`).toBe(true);
      expect.soft(results[fixture.name]?.previewable, `${fixture.name} previewable`).toBe(true);
    }

    expect(results['sample.txt'].content).toContain('EcoreX text preview fixture');
    expect(results['sample.md'].renderMode).toBe('markdown');
    expect(results['sample.html'].renderMode).toBe('sandbox-srcdoc');
    expect(results['sample.json'].renderMode).toBe('json');
    expect(results['sample.csv'].renderMode).toBe('csv');
    expect(results['sample.png'].dataUrl).toMatch(/^data:image\/png;base64,/);
    expect(results['sample.svg'].dataUrl).toMatch(/^data:image\/svg\+xml;base64,/);

    for (const name of ['sample.pdf', 'sample.docx', 'sample.xlsx', 'sample.pptx']) {
      expect.soft(results[name].renderMode, `${name} render mode`).toBe('vue-office');
      expect.soft(results[name].previewUrl, `${name} preview url`).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/index\.html/);
    }
    expect(results['sample.xlsx'].previewUrl).toContain('type=excel');

    const officeEvents = await page.evaluate((previewItems) => Promise.all(previewItems.map((item) => new Promise((resolve) => {
      const iframe = document.createElement('iframe');
      iframe.id = `${item.name}-vue-office-smoke`;
      iframe.src = item.previewUrl;
      iframe.style.cssText = 'position:fixed;left:-10000px;top:0;width:960px;height:620px;border:0;opacity:0.01;pointer-events:none;';
      const cleanup = () => {
        window.clearTimeout(timeout);
        window.removeEventListener('message', onMessage);
        iframe.remove();
      };
      const timeout = window.setTimeout(() => {
        cleanup();
        resolve({ kind: 'timeout', detail: '', name: item.name });
      }, 15000);
      const onMessage = (event) => {
        const data = event.data || {};
        if (data.type !== 'ecorex-vue-office-preview' || data.name !== item.name) return;
        cleanup();
        resolve({ kind: data.kind, detail: data.detail || '', name: data.name });
      };
      window.addEventListener('message', onMessage);
      document.body.appendChild(iframe);
    }))), [
      { name: 'sample.docx', previewUrl: results['sample.docx'].previewUrl },
      { name: 'sample.xlsx', previewUrl: results['sample.xlsx'].previewUrl },
      { name: 'sample.pptx', previewUrl: results['sample.pptx'].previewUrl }
    ]);

    const eventByName = Object.fromEntries(officeEvents.map((event) => [event.name, event]));
    for (const name of ['sample.docx', 'sample.xlsx', 'sample.pptx']) {
      expect(eventByName[name].detail || '').not.toContain('anchors');
      expect(eventByName[name].kind, JSON.stringify(eventByName[name])).toBe('rendered');
    }
    expect(JSON.parse(eventByName['sample.docx'].detail || '{}').text).toContain('EcoreX docx preview fixture');
    expect(JSON.parse(eventByName['sample.pptx'].detail || '{}').text).toContain('EcoreX pptx preview fixture');
  });

  test('renders generated image artifacts inline inside assistant rich text', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'inline-image-artifacts');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const imagePath = path.join(artifactRoot, 'inline-creative.png').replace(/\\/g, '/');
    const imageDataUrl = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=';

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, payload) => {
      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, previewPayload = {}) => {
        const targetPath = String(previewPayload.path || previewPayload.filePath || '').replace(/\\/g, '/');
        if (targetPath === payload.imagePath) {
          return {
            ok: true,
            previewable: true,
            kind: 'image',
            path: targetPath,
            name: 'inline-creative.png',
            mimeType: 'image/png',
            previewUrl: payload.imageDataUrl,
            sizeBytes: 68,
            metadata: { width: 1, height: 1 }
          };
        }
        return { ok: false, reason: 'not-found' };
      });

      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, runPayload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: runPayload.sessionId,
            events: [
              {
                sessionId: runPayload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  'Inline generated image:',
                  `- [inline-creative.png](${payload.imagePath})`
                ].join('\n')
              },
              { sessionId: runPayload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: runPayload.sessionId,
          initialEvent: { sessionId: runPayload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    }, { imagePath, imageDataUrl });

    await page.locator('[data-testid="chat-input"]').fill('show inline generated image');
    await page.locator('[data-testid="chat-input"]').press('Enter');

    const assistantCard = page.locator('.assistant-card').filter({ hasText: 'Inline generated image' }).first();
    await expect(assistantCard).toBeVisible({ timeout: 10_000 });
    await expect(assistantCard.locator('[data-testid="chat-inline-artifact-image"] img')).toBeVisible({ timeout: 10_000 });
    await expect(assistantCard.locator('[data-testid="chat-inline-artifact-image"] img')).toHaveAttribute('src', /^data:image\/png;base64,/);
    await expect(assistantCard.locator('.artifact-thumb-card').filter({ hasText: 'inline-creative.png' })).toBeVisible();
  });

  test('allows same-session AI artifacts outside the workspace without previewing arbitrary local files', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const outsideDir = path.join(paths.root, 'Outside Workspace Artifacts');
    fs.mkdirSync(outsideDir, { recursive: true });
    const artifactPath = path.join(outsideDir, 'agent-output.md');
    fs.writeFileSync(artifactPath, '# Agent Output\nselectable generated artifact\n', 'utf8');
    const sessionId = `artifact-session-${Date.now()}`;

    const blocked = await page.evaluate(async (artifactPath) => window.ecorex.previewFile({ path: artifactPath }), artifactPath);
    expect(blocked.ok).toBe(false);
    expect(blocked.error).toContain('outside allowed roots');

    const userData = await appUserDataPath(electronApp);
    const sessionsDir = path.join(userData, 'sessions');
    fs.mkdirSync(sessionsDir, { recursive: true });
    fs.writeFileSync(
      path.join(sessionsDir, `${new Date().toISOString().replace(/[:.]/g, '-')}-${sessionId}.json`),
      JSON.stringify({
        sessionId,
        status: 'completed',
        workspacePath: 'workspace:/',
        events: [
          {
            kind: 'tool',
            status: 'completed',
            textPreview: `File created successfully at: ${artifactPath}`,
            ledger: {
              type: 'tool',
              phase: 'finish',
              toolName: 'Write',
              inputSummary: JSON.stringify({ file_path: artifactPath }),
              outputSummary: `File created successfully at: ${artifactPath}`,
              status: 'completed'
            }
          }
        ]
      }, null, 2),
      'utf8'
    );

    const preview = await page.evaluate(async ({ artifactPath, sessionId }) => window.ecorex.previewFile({
      path: artifactPath,
      sessionId,
      source: 'assistant-artifact'
    }), { artifactPath, sessionId });
    expect(preview.ok).toBe(true);
    expect(preview.previewable).toBe(true);
    expect(preview.text || preview.content).toContain('selectable generated artifact');
    expect(preview.file.pathLabel).toBe('artifact:/agent-output.md');
  });

  test('renders AI artifact outputs as hideable file cards before previewing them', async ({ ecorex }) => {
    const { electronApp, page } = ecorex;
    await login(page);

    const userData = await appUserDataPath(electronApp);
    const artifactRoot = path.join(userData, 'workspace', 'artifact-cards');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const artifactPaths = {
      md: path.join(artifactRoot, 'card-report.md').replace(/\\/g, '/'),
      html: path.join(artifactRoot, 'card-page.html').replace(/\\/g, '/')
    };
    fs.writeFileSync(artifactPaths.md, ['# Card Report', 'select this exact card line', 'closing line'].join('\n'));
    fs.writeFileSync(artifactPaths.html, '<h1>Card Preview</h1>');

    await electronApp.evaluate(({ ipcMain, BrowserWindow, shell }, artifactPaths) => {
      global.__ecorexExternalOpenCalls = [];
      const originalOpenExternal = shell.openExternal.bind(shell);
      const originalOpenPath = shell.openPath.bind(shell);
      shell.openExternal = async (...args) => {
        global.__ecorexExternalOpenCalls.push({ method: 'openExternal', args });
        return '';
      };
      shell.openPath = async (...args) => {
        global.__ecorexExternalOpenCalls.push({ method: 'openPath', args });
        return '';
      };
      global.__ecorexRestoreShellOpen = () => {
        shell.openExternal = originalOpenExternal;
        shell.openPath = originalOpenPath;
      };
      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, payload = {}) => {
        const targetPath = String(payload.path || payload.filePath || '').replace(/\\/g, '/');
        return {
          ok: true,
          path: targetPath,
          mimeType: targetPath.endsWith('.html') ? 'text/html' : 'text/markdown',
          language: targetPath.endsWith('.html') ? 'html' : 'markdown',
          startLine: 1,
          content: targetPath.endsWith('.html')
            ? '<h1>Card Preview</h1>'
            : ['# Card Report', 'select this exact card line', 'closing line'].join('\n')
        };
      });
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  'Artifact cards acceptance output:',
                  `- [card-report.md](${artifactPaths.md})`,
                  `- [card-page.html](${artifactPaths.html})`
                ].join('\n')
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return {
          ok: true,
          sessionId: payload.sessionId,
          initialEvent: { sessionId: payload.sessionId, kind: 'status', status: 'started', text: 'started' }
        };
      });
    }, artifactPaths);

    const initialWindowCount = await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
    await page.locator('[data-testid="chat-input"]').fill('produce artifact cards');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    const assistantCard = page.locator('.assistant-card').filter({ hasText: 'Artifact cards acceptance output' }).first();
    await expect(assistantCard).toBeVisible({ timeout: 10_000 });

    const fileCards = assistantCard.locator('[data-testid="artifact-file-card"], .artifact-file-card');
    const legacyPills = page.locator('.artifact-preview-strip button');
    expect.soft(await fileCards.count()).toBe(2);
    expect.soft(await legacyPills.count()).toBe(0);
    await expect.soft(fileCards.first()).toContainText('card-report.md');
    await expect.soft(fileCards.first().locator('[data-testid="artifact-file-icon"], .artifact-file-icon, svg').first()).toBeVisible();
    await expect.soft(fileCards.first().locator('[data-testid="artifact-file-produced-at"], time, .artifact-file-time').first()).toBeVisible();
    await expect.soft(fileCards.first().locator('[data-testid="artifact-file-open"], .artifact-file-open, [aria-label*="打开"], [title*="打开"]').first()).toBeVisible();

    const reportCard = page.locator('[data-testid="artifact-file-card"], .artifact-file-card').filter({ hasText: 'card-report.md' }).first();
    if (await reportCard.isVisible().catch(() => false)) {
      await reportCard.locator('[data-testid="artifact-file-open"], .artifact-file-open').first().click();
      await expect.soft(page.locator('.chat-layout')).toHaveClass(/preview-focus/);
      await expect.soft(page.locator('.artifact-focus-panel .artifact-preview-card')).toBeVisible();
      await page.locator('.artifact-line').filter({ hasText: 'select this exact card line' }).click();
      await expect.soft(page.locator('.composer-reference-chip')).toContainText(/card-report\.md:2/);
      await expect.soft(page.locator('.composer-reference-chip')).toContainText(/select this exact card line/);
      await page.locator('[data-testid="artifact-preview-local-open"]').first().click();
    }

    const hideButton = reportCard.locator('[data-testid="artifact-file-hide"], [data-testid="artifact-file-delete"], .artifact-file-hide, .artifact-file-delete, [aria-label*="隐藏"], [title*="隐藏"]').first();
    await expect.soft(hideButton).toBeVisible();
    if (await hideButton.isVisible().catch(() => false)) {
      await hideButton.click();
      expect.soft(await fileCards.count()).toBe(1);
    }

    const { finalWindowCount, externalOpenCalls } = await electronApp.evaluate(({ BrowserWindow }) => {
      try {
        global.__ecorexRestoreShellOpen?.();
      } catch {
        // Best-effort cleanup for the shell method stubs.
      }
      return {
        finalWindowCount: BrowserWindow.getAllWindows().length,
        externalOpenCalls: global.__ecorexExternalOpenCalls || []
      };
    });
    expect.soft(finalWindowCount).toBe(initialWindowCount);
    expect.soft(externalOpenCalls.some((call) => call.method === 'openPath' && String(call.args?.[0] || '').endsWith('card-report.md'))).toBe(true);
  });

  test('shows only final deliverable artifacts and keeps spaced paths previewable', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'final deliverables with spaces');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const draftPath = path.join(artifactRoot, 'draft working note.md');
    const finalPath = path.join(artifactRoot, 'final delivery report.md');
    fs.writeFileSync(draftPath, '# Draft\nintermediate only\n', 'utf8');
    fs.writeFileSync(finalPath, '# Final\napproved delivery\n', 'utf8');

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, paths) => {
      global.__ecorexPreviewRequests = [];
      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, payload = {}) => {
        const targetPath = String(payload.path || payload.filePath || '');
        global.__ecorexPreviewRequests.push(targetPath);
        if (targetPath === paths.finalPath) {
          return {
            ok: true,
            path: targetPath,
            mimeType: 'text/markdown',
            language: 'markdown',
            content: '# Final\napproved delivery\n'
          };
        }
        return { ok: false, error: 'not found' };
      });
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'assistant',
                status: 'running',
                text: `Intermediate draft generated at ${paths.draftPath}`
              },
              {
                sessionId: payload.sessionId,
                kind: 'tool',
                status: 'completed',
                toolName: 'Write',
                text: `File created successfully at: ${paths.draftPath}`,
                ledger: {
                  toolName: 'Write',
                  inputSummary: JSON.stringify({ file_path: paths.draftPath }),
                  outputSummary: `File created successfully at: ${paths.draftPath}`,
                  status: 'completed'
                }
              },
              {
                sessionId: payload.sessionId,
                kind: 'tool',
                status: 'completed',
                toolName: 'Write',
                text: `File created successfully at: ${paths.finalPath}`,
                ledger: {
                  toolName: 'Write',
                  inputSummary: JSON.stringify({ file_path: paths.finalPath }),
                  outputSummary: `File created successfully at: ${paths.finalPath}`,
                  status: 'completed'
                }
              },
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: `最终交付物：${paths.finalPath}`
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return { ok: true, sessionId: payload.sessionId, status: 'running' };
      });
    }, { draftPath, finalPath });

    await page.locator('[data-testid="chat-input"]').fill('produce final deliverable only');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: '最终交付物' }).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'final delivery report.md' })).toHaveCount(1);
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'draft working note.md' })).toHaveCount(0);

    await page.locator('.artifact-thumb-card').filter({ hasText: 'final delivery report.md' }).locator('.artifact-thumb-open').click();
    await expect(page.locator('.artifact-focus-panel .artifact-preview-card')).toContainText('approved delivery');
    const previewRequests = await electronApp.evaluate(() => global.__ecorexPreviewRequests || []);
    expect(previewRequests).toContain(finalPath);
    expect(previewRequests).not.toContain(draftPath);
  });

  test('creates separate final artifact cards from tool result paths named in the final reply', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'final-tool-result-artifacts');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const artifactPaths = {
      docx: path.join(artifactRoot, 'hello.docx'),
      pptx: path.join(artifactRoot, 'hello.pptx'),
      xlsx: path.join(artifactRoot, 'hello.xlsx')
    };
    for (const targetPath of Object.values(artifactPaths)) {
      fs.writeFileSync(targetPath, `placeholder for ${path.basename(targetPath)}`, 'utf8');
    }

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, payload) => {
      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, previewPayload = {}) => {
        const targetPath = String(previewPayload.path || previewPayload.filePath || '');
        if (Object.values(payload.artifactPaths).includes(targetPath)) {
          return {
            ok: true,
            path: targetPath,
            file: { path: targetPath, name: targetPath.split(/[\\/]/).pop() },
            mimeType: 'application/octet-stream',
            content: `preview ${targetPath}`
          };
        }
        return { ok: false, error: 'not found' };
      });
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, runPayload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        const rootLabel = payload.artifactRoot.endsWith('\\') || payload.artifactRoot.endsWith('/')
          ? payload.artifactRoot
          : `${payload.artifactRoot}\\`;
        setTimeout(() => {
          if (!win || win.isDestroyed()) return;
          win.webContents.send('agent:events', {
            sessionId: runPayload.sessionId,
            events: [
              {
                sessionId: runPayload.sessionId,
                kind: 'tool',
                status: 'completed',
                toolName: 'Write',
                text: [
                  `${payload.artifactPaths.docx} (934 bytes)`,
                  `${payload.artifactPaths.pptx} (1868 bytes)`,
                  `${payload.artifactPaths.xlsx} (1613 bytes)`
                ].join('\n')
              },
              {
                sessionId: runPayload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  'Created final files:',
                  '1. hello.docx',
                  '2. hello.pptx',
                  '3. hello.xlsx',
                  '',
                  `Path: ${rootLabel}`
                ].join('\n')
              },
              { sessionId: runPayload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return { ok: true, sessionId: runPayload.sessionId, status: 'running' };
      });
    }, { artifactRoot, artifactPaths });

    await page.locator('[data-testid="chat-input"]').fill('create office deliverables');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: 'Created final files' }).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'hello.docx' })).toHaveCount(1);
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'hello.pptx' })).toHaveCount(1);
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'hello.xlsx' })).toHaveCount(1);
  });

  test('does not render remote urls or bare filenames as AI artifact cards', async ({ ecorex }) => {
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'local-artifacts-only');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const localPath = path.join(artifactRoot, 'saved-output.html').replace(/\\/g, '/');
    fs.writeFileSync(localPath, '<h1>saved local output</h1>', 'utf8');

    await electronApp.evaluate(({ ipcMain, BrowserWindow }, localPath) => {
      ipcMain.removeHandler('file:preview');
      ipcMain.handle('file:preview', (_event, payload = {}) => {
        const targetPath = String(payload.path || payload.filePath || '');
        if (targetPath === localPath) {
          return {
            ok: true,
            path: targetPath,
            file: { path: targetPath, name: 'saved-output.html' },
            mimeType: 'text/html',
            renderMode: 'sandbox-srcdoc',
            content: '<h1>saved local output</h1>'
          };
        }
        return {
          ok: false,
          previewable: false,
          reason: 'not-found',
          file: { path: targetPath, name: String(targetPath || 'missing.html').split(/[\\/]/).pop() || 'missing.html' },
          error: 'File was not found.'
        };
      });
      ipcMain.removeHandler('agent:run');
      ipcMain.handle('agent:run', (event, payload) => {
        const win = BrowserWindow.fromWebContents(event.sender);
        setTimeout(() => {
          win?.webContents.send('agent:events', {
            sessionId: payload.sessionId,
            events: [
              {
                sessionId: payload.sessionId,
                kind: 'result',
                status: 'completed',
                text: [
                  '交付内容：',
                  '- [网页链接](https://example.com/202605.html)',
                  '- [未保存文件](202605.html)',
                  `- [saved-output.html](${localPath})`
                ].join('\n')
              },
              { sessionId: payload.sessionId, kind: 'done', status: 'completed', text: 'done' }
            ]
          });
        }, 40);
        return { ok: true, sessionId: payload.sessionId, status: 'running' };
      });
    }, localPath);

    await page.locator('[data-testid="chat-input"]').fill('show only local saved artifacts');
    await page.locator('[data-testid="chat-input"]').press('Enter');
    await expect(page.locator('.assistant-card').filter({ hasText: '交付内容' }).first()).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.artifact-thumb-card')).toHaveCount(1);
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'saved-output.html' })).toBeVisible();
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: '202605.html' })).toHaveCount(0);
    await expect(page.locator('.artifact-thumb-card').filter({ hasText: 'shanghai.html' })).toHaveCount(0);
    await page.locator('.artifact-thumb-card').filter({ hasText: 'saved-output.html' }).locator('.artifact-thumb-open').click();
    await expect(page.locator('.artifact-focus-panel .artifact-html-frame')).toBeVisible();
    await expect(page.frameLocator('.artifact-html-frame').locator('h1')).toHaveText('saved local output');
  });
});

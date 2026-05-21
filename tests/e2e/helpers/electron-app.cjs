const fs = require('fs');
const os = require('os');
const path = require('path');
const { _electron: electron, expect } = require('@playwright/test');
const electronPath = require('electron');

const repoRoot = path.resolve(__dirname, '..', '..', '..');
const appUrlPattern = /^http:\/\/127\.0\.0\.1:5188(?:\/|$)/;
const secretEnvKeys = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY',
  'ANTHROPIC_BASE_URL',
  'OPENAI_BASE_URL'
];

function makeTempAppPaths() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ecorex-e2e-'));
  const paths = {
    root,
    appData: path.join(root, 'AppData', 'Roaming'),
    localAppData: path.join(root, 'AppData', 'Local'),
    temp: path.join(root, 'Temp'),
    userData: path.join(root, 'ElectronUserData')
  };
  fs.mkdirSync(paths.appData, { recursive: true });
  fs.mkdirSync(paths.localAppData, { recursive: true });
  fs.mkdirSync(paths.temp, { recursive: true });
  fs.mkdirSync(paths.userData, { recursive: true });
  return paths;
}

function testEnv(paths) {
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

async function waitForAppWindow(electronApp, timings = {}) {
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

  throw new Error(`Timed out waiting for EcoreX renderer window. Seen windows: ${lastUrls.join(', ') || 'none'}`);
}

async function launchEcorex() {
  const paths = makeTempAppPaths();
  const timings = { launchStartedAt: Date.now() };
  const electronApp = await electron.launch({
    executablePath: electronPath,
    args: ['--no-sandbox', `--user-data-dir=${paths.userData}`, '.'],
    cwd: repoRoot,
    env: testEnv(paths),
    timeout: 45 * 1000
  });
  timings.electronLaunchMs = Date.now() - timings.launchStartedAt;
  const page = await waitForAppWindow(electronApp, timings);
  return { electronApp, page, paths, timings };
}

async function closeEcorex(instance) {
  if (!instance) return;
  try {
    await instance.electronApp?.close();
  } finally {
    if (instance.paths?.root) {
      fs.rmSync(instance.paths.root, { recursive: true, force: true });
    }
  }
}

async function login(page, options = {}) {
  const email = options.email || 'e2e.owner@ecorex.local';
  const password = options.password || 'EcoreX123!';
  const startedAt = Date.now();

  if (await page.locator('[data-testid="app-shell"], .app-shell').first().isVisible().catch(() => false)) {
    return { alreadyLoggedIn: true, loginMs: 0 };
  }
  await expect(page.locator('[data-testid="login-form"], form.login-panel').first()).toBeVisible();
  await page.locator('[data-testid="login-email-input"], form.login-panel input[type="email"]').first().fill(email);
  await page.locator('[data-testid="login-secret-input"], form.login-panel input[type="password"]').first().fill(password);
  await page.locator('[data-testid="login-submit-button"], form.login-panel button.primary.wide').first().click();

  await expect(page.locator('[data-testid="app-shell"], .app-shell').first()).toBeVisible({ timeout: 20 * 1000 });
  await expect(page.locator('.side-nav')).toBeVisible();
  return { alreadyLoggedIn: false, loginMs: Date.now() - startedAt };
}

module.exports = {
  closeEcorex,
  launchEcorex,
  login,
  repoRoot
};

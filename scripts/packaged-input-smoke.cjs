const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { chromium } = require('@playwright/test');

const repoRoot = path.resolve(__dirname, '..');
const packagedExe = path.join(repoRoot, 'release', 'win-unpacked', 'EcoreX Agent.exe');
const secretEnvKeys = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY',
  'ANTHROPIC_BASE_URL',
  'OPENAI_BASE_URL',
  'ECOREX_REAL_MODEL_API_KEY'
];

function addNoProxy() {
  const entries = new Set(
    String(process.env.NO_PROXY || process.env.no_proxy || '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  );
  entries.add('127.0.0.1');
  entries.add('localhost');
  process.env.NO_PROXY = [...entries].join(',');
  process.env.no_proxy = process.env.NO_PROXY;
}

function tempPaths() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ecorex-packaged-input-'));
  return {
    root,
    appData: path.join(root, 'AppData', 'Roaming'),
    localAppData: path.join(root, 'AppData', 'Local'),
    temp: path.join(root, 'Temp'),
    userData: path.join(root, 'UserData')
  };
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
      });
      response.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.setTimeout(1000, () => request.destroy(new Error('timeout')));
  });
}

async function waitForDebugPort(port) {
  const deadline = Date.now() + 30_000;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      return await getJson(`http://127.0.0.1:${port}/json/version`);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw lastError || new Error('Timed out waiting for packaged DevTools endpoint.');
}

async function findAppPage(browser) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    for (const page of pages) {
      const hasFrame = await page.locator('.app-frame').count().catch(() => 0);
      if (hasFrame) {
        page.setDefaultTimeout(10_000);
        return page;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Timed out waiting for packaged renderer page.');
}

function makeEnv(paths) {
  const env = {
    ...process.env,
    APPDATA: paths.appData,
    LOCALAPPDATA: paths.localAppData,
    TEMP: paths.temp,
    TMP: paths.temp,
    ECOREX_E2E: '1',
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true'
  };
  delete env.ELECTRON_RUN_AS_NODE;
  for (const key of secretEnvKeys) delete env[key];
  return env;
}

async function main() {
  addNoProxy();
  if (!fs.existsSync(packagedExe)) {
    throw new Error(`Packaged app not found: ${packagedExe}. Run npm run pack or npm run dist first.`);
  }

  const paths = tempPaths();
  for (const dir of [paths.appData, paths.localAppData, paths.temp, paths.userData]) {
    fs.mkdirSync(dir, { recursive: true });
  }

  const port = 9600 + Math.floor(Math.random() * 300);
  const child = spawn(
    packagedExe,
    [`--remote-debugging-port=${port}`, `--user-data-dir=${paths.userData}`],
    {
      cwd: path.dirname(packagedExe),
      env: makeEnv(paths),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: false
    }
  );
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });

  try {
    const version = await waitForDebugPort(port);
    const browser = await chromium.connectOverCDP(version.webSocketDebuggerUrl || `http://127.0.0.1:${port}`);
    try {
      const page = await findAppPage(browser);
      await page.bringToFront();
      await page.locator('[data-testid="login-email-input"]').waitFor({ state: 'visible' });
      await page.locator('[data-testid="login-email-input"]').click();
      await page.keyboard.type('e2e.owner@ecorex.local');
      await page.locator('[data-testid="login-secret-input"]').click();
      await page.keyboard.type('EcoreX123!');

      const loginValues = await page.evaluate(() => ({
        email: document.querySelector('[data-testid="login-email-input"]')?.value || '',
        secretLength: document.querySelector('[data-testid="login-secret-input"]')?.value?.length || 0,
        focused: document.activeElement?.getAttribute('data-testid') || document.activeElement?.tagName || ''
      }));

      if (loginValues.email !== 'e2e.owner@ecorex.local' || loginValues.secretLength !== 'EcoreX123!'.length) {
        throw new Error(`Packaged login inputs did not accept text: ${JSON.stringify(loginValues)}`);
      }

      await page.locator('[data-testid="login-submit-button"]').click();
      await page.locator('[data-testid="app-shell"]').waitFor({ state: 'visible', timeout: 20_000 });
      await page.locator('[data-testid="chat-input"]').click();
      await page.keyboard.type('packaged input smoke abc');

      const chatValues = await page.evaluate(() => {
        const textarea = document.querySelector('[data-testid="chat-input"]');
        const rect = textarea?.getBoundingClientRect();
        const hit = rect ? document.elementFromPoint(rect.left + 8, rect.top + 8) : null;
        return {
          value: textarea?.value || '',
          focused: document.activeElement?.getAttribute('data-testid') || document.activeElement?.tagName || '',
          region: textarea ? window.getComputedStyle(textarea).webkitAppRegion : '',
          hitTestId: hit?.getAttribute?.('data-testid') || '',
          hitTag: hit?.tagName || ''
        };
      });

      if (chatValues.value !== 'packaged input smoke abc') {
        throw new Error(`Packaged chat input did not accept text: ${JSON.stringify(chatValues)}`);
      }

      console.log(JSON.stringify({
        ok: true,
        packagedExe,
        login: loginValues,
        chat: chatValues
      }, null, 2));
    } finally {
      await browser.close().catch(() => {});
    }
  } finally {
    child.kill();
    setTimeout(() => fs.rmSync(paths.root, { recursive: true, force: true }), 500);
    const meaningfulStderr = stderr
      .split(/\r?\n/)
      .filter((line) => line.trim() && !line.includes('DevTools listening on'))
      .join('\n');
    if (meaningfulStderr) {
      console.error(meaningfulStderr);
    }
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});

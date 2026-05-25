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
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ecorex-packaged-project-'));
  return {
    root,
    appData: path.join(root, 'AppData', 'Roaming'),
    localAppData: path.join(root, 'AppData', 'Local'),
    temp: path.join(root, 'Temp'),
    userData: path.join(root, 'UserData'),
    workspace: path.join(root, 'Workspace')
  };
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
        page.setDefaultTimeout(12_000);
        return page;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Timed out waiting for packaged renderer page.');
}

async function login(page) {
  if (await page.locator('[data-testid="app-shell"]').first().isVisible().catch(() => false)) return;
  await page.locator('[data-testid="login-email-input"]').waitFor({ state: 'visible', timeout: 20_000 });
  await page.locator('[data-testid="login-email-input"]').fill('e2e.owner@ecorex.local');
  await page.locator('[data-testid="login-secret-input"]').fill('EcoreX123!');
  await page.locator('[data-testid="login-submit-button"]').click();
  await page.locator('[data-testid="app-shell"]').waitFor({ state: 'visible', timeout: 20_000 });
}

function samePath(left, right) {
  const resolvedLeft = path.resolve(left);
  const resolvedRight = path.resolve(right);
  return process.platform === 'win32'
    ? resolvedLeft.toLowerCase() === resolvedRight.toLowerCase()
    : resolvedLeft === resolvedRight;
}

function waitForProcessExit(child, timeoutMs = 5000) {
  if (!child || child.exitCode !== null || child.signalCode) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

function removeTempRoot(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  } catch {
    // Packaged smoke tests must not fail after assertions because a sidecar still
    // holds a temporary file for a moment.
  }
}

async function main() {
  addNoProxy();
  if (!fs.existsSync(packagedExe)) {
    throw new Error(`Packaged app not found: ${packagedExe}. Run npm run dist first.`);
  }

  const paths = tempPaths();
  for (const dir of [paths.appData, paths.localAppData, paths.temp, paths.userData, paths.workspace]) {
    fs.mkdirSync(dir, { recursive: true });
  }

  const port = 9900 + Math.floor(Math.random() * 300);
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
      await login(page);

      const settings = await page.evaluate(async (workspaceRoot) => window.ecorex.updateSettings({
        workspaceRoot,
        confirmCustomWorkspaceRoot: true
      }), paths.workspace);
      if (!settings?.ok) throw new Error(`Failed to set packaged workspace: ${JSON.stringify(settings)}`);

      const suffix = Date.now();
      const originalName = `Packaged Project ${suffix}`;
      const renamedName = `Packaged Project Renamed ${suffix}`;

      await page.locator('[data-testid="sidebar-projects-nav"]').click();
      await page.locator('[data-testid="projects-create-name"]').waitFor({ state: 'visible', timeout: 20_000 });
      await page.locator('[data-testid="projects-create-name"]').fill(originalName);
      await page.locator('[data-testid="projects-create-submit"]').click();
      await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: originalName }).first().waitFor({ state: 'visible', timeout: 20_000 });

      const created = await page.evaluate(async (name) => {
        const listed = await window.ecorex.listProjects();
        return listed.projects.find((project) => project.name === name);
      }, originalName);
      if (!created) throw new Error('Packaged project was not created.');
      const createdDir = path.join(paths.workspace, created.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
      if (!fs.existsSync(createdDir)) throw new Error(`Created project directory missing: ${createdDir}`);

      await page.locator('[data-testid="project-edit-name"]').fill(renamedName);
      await page.locator('[data-testid="project-detail-save"]').click();
      await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName }).first().waitFor({ state: 'visible', timeout: 20_000 });

      const renamed = await page.evaluate(async (name) => {
        const listed = await window.ecorex.listProjects();
        return listed.projects.find((project) => project.name === name);
      }, renamedName);
      if (!renamed) throw new Error('Packaged project was not renamed.');
      const renamedDir = path.join(paths.workspace, renamed.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
      if (fs.existsSync(createdDir)) throw new Error(`Old project directory still exists after rename: ${createdDir}`);
      if (!fs.existsSync(renamedDir)) throw new Error(`Renamed project directory missing: ${renamedDir}`);

      page.once('dialog', (dialog) => dialog.accept());
      await page.locator('[data-testid="project-detail-delete"]').click();
      await page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName }).waitFor({ state: 'detached', timeout: 20_000 });

      const afterDelete = await page.evaluate(async () => window.ecorex.listProjects());
      if (afterDelete.projects.some((project) => project.id === renamed.id)) {
        throw new Error('Deleted project still appears in packaged project list.');
      }
      if (fs.existsSync(renamedDir)) throw new Error(`Renamed project directory still exists after delete: ${renamedDir}`);

      console.log(JSON.stringify({
        ok: true,
        packagedExe,
        workspace: paths.workspace,
        created: created.pathLabel,
        renamed: renamed.pathLabel,
        deletedProjectId: renamed.id
      }, null, 2));
    } finally {
      await browser.close().catch(() => {});
    }
  } finally {
    child.kill();
    await waitForProcessExit(child);
    removeTempRoot(paths.root);
    const meaningfulStderr = stderr
      .split(/\r?\n/)
      .filter((line) => line.trim() && !line.includes('DevTools listening on'))
      .join('\n');
    if (meaningfulStderr) console.error(meaningfulStderr);
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});

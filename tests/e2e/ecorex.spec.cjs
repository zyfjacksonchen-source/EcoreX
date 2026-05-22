const base = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const { closeEcorex, launchEcorex, login } = require('./helpers/electron-app.cjs');

const { expect } = base;

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

  test('manages advertising projects with metadata and archive state', async ({ ecorex }) => {
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
        goal: '提升新品线索转化并沉淀 A/B 测试结论',
        status: 'paused'
      });
      const archived = await window.ecorex.archiveProject({ id: created.project.id });
      const listed = await window.ecorex.listProjects();
      return { settings, created, status, updated, archived, listed };
    }, { projectWorkspace });

    expect(result.settings.ok).toBe(true);
    expect(result.created.ok).toBe(true);
    expect(result.created.project.client).toBe('星河饮品');
    expect(result.created.project.memoryLabel).toContain('.ecorex-memory');
    expect(result.status.activeProject.id).toBe(result.created.project.id);
    expect(result.updated.project.status).toBe('paused');
    expect(result.archived.project.status).toBe('archived');
    expect(result.listed.projects.some((project) => project.id === result.created.project.id && project.archived)).toBe(true);
  });
});

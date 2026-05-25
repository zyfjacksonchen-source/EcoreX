const base = require('@playwright/test');
const fs = require('fs');
const http = require('http');
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
    await expect(page.locator('.running-session-strip')).toBeHidden({ timeout: 15_000 });
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
    await expect(page.locator('.recent-empty')).toContainText('暂无最近对话');

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
    await expect(page.locator('.recent-empty')).toContainText('暂无最近对话');
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
    await expect(page.locator('.inline-permission-request')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.inline-permission-actions button').filter({ hasText: '允许一次' })).toBeVisible();
    await page.locator('.inline-permission-actions button').filter({ hasText: '允许一次' }).click();
    await expect(page.locator('.inline-permission-request')).toHaveCount(0);
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
    await expect(page.locator('[data-testid="projects-create-name"]')).toBeEnabled({ timeout: 15_000 });

    const suffix = Date.now();
    const originalName = `UI Project ${suffix}`;
    const renamedName = `UI Project Renamed ${suffix}`;

    await page.locator('[data-testid="projects-create-name"]').fill(originalName);
    await expect(page.locator('[data-testid="projects-create-submit"]')).toBeEnabled();
    await page.locator('[data-testid="projects-create-submit"]').click();
    await expect(page.locator('[data-testid="projects-list-entry"]').filter({ hasText: originalName }).first()).toBeVisible({ timeout: 15_000 });

    const createdProject = await page.evaluate(async (name) => {
      const listed = await window.ecorex.listProjects();
      return listed.projects.find((project) => project.name === name);
    }, originalName);
    expect(createdProject).toBeTruthy();
    const createdDir = path.join(projectWorkspace, createdProject.pathLabel.replace(/^workspace:\//, '').replace(/\//g, path.sep));
    expect(fs.existsSync(createdDir)).toBe(true);

    await page.locator('[data-testid="project-edit-name"]').fill(renamedName);
    await page.locator('[data-testid="project-detail-save"]').click();
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
    await page.evaluate(() => {
      window.__ecorexOriginalPrompt = window.prompt;
      window.prompt = () => 'Project Session Renamed';
    });
    await page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Draft' }).locator('[aria-label="重命名会话"]').click({ force: true });
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' })).toBeVisible();
    const renamedSession = await page.evaluate((sessionId) => {
      const items = JSON.parse(localStorage.getItem('ecorex-recent-chats') || '[]');
      return items.find((item) => item.id === sessionId);
    }, sessionId);
    expect(renamedSession.title).toBe('Project Session Renamed');
    await page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' }).locator('[aria-label="删除会话"]').click({ force: true });
    await expect(page.locator('.sidebar-project-session-row').filter({ hasText: 'Project Session Renamed' })).toHaveCount(0);
    await page.evaluate(() => {
      if (window.__ecorexOriginalPrompt) window.prompt = window.__ecorexOriginalPrompt;
    });

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
    await expect(page.locator('[data-testid="projects-list-entry"]').filter({ hasText: renamedName })).toHaveCount(0, { timeout: 15_000 });
    const listedAfterDelete = await page.evaluate(async () => window.ecorex.listProjects());
    expect(listedAfterDelete.projects.some((project) => project.id === renamedProject.id)).toBe(false);
    expect(fs.existsSync(renamedDir)).toBe(false);
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
    await expect(page.locator('.agent-trace-list')).toHaveCount(0);

    await trace.locator('.agent-trace-summary').click();
    await expect(trace).toHaveClass(/expanded/);
    await expect(trace.locator('.agent-trace-row')).toHaveCount(6);
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
      pdf: path.join(artifactRoot, 'media-plan.pdf').replace(/\\/g, '/')
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
            renderMode: 'kkfileview',
            path: targetPath,
            name: 'media-plan.pdf',
            mimeType: 'application/pdf',
            sizeBytes: 8192,
            previewUrl: 'http://127.0.0.1:65535/onlinePreview?url=stub',
            metadata: { pages: 7, title: 'Media Plan', previewEngine: 'kkFileView' }
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
                  `- [media-plan.pdf](${artifactPaths.pdf})`
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
      await expect(card.locator('header')).toContainText(name);
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
    const kkFrame = pdfCard.locator('iframe.artifact-kkfileview-frame');
    await expect(kkFrame).toBeVisible();
    await expect(kkFrame).toHaveAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
    await expect(kkFrame).toHaveAttribute('src', /127\.0\.0\.1:65535\/onlinePreview/);

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
      'preview-report.md'
    ]);
    expect(externalOpenCalls).toEqual([]);
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
    const { electronApp, page, paths } = ecorex;
    await login(page);

    const artifactRoot = path.join(paths.root, 'artifact-cards');
    fs.mkdirSync(artifactRoot, { recursive: true });
    const artifactPaths = {
      md: path.join(artifactRoot, 'card-report.md').replace(/\\/g, '/'),
      html: path.join(artifactRoot, 'card-page.html').replace(/\\/g, '/')
    };

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
    await expect(page.locator('.assistant-card').filter({ hasText: 'Artifact cards acceptance output' }).first()).toBeVisible({ timeout: 10_000 });

    const fileCards = page.locator('[data-testid="artifact-file-card"], .artifact-file-card');
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
    expect.soft(externalOpenCalls).toEqual([]);
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
            content: '<h1>saved local output</h1>'
          };
        }
        return { ok: false, reason: 'not-found', error: 'not found' };
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
  });
});

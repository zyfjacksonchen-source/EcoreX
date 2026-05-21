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
  const nav = page.locator('[data-testid="nav-diagnostics"]');
  if (await nav.count()) {
    await nav.click();
  } else {
    await page.locator('.side-nav button').nth(2).click();
  }
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
  });

  test('opens diagnostics from the sidebar and shows the health check area', async ({ ecorex }) => {
    const { page } = ecorex;
    await login(page);

    await openDiagnostics(page);

    await expect(page.getByRole('heading', { name: /诊断 \/ 设置/ })).toBeVisible();
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

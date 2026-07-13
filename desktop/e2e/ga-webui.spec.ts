import {
  expect,
  test as base,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";

const LOCAL_ORIGIN = "http://127.0.0.1:4179";
const ARTIFACT_NAME = "活动主视觉_20260710-1534_01.png";

interface AccessibilitySummary {
  violations: Array<{ id: string; impact: string | null; nodes: number }>;
  incomplete: Array<{ id: string; impact: string | null; nodes: number }>;
  passes: number;
}

interface GaViewportReport {
  viewport: {
    expected_width: number;
    expected_height: number;
    actual_width: number;
    actual_height: number;
  };
  theme: { expected: "light" | "dark"; actual: string | null };
  horizontal_overflow: { present: boolean; overflow_pixels: number };
  wrapped_clickable_labels: Array<{ label: string; lines: number }>;
  key_controls: Record<string, { present: boolean; visible: boolean }>;
  accessibility: AccessibilitySummary;
  passed: boolean;
}

interface GuardState {
  externalRequests: string[];
  browserErrors: string[];
}

async function installFailClosedGuards(page: Page): Promise<GuardState> {
  const state: GuardState = { externalRequests: [], browserErrors: [] };
  page.on("pageerror", (error) => state.browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") {
      const location = message.location();
      // This exact 404 is the intentional negative fixture used to prove that
      // a stale/manual task ID cannot blank the current conversation.
      if (
        message.text().includes("404 (Not Found)")
        && location.url.includes("/api/v1/threads/thr_missing_ga/projection")
      ) return;
      const suffix = location.url
        ? ` @ ${location.url}:${location.lineNumber}:${location.columnNumber}`
        : "";
      state.browserErrors.push(`${message.text()}${suffix}`);
    }
  });
  await page.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    const parsed = new URL(requestUrl);
    if (
      parsed.origin === LOCAL_ORIGIN
      || parsed.protocol === "blob:"
      || parsed.protocol === "data:"
    ) {
      await route.continue();
      return;
    }
    state.externalRequests.push(requestUrl);
    await route.abort("blockedbyclient");
  });
  return state;
}

function assertGuardState(state: GuardState): void {
  expect(state.externalRequests, "the GA browser gate must never use the public network").toEqual([]);
  expect(state.browserErrors, "page and console errors fail the GA browser gate").toEqual([]);
}

const test = base.extend<{ guardedPage: Page }>({
  guardedPage: async ({ page }, use, testInfo) => {
    const state = await installFailClosedGuards(page);
    await use(page);
    if (testInfo.status === testInfo.expectedStatus) assertGuardState(state);
  },
});

async function openArtifactScenario(page: Page, theme: "light" | "dark" = "light"): Promise<void> {
  await openThreadScenario(page, "artifact", theme);
  await expect(page.getByRole("region", { name: "任务产物" })).toBeVisible();
  await expect(page.locator("button.ex-artifact-primary").filter({ hasText: ARTIFACT_NAME })).toBeVisible();
}

async function openThreadScenario(
  page: Page,
  scenario: "thinking" | "retry" | "hitl" | "connector-login" | "connector-device" | "connector-reauth" | "connector-restart" | "artifact" | "replay" | "thread-switch",
  theme: "light" | "dark" = "light",
): Promise<void> {
  await page.goto(`/__ga/frame-app?scenario=${scenario}&theme=${theme}`, {
    waitUntil: "domcontentloaded",
  });
  const openThread = page.getByRole("button", { name: /^打开任务：/ }).first();
  const viewport = page.viewportSize();
  if (viewport && viewport.width < 840) {
    await expect(page.getByRole("button", { name: "打开任务导航" })).toBeVisible();
    await page.getByRole("button", { name: "打开任务导航" }).click();
  }
  await expect(openThread).toBeVisible();
  await openThread.click();
  await expect(page.getByRole("region", { name: "对话" })).toBeVisible();
}

async function withGuardedContext(
  browser: Browser,
  options: Parameters<Browser["newContext"]>[0],
  run: (page: Page, context: BrowserContext) => Promise<void>,
): Promise<void> {
  const context = await browser.newContext({
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    ...options,
  });
  const page = await context.newPage();
  const state = await installFailClosedGuards(page);
  try {
    await run(page, context);
    assertGuardState(state);
  } finally {
    await context.close();
  }
}

const viewports = [
  { id: "1440x900", width: 1440, height: 900 },
  { id: "1024x768", width: 1024, height: 768 },
  { id: "768x900", width: 768, height: 900 },
  { id: "390x844", width: 390, height: 844 },
  { id: "320x568", width: 320, height: 568 },
] as const;

for (const viewport of viewports) {
  for (const theme of ["light", "dark"] as const) {
    test(`${viewport.id} ${theme} GA report passes with zero axe violations`, async ({ guardedPage }, testInfo) => {
      await guardedPage.goto(
        `/__ga/viewport?viewport=${viewport.id}&theme=${theme}&scenario=artifact`,
        { waitUntil: "domcontentloaded" },
      );
      await expect(guardedPage.locator("body")).toHaveAttribute("data-ga-status", "passed", {
        timeout: 20_000,
      });
      const report = await guardedPage.evaluate(() => (
        window as unknown as { __ECOREX_GA_VIEWPORT_REPORT__: GaViewportReport }
      ).__ECOREX_GA_VIEWPORT_REPORT__);

      expect(report.passed).toBe(true);
      expect(report.viewport).toMatchObject({
        expected_width: viewport.width,
        expected_height: viewport.height,
        actual_width: viewport.width,
        actual_height: viewport.height,
      });
      expect(report.theme).toEqual({ expected: theme, actual: theme });
      expect(report.horizontal_overflow.present).toBe(false);
      expect(report.horizontal_overflow.overflow_pixels).toBeLessThanOrEqual(1);
      expect(report.wrapped_clickable_labels).toEqual([]);
      for (const control of Object.values(report.key_controls)) {
        expect(control.present).toBe(true);
        expect(control.visible).toBe(true);
      }
      expect(report.accessibility.violations).toEqual([]);
      expect(report.accessibility.passes).toBeGreaterThan(0);
      await testInfo.attach(`ecorex-${viewport.id}-${theme}`, {
        body: await guardedPage.locator("#ga-viewport-frame").screenshot(),
        contentType: "image/png",
      });
    });
  }
}

test("ordinary controls keep Codex density and reveal a light frame only while interacting", async ({ guardedPage }) => {
  await guardedPage.goto("/__ga/frame-app?scenario=artifact&theme=light", {
    waitUntil: "domcontentloaded",
  });
  const taskButton = guardedPage.getByRole("button", { name: /^打开任务：/ }).first();
  await expect(taskButton).toBeVisible();

  const readVisualState = async (control: Locator) => control.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      borderColor: style.borderTopColor,
      borderStyle: style.borderTopStyle,
      borderWidth: style.borderTopWidth,
      boxShadow: style.boxShadow,
      fontSize: style.fontSize,
      lineHeight: style.lineHeight,
    };
  });
  const isTransparent = (value: string) => (
    value === "transparent"
    || value === "rgba(0, 0, 0, 0)"
    || value === "rgba(0,0,0,0)"
  );

  const idle = await readVisualState(taskButton);
  expect(idle).toMatchObject({
    borderStyle: "solid",
    borderWidth: "1px",
    boxShadow: "none",
    fontSize: "13px",
    lineHeight: "20px",
  });
  expect(isTransparent(idle.borderColor), `idle border: ${idle.borderColor}`).toBe(true);
  expect(isTransparent(idle.backgroundColor), `idle background: ${idle.backgroundColor}`).toBe(true);

  await taskButton.hover();
  const hovered = await readVisualState(taskButton);
  expect(isTransparent(hovered.borderColor), `hover border: ${hovered.borderColor}`).toBe(false);
  expect(isTransparent(hovered.backgroundColor), `hover background: ${hovered.backgroundColor}`).toBe(false);

  await taskButton.focus();
  const focused = await readVisualState(taskButton);
  expect(isTransparent(focused.borderColor), `focus border: ${focused.borderColor}`).toBe(false);
  expect(isTransparent(focused.backgroundColor), `focus background: ${focused.backgroundColor}`).toBe(false);

  const box = await taskButton.boundingBox();
  expect(box).not.toBeNull();
  await guardedPage.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await guardedPage.mouse.down();
  const active = await readVisualState(taskButton);
  await guardedPage.mouse.up();
  expect(isTransparent(active.borderColor), `active border: ${active.borderColor}`).toBe(false);
  expect(isTransparent(active.backgroundColor), `active background: ${active.backgroundColor}`).toBe(false);

  const modeGroup = guardedPage.getByRole("group", { name: "任务类型" });
  await expect(modeGroup).toBeVisible();
  const imageModeButton = modeGroup.getByRole("button", { name: "图片" });
  await expect(imageModeButton).toHaveAttribute("aria-pressed", "false");
  const contextualIdle = await readVisualState(imageModeButton);
  expect(contextualIdle).toMatchObject({
    borderStyle: "solid",
    borderWidth: "1px",
    boxShadow: "none",
    fontSize: "13px",
    lineHeight: "20px",
  });
  expect(isTransparent(contextualIdle.borderColor), `context idle border: ${contextualIdle.borderColor}`).toBe(true);
  expect(isTransparent(contextualIdle.backgroundColor), `context idle background: ${contextualIdle.backgroundColor}`).toBe(true);

  await imageModeButton.hover();
  const contextualHover = await readVisualState(imageModeButton);
  expect(isTransparent(contextualHover.borderColor), `context hover border: ${contextualHover.borderColor}`).toBe(false);
  expect(isTransparent(contextualHover.backgroundColor), `context hover background: ${contextualHover.backgroundColor}`).toBe(false);

  await imageModeButton.focus();
  const contextualFocus = await readVisualState(imageModeButton);
  expect(isTransparent(contextualFocus.borderColor), `context focus border: ${contextualFocus.borderColor}`).toBe(false);
  expect(isTransparent(contextualFocus.backgroundColor), `context focus background: ${contextualFocus.backgroundColor}`).toBe(false);
});

test("Composer renders server-reported quota, token usage, and context window", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const meter = guardedPage.locator(".ex-usage-meter");
  await expect(meter).toContainText("今日 5.2k");
  await expect(meter).toContainText("本周 22.6k");
  await expect(meter).toContainText("上下文 42.2k / 272k");
  await expect(meter).toContainText("额度 128次");
  await expect(meter.locator("span")).toHaveCount(4);
});

test("short user messages retain their intrinsic bubble width", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const bubble = guardedPage.locator(".ex-message.is-user .ex-message-body");
  const dimensions = await bubble.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return { width: box.width, height: box.height };
  });
  expect(dimensions.width).toBeGreaterThan(80);
  expect(dimensions.height).toBeLessThan(48);
});

test("WorkspaceSurface owns the desktop outline without square child shells or ordinary shadows", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage, "dark");
  const result = await guardedPage.locator(".ex-workspace").evaluate((workspace) => {
    const visual = (element: Element) => {
      const style = getComputedStyle(element);
      return {
        topLeft: style.borderTopLeftRadius,
        topRight: style.borderTopRightRadius,
        bottomRight: style.borderBottomRightRadius,
        bottomLeft: style.borderBottomLeftRadius,
        boxShadow: style.boxShadow,
      };
    };
    const shell = getComputedStyle(workspace);
    const structural = [
      workspace.querySelector(".ex-workspace-header"),
      workspace.querySelector(".ex-status-stack"),
      workspace.querySelector(".ex-timeline"),
      workspace.querySelector(".ex-composer-region"),
    ].filter((element): element is Element => element !== null).map(visual);
    const ordinary = [
      workspace.querySelector(".ex-message"),
      workspace.querySelector(".ex-artifact"),
    ].filter((element): element is Element => element !== null).map(visual);
    return {
      shell: visual(workspace),
      overflow: shell.overflow,
      structural,
      ordinary,
    };
  });
  expect(result.shell).toMatchObject({
    topLeft: "16px",
    topRight: "16px",
    bottomRight: "16px",
    bottomLeft: "16px",
    boxShadow: "none",
  });
  expect(result.overflow).toBe("clip");
  expect(result.structural.every((surface) => (
    surface.topLeft === "0px"
    && surface.topRight === "0px"
    && surface.bottomRight === "0px"
    && surface.bottomLeft === "0px"
    && surface.boxShadow === "none"
  ))).toBe(true);
  expect(result.ordinary.every((surface) => surface.boxShadow === "none")).toBe(true);
});

test("workspace title uses the v0.3 workbench geometry with a Codex-like inline task menu", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage, "dark");
  const header = guardedPage.locator(".ex-workspace-header");
  const title = header.locator(".ex-title-row");
  const menu = title.getByRole("button", { name: "打开任务更多菜单" });

  await expect(header.locator(".ex-workspace-symbol")).toBeVisible();
  await expect(title.locator("h1")).toBeVisible();
  await expect(menu).toBeVisible();
  await expect(
    guardedPage.locator(".ex-header-actions").getByRole("button", { name: "打开任务更多菜单" }),
  ).toHaveCount(0);

  const geometry = await guardedPage.locator(".ex-app-shell").evaluate((shell) => {
    const sidebar = shell.querySelector<HTMLElement>(".ex-sidebar");
    const workspace = shell.querySelector<HTMLElement>(".ex-workspace");
    if (!sidebar || !workspace) throw new Error("workbench surfaces are missing");
    const shellStyle = getComputedStyle(shell);
    const sidebarStyle = getComputedStyle(sidebar);
    const workspaceStyle = getComputedStyle(workspace);
    return {
      columns: shellStyle.gridTemplateColumns,
      gap: shellStyle.gap,
      padding: shellStyle.padding,
      sidebarRightBorder: sidebarStyle.borderRightWidth,
      workspaceRadius: workspaceStyle.borderTopLeftRadius,
    };
  });
  expect(geometry).toMatchObject({
    gap: "0px",
    padding: "0px",
    sidebarRightBorder: "1px",
    workspaceRadius: "16px",
  });
  expect(geometry.columns.split(" ")[0]).toBe("280px");
});

test("task ID continuation keeps the current task on 404 and restores a valid task from Enter", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "thread-switch");
  await expect(guardedPage.getByText("当前任务原始内容", { exact: true })).toBeVisible();

  await guardedPage.getByRole("button", { name: "按任务 ID 继续" }).click();
  const dialog = guardedPage.getByRole("dialog", { name: "继续已有任务" });
  const input = dialog.getByLabel("任务 ID");
  await input.fill("thr_missing_ga");
  await input.press("Enter");
  await expect(dialog.getByText("未找到任务，或记录暂不可读。请核对 ID。", { exact: true })).toBeVisible();
  await expect(guardedPage.getByText("当前任务原始内容", { exact: true })).toBeVisible();

  await input.fill("thr_target_ga");
  await input.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(guardedPage.getByText("年度任务已从恢复点载入", { exact: true })).toBeVisible();
  await expect(guardedPage.getByText("当前任务原始内容", { exact: true })).toBeHidden();
});

test("rapid task switching is last-wins even when the older projection arrives late", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "thread-switch");
  await guardedPage.getByRole("button", { name: "打开任务：较慢的历史任务" }).click();
  await guardedPage.getByRole("button", { name: "打开任务：较新的快速任务" }).click();
  await expect(guardedPage.getByText("快速任务最终内容", { exact: true })).toBeVisible();
  await guardedPage.waitForTimeout(650);
  await expect(guardedPage.getByText("快速任务最终内容", { exact: true })).toBeVisible();
  await expect(guardedPage.getByText("较慢任务内容", { exact: true })).toBeHidden();
});

test("mobile task drawer continues a task by ID and closes after recovery", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      colorScheme: "light",
    },
    async (page) => {
      await openThreadScenario(page, "thread-switch");
      await expect(page.getByText("当前任务原始内容", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "打开任务导航" }).click();
      await page.getByRole("button", { name: "按任务 ID 继续" }).click();
      const dialog = page.getByRole("dialog", { name: "继续已有任务" });
      const input = dialog.getByLabel("任务 ID");
      await input.fill("thr_target_ga");
      await input.press("Enter");
      await expect(dialog).toBeHidden();
      await expect(page.getByText("年度任务已从恢复点载入", { exact: true })).toBeVisible();
      await expect(page.locator(".ex-sidebar")).not.toHaveClass(/is-open/);
    },
  );
});

test("settings persist output location, memory reset undo, and full-access revocation", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  await guardedPage.getByRole("button", { name: "设置" }).click();
  const dialog = guardedPage.getByRole("dialog", { name: "设置" });
  await expect(dialog).toBeVisible();

  const outputLocation = dialog.getByLabel("默认产物保存位置");
  await expect(outputLocation).toHaveValue("documents");
  await outputLocation.selectOption("downloads");
  await expect(outputLocation).toHaveValue("downloads");

  const memorySection = dialog.getByRole("heading", { name: "记忆", exact: true }).locator("..");
  await memorySection.getByRole("button", { name: "一键重置" }).click();
  await memorySection.getByRole("button", { name: "确认重置" }).click();
  await expect(memorySection.getByText("0 项可重置的偏好和资料记忆", { exact: true })).toBeVisible();
  await memorySection.getByRole("button", { name: "撤销重置" }).click();
  await expect(memorySection.getByText("2 项可重置的偏好和资料记忆", { exact: true })).toBeVisible();

  const permissionSection = dialog.getByRole("heading", { name: "权限", exact: true }).locator("..");
  await permissionSection.getByRole("button", { name: "启用完全访问" }).click();
  await permissionSection.getByRole("button", { name: "确认启用" }).click();
  await expect(permissionSection.getByText("完全访问", { exact: true })).toBeVisible();
  await permissionSection.getByRole("button", { name: "恢复默认权限" }).click();
  await expect(permissionSection.getByText("默认权限", { exact: true })).toBeVisible();

  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state).toMatchObject({
    output_location: "downloads",
    memory_resettable_count: 2,
    permission_profile: "default",
  });
});

test("task inspection keeps implementation details collapsed and explicitly starts a new work step", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "replay");
  await guardedPage.getByRole("button", { name: "打开任务更多菜单" }).click();
  await guardedPage.getByRole("menuitem", { name: "任务检查与重新运行" }).click();

  const dialog = guardedPage.getByRole("dialog", { name: "任务检查与重新运行" });
  await expect(dialog.getByText("任务记录完整且可以恢复", { exact: true })).toBeVisible();
  await expect(dialog.getByText("记录摘要", { exact: true })).toBeHidden();
  await expect(dialog.getByText("工作步骤 ID", { exact: true })).toHaveCount(0);
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "开始重新运行" }).click();

  await expect(dialog.getByText("新的工作步骤已加入当前任务", { exact: true })).toBeVisible();
  await expect(dialog.getByText("工作步骤 ID", { exact: true })).toBeHidden();
  await expect(dialog.getByText("权限记录 ID", { exact: true })).toBeHidden();
  await dialog.getByRole("button", { name: "关闭并查看新结果" }).click();
  await expect(dialog).toBeHidden();
});

test("reasoning stays visible until replacement and terminal facts clear the first-turn indicator", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "thinking");
  const first = guardedPage.getByText("正在核对季度资料。", { exact: true });
  await expect(first).toBeVisible({ timeout: 1_000 });
  await guardedPage.waitForTimeout(200);
  await expect(first).toBeVisible();

  const next = guardedPage.getByText("资料已核对，正在整理结果。", { exact: true });
  await expect(next).toBeVisible({ timeout: 1_500 });
  await expect(first).toBeHidden();
  await expect(guardedPage.locator(".ex-thinking")).toBeHidden({ timeout: 2_000 });
  await expect(guardedPage.locator(".ex-message.is-assistant")).toHaveCount(0);
});

test("retry state stays actionable and can be interrupted without a stale thinking indicator", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "retry");
  await expect(guardedPage.locator(".ex-thinking")).toContainText("等待重试");
  await guardedPage.getByRole("button", { name: "停止当前任务" }).click();
  await expect(guardedPage.locator(".ex-thinking")).toBeHidden();
});

test("persisted HITL remains visible until the selected backend action resolves", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "hitl");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await expect(interaction).toBeVisible();
  await expect(interaction.getByText("需要你的允许", { exact: true })).toBeVisible();
  await interaction.getByRole("button", { name: "本次允许" }).click();
  await expect(interaction).toBeHidden();
});

test("connector-login HITL opens the safe URL and resolves only through dedicated checks", async ({ guardedPage }) => {
  await guardedPage.addInitScript(() => {
    const opened: string[] = [];
    Object.defineProperty(window, "__gaConnectorUrls", { value: opened });
    Object.defineProperty(window, "open", {
      configurable: true,
      value: () => ({
        opener: null,
        location: { replace: (url: string) => opened.push(String(url)) },
        focus: () => undefined,
        close: () => undefined,
      }),
    });
  });
  await openThreadScenario(guardedPage, "connector-login");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await interaction.getByRole("button", { name: "开始登录" }).click();
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toHaveAttribute(
    "href",
    /^https:\/\/open\.feishu\.cn\//,
  );
  expect(await guardedPage.evaluate(() => (
    window as unknown as { __gaConnectorUrls: string[] }
  ).__gaConnectorUrls)).toEqual(["https://open.feishu.cn/authorize?state=ga"]);

  await interaction.getByRole("button", { name: "检查状态" }).click();
  await expect(interaction).toContainText("尚未确认连接");
  await expect(interaction).toBeHidden({ timeout: 6_000 });
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.connector_login).toMatchObject({
    begin: 1,
    check: 2,
    cancel: 0,
    ordinary_respond: 0,
  });
});

test("connector device login shows a safe verification link and public code", async ({ guardedPage }) => {
  await guardedPage.addInitScript(() => {
    const popup = { navigated: [] as string[], closed: 0 };
    Object.defineProperty(window, "__gaConnectorDevicePopup", { value: popup });
    Object.defineProperty(window, "open", {
      configurable: true,
      value: () => ({
        opener: null,
        location: { replace: (url: string) => popup.navigated.push(String(url)) },
        focus: () => undefined,
        close: () => { popup.closed += 1; },
      }),
    });
  });
  await openThreadScenario(guardedPage, "connector-device");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await interaction.getByRole("button", { name: "开始登录" }).click();
  await expect(interaction.getByText("ECX-2048", { exact: true })).toBeVisible();
  await expect(interaction.getByRole("link", { name: "打开验证页" })).toHaveAttribute(
    "href",
    "https://open.feishu.cn/device",
  );
  expect(await guardedPage.evaluate(() => (
    window as unknown as {
      __gaConnectorDevicePopup: { navigated: string[]; closed: number };
    }
  ).__gaConnectorDevicePopup)).toEqual({ navigated: [], closed: 1 });
  await interaction.getByRole("button", { name: "取消", exact: true }).click();
  await expect(interaction).toBeHidden();
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.connector_login).toMatchObject({
    begin: 1,
    check: 0,
    cancel: 1,
    ordinary_respond: 0,
  });
});

test("connector-login cancel revokes through its dedicated route before the card disappears", async ({ guardedPage }) => {
  await guardedPage.addInitScript(() => {
    Object.defineProperty(window, "open", {
      configurable: true,
      value: () => ({
        opener: null,
        location: { replace: () => undefined },
        focus: () => undefined,
        close: () => undefined,
      }),
    });
  });
  await openThreadScenario(guardedPage, "connector-login");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await interaction.getByRole("button", { name: "开始登录" }).click();
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toBeVisible();
  await interaction.getByRole("button", { name: "取消", exact: true }).click();
  await expect(interaction).toBeHidden();
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.connector_login).toMatchObject({
    begin: 1,
    check: 0,
    cancel: 1,
    ordinary_respond: 0,
  });
});

test("connector partial scope stops polling and asks for a fresh authorization", async ({ guardedPage }) => {
  await guardedPage.addInitScript(() => {
    Object.defineProperty(window, "open", {
      configurable: true,
      value: () => ({
        opener: null,
        location: { replace: () => undefined },
        focus: () => undefined,
        close: () => undefined,
      }),
    });
  });
  await openThreadScenario(guardedPage, "connector-reauth");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await interaction.getByRole("button", { name: "开始登录" }).click();
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toBeVisible();
  await interaction.getByRole("button", { name: "检查状态" }).click();
  await expect(interaction).toContainText("当前登录缺少所需权限，请重新登录授权。");
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toHaveCount(0);
  await expect(interaction.getByRole("button", { name: "开始登录" })).toBeEnabled();
  await guardedPage.waitForTimeout(2_300);
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.connector_login).toMatchObject({
    begin: 1,
    check: 1,
    cancel: 0,
    ordinary_respond: 0,
  });
});

test("interrupted connector completion stops polling and keeps retry-start available", async ({ guardedPage }) => {
  await guardedPage.addInitScript(() => {
    Object.defineProperty(window, "open", {
      configurable: true,
      value: () => ({
        opener: null,
        location: { replace: () => undefined },
        focus: () => undefined,
        close: () => undefined,
      }),
    });
  });
  await openThreadScenario(guardedPage, "connector-restart");
  const interaction = guardedPage.getByRole("region", { name: "等待你的操作" });
  await interaction.getByRole("button", { name: "开始登录" }).click();
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toBeVisible();
  await interaction.getByRole("button", { name: "检查状态" }).click();
  await expect(interaction).toContainText("登录未完成，请重新开始登录。");
  await expect(interaction.getByRole("link", { name: "打开登录页" })).toHaveCount(0);
  await expect(interaction.getByRole("button", { name: "开始登录" })).toBeEnabled();
  await guardedPage.waitForTimeout(2_300);
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.connector_login).toMatchObject({
    begin: 1,
    check: 1,
    cancel: 0,
    ordinary_respond: 0,
  });
});

test("320px touch Composer keeps the durable queue action reachable and single-line", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 320, height: 568 },
      hasTouch: true,
      isMobile: true,
      colorScheme: "light",
    },
    async (page) => {
      await openThreadScenario(page, "retry");
      const disposition = page.getByRole("button", { name: "追加到当前任务" });
      await expect(disposition).toBeVisible();
      await expect(disposition).toHaveCSS("white-space", "nowrap");
      await disposition.click();
      await page.getByRole("menuitem", { name: "排到下一轮" }).click();
      await expect(page.getByRole("button", { name: "排到下一轮" })).toBeVisible();
      await page.getByLabel("给 EcoreX 发消息").fill("下一轮整理附件");
      await page.getByRole("button", { name: "发送" }).click();
      await expect(page.getByText("下一轮整理附件", { exact: true })).toBeVisible();
      await expect(page.locator(".ex-thinking")).toContainText("已排队");
      expect(await page.evaluate(() => (
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
        - document.documentElement.clientWidth
      ))).toBeLessThanOrEqual(1);
    },
  );
});

test("share dialog keeps user-facing workflow copy in Chinese", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  await guardedPage.getByRole("button", { name: "分享当前任务" }).click();
  const dialog = guardedPage.getByRole("dialog", { name: "分享任务" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("链接只包含创建时已有的内容；之后的新消息不会自动加入。", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("region", { name: "创建分享" })).toBeVisible();
  await expect(dialog.getByRole("region", { name: "已有分享" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "创建新链接" })).toBeVisible();
});

test("image artifact opens fitted, keeps zoom controls, and restores keyboard focus", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  const artifactButton = guardedPage.locator("button.ex-artifact-primary").filter({ hasText: ARTIFACT_NAME });
  await artifactButton.focus();
  await expect(artifactButton).toBeFocused();

  await guardedPage.keyboard.press("Tab");
  await expect(guardedPage.getByRole("button", { name: "有帮助" })).toBeFocused();
  const actionRail = guardedPage.getByRole("group", { name: new RegExp(`产物操作：${ARTIFACT_NAME}`) });
  await expect(actionRail).toHaveCSS("opacity", "1");
  await expect(actionRail).toHaveCSS("pointer-events", "auto");

  await guardedPage.keyboard.press("Shift+Tab");
  await expect(artifactButton).toBeFocused();
  await guardedPage.keyboard.press("Enter");

  const dialog = guardedPage.getByRole("dialog", { name: ARTIFACT_NAME });
  await expect(dialog).toBeVisible();
  const image = dialog.getByRole("img", { name: ARTIFACT_NAME });
  await expect(image).toBeVisible();
  await expect(dialog.getByText("适合窗口", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "放大图片" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "缩小图片" })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "显示完整图片" })).toBeDisabled();

  const fitted = await dialog.locator(".ex-preview-body").evaluate((body) => {
    const preview = body.querySelector("img");
    if (!(preview instanceof HTMLImageElement)) return null;
    return {
      complete: preview.complete,
      naturalWidth: preview.naturalWidth,
      naturalHeight: preview.naturalHeight,
      objectFit: getComputedStyle(preview).objectFit,
      horizontalOverflow: body.scrollWidth - body.clientWidth,
      verticalOverflow: body.scrollHeight - body.clientHeight,
    };
  });
  expect(fitted).not.toBeNull();
  expect(fitted).toMatchObject({ complete: true, naturalWidth: 2, naturalHeight: 2, objectFit: "contain" });
  expect(fitted!.horizontalOverflow).toBeLessThanOrEqual(1);
  expect(fitted!.verticalOverflow).toBeLessThanOrEqual(1);

  await dialog.getByRole("button", { name: "放大图片" }).click();
  await expect(dialog.getByText("125%", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "缩小图片" })).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "显示完整图片" })).toBeEnabled();
  await dialog.getByRole("button", { name: "显示完整图片" }).click();
  await expect(dialog.getByText("适合窗口", { exact: true })).toBeVisible();

  await guardedPage.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(artifactButton).toBeFocused();
});

test("forced colors and reduced motion retain a visible keyboard focus treatment", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 1024, height: 768 },
      colorScheme: "dark",
      forcedColors: "active",
      reducedMotion: "reduce",
    },
    async (page) => {
      await openArtifactScenario(page, "dark");
      expect(await page.evaluate(() => matchMedia("(forced-colors: active)").matches)).toBe(true);
      expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
      const artifactButton = page.locator("button.ex-artifact-primary").filter({ hasText: ARTIFACT_NAME });
      await artifactButton.focus();
      await page.keyboard.press("Tab");
      await page.keyboard.press("Shift+Tab");
      await expect(artifactButton).toBeFocused();
      const styles = await artifactButton.evaluate((element) => {
        const computed = getComputedStyle(element);
        const root = getComputedStyle(document.documentElement);
        return {
          outlineStyle: computed.outlineStyle,
          outlineWidth: computed.outlineWidth,
          fastDuration: root.getPropertyValue("--duration-fast").trim(),
          baseDuration: root.getPropertyValue("--duration-base").trim(),
        };
      });
      expect(styles.outlineStyle).toBe("solid");
      expect(Number.parseFloat(styles.outlineWidth)).toBeGreaterThanOrEqual(2);
      expect(styles.fastDuration).toBe("0ms");
      expect(styles.baseDuration).toBe("0ms");
      const transitionDurations = await page.locator(".ex-artifact-actions").evaluate((element) => (
        getComputedStyle(element).transitionDuration.split(",").map((value) => value.trim())
      ));
      expect(transitionDurations.every((duration) => duration === "0s")).toBe(true);
    },
  );
});

test("touch artifact exposes one real more target and opens the bottom action sheet", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      colorScheme: "light",
    },
    async (page) => {
      await openArtifactScenario(page);
      expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
      const rail = page.getByRole("group", { name: new RegExp(`产物操作：${ARTIFACT_NAME}`) });
      const hitTargets = await rail.locator("button").evaluateAll((buttons) => buttons.map((button) => {
        const style = getComputedStyle(button);
        const rect = button.getBoundingClientRect();
        return {
          label: button.getAttribute("aria-label"),
          display: style.display,
          visibility: style.visibility,
          opacity: Number(style.opacity),
          pointerEvents: style.pointerEvents,
          width: rect.width,
          height: rect.height,
        };
      }));
      const interactive = hitTargets.filter((target) => (
        target.display !== "none"
        && target.visibility !== "hidden"
        && target.opacity > 0
        && target.pointerEvents !== "none"
        && target.width > 0
        && target.height > 0
      ));
      expect(interactive).toHaveLength(1);
      expect(interactive[0]).toMatchObject({ label: `更多：${ARTIFACT_NAME}` });
      expect(interactive[0].width).toBeGreaterThanOrEqual(44);
      expect(interactive[0].height).toBeGreaterThanOrEqual(44);
      expect(hitTargets.some((target) => (
        target.opacity === 0 && target.pointerEvents !== "none" && target.width > 0 && target.height > 0
      ))).toBe(false);

      await page.getByRole("button", { name: `更多：${ARTIFACT_NAME}` }).click();
      const sheet = page.getByRole("dialog", { name: "产物操作" });
      await expect(sheet).toBeVisible();
      const sheetBox = await sheet.boundingBox();
      expect(sheetBox).not.toBeNull();
      expect(844 - (sheetBox!.y + sheetBox!.height)).toBeLessThanOrEqual(12);
      const actionSizes = await sheet.locator(".ex-artifact-sheet-action").evaluateAll((actions) => (
        actions.map((action) => action.getBoundingClientRect().height)
      ));
      expect(actionSizes.length).toBeGreaterThan(0);
      expect(
        Math.min(...actionSizes),
        `touch action heights: ${JSON.stringify(actionSizes)}`,
      ).toBeGreaterThanOrEqual(44);
      await expect(sheet.getByRole("button", { name: "预览" })).toBeVisible();
      await expect(sheet.getByRole("button", { name: "有帮助" })).toBeVisible();
      await expect(sheet.getByRole("button", { name: "精准修图" })).toBeVisible();
      await sheet.getByRole("button", { name: "关闭产物操作" }).click();
      await expect(sheet).toBeHidden();
    },
  );
});

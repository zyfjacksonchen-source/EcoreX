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
  guardedPage: async ({ page }, use) => {
    const state = await installFailClosedGuards(page);
    await use(page);
    // Network or runtime errors are always a gate failure, even when a later
    // locator assertion also fails. Otherwise a blank app can mask its cause.
    assertGuardState(state);
  },
});

async function openArtifactScenario(page: Page, theme: "light" | "dark" = "light"): Promise<void> {
  await openThreadScenario(page, "artifact", theme);
  await expect(page.getByRole("region", { name: "任务产物" })).toBeVisible();
  await expect(page.locator("button.ex-artifact-primary").filter({ hasText: ARTIFACT_NAME })).toBeVisible();
}

async function openThreadScenario(
  page: Page,
  scenario: "thinking" | "codex-layout" | "slow-reconnect" | "retry" | "hitl" | "connector-login" | "connector-device" | "connector-reauth" | "connector-restart" | "artifact" | "image-gallery" | "replay" | "thread-switch" | "many-threads" | "long-timeline" | "streaming-jitter",
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

for (const viewport of [
  { width: 641, height: 249 },
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
] as const) {
  test(`${viewport.width}x${viewport.height} home hero and Composer remain reachable without clipping`, async ({ browser }) => {
    await withGuardedContext(browser, { viewport }, async (page) => {
      await page.goto("/__ga/frame-app?scenario=many-threads&theme=dark", {
        waitUntil: "domcontentloaded",
      });
      await expect(page.getByRole("img", { name: "e-Mate 五位办公助手" })).toBeVisible();
      const controls = [
        page.locator(".ex-home-hero-stage"),
        page.getByRole("heading", { name: "和小芯一起开始工作吧" }),
        page.locator(".ex-home-composer .ex-composer"),
      ];
      for (const control of controls) {
        await control.scrollIntoViewIfNeeded();
        await expect(control).toBeVisible();
        const bounds = await control.boundingBox();
        expect(bounds).not.toBeNull();
        expect(bounds!.y).toBeGreaterThanOrEqual(-1);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height + 1);
      }
      const overflow = await page.evaluate(() => (
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
        - window.innerWidth
      ));
      expect(overflow).toBeLessThanOrEqual(1);
    });
  });
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

  await expect(guardedPage.getByRole("group", { name: "任务类型" })).toHaveCount(0);
  const modelTrigger = guardedPage.getByRole("button", { name: "选择模型" });
  await expect(modelTrigger).toBeVisible();
  const contextualIdle = await readVisualState(modelTrigger);
  expect(contextualIdle).toMatchObject({
    borderStyle: "solid",
    borderWidth: "1px",
    boxShadow: "none",
    fontSize: "13px",
    lineHeight: "20px",
  });
  expect(isTransparent(contextualIdle.borderColor), `context idle border: ${contextualIdle.borderColor}`).toBe(true);
  expect(isTransparent(contextualIdle.backgroundColor), `context idle background: ${contextualIdle.backgroundColor}`).toBe(true);

  await modelTrigger.hover();
  const contextualHover = await readVisualState(modelTrigger);
  expect(isTransparent(contextualHover.borderColor), `context hover border: ${contextualHover.borderColor}`).toBe(false);
  expect(isTransparent(contextualHover.backgroundColor), `context hover background: ${contextualHover.backgroundColor}`).toBe(false);

  await modelTrigger.focus();
  const contextualFocus = await readVisualState(modelTrigger);
  expect(isTransparent(contextualFocus.borderColor), `context focus border: ${contextualFocus.borderColor}`).toBe(false);
  expect(isTransparent(contextualFocus.backgroundColor), `context focus background: ${contextualFocus.backgroundColor}`).toBe(false);

  await modelTrigger.click();
  const modelMenu = guardedPage.locator(".ex-model-menu");
  await expect(modelMenu).toBeVisible();
  await expect(modelMenu).toContainText("Agent 模型");
  await expect(modelMenu).toContainText("DeepSeek V4 Pro");
  await expect(modelMenu).toContainText("Gemini 3.1 Pro");
  await expect(modelMenu).toContainText("豆包 Seed 2.0 Pro");
  await expect(modelMenu).toContainText("图片模型");
  await expect(modelMenu).toContainText("按意图自动调用");
  expect(await modelMenu.getByRole("menuitemradio").count()).toBeGreaterThanOrEqual(2);
  await expect(modelMenu.locator(".ex-model-provider-icon")).toHaveCount(6);
});

test("Composer renders server-reported quota, token usage, and context window", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const summary = guardedPage.locator(".ex-workspace-bottom .ex-usage-summary");
  await expect(summary).toBeVisible();
  await expect(summary.locator(".ex-context-ring")).toBeVisible();
  await summary.hover();
  const tooltip = guardedPage.locator(".ex-usage-tooltip");
  await expect(tooltip).toContainText("今日5.2k");
  await expect(tooltip).toContainText("本周22.6k");
  await expect(tooltip).toContainText("上下文42.2k / 272k");
  await expect(tooltip).toContainText("额度128次");
});

test("assistant replies use the Xiaoxin identity and transparent avatar asset", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const heading = guardedPage.locator(".ex-assistant-heading").first();
  await expect(heading).toContainText("小芯");
  const avatar = heading.locator("span");
  await expect(avatar).toBeVisible();
  expect(await avatar.evaluate((element) => getComputedStyle(element).backgroundImage))
    .toMatch(/xiaoxin-avatar/u);
});

test("model selection uses the managed provider route and keeps the vendor icon", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const trigger = guardedPage.getByRole("button", { name: "选择模型" });
  await trigger.click();
  await guardedPage.getByRole("menuitemradio", { name: "DeepSeek V4 Pro" }).click();
  await expect(trigger).toContainText("DeepSeek V4 Pro");
  await expect(trigger.locator(".ex-model-provider-icon.is-deepseek")).toBeVisible();

  await guardedPage.locator(".ex-composer textarea").fill("整理今天的会议结论");
  await guardedPage.getByRole("button", { name: "发送" }).click();
  await expect(guardedPage.getByText("已完成资料整理；关键结论与待办已写入结果。", { exact: true })).toBeVisible();
  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state.last_turn_model).toBe("ecorex-deepseek-v4-pro");
});

test("terminal replies expose durable elapsed time and truthful quick copy feedback", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const completion = guardedPage.locator(".ex-turn-completion").last();
  await expect(completion).toContainText(/^已完成 /);
  await completion.getByRole("button", { name: "复制本次回复" }).click();
  await expect(completion.getByText("已复制", { exact: true })).toBeVisible();
  await expect(completion.getByRole("button", { name: "回复已复制" })).toBeVisible();
});

test("conversation pinning persists through the backend catalog", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const task = guardedPage.getByRole("button", { name: "打开任务：季度资料整理" });
  const manageTask = guardedPage.getByRole("button", { name: "管理任务：季度资料整理" });
  await manageTask.locator("..").hover();
  await manageTask.click();
  await guardedPage.getByRole("menuitem", { name: "置顶会话" }).click();
  await expect(task.locator(".ex-task-pin")).toBeVisible();

  await guardedPage.reload({ waitUntil: "domcontentloaded" });
  await expect(guardedPage.getByRole("button", { name: "打开任务：季度资料整理" }).locator(".ex-task-pin")).toBeVisible();
});

test("Composer stays at the workspace bottom and moves into the new-task home", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "artifact");
  const workspace = guardedPage.locator(".ex-workspace");
  const normalComposer = guardedPage.locator(".ex-workspace-bottom .ex-composer-region");
  await expect(normalComposer).toBeVisible();
  await expect(normalComposer.locator("textarea")).toHaveAttribute("placeholder", "给小芯发送消息，支持粘贴图片或文件");
  expect(await normalComposer.evaluate((element) => getComputedStyle(element).borderTopStyle)).toBe("none");
  const composerBox = await normalComposer.locator(".ex-composer").boundingBox();
  expect(composerBox).not.toBeNull();
  for (const control of [
    normalComposer.getByRole("button", { name: "选择模型" }),
    normalComposer.getByRole("button", { name: "打开外部连接与通道" }),
  ]) {
    const controlBox = await control.boundingBox();
    expect(controlBox).not.toBeNull();
    expect(controlBox!.y).toBeGreaterThanOrEqual(composerBox!.y);
    expect(controlBox!.y + controlBox!.height).toBeLessThanOrEqual(composerBox!.y + composerBox!.height);
  }
  await expect(guardedPage.locator(".ex-permission-tooltip")).toHaveCount(0);
  await normalComposer.getByRole("button", { name: "打开外部连接与通道" }).focus();
  await expect(guardedPage.locator(".ex-permission-tooltip")).toBeVisible();
  const [workspaceBox, normalComposerBox] = await Promise.all([workspace.boundingBox(), normalComposer.boundingBox()]);
  expect(workspaceBox).not.toBeNull();
  expect(normalComposerBox).not.toBeNull();
  expect(Math.abs((workspaceBox?.y ?? 0) + (workspaceBox?.height ?? 0) - ((normalComposerBox?.y ?? 0) + (normalComposerBox?.height ?? 0)))).toBeLessThanOrEqual(2);

  await guardedPage.getByRole("button", { name: "新建任务" }).click();
  const home = guardedPage.locator(".ex-home-dashboard");
  const newConversationComposer = home.locator(".ex-home-composer .ex-composer-region");
  await expect(home).toBeVisible();
  await expect(newConversationComposer).toBeVisible();
  await expect(home.getByRole("button", { name: "选择项目会话" })).toBeVisible();
  expect(await home.locator("h1").evaluate((element) => getComputedStyle(element).fontSize)).toBe("32px");
  await expect(guardedPage.locator(".ex-workspace-bottom")).toHaveCount(0);
  const [homeBox, newComposerBox] = await Promise.all([home.boundingBox(), newConversationComposer.boundingBox()]);
  expect(homeBox).not.toBeNull();
  expect(newComposerBox).not.toBeNull();
  expect(newComposerBox?.y ?? 0).toBeGreaterThanOrEqual(homeBox?.y ?? 0);
  expect((newComposerBox?.y ?? 0) + (newComposerBox?.height ?? 0)).toBeLessThanOrEqual((homeBox?.y ?? 0) + (homeBox?.height ?? 0) + 1);
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
    sidebarRightBorder: "0px",
    workspaceRadius: "16px",
  });
  expect(geometry.columns.split(" ")[0]).toBe("248px");
});

test("task ID continuation keeps the current task on 404 and restores a valid task from Enter", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "thread-switch");
  await expect(guardedPage.getByText("当前任务原始内容", { exact: true })).toBeVisible();

  await guardedPage.getByRole("button", { name: "搜索会话" }).click();
  await guardedPage.getByRole("dialog", { name: "搜索会话" }).getByRole("button", { name: /按任务 ID 继续/u }).click();
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

test("sidebar keeps v0.3 view-more behavior without hiding current or running sessions", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "many-threads");

  const projectSessions = guardedPage.getByRole("group", { name: "季度报告 的会话" });
  await expect(projectSessions.getByRole("button", { name: "打开任务：项目历史 10" })).toBeVisible();
  await expect(projectSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(8);
  await projectSessions.getByRole("button", { name: "查看更多（3）" }).click();
  await expect(projectSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(11);
  await projectSessions.getByRole("button", { name: "收起" }).click();
  await expect(projectSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(8);

  const generalSessions = guardedPage.getByRole("region", { name: "会话" });
  await expect(generalSessions.getByRole("button", { name: "打开任务：通用历史 11" })).toBeVisible();
  await expect(generalSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(8);
  await generalSessions.getByRole("button", { name: "查看更多（4）" }).click();
  await expect(generalSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(12);
  await generalSessions.getByRole("button", { name: "收起" }).click();
  await expect(generalSessions.getByRole("button", { name: /^打开任务：/ })).toHaveCount(8);
});

test("session summaries scroll independently while sidebar chrome stays fixed", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 1024, height: 568 },
      colorScheme: "dark",
    },
    async (page) => {
      await openThreadScenario(page, "many-threads");
      const nav = page.locator(".ex-task-nav");
      const footer = page.locator(".ex-sidebar-footer");
      const brand = page.locator(".ex-sidebar-brand");
      await page.getByRole("group", { name: "季度报告 的会话" })
        .getByRole("button", { name: "查看更多（3）" })
        .click();
      await page.getByRole("region", { name: "会话" })
        .getByRole("button", { name: "查看更多（4）" })
        .click();

      const before = await page.evaluate(() => {
        const body = document.body;
        const navElement = document.querySelector<HTMLElement>(".ex-task-nav");
        const footerElement = document.querySelector<HTMLElement>(".ex-sidebar-footer");
        const brandElement = document.querySelector<HTMLElement>(".ex-sidebar-brand");
        if (!navElement || !footerElement || !brandElement) throw new Error("sidebar regions are missing");
        return {
          bodyOverflow: body.scrollHeight - body.clientHeight,
          navClientHeight: navElement.clientHeight,
          navScrollHeight: navElement.scrollHeight,
          navTop: navElement.getBoundingClientRect().top,
          footerTop: footerElement.getBoundingClientRect().top,
          brandTop: brandElement.getBoundingClientRect().top,
        };
      });
      expect(before.bodyOverflow).toBeLessThanOrEqual(1);
      expect(before.navScrollHeight).toBeGreaterThan(before.navClientHeight);

      await nav.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      await expect.poll(() => nav.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);

      const after = await page.evaluate(() => {
        const navElement = document.querySelector<HTMLElement>(".ex-task-nav");
        const footerElement = document.querySelector<HTMLElement>(".ex-sidebar-footer");
        const brandElement = document.querySelector<HTMLElement>(".ex-sidebar-brand");
        if (!navElement || !footerElement || !brandElement) throw new Error("sidebar regions are missing");
        return {
          navTop: navElement.getBoundingClientRect().top,
          footerTop: footerElement.getBoundingClientRect().top,
          brandTop: brandElement.getBoundingClientRect().top,
        };
      });
      expect(after.navTop).toBeCloseTo(before.navTop, 0);
      expect(after.footerTop).toBeCloseTo(before.footerTop, 0);
      expect(after.brandTop).toBeCloseTo(before.brandTop, 0);
      await expect(footer).toBeVisible();
      await expect(brand).toBeVisible();
    },
  );
});

test("skills workspace uses backend categories and keeps required skills locked", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  await guardedPage.getByRole("button", { name: "能力中心", exact: true }).click();

  const workspace = guardedPage.getByRole("region", { name: "能力中心" });
  const installedTab = workspace.getByRole("tab", { name: /已安装 3/u });
  await expect(installedTab).toBeVisible();
  await installedTab.click();
  const protectedSkills = workspace.locator("details.ex-protected-skills");
  await expect(protectedSkills).not.toHaveAttribute("open", "");
  await protectedSkills.locator("summary").click();
  await expect(protectedSkills.getByRole("switch", { name: "停用e-Mate 办公工具" })).toBeDisabled();

  const feishuSwitch = workspace.getByRole("switch", { name: "停用飞书 MCP" });
  await feishuSwitch.click();
  const confirm = guardedPage.getByRole("dialog", { name: "确认停用" });
  await confirm.getByRole("button", { name: "确认停用" }).click();
  await expect(workspace.getByRole("switch", { name: "启用飞书 MCP" })).toBeVisible();

  await workspace.locator(".ex-skill-card-main").filter({ hasText: "旧版文档整理技能" }).click();
  const detail = guardedPage.getByRole("region", { name: "技能详情" });
  await expect(detail.getByRole("heading", { name: "旧版文档整理技能" })).toBeVisible();
  await detail.getByRole("button", { name: "返回技能" }).click();
  await workspace.getByRole("tab", { name: "发现" }).click();
  await expect(workspace.locator("article.ex-hub-card").filter({ hasText: "文档助手" })).toContainText("已启用");
});

test("scheduled tasks and external connections enter the real conversation and channel surfaces", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  await guardedPage.getByTestId("sidebar-schedules").click();
  const schedules = guardedPage.getByTestId("schedules-workspace");
  await expect(schedules.getByRole("heading", { name: "定时任务" })).toBeVisible();
  await schedules.getByRole("button", { name: /查看定时任务/u }).click();
  await expect(guardedPage.locator(".ex-composer textarea")).toHaveValue(/调用定时任务能力/u);

  await guardedPage.getByTestId("composer-connections").click();
  const channels = guardedPage.getByTestId("capability-channels");
  await expect(channels).toBeVisible();
  await expect(guardedPage.getByRole("heading", { name: "能力中心" })).toBeVisible();

  const dingtalk = channels.locator("article.ex-connector-row").filter({ hasText: "钉钉" });
  await expect(dingtalk).toContainText("当前安装未包含这个连接所需的组件");
  await dingtalk.getByRole("button", { name: "配置账号" }).click();
  await expect(dingtalk.getByLabel("连接名称")).toBeVisible();
  await expect(dingtalk.getByLabel("Client ID")).toBeVisible();
  await expect(dingtalk.getByLabel("Client Secret")).toHaveAttribute("type", "password");
  await expect(dingtalk.getByRole("button", { name: "保存配置" })).toBeDisabled();

  const unavailable = channels.locator("article.ex-connector-row").filter({ hasText: "微信公众号" });
  await expect(unavailable.getByRole("status")).toContainText("当前安装暂不支持这个通道");
  await expect(unavailable.getByRole("button")).toHaveCount(0);
  await expect(unavailable.getByRole("button", { name: "配置账号" })).toHaveCount(0);

  const weixin = channels.locator("article.ex-connector-row").filter({
    has: guardedPage.locator(".ex-connector-title > strong", { hasText: /^微信$/u }),
  });
  await weixin.getByRole("button", { name: "扫码登录" }).click();
  await expect(weixin.getByText("微信中的发送者名称来自所登录账号，请先将账号名称设为 e-Mate。"))
    .toBeVisible();
  const weixinQr = weixin.getByTestId("weixin-device-qr");
  await expect(weixinQr).toBeVisible();
  await expect(weixinQr).toHaveAttribute("src", /^data:image\/png;base64,/u);
  await expect(weixin.getByTestId("weixin-device-code")).toContainText("https://weixin.qq.com/");
  await expect(weixin.getByText("已扫码，请在手机上确认")).toBeVisible();
  await expect(weixin.getByRole("button", { name: "重新获取登录码" })).toBeVisible();
  await expect(weixinQr).toHaveCount(0);
  await weixin.getByRole("button", { name: "重新获取登录码" }).click();
  await expect(weixinQr).toBeVisible();
  await expect(weixin.getByText("已连接", { exact: true })).toBeVisible();
  await expect(weixinQr).toHaveCount(0);

  const telegram = channels.locator("article.ex-connector-row").filter({ hasText: "Telegram" });
  await expect(telegram).not.toContainText("当前安装未包含这个连接所需的组件");
  await telegram.getByRole("button", { name: "配置账号" }).click();
  await telegram.getByLabel("连接名称").fill("办公通知机器人");
  const secret = telegram.locator('input[type="password"]');
  await expect(secret).toHaveAttribute("autocomplete", "new-password");
  await secret.fill("ga-token-never-echoed");
  await telegram.getByRole("button", { name: "保存配置" }).click();
  await expect(secret).toHaveValue("");
  await expect(telegram.getByText("已配置", { exact: true })).toBeVisible();
  await telegram.getByRole("button", { name: "重试连接" }).click();
  await expect(telegram.getByText("已连接", { exact: true })).toBeVisible();
  await telegram.getByRole("button", { name: "测试连接" }).click();
  await expect(telegram.getByText("已连接", { exact: true })).toBeVisible();
  await telegram.getByRole("button", { name: "停用" }).click();
  await expect(telegram.getByText("已停用", { exact: true })).toBeVisible();
  await telegram.getByRole("button", { name: "启用" }).click();
  await expect(telegram.getByText("已连接", { exact: true })).toBeVisible();
  await telegram.getByRole("button", { name: "断开" }).click();
  await telegram.getByRole("button", { name: "确认断开并删除凭据" }).click();
  await expect(telegram.getByText("已配置", { exact: true })).toHaveCount(0);
});

test("remote MCP self-service closes HTTPS, secret, test, lifecycle, and delete flows", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  await guardedPage.getByRole("button", { name: "能力中心", exact: true }).click();
  const workspace = guardedPage.getByRole("region", { name: "能力中心" });
  await workspace.getByRole("tab", { name: /已安装/u }).click();
  await workspace.getByRole("button", { name: "通道", exact: true }).click();

  const mcp = workspace.getByRole("region", { name: "远程 MCP" });
  await expect(mcp.getByText("尚未添加远程 MCP")).toBeVisible();
  await mcp.getByRole("button", { name: "刷新远程 MCP" }).click();
  await mcp.getByRole("button", { name: "添加远程 MCP" }).click();
  await mcp.getByLabel("显示名称").fill("办公资料 MCP");
  const endpoint = mcp.getByLabel("HTTPS 地址");
  await endpoint.fill("http://mcp.example.test/service");
  await expect(endpoint).toHaveAttribute("aria-invalid", "true");
  await expect(mcp.getByRole("button", { name: "保存配置" })).toBeDisabled();
  await endpoint.fill("https://mcp.example.test/service");

  const auth = mcp.getByLabel("认证方式");
  await auth.selectOption("oauth2");
  await expect(mcp.getByLabel("OAuth Client ID（可选）")).toBeVisible();
  await expect(mcp.getByLabel("授权范围（空格分隔）")).toBeVisible();
  await expect(mcp.getByLabel("额外授权域名（逗号分隔）")).toBeVisible();
  await auth.selectOption("bearer");
  const secret = mcp.getByLabel("Bearer 令牌", { exact: true });
  await expect(secret).toHaveAttribute("type", "password");
  await expect(secret).toHaveAttribute("autocomplete", "new-password");
  await secret.fill("ga-mcp-token-never-echoed");
  await mcp.getByRole("button", { name: "保存配置" }).click();

  const row = mcp.locator("article.ex-mcp-server-row").filter({ hasText: "办公资料 MCP" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "编辑" }).click();
  await expect(mcp.getByLabel(/Bearer 令牌.*已配置/u)).toHaveValue("");
  await expect(mcp.getByLabel(/Bearer 令牌.*已配置/u)).toHaveAttribute("placeholder", "已安全保存；留空不修改");
  await mcp.getByLabel("显示名称").fill("办公资料检索 MCP");
  await mcp.getByLabel("HTTPS 地址").fill("https://mcp.example.test/service");
  await mcp.getByRole("button", { name: "保存配置" }).click();

  const renamed = mcp.locator("article.ex-mcp-server-row").filter({ hasText: "办公资料检索 MCP" });
  await renamed.getByRole("button", { name: "真实测试" }).click();
  await expect(renamed.getByText("已冻结工具：2")).toBeVisible();
  await expect(renamed.getByText("documents.search")).toBeVisible();
  await expect(renamed.getByText("documents.write")).toBeVisible();
  await renamed.getByRole("button", { name: "启用" }).click();
  await expect(renamed.getByText("已启用", { exact: true })).toBeVisible();
  await expect(renamed.getByRole("button", { name: "编辑" })).toBeDisabled();
  await renamed.getByRole("button", { name: "停用" }).click();
  await expect(renamed.getByText("未启用", { exact: true })).toBeVisible();
  await expect(renamed.getByRole("button", { name: "编辑" })).toBeEnabled();

  await renamed.getByRole("button", { name: "删除" }).click();
  await renamed.getByRole("button", { name: "取消删除" }).click();
  await renamed.getByRole("button", { name: "删除" }).click();
  await renamed.getByRole("button", { name: "确认删除并清除凭据" }).click();
  await expect(mcp.getByText("尚未添加远程 MCP")).toBeVisible();

  await mcp.getByRole("button", { name: "添加远程 MCP" }).click();
  await mcp.getByLabel("显示名称").fill("OAuth MCP");
  await mcp.getByLabel("HTTPS 地址").fill("https://oauth-mcp.example.test/service");
  await mcp.getByLabel("认证方式").selectOption("oauth2");
  await mcp.getByLabel("OAuth Client ID（可选）").fill("desktop-client");
  await mcp.getByLabel("授权范围（空格分隔）").fill("mcp.read mcp.write");
  await mcp.getByLabel("额外授权域名（逗号分隔）").fill("auth.example.test");
  await mcp.getByRole("button", { name: "保存配置" }).click();
  const oauthRow = mcp.locator("article.ex-mcp-server-row").filter({ hasText: "OAuth MCP" });
  await oauthRow.getByRole("button", { name: "刷新授权状态" }).click();
  await expect(oauthRow.getByText("OAuth 状态：待授权")).toBeVisible();
  await guardedPage.evaluate(() => {
    window.open = () => ({
      close() {},
      focus() {},
      location: { href: "" },
      opener: null,
    }) as unknown as Window;
  });
  await oauthRow.getByRole("button", { name: "开始授权" }).click();
  await expect(oauthRow.getByText("OAuth 状态：已授权")).toBeVisible();
  await oauthRow.getByRole("button", { name: "取消授权" }).click();
  await expect(oauthRow.getByText("OAuth 状态：待授权")).toBeVisible();
  await oauthRow.getByRole("button", { name: "删除" }).click();
  await oauthRow.getByRole("button", { name: "确认删除并清除凭据" }).click();
  await expect(mcp.getByText("尚未添加远程 MCP")).toBeVisible();
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
      await page.getByRole("button", { name: "搜索会话" }).click();
      await page.getByRole("dialog", { name: "搜索会话" }).getByRole("button", { name: /按任务 ID 继续/u }).click();
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

test("mobile session drawer remains inside the viewport and scrolls only its summaries", async ({ browser }) => {
  await withGuardedContext(
    browser,
    {
      viewport: { width: 390, height: 568 },
      hasTouch: true,
      isMobile: true,
      colorScheme: "dark",
    },
    async (page) => {
      await openThreadScenario(page, "many-threads");
      await page.getByRole("button", { name: "打开任务导航" }).click();
      const sidebar = page.locator(".ex-sidebar");
      const nav = page.locator(".ex-task-nav");
      const footer = page.locator(".ex-sidebar-footer");
      await expect(sidebar).toHaveClass(/is-open/);
      await page.getByRole("group", { name: "季度报告 的会话" })
        .getByRole("button", { name: "查看更多（3）" })
        .click();
      await page.getByRole("region", { name: "会话" })
        .getByRole("button", { name: "查看更多（4）" })
        .click();

      const geometry = await page.evaluate(() => {
        const body = document.body;
        const sidebarElement = document.querySelector<HTMLElement>(".ex-sidebar");
        const navElement = document.querySelector<HTMLElement>(".ex-task-nav");
        const footerElement = document.querySelector<HTMLElement>(".ex-sidebar-footer");
        if (!sidebarElement || !navElement || !footerElement) throw new Error("mobile sidebar regions are missing");
        const sidebarRect = sidebarElement.getBoundingClientRect();
        const footerRect = footerElement.getBoundingClientRect();
        return {
          bodyOverflow: body.scrollHeight - body.clientHeight,
          sidebarTop: sidebarRect.top,
          sidebarBottom: sidebarRect.bottom,
          viewportHeight: window.innerHeight,
          footerTop: footerRect.top,
          footerBottom: footerRect.bottom,
          navClientHeight: navElement.clientHeight,
          navScrollHeight: navElement.scrollHeight,
        };
      });
      expect(geometry.bodyOverflow).toBeLessThanOrEqual(1);
      expect(geometry.sidebarTop).toBeGreaterThanOrEqual(0);
      expect(geometry.sidebarBottom).toBeLessThanOrEqual(geometry.viewportHeight);
      expect(geometry.footerTop).toBeGreaterThanOrEqual(geometry.sidebarTop);
      expect(geometry.footerBottom).toBeLessThanOrEqual(geometry.sidebarBottom);
      expect(geometry.navScrollHeight).toBeGreaterThan(geometry.navClientHeight);

      await nav.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      await expect.poll(() => nav.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
      await expect(footer).toBeVisible();
    },
  );
});

test("settings persist output location and expose Cow knowledge and memory files", async ({ guardedPage }) => {
  const remoteMarkdownRequests: string[] = [];
  guardedPage.on("request", (request) => {
    if (request.url().includes("example.invalid")) remoteMarkdownRequests.push(request.url());
  });
  await openArtifactScenario(guardedPage);
  await guardedPage.getByRole("button", { name: "设置", exact: true }).click();
  const dialog = guardedPage.getByTestId("settings-workspace");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "个人资料", exact: true })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "知识", exact: true })).toBeHidden();
  await expect(dialog.getByRole("heading", { name: "记忆", exact: true })).toBeHidden();

  await dialog.locator('input[type="file"][accept*="image/png"]').setInputFiles({
    name: "avatar.png",
    mimeType: "image/png",
    buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
  });
  await expect(dialog.getByRole("img", { name: "当前头像" })).toBeVisible();

  await dialog.getByRole("button", { name: "常规设置", exact: true }).click();
  const outputLocation = dialog.getByLabel("默认产物保存位置");
  await expect(outputLocation).toHaveValue("documents");
  await outputLocation.selectOption("downloads");
  await expect(outputLocation).toHaveValue("downloads");
  await dialog.getByRole("button", { name: "选择文件夹" }).click();
  await expect(outputLocation).toHaveValue("workspace");

  await dialog.getByRole("button", { name: "知识", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "知识", exact: true })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "记忆", exact: true })).toBeHidden();
  const knowledgeSection = dialog.getByRole("heading", { name: "知识", exact: true }).locator("..");
  await expect(knowledgeSection.getByRole("button", { name: "新建分类" })).toBeVisible();
  await expect(knowledgeSection.getByRole("button", { name: "新建文档" })).toBeVisible();
  await knowledgeSection.getByRole("button", { name: "README.md", exact: true }).click();
  await expect(knowledgeSection.getByRole("heading", { name: "README.md" })).toBeVisible();
  await knowledgeSection.getByRole("button", { name: "新建分类" }).click();
  await knowledgeSection.getByLabel("分类路径").fill("验收分类");
  await knowledgeSection.getByRole("button", { name: "创建", exact: true }).click();
  const categorySummary = knowledgeSection.locator("summary").filter({ hasText: "验收分类" });
  await expect(categorySummary).toBeVisible();
  await categorySummary.focus();
  await categorySummary.press("Enter");
  await expect(categorySummary.locator("..")).toHaveAttribute("open", "");
  await categorySummary.press("Enter");
  await expect(categorySummary.locator("..")).not.toHaveAttribute("open", "");
  await categorySummary.press("Enter");
  await knowledgeSection.getByRole("button", { name: "新建文档" }).click();
  await knowledgeSection.getByLabel("文档路径").fill("验收.md");
  await knowledgeSection.getByLabel("初始内容").fill([
    "# 真实知识验收",
    "[坏编码](%ZZ.md)",
    "[不存在](missing.md)",
    "[不安全](http://example.invalid)",
    "[安全外链](https://example.com)",
    "![远程图片](https://example.invalid/tracker.png)",
  ].join("\n"));
  await knowledgeSection.getByRole("button", { name: "创建", exact: true }).click();
  await expect(knowledgeSection.getByRole("heading", { name: "验收.md" })).toBeVisible();
  await expect(knowledgeSection.getByRole("link", { name: "安全外链" })).toHaveAttribute("href", "https://example.com/");
  await expect(knowledgeSection.getByRole("link", { name: /坏编码|不存在|不安全/u })).toHaveCount(0);
  await expect(knowledgeSection.getByRole("img", { name: "远程图片" })).toHaveCount(0);
  expect(remoteMarkdownRequests).toEqual([]);
  await knowledgeSection.locator('input[type="file"][accept*=".md"]').setInputFiles({
    name: "导入.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# 已导入"),
  });
  await expect(knowledgeSection.getByRole("heading", { name: "导入.md" })).toBeVisible();
  await knowledgeSection.getByRole("tab", { name: "关系图" }).click();
  await expect(knowledgeSection.getByRole("button", { name: "README README.md", exact: true })).toBeVisible();

  await dialog.getByRole("button", { name: "记忆", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "记忆", exact: true })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "知识", exact: true })).toBeHidden();
  const memorySection = dialog.getByRole("heading", { name: "记忆", exact: true }).locator("..");
  await expect(memorySection.getByRole("tab", { name: "记忆文件" })).toBeVisible();
  await memorySection.getByRole("button", { name: /factory\.md/u }).click();
  await expect(memorySection.getByText("e-Mate 内置记忆", { exact: false })).toBeVisible();
  await memorySection.getByRole("tab", { name: "进化记录" }).click();
  await memorySection.getByRole("button", { name: /偏好\.md/u }).click();
  await expect(memorySection.getByText(/专业严谨/u)).toBeVisible();

  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state).toMatchObject({ output_location: "workspace" });
});

test("account menu exposes the user name and performs a real lease-bound logout", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  const account = guardedPage.getByRole("button", { name: "用户中心，验收账号" });
  await expect(account).toHaveAccessibleName("用户中心，验收账号");
  await account.click();
  await guardedPage.getByRole("menuitem", { name: "退出登录" }).click();

  const dialog = guardedPage.getByRole("dialog", { name: "退出 e-Mate？" });
  await expect(dialog).toContainText("会话和本地产物会保留");
  await dialog.getByRole("button", { name: "退出登录" }).click();
  await expect(guardedPage.getByRole("heading", { name: "e-Mate", exact: true })).toBeVisible();
  await expect(guardedPage.getByLabel("账号或邮箱")).toBeVisible();
  await expect(guardedPage.getByLabel("密码")).toBeVisible();
  await expect(guardedPage.locator(".ex-app-shell")).toHaveCount(0);

  const state = await guardedPage.evaluate(async () => fetch("/__ga/state").then((response) => response.json()));
  expect(state).toMatchObject({ authenticated: false, session_logout_count: 1 });
});

test("menu-launched confirmation dialogs release the workspace after cancel", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  const body = guardedPage.locator("body");
  const assertReleased = async () => {
    await expect(guardedPage.getByRole("dialog")).toHaveCount(0);
    await expect(body).not.toHaveCSS("pointer-events", "none");
    await guardedPage.getByRole("button", { name: "设置", exact: true }).click();
    await expect(guardedPage.getByTestId("settings-workspace")).toBeVisible();
    await guardedPage.getByRole("button", { name: "关闭设置" }).click();
  };

  const manage = guardedPage.getByRole("button", { name: "管理任务：季度资料整理" });
  await manage.locator("..").hover();
  await manage.click();
  await guardedPage.getByRole("menuitem", { name: "重命名" }).click();
  await guardedPage.getByRole("dialog", { name: "重命名任务" }).getByRole("button", { name: "取消" }).click();
  await assertReleased();

  await guardedPage.getByRole("button", { name: "用户中心，验收账号" }).click();
  await guardedPage.getByRole("menuitem", { name: "退出登录" }).click();
  await guardedPage.getByRole("dialog", { name: "退出 e-Mate？" }).getByRole("button", { name: "取消" }).click();
  await assertReleased();

  await manage.locator("..").hover();
  await manage.click();
  await guardedPage.getByRole("menuitem", { name: "归档" }).click();
  await guardedPage.locator("details.ex-archived-tasks > summary").click();
  const manageArchived = guardedPage.getByRole("button", { name: "管理已归档任务：季度资料整理" });
  await manageArchived.locator("..").hover();
  await manageArchived.click();
  await guardedPage.getByRole("menuitem", { name: "删除任务" }).click();
  await guardedPage.getByRole("dialog", { name: "删除已归档任务？" }).getByRole("button", { name: "取消" }).click();
  await assertReleased();
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

test("closing a cold lazy task dialog releases the workspace for every task-detail control", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "replay");
  await guardedPage.route("**/*.js", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.continue();
  });

  await guardedPage.getByRole("button", { name: "打开任务更多菜单" }).click();
  await guardedPage.getByRole("menuitem", { name: "任务检查与重新运行" }).click();
  await expect(guardedPage.getByRole("heading", { name: "正在打开任务检查与重新运行" })).toBeVisible();
  const replay = guardedPage.getByRole("dialog", { name: "任务检查与重新运行" });
  await expect(replay).toBeVisible();
  await replay.getByRole("button", { name: "关闭任务检查" }).click();
  await expect(guardedPage.locator("body")).not.toHaveCSS("pointer-events", "none");

  await guardedPage.getByRole("button", { name: "选择模型" }).click();
  await expect(guardedPage.getByRole("menu", { name: "选择模型" })).toBeVisible();
  await guardedPage.getByRole("menuitemradio", { name: "GPT-5.6 Luna · 最大推理" }).click();

  const usage = guardedPage.getByRole("button", { name: /查看用量/u });
  await usage.click();
  await expect(usage).toHaveAttribute("aria-pressed", "true");
  await expect(guardedPage.locator(".ex-usage-tooltip")).toBeVisible();
  await usage.click();
  await expect(usage).toHaveAttribute("aria-pressed", "false");
  await guardedPage.mouse.move(0, 0);
  await expect(guardedPage.locator(".ex-usage-tooltip")).toBeHidden();

  const manageTask = guardedPage.getByRole("button", { name: "管理任务：季度资料整理" });
  await manageTask.locator("..").hover();
  await manageTask.click();
  await expect(guardedPage.getByRole("menuitem", { name: "重命名" })).toBeVisible();
  await guardedPage.keyboard.press("Escape");

  await guardedPage.getByRole("button", { name: "设置", exact: true }).click();
  const settings = guardedPage.getByTestId("settings-workspace");
  await expect(settings).toBeVisible();
  await settings.getByRole("button", { name: "关闭设置" }).click();

  await guardedPage.getByRole("button", { name: "打开外部连接与通道" }).click();
  await expect(guardedPage.getByTestId("capability-channels")).toBeVisible();
  await guardedPage.getByRole("button", { name: "打开任务：季度资料整理" }).click();
  await expect(guardedPage.getByRole("region", { name: "对话" })).toBeVisible();

  await guardedPage.getByRole("button", { name: "新建任务" }).click();
  await expect(guardedPage.locator(".ex-home-dashboard")).toBeVisible();
});

test("reasoning stays visible until replacement and terminal facts clear the first-turn indicator", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "thinking");
  const threadSpinner = guardedPage.getByLabel("任务正在进行");
  await expect(threadSpinner).toBeVisible();
  const first = guardedPage.getByText("正在核对季度资料。", { exact: true });
  await expect(first).toBeVisible({ timeout: 1_000 });

  const next = guardedPage.getByText("资料已核对，正在整理结果。", { exact: true });
  await expect(next).toBeVisible({ timeout: 1_500 });
  await expect(first).toBeHidden();
  await expect(guardedPage.locator(".ex-thinking-state")).toBeHidden({ timeout: 2_000 });
  await expect(guardedPage.locator(".ex-message.is-assistant")).toHaveCount(0);
  await expect(threadSpinner).toBeHidden({ timeout: 2_500 });
});

test("thinking state uses the B5 handoff motion and stays readable with reduced motion", async ({ browser }) => {
  await withGuardedContext(browser, {}, async (page) => {
    await openThreadScenario(page, "thinking");
    const thinking = page.locator(".ex-turn-running");
    await expect(thinking).toBeVisible();
    await expect(thinking).toContainText("思考中");
    await expect(thinking.locator(".ex-thinking-orb-shape")).toHaveCount(3);
    const firstShape = thinking.locator(".ex-thinking-orb-shape.is-a");
    const first = await firstShape.evaluate((element) => {
      const style = getComputedStyle(element);
      return { animation: style.animationName, transform: style.transform };
    });
    await page.waitForTimeout(150);
    const secondTransform = await firstShape.evaluate((element) => getComputedStyle(element).transform);
    expect(first.animation).toBe("ex-orb-handoff");
    expect(secondTransform).not.toBe(first.transform);
    expect(await page.getByLabel("任务正在进行").evaluate((element) => getComputedStyle(element).animationName))
      .toBe("ex-task-spin");
  });

  await withGuardedContext(browser, { reducedMotion: "reduce" }, async (page) => {
    await openThreadScenario(page, "thinking");
    const thinking = page.locator(".ex-turn-running");
    await expect(thinking).toBeVisible();
    await expect(thinking).toContainText("思考中");
    const animation = await thinking.locator(".ex-thinking-orb-shape.is-a")
      .evaluate((element) => getComputedStyle(element).animationName);
    expect(animation).toBe("none");
    expect(await page.getByLabel("任务正在进行").evaluate((element) => getComputedStyle(element).animationName))
      .toBe("none");
  });
});

test("message facts follow backend sequence and the task list hovers above the Composer", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "codex-layout");
  const body = guardedPage.getByText("收到，正在核对后端事实投影与现有能力调用链。", { exact: true });
  const action = guardedPage.locator(".ex-activity-row").filter({ hasText: "读取了项目准则" });
  await expect(body).toBeVisible();
  await expect(action).toBeVisible();
  const [bodyBox, actionBox] = await Promise.all([body.boundingBox(), action.boundingBox()]);
  expect(bodyBox).not.toBeNull();
  expect(actionBox).not.toBeNull();
  expect(actionBox!.y).toBeGreaterThan(bodyBox!.y + bodyBox!.height);

  await expect(guardedPage.locator(".ex-timeline .ex-runtime-task-list")).toHaveCount(0);
  const taskList = guardedPage.locator(".ex-composer-region .ex-runtime-task-list");
  const taskSummary = taskList.locator("summary");
  const taskContent = taskList.locator(".ex-runtime-task-list-content");
  await expect(taskSummary).toContainText("1/3 已完成");
  await expect(taskContent).toBeHidden();
  await taskSummary.hover();
  await expect(taskContent).toBeVisible();
  await expect(taskContent).toContainText("验证系统能力调用");

  expect(await guardedPage.getByLabel("任务正在进行").evaluate((element) => getComputedStyle(element).animationName))
    .toBe("ex-task-spin");
});

test("retry state stays actionable and can be interrupted without a stale thinking indicator", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "retry");
  await expect(guardedPage.locator(".ex-turn-running")).toContainText(/等待重试 \d+s/u);
  await guardedPage.getByRole("button", { name: "停止当前任务" }).click();
  await expect(guardedPage.locator(".ex-thinking-state")).toBeHidden();
});

test("slow event stream reconnects from the durable cursor without duplicate output", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "slow-reconnect");
  await expect(guardedPage.locator(".ex-reasoning")).toContainText("已确认资料读取进度");
  const completed = guardedPage.getByText(
    "网络恢复后已从确认位置继续，季度资料清单完整生成且没有重复执行。",
    { exact: true },
  );
  await expect(completed).toBeVisible({ timeout: 5_000 });
  await expect(completed).toHaveCount(1);
  await expect(guardedPage.locator(".ex-thinking-state")).toBeHidden();
  const streamEvidence = await guardedPage.evaluate(async () => {
    const response = await fetch("/__ga/state", { cache: "no-store" });
    return response.json() as Promise<{
      event_stream: { connections: number; after_seq: number[] };
    }>;
  });
  expect(streamEvidence.event_stream.after_seq).toEqual([2, 3]);
  expect(streamEvidence.event_stream.connections).toBe(2);
});

test("rapid precise-retouch submit flushes its draft once and renders result evidence", async ({ guardedPage }) => {
  await openArtifactScenario(guardedPage);
  const artifact = guardedPage.locator("button.ex-artifact-primary").filter({ hasText: ARTIFACT_NAME });
  await artifact.hover();
  await guardedPage.getByRole("button", { name: "精准修图" }).click();
  const dialog = guardedPage.getByRole("dialog", { name: "精准修图" });
  await expect(dialog).toBeVisible();
  let submitRequests = 0;
  guardedPage.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/retouch-workspaces\/[^/]+\/submit$/.test(new URL(request.url()).pathname)) {
      submitRequests += 1;
    }
  });
  await dialog.getByRole("textbox", { name: "整体修改说明" }).fill("保持构图不变，只优化主体边缘过渡。");
  await dialog.getByRole("button", { name: "开始修图" }).click();
  await expect(dialog.getByRole("status")).toContainText("新修订已完成", { timeout: 5_000 });
  await expect(dialog.getByText("已按整体说明完成调整：保持构图不变，只优化主体边缘过渡。", { exact: true })).toBeVisible();
  await expect(guardedPage.getByRole("button", { name: "查看修图结果：精准修图_20260710-1534_01.png" })).toBeVisible();
  expect(submitRequests).toBe(1);
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

test("320px touch Composer keeps the Cow steer action reachable without horizontal overflow", async ({ browser }) => {
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
      await page.getByLabel("给小芯发消息").fill("继续当前任务");
      const disposition = page.getByRole("button", { name: "追加到当前任务" });
      await expect(disposition).toBeVisible();
      await expect(disposition).toBeInViewport();
      await disposition.click();
      await expect(
        page.getByRole("region", { name: "对话" }).getByText("继续当前任务", { exact: true }),
      ).toBeVisible();
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

test("a durable partial image batch survives refresh as one accessible gallery", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "image-gallery");
  const gallery = guardedPage.getByRole("region", { name: /图片画廊/ });
  await expect(gallery).toBeVisible();
  await expect(gallery).toHaveAttribute("aria-label", "图片画廊，第 1 张，共 3 张");
  await expect(guardedPage.locator('[data-artifact-status="ready"]')).toHaveCount(2);
  await expect(guardedPage.locator('[data-artifact-status="ready"]').first()).toContainText("已完成");
  const failedSlot = guardedPage.locator('[data-image-batch-task-id="image-batch-task-ga-1"]');
  await expect(failedSlot).toHaveAttribute("data-artifact-status", "failed");
  await expect(failedSlot).toContainText("生成失败");
  await expect(guardedPage.getByRole("button", { name: "打开：批次图片_01.png" })).toHaveCount(0);
  await expect(guardedPage.getByRole("button", { name: "下载：批次图片_01.png" })).toBeVisible();

  await guardedPage.getByRole("button", { name: "下一张图片" }).click();
  await expect(gallery).toHaveAttribute("aria-label", "图片画廊，第 2 张，共 3 张");
  await gallery.focus();
  await guardedPage.keyboard.press("ArrowRight");
  await expect(gallery).toHaveAttribute("aria-label", "图片画廊，第 3 张，共 3 张");
  await expect(guardedPage.getByRole("button", { name: "下一张图片" })).toBeDisabled();

  await guardedPage.getByRole("button", { name: "上一张图片" }).click();
  await guardedPage.getByRole("button", { name: "上一张图片" }).click();
  await expect(gallery).toHaveAttribute("aria-label", "图片画廊，第 1 张，共 3 张");
  await guardedPage.getByRole("button", { name: "预览图片：批次图片_01.png" }).click();
  await expect(guardedPage.getByRole("dialog", { name: "批次图片_01.png" })).toBeVisible();
  expect(await guardedPage.evaluate(() => (
    Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
    - document.documentElement.clientWidth
  ))).toBeLessThanOrEqual(1);

  await guardedPage.keyboard.press("Escape");
  await guardedPage.reload({ waitUntil: "domcontentloaded" });
  const openThread = guardedPage.getByRole("button", { name: /^打开任务：/ }).first();
  await expect(openThread).toBeVisible();
  await openThread.click();
  await expect(gallery).toHaveAttribute("aria-label", "图片画廊，第 1 张，共 3 张");
  await expect(failedSlot).toHaveAttribute("data-artifact-status", "failed");
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

test("timeline exposes a persistent jump-to-latest control after scrolling away from the bottom", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "long-timeline");
  const timeline = guardedPage.locator(".ex-timeline");
  await expect(timeline.locator('[data-turn-id="turn-long-120"]')).toBeVisible();
  await expect.poll(() => timeline.locator(".ex-timeline-turn").count()).toBeGreaterThan(0);
  await expect.poll(() => timeline.locator(".ex-timeline-turn").count()).toBeLessThan(60);
  await expect.poll(() => timeline.evaluate((element) => (
    Math.max(0, Math.round(element.scrollHeight - element.clientHeight - element.scrollTop))
  ))).toBeLessThanOrEqual(4);
  await timeline.hover();
  await guardedPage.mouse.wheel(0, -5_000);
  await expect.poll(() => timeline.evaluate((element) => (
    Math.max(0, Math.round(element.scrollHeight - element.clientHeight - element.scrollTop))
  ))).toBeGreaterThan(72);

  const jumpButton = guardedPage.getByRole("button", { name: "回到底部" });
  await expect(jumpButton).toBeVisible();
  await expect(timeline).toHaveAttribute("data-scroll-anchor-turn-id", /.+/);
  const pausedAnchor = await timeline.evaluate((element) => {
    const turnId = element.dataset.scrollAnchorTurnId;
    const viewportOffset = Number(element.dataset.scrollAnchorOffset);
    return turnId && Number.isFinite(viewportOffset) ? {
      turnId,
      viewportOffset: Math.round(viewportOffset),
    } : null;
  });
  expect(pausedAnchor).not.toBeNull();
  await guardedPage.locator(".ex-workspace-bottom .ex-composer textarea").fill("长会话滚动暂停验收");
  await guardedPage.getByRole("button", { name: "发送" }).click();
  await expect.poll(() => timeline.evaluate((element, anchor) => {
    const viewport = element.getBoundingClientRect();
    const row = [...element.querySelectorAll<HTMLElement>(".ex-timeline-turn[data-turn-id]")]
      .find((candidate) => candidate.dataset.turnId === anchor.turnId);
    if (!row) return Number.POSITIVE_INFINITY;
    return Math.abs(
      Math.round(row.getBoundingClientRect().top - viewport.top) - anchor.viewportOffset,
    );
  }, pausedAnchor!)).toBeLessThanOrEqual(4);
  await expect(jumpButton).toBeVisible();
  await expect.poll(() => timeline.locator(".ex-timeline-turn").count()).toBeLessThan(60);
  await jumpButton.click();

  await expect.poll(() => guardedPage.evaluate(() => {
    const timeline = document.querySelector<HTMLElement>(".ex-timeline");
    if (!timeline) throw new Error("timeline missing");
    const remaining = timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop;
    return Math.max(0, Math.round(remaining));
  })).toBeLessThanOrEqual(4);
  await expect(jumpButton).toBeHidden();
});

test("conversation directory jumps from turn 120 to turn 1 and tracks the top range", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "long-timeline");
  const timeline = guardedPage.locator(".ex-timeline");
  const directory = guardedPage.getByRole("navigation", { name: "对话目录" });
  const first = directory.getByRole("button", { name: "跳转到第 1 轮：第 1 轮问题" });
  await first.scrollIntoViewIfNeeded();
  await first.click();

  const firstTurn = timeline.locator('[data-turn-id="turn-long-1"]');
  await expect(firstTurn).toBeVisible();
  await expect(first).toHaveAttribute("aria-current", "location");
  await expect.poll(() => firstTurn.evaluate((row) => {
    const viewport = row.closest(".ex-timeline")?.getBoundingClientRect();
    return viewport ? Math.abs(Math.round(row.getBoundingClientRect().top - viewport.top)) : Number.POSITIVE_INFINITY;
  })).toBeLessThanOrEqual(4);
});

test("streaming reply never scrolls backward while following the latest output", async ({ guardedPage }) => {
  await openThreadScenario(guardedPage, "streaming-jitter");
  const evidence = await guardedPage.evaluate(async () => {
    const timeline = document.querySelector<HTMLElement>(".ex-timeline");
    if (!timeline) throw new Error("timeline missing");
    const samples: Array<{ top: number; status: string | null }> = [];
    let sampling = true;
    const sample = () => {
      const active = timeline.querySelector('[data-turn-id="turn-stream-active"]');
      samples.push({
        top: timeline.scrollTop,
        status: active?.getAttribute("data-turn-status") ?? null,
      });
      if (sampling) window.requestAnimationFrame(sample);
    };
    window.requestAnimationFrame(sample);
    await new Promise<void>((resolve) => {
      const observer = new MutationObserver(() => {
        const active = timeline.querySelector('[data-turn-id="turn-stream-active"]');
        if (active?.getAttribute("data-turn-status") !== "completed") return;
        observer.disconnect();
        resolve();
      });
      observer.observe(timeline, { attributes: true, childList: true, subtree: true });
    });
    sampling = false;
    const reversals = samples.flatMap((sample, index) => {
      const previous = samples[index - 1];
      return previous
        && previous.status === "streaming"
        && sample.status === "streaming"
        && sample.top < previous.top - 0.5
        ? [{ before: previous.top, after: sample.top }]
        : [];
    });
    return {
      reversals,
      startTop: samples[0]?.top ?? 0,
      maxTop: Math.max(...samples.map((sample) => sample.top)),
    };
  });
  expect(evidence.reversals).toEqual([]);
  expect(evidence.maxTop - evidence.startTop).toBeGreaterThan(500);
  await expect.poll(() => guardedPage.locator(".ex-timeline").evaluate((timeline) => (
    Math.max(0, timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop)
  ))).toBeLessThanOrEqual(4);
});

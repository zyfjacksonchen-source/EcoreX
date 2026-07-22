#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, rm, writeFile, rename } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const toolRoot = dirname(fileURLToPath(import.meta.url));
const desktopRoot = resolve(toolRoot, "..");
const repositoryRoot = resolve(desktopRoot, "..");
const scenarioIds = [
  "new-composer-centered",
  "normal-composer-bottom",
  "model-before-first-message",
  "model-switch-chat-image",
  "image-intent-routing",
  "tool-progressive-disclosure",
  "steer-queue-replace",
  "reasoning-sticky-replacement",
  "permission-default-full",
  "artifact-hover-actions",
  "image-fit-preview",
  "precise-retouch",
  "share-chat-image",
  "share-role-separation",
  "project-session",
  "office-document-flow",
  "connector-catalog",
  "memory-reset-output-path",
];
const viewports = [
  { id: "1440x900", width: 1440, height: 900 },
  { id: "1024x768", width: 1024, height: 768 },
  { id: "768x900", width: 768, height: 900 },
  { id: "390x844", width: 390, height: 844 },
];
const artifactName = "活动主视觉_20260710-1534_01.png";
const sha256 = (payload) => createHash("sha256").update(payload).digest("hex");

function argument(name) {
  const prefix = `${name}=`;
  const value = process.argv.slice(2).find((item) => item.startsWith(prefix));
  return value ? value.slice(prefix.length) : null;
}

function chromeExecutable() {
  const explicit = process.env.ECOREX_LOCAL_CDP_CHROME;
  const candidates = [
    explicit,
    process.platform === "win32" ? join(process.env.PROGRAMFILES || "", "Google", "Chrome", "Application", "chrome.exe") : null,
    process.platform === "win32" ? join(process.env["PROGRAMFILES(X86)"] || "", "Google", "Chrome", "Application", "chrome.exe") : null,
    process.platform === "win32" ? join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe") : null,
    process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : null,
    process.platform === "linux" ? "/usr/bin/google-chrome" : null,
    process.platform === "linux" ? "/usr/bin/google-chrome-stable" : null,
  ].filter(Boolean);
  const selected = candidates.find((candidate) => existsSync(candidate));
  if (!selected) throw new Error("chrome_unavailable");
  return resolve(selected);
}

async function freePort() {
  return await new Promise((resolvePort, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => error ? reject(error) : resolvePort(port));
    });
  });
}

async function waitForJson(url, timeoutMilliseconds) {
  const expiresAt = Date.now() + timeoutMilliseconds;
  while (Date.now() < expiresAt) {
    try {
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(1_000) });
      if (response.ok) return await response.json();
    } catch {
      // The bounded poll owns startup races.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error("startup_timeout");
}

async function bounded(operation, timeoutMilliseconds, code) {
  let timer;
  try {
    return await Promise.race([
      operation,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(code)), timeoutMilliseconds);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function terminate(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
  } else {
    try { process.kill(-child.pid, "SIGTERM"); } catch { child.kill("SIGTERM"); }
  }
}

async function waitVisible(locator, code, timeout = 8_000) {
  try {
    await locator.waitFor({ state: "visible", timeout });
  } catch {
    throw new Error(code);
  }
}

async function runPreflight() {
  const startedAt = performance.now();
  const serverPort = await freePort();
  const cdpPort = await freePort();
  const baseUrl = `http://127.0.0.1:${serverPort}`;
  const profileRoot = await mkdtemp(join(tmpdir(), "ecorex-v1-local-cdp-"));
  const expectedPrefix = resolve(tmpdir()) + sep;
  if (!resolve(profileRoot).startsWith(expectedPrefix)) throw new Error("profile_root_invalid");

  const server = spawn(process.execPath, [
    "tools/ga-mock-server.mjs",
    `--port=${serverPort}`,
    "--scenario=artifact",
  ], {
    cwd: desktopRoot,
    env: { ...process.env, NO_COLOR: "1" },
    stdio: ["ignore", "ignore", "ignore"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  let chrome = null;
  let browser = null;
  let currentScenario = "startup";
  let assertions = 0;
  const screenshotDigests = {};
  const diagnostics = { console_errors: 0, page_errors: 0, failed_requests: 0, external_requests: 0 };
  const failedRequestKinds = [];
  const expiresAt = Date.now() + 270_000;
  const progress = argument("--progress") === "true";

  const check = (condition, code) => {
    assertions += 1;
    if (!condition) throw new Error(code);
  };
  const checkedVisible = async (locator, code, timeout = 8_000) => {
    await waitVisible(locator, code, timeout);
    assertions += 1;
  };

  try {
    await waitForJson(`${baseUrl}/__ga/state`, 20_000);
    chrome = spawn(chromeExecutable(), [
      "--headless=new",
      `--remote-debugging-port=${cdpPort}`,
      "--remote-debugging-address=127.0.0.1",
      `--user-data-dir=${profileRoot}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-sync",
      "--metrics-recording-only",
      "about:blank",
    ], {
      stdio: ["ignore", "ignore", "ignore"],
      windowsHide: true,
      detached: process.platform !== "win32",
    });
    const versionProjection = await waitForJson(`http://127.0.0.1:${cdpPort}/json/version`, 20_000);
    check(typeof versionProjection.webSocketDebuggerUrl === "string", "cdp_endpoint_missing");
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`, { timeout: 20_000 });
    const context = browser.contexts()[0];
    check(Boolean(context), "cdp_context_missing");
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseUrl });
    await context.route("**/*", async (route) => {
      const url = route.request().url();
      let parsed;
      try { parsed = new URL(url); } catch { diagnostics.external_requests += 1; await route.abort("blockedbyclient"); return; }
      if (parsed.origin === baseUrl || parsed.protocol === "blob:" || parsed.protocol === "data:") {
        await route.continue();
      } else {
        diagnostics.external_requests += 1;
        await route.abort("blockedbyclient");
      }
    });

    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(8_000);
    page.setDefaultNavigationTimeout(15_000);
    page.on("console", (message) => { if (message.type() === "error") diagnostics.console_errors += 1; });
    page.on("pageerror", () => { diagnostics.page_errors += 1; });
    page.on("requestfailed", (request) => {
      try {
        const parsed = new URL(request.url());
        const failure = request.failure()?.errorText || "unknown";
        if (
          parsed.origin === baseUrl
          && /\/events\/stream$/u.test(parsed.pathname)
          && /ERR_(?:ABORTED|FAILED)/u.test(failure)
        ) return;
        if (parsed.origin === baseUrl) {
          diagnostics.failed_requests += 1;
          failedRequestKinds.push(`${request.resourceType()}:${parsed.pathname}:${failure}`);
        }
      } catch {
        diagnostics.failed_requests += 1;
        failedRequestKinds.push("invalid-url");
      }
    });

    const reset = async (scenario) => {
      const response = await fetch(`${baseUrl}/__ga/reset?scenario=${encodeURIComponent(scenario)}`, {
        method: "POST",
        signal: AbortSignal.timeout(5_000),
      });
      check(response.ok, "scenario_reset_failed");
    };
    const gotoApp = async (scenario = "artifact", viewport = viewports[0]) => {
      await reset(scenario);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto(`${baseUrl}/__ga/frame-app?scenario=${scenario}&theme=light`, {
        waitUntil: "domcontentloaded",
        timeout: 15_000,
      });
      await checkedVisible(page.getByRole("button", { name: "新建任务" }), "app_shell_missing");
    };
    const openThread = async (scenario = "artifact", viewport = viewports[0]) => {
      await gotoApp(scenario, viewport);
      if (viewport.width < 840) {
        await page.getByRole("button", { name: "打开任务导航" }).click();
      }
      const thread = page.getByRole("button", { name: /^打开任务：/ }).first();
      await checkedVisible(thread, "thread_entry_missing");
      await thread.click();
      await checkedVisible(page.getByRole("region", { name: "对话" }), "timeline_missing");
    };
    const settings = async () => {
      await page.getByRole("button", { name: /验收账号，打开账号菜单/u }).click();
      await page.getByRole("menuitem", { name: "设置" }).click();
      const dialog = page.getByRole("dialog", { name: "设置" });
      await checkedVisible(dialog, "settings_missing");
      return dialog;
    };

    for (const viewport of viewports) {
      currentScenario = `viewport-${viewport.id}`;
      if (progress) process.stderr.write(`${JSON.stringify({ phase: currentScenario })}\n`);
      await bounded((async () => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await page.goto(`${baseUrl}/__ga/viewport?viewport=${viewport.id}&theme=light&scenario=artifact`, {
          waitUntil: "domcontentloaded",
          timeout: 15_000,
        });
        await page.locator("body").waitFor({ state: "visible" });
        await page.waitForFunction(() => document.body.dataset.gaStatus === "passed", null, { timeout: 20_000 });
        check(await page.locator("body").getAttribute("data-ga-status") === "passed", "viewport_gate_failed");
      })(), 25_000, "viewport_timeout");
    }

    const scenarios = {
      "new-composer-centered": async () => {
        await gotoApp("artifact");
        await page.getByRole("button", { name: "新建任务" }).click();
        const chooser = page.locator(".ex-new-conversation-start");
        await checkedVisible(chooser, "new_composer_missing");
        check(await chooser.locator(".ex-new-conversation-options > button").count() === 2, "new_conversation_choices_invalid");
        check(await page.locator(".ex-workspace-bottom").count() === 0, "new_composer_not_centered");
      },
      "normal-composer-bottom": async () => {
        await openThread("artifact");
        const composer = page.locator(".ex-workspace-bottom .ex-composer-region");
        await checkedVisible(composer, "bottom_composer_missing");
        const aligned = await page.locator(".ex-workspace").evaluate((workspace) => {
          const editor = workspace.querySelector(".ex-workspace-bottom .ex-composer-region");
          if (!(editor instanceof HTMLElement)) return false;
          const outer = workspace.getBoundingClientRect();
          const inner = editor.getBoundingClientRect();
          return Math.abs(outer.bottom - inner.bottom) <= 2;
        });
        check(aligned, "bottom_composer_misaligned");
      },
      "model-before-first-message": async () => {
        await gotoApp("artifact");
        await page.getByRole("button", { name: "新建任务" }).click();
        const trigger = page.getByRole("button", { name: "选择模型" });
        await checkedVisible(trigger, "initial_model_selector_missing");
        await trigger.click();
        const menu = page.locator(".ex-model-menu");
        await checkedVisible(menu, "initial_model_menu_missing");
        check((await menu.textContent())?.includes("GPT-5.6 SOL") === true, "gpt_56_missing");
      },
      "model-switch-chat-image": async () => {
        await openThread("artifact");
        const trigger = page.getByRole("button", { name: "选择模型" });
        await trigger.click();
        await page.getByRole("menuitemradio", { name: "DeepSeek V4 Pro" }).click();
        check((await trigger.textContent())?.includes("DeepSeek V4 Pro") === true, "chat_model_switch_failed");
        await trigger.click();
        const menu = page.locator(".ex-model-menu");
        check((await menu.textContent())?.includes("按意图自动调用") === true, "image_model_catalog_missing");
        check(await menu.locator(".ex-model-provider-icon").count() === 5, "model_provider_icons_missing");
      },
      "image-intent-routing": async () => {
        await openThread("artifact");
        await page.locator(".ex-composer textarea").fill("生成一张蓝色季度报告封面图片");
        await page.getByRole("button", { name: "发送" }).click();
        await checkedVisible(
          page.locator("button.ex-artifact-primary").filter({ hasText: "生成图片_20260710-1534_01.png" }),
          "image_route_result_missing",
          10_000,
        );
        check(await page.getByRole("group", { name: "任务类型" }).count() === 0, "hardcoded_intent_split_present");
      },
      "tool-progressive-disclosure": async () => {
        await openThread("artifact");
        await page.getByRole("button", { name: "技能", exact: true }).click();
        const workspace = page.getByRole("region", { name: "技能" });
        await checkedVisible(workspace, "skill_workspace_missing");
        check((await workspace.textContent())?.includes("技能、MCP、工具组件和能力包") === true, "extension_categories_missing");
        check(await workspace.locator("details.ex-protected-skills").count() === 1, "protected_skills_disclosure_missing");
      },
      "steer-queue-replace": async () => {
        await openThread("retry");
        const disposition = page.getByRole("button", { name: "追加到当前任务" });
        await disposition.click();
        check(await page.getByRole("menuitem", { name: "排到下一轮" }).count() === 1, "queue_choice_missing");
        check(await page.getByRole("menuitem", { name: "替换当前任务" }).count() === 1, "replace_choice_missing");
        await page.getByRole("menuitem", { name: "排到下一轮" }).click();
        await page.getByLabel("给 e-Mate 发消息").fill("下一轮整理附件");
        await page.getByRole("button", { name: "发送" }).click();
        await checkedVisible(page.getByText("下一轮整理附件", { exact: true }), "queue_result_missing");
      },
      "reasoning-sticky-replacement": async () => {
        await openThread("thinking");
        const first = page.getByText("正在核对季度资料。", { exact: true });
        await checkedVisible(first, "first_reasoning_missing", 2_000);
        await page.waitForTimeout(200);
        check(await first.isVisible(), "first_reasoning_flapped");
        await checkedVisible(page.getByText("资料已核对，正在整理结果。", { exact: true }), "reasoning_replacement_missing", 3_000);
        await page.waitForTimeout(100);
        check(!(await first.isVisible()), "old_reasoning_not_replaced");
      },
      "permission-default-full": async () => {
        await openThread("artifact");
        const dialog = await settings();
        const section = dialog.getByRole("heading", { name: "权限", exact: true }).locator("..");
        await section.getByRole("button", { name: "启用完全访问" }).click();
        await section.getByRole("button", { name: "确认启用" }).click();
        await checkedVisible(section.getByText("完全访问", { exact: true }), "full_access_failed");
        await section.getByRole("button", { name: "恢复默认权限" }).click();
        await checkedVisible(section.getByText("默认权限", { exact: true }), "default_access_restore_failed");
      },
      "artifact-hover-actions": async () => {
        await openThread("artifact");
        const artifact = page.locator("button.ex-artifact-primary").filter({ hasText: artifactName });
        await checkedVisible(artifact, "artifact_missing");
        check(Number(await page.locator(".ex-artifact-actions").evaluate((element) => getComputedStyle(element).opacity)) === 0, "artifact_actions_not_progressive");
        await artifact.hover();
        await page.waitForFunction(() => Number(getComputedStyle(document.querySelector(".ex-artifact-actions")).opacity) === 1);
        check(Number(await page.locator(".ex-artifact-actions").evaluate((element) => getComputedStyle(element).opacity)) === 1, "artifact_actions_hover_missing");
        await checkedVisible(page.getByRole("button", { name: "精准修图" }), "retouch_hover_action_missing");
      },
      "image-fit-preview": async () => {
        await openThread("artifact");
        await page.locator("button.ex-artifact-primary").filter({ hasText: artifactName }).click();
        const dialog = page.getByRole("dialog", { name: artifactName });
        await checkedVisible(dialog, "image_preview_missing");
        await checkedVisible(dialog.getByText("适合窗口", { exact: true }), "fit_mode_missing");
        check(await dialog.getByRole("button", { name: "缩小图片" }).isDisabled(), "initial_preview_not_fit");
        await dialog.getByRole("button", { name: "放大图片" }).click();
        await dialog.getByRole("button", { name: "显示完整图片" }).click();
        await checkedVisible(dialog.getByText("适合窗口", { exact: true }), "fit_restore_failed");
      },
      "precise-retouch": async () => {
        await openThread("artifact");
        const artifact = page.locator("button.ex-artifact-primary").filter({ hasText: artifactName });
        await artifact.hover();
        await page.getByRole("button", { name: "精准修图" }).click();
        const dialog = page.getByRole("dialog", { name: "精准修图" });
        await checkedVisible(dialog, "retouch_dialog_missing");
        await dialog.getByRole("textbox", { name: "整体修改说明" }).fill("保持构图不变，只优化主体边缘过渡。");
        await dialog.getByRole("button", { name: "开始修图" }).click();
        await checkedVisible(dialog.getByText(/新修订已完成/u), "retouch_completion_missing", 8_000);
        await checkedVisible(page.getByRole("button", { name: /查看修图结果：精准修图_/u }), "retouch_result_missing");
      },
      "share-chat-image": async () => {
        await openThread("artifact");
        await page.getByRole("button", { name: "分享当前任务" }).click();
        const dialog = page.getByRole("dialog", { name: "分享任务" });
        await checkedVisible(dialog, "share_dialog_missing");
        await dialog.getByRole("button", { name: "创建新链接" }).click();
        await checkedVisible(dialog.locator(".ex-share-url input"), "share_url_missing");
        check(await page.locator("button.ex-artifact-primary").filter({ hasText: artifactName }).count() === 1, "share_source_image_missing");
      },
      "share-role-separation": async () => {
        await openThread("artifact");
        check(await page.locator(".ex-message.is-user").count() >= 1, "user_role_missing");
        check(await page.locator(".ex-message.is-assistant").count() >= 1, "assistant_role_missing");
        check(await page.locator(".ex-message-avatar").count() === 0, "chat_avatar_not_removed");
      },
      "project-session": async () => {
        await gotoApp("artifact");
        await page.getByRole("button", { name: "新建任务" }).click();
        await page.getByRole("button", { name: "选择项目会话" }).click();
        await page.getByRole("menuitemradio", { name: /季度报告/u }).click();
        await checkedVisible(page.getByText("季度报告 项目会话", { exact: true }), "project_session_selection_failed");
        await checkedVisible(page.getByText(/将从 季度报告 项目开始/u), "project_context_notice_missing");
      },
      "office-document-flow": async () => {
        await openThread("artifact");
        await page.locator(".ex-composer textarea").fill("把季度资料整理成正式办公文档");
        await page.getByRole("button", { name: "发送" }).click();
        await checkedVisible(page.getByText("已完成资料整理；关键结论与待办已写入结果。", { exact: true }), "office_response_missing", 8_000);
        check(await page.getByRole("group", { name: "任务类型" }).count() === 0, "office_mode_split_present");
      },
      "connector-catalog": async () => {
        await openThread("artifact");
        await page.getByRole("button", { name: /^管理连接器/ }).click();
        const popover = page.locator(".ex-connector-popover");
        await checkedVisible(popover, "connector_catalog_missing");
        const text = await popover.textContent();
        check(text?.includes("飞书") === true, "feishu_connector_missing");
        check(text?.includes("腾讯文档") === true, "tencent_docs_connector_missing");
      },
      "memory-reset-output-path": async () => {
        await openThread("artifact");
        const dialog = await settings();
        const output = dialog.getByLabel("默认产物保存位置");
        await output.selectOption("downloads");
        await page.waitForFunction(() => document.querySelector("select[aria-label='默认产物保存位置']")?.value === "downloads");
        check(await output.inputValue() === "downloads", "output_location_failed");
        const memory = dialog.getByRole("heading", { name: "记忆", exact: true }).locator("..");
        await memory.getByRole("button", { name: "一键重置" }).click();
        await memory.getByRole("button", { name: "确认重置" }).click();
        await checkedVisible(memory.getByText("0 项可重置的偏好和资料记忆", { exact: true }), "memory_reset_failed");
        await memory.getByRole("button", { name: "撤销重置" }).click();
        await checkedVisible(memory.getByText("2 项可重置的偏好和资料记忆", { exact: true }), "memory_undo_failed");
      },
    };

    for (const scenarioId of scenarioIds) {
      currentScenario = scenarioId;
      if (progress) process.stderr.write(`${JSON.stringify({ phase: scenarioId })}\n`);
      const remaining = expiresAt - Date.now();
      if (remaining <= 0) throw new Error("preflight_timeout");
      await bounded(scenarios[scenarioId](), Math.min(30_000, remaining), "scenario_timeout");
      screenshotDigests[scenarioId] = sha256(await bounded(
        page.screenshot({ type: "png" }),
        Math.min(10_000, Math.max(1_000, expiresAt - Date.now())),
        "screenshot_timeout",
      ));
    }
    if (progress && failedRequestKinds.length) {
      process.stderr.write(`${JSON.stringify({ failed_request_kinds: failedRequestKinds.slice(0, 12) })}\n`);
    }
    check(diagnostics.console_errors === 0, "console_errors_present");
    check(diagnostics.page_errors === 0, "page_errors_present");
    check(diagnostics.failed_requests === 0, "failed_requests_present");
    check(diagnostics.external_requests === 0, "external_requests_present");

    const commit = spawnSync("git", ["rev-parse", "HEAD"], {
      cwd: repositoryRoot,
      encoding: "utf8",
      windowsHide: true,
    }).stdout.trim();
    check(/^[0-9a-f]{40}$/.test(commit), "commit_identity_invalid");
    return {
      schema_version: 1,
      evidence_type: "ecorex-local-live-preflight",
      status: "passed",
      commit_sha: commit,
      candidate_bound: false,
      protected_provenance_claimed: false,
      runtime_source: "local-ga-contract-runtime",
      browser: {
        engine: "chrome",
        version: String(versionProjection.Browser || "").replace(/^Chrome\//, ""),
        protocol: "cdp",
        isolated_profile: true,
      },
      scenarios: scenarioIds,
      viewports: viewports.map((viewport) => viewport.id),
      assertions,
      diagnostics,
      screenshot_sha256: screenshotDigests,
      duration_milliseconds: Math.round(performance.now() - startedAt),
    };
  } catch (error) {
    const code = error instanceof Error && /^[a-z][a-z0-9_]{2,127}$/.test(error.message)
      ? error.message
      : "local_cdp_preflight_failed";
    const failure = new Error(code);
    failure.failedScenario = currentScenario;
    throw failure;
  } finally {
    if (browser) await bounded(browser.close(), 3_000, "browser_close_timeout").catch(() => undefined);
    terminate(chrome);
    terminate(server);
    await rm(profileRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

async function main() {
  const reportPath = argument("--report");
  try {
    const report = await runPreflight();
    const payload = `${JSON.stringify(report, null, 2)}\n`;
    if (reportPath) {
      const destination = resolve(reportPath);
      const temporary = `${destination}.tmp-${process.pid}`;
      await writeFile(temporary, payload, { encoding: "utf8", flag: "wx" });
      await rename(temporary, destination);
    }
    process.stdout.write(payload);
  } catch (error) {
    const code = error instanceof Error && /^[a-z][a-z0-9_]{2,127}$/.test(error.message)
      ? error.message
      : "local_cdp_preflight_failed";
    const failedScenario = typeof error?.failedScenario === "string" ? error.failedScenario : "unknown";
    process.stderr.write(`${JSON.stringify({ ok: false, error: code, failed_scenario: failedScenario })}\n`);
    process.exitCode = 1;
  }
}

await main();

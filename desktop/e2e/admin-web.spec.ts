import { expect, test, type Page, type Route } from "@playwright/test";

const ADMIN_ORIGIN = "http://127.0.0.1:4180";
const ADMIN_TOKEN = "x.eyJleHAiOjQxMDI0NDQ4MDB9.x";
const RELEASE_ID = "release-stable-admin-e2e";
const RELEASE_GATES = [
  "bootstrap-index",
  "cdn-sync",
  "cdp-acceptance",
  "contract",
  "e2e",
  "github-release",
  "image-shared-storage",
  "image-soak",
  "integration",
  "license",
  "lint",
  "live-image",
  "live-model",
  "macos-build",
  "migration-dry-run",
  "mirror-sync",
  "reproducibility",
  "sbom",
  "secret-scan",
  "signature",
  "size-scan",
  "typecheck",
  "unit",
  "windows-build",
] as const;

const candidate = (missing: string[] = [], status = "candidate") => ({
  release_id: RELEASE_ID,
  version: "1.0.0",
  build_digest: "a".repeat(64),
  channel: "stable",
  status,
  gates: Object.fromEntries(
    RELEASE_GATES.map((gate) => [gate, missing.includes(gate) ? "missing" : "passed"]),
  ),
  missing_gates: missing,
});

const resume = (missing: string[] = []) => ({
  schema_version: 1,
  candidates: [candidate(missing)],
  latest_candidate_id: RELEASE_ID,
  rollouts: [],
  latest_rollout_id: null,
  channel_kill_switches: [
    { channel: "canary", kill_switch_active: false, halted_rollout_ids: [] },
    { channel: "stable", kill_switch_active: false, halted_rollout_ids: [] },
  ],
  distribution: { total_clients: 0, versions: {}, update_states: {} },
  captured_at: "2026-07-14T08:00:00Z",
});

const adminUser = (body: Record<string, unknown>) => ({
  account_id: body.account_id,
  display_name: body.display_name,
  email: body.email,
  organization_id: body.organization_id,
  status: "active",
  token_limit: body.token_limit,
  tokens_used: 0,
  image_limit: body.image_limit,
  images_used: 0,
  password_configured: typeof body.password === "string" && body.password.length >= 10,
  credential_state: typeof body.password === "string" && body.password.length >= 10 ? "configured" : "missing",
  password_changed_at: typeof body.password === "string" && body.password.length >= 10
    ? "2026-07-14T08:00:00Z"
    : null,
  revision: 1,
  created_at: "2026-07-14T08:00:00Z",
  updated_at: "2026-07-14T08:00:00Z",
});

const modelRevision = (body: Record<string, unknown>, status = "draft") => ({
  config_id: "model-config-e2e",
  revision: 1,
  local_model_id: body.local_model_id,
  modality: body.modality,
  display_name: body.display_name,
  upstream_model_id: body.upstream_model_id,
  provider_preset: body.provider_preset,
  is_default: body.is_default,
  enabled: body.enabled,
  status,
  key_configured: true,
  key_fingerprint: "0123456789abcdef",
  test_id: status === "active" ? "model-test-e2e" : null,
  test_status: status === "active" ? "passed" : "not_tested",
  test_error_code: null,
  tested_at: status === "active" ? "2026-07-14T08:01:00Z" : null,
  created_at: "2026-07-14T08:00:00Z",
  updated_at: "2026-07-14T08:01:00Z",
});

async function installAdminApi(
  page: Page,
  options: { missing?: string[]; onPublish?: (body: unknown) => void } = {},
): Promise<{
  browserErrors: string[];
  externalRequests: string[];
  userCreateBody: unknown;
  modelCreateBody: unknown;
  modelTestBody: unknown;
  rolloutBody: unknown;
}> {
  const guard = {
    browserErrors: [] as string[],
    externalRequests: [] as string[],
    userCreateBody: null as unknown,
    modelCreateBody: null as unknown,
    modelTestBody: null as unknown,
    rolloutBody: null as unknown,
  };
  let storedUser: ReturnType<typeof adminUser> | null = null;
  let storedModel: { config_id: string; active: ReturnType<typeof modelRevision> | null; draft: ReturnType<typeof modelRevision> | null } | null = null;
  page.on("pageerror", (error) => guard.browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") guard.browserErrors.push(message.text());
  });
  await page.route("**/*", async (route: Route) => {
    const parsed = new URL(route.request().url());
    if (parsed.origin === ADMIN_ORIGIN || parsed.protocol === "blob:" || parsed.protocol === "data:") {
      await route.fallback();
      return;
    }
    guard.externalRequests.push(parsed.href);
    await route.abort("blockedbyclient");
  });
  await page.route("**/api/v1/admin/**", async (route: Route) => {
    const request = route.request();
    expect(request.headers().authorization).toBe(`Bearer ${ADMIN_TOKEN}`);
    const path = new URL(request.url()).pathname;
    if (path.includes("/gates/") || path.endsWith("/gate-bundle")) {
      throw new Error(`browser attempted forbidden gate mutation: ${path}`);
    }
    if (path === "/api/v1/admin/resume" && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(resume(options.missing)) });
      return;
    }
    if (path === "/api/v1/admin/users" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: storedUser ? [storedUser] : [], total: storedUser ? 1 : 0, offset: 0, limit: 200 }),
      });
      return;
    }
    if (path === "/api/v1/admin/users" && request.method() === "POST") {
      guard.userCreateBody = request.postDataJSON();
      storedUser = adminUser(guard.userCreateBody as Record<string, unknown>);
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(storedUser) });
      return;
    }
    if (path === "/api/v1/admin/usage/summary" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          users_total: storedUser ? 1 : 0,
          users_active: storedUser ? 1 : 0,
          token_limit: storedUser?.token_limit || 0,
          tokens_used: 0,
          image_limit: storedUser?.image_limit || 0,
          images_used: 0,
          captured_at: "2026-07-14T08:00:00Z",
        }),
      });
      return;
    }
    if (path === "/api/v1/admin/models" && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(storedModel ? [storedModel] : []) });
      return;
    }
    if (path === "/api/v1/admin/models" && request.method() === "POST") {
      guard.modelCreateBody = request.postDataJSON();
      const revision = modelRevision(guard.modelCreateBody as Record<string, unknown>);
      storedModel = { config_id: revision.config_id, active: null, draft: revision };
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(storedModel) });
      return;
    }
    if (path === "/api/v1/admin/models/model-config-e2e/test-and-activate" && request.method() === "POST") {
      guard.modelTestBody = request.postDataJSON();
      if (!storedModel?.draft) throw new Error("model test was called without a draft");
      const active = { ...storedModel.draft, status: "active", test_id: "model-test-e2e", test_status: "passed", tested_at: "2026-07-14T08:01:00Z" };
      storedModel = { config_id: storedModel.config_id, active, draft: null };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          test_id: "model-test-e2e",
          config_id: "model-config-e2e",
          revision: 1,
          status: "passed",
          error_code: null,
          active_revision: 1,
          completed_at: "2026-07-14T08:01:00Z",
        }),
      });
      return;
    }
    if (path === "/api/v1/admin/rollouts" && request.method() === "POST") {
      guard.rolloutBody = request.postDataJSON();
      const body = guard.rolloutBody as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          rollout_id: "rollout-full-e2e",
          release_id: body.release_id,
          channel: "stable",
          status: "pending",
          percentage: body.percentage,
          target_organization_ids: body.target_organization_ids,
          target_account_ids: body.target_account_ids,
          minimum_compatible_version: body.minimum_compatible_version,
          created_at: "2026-07-14T08:02:00Z",
        }),
      });
      return;
    }
    if (path === `/api/v1/admin/releases/${RELEASE_ID}/publish` && request.method() === "POST") {
      options.onPublish?.(request.postDataJSON());
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(candidate([], "published")) });
      return;
    }
    throw new Error(`unexpected administrator API request: ${request.method()} ${path}`);
  });
  await page.route("**/v1/session/login", async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    expect(body).toMatchObject({
      schema_version: 1,
      client_id: "ecorex-admin-web",
      identifier: "admin@example.com",
      password: "admin-password-e2e",
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "authorized",
        access_token: ADMIN_TOKEN,
        refresh_token: "r".repeat(32),
        lease: { claims: { lease_id: "lease-admin-e2e" } },
      }),
    });
  });
  return guard;
}

async function connect(page: Page): Promise<void> {
  await page.locator("#admin-identifier").fill("admin@example.com");
  await page.locator("#admin-password").fill("admin-password-e2e");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.locator("#session-label")).toContainText("已连接");
}

test("administrator gates are a signed read-only projection and publication stays server-authoritative", async ({ page }) => {
  let publicationBody: unknown = null;
  const guard = await installAdminApi(page, { onPublish: (body) => { publicationBody = body; } });
  const response = await page.goto(`${ADMIN_ORIGIN}/admin/`, { waitUntil: "domcontentloaded" });
  expect(response?.headers()["cache-control"]).toContain("no-store");
  expect(response?.headers()["content-security-policy"]).toContain("default-src 'none'");
  await connect(page);

  await expect(page.locator("#gate-table-body tr")).toHaveCount(RELEASE_GATES.length);
  await expect(page.locator("#gate-table-body select, #gate-table-body input, #gate-table-body button")).toHaveCount(0);
  await expect(page.locator("#gate-summary")).toContainText("24/24 已通过");
  await expect(page.locator("#gate-summary")).toContainText("签名 Candidate 门禁包导入");

  const publish = page.getByRole("button", { name: "发布候选" });
  await expect(publish).toBeEnabled();
  await publish.click();
  await expect(page.getByRole("dialog", { name: "确认发布候选" })).toBeVisible();
  await page.locator("#confirm-submit-button").click();
  await expect(page.locator("#candidate-status")).toHaveText("published");
  await expect(publish).toBeDisabled();
  expect(publicationBody).toMatchObject({ client_request_id: expect.stringMatching(/^admin_[0-9a-f]{32}$/u) });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#admin-password")).toHaveValue("");
  await expect(page.locator("#session-label")).toHaveText("未连接");
  expect(guard.externalRequests).toEqual([]);
  expect(guard.browserErrors).toEqual([]);
});

test("administrator manages users, hot-tests a model, and creates an explicit full rollout", async ({ page }) => {
  const guard = await installAdminApi(page);
  await page.goto(`${ADMIN_ORIGIN}/admin/`, { waitUntil: "domcontentloaded" });
  await connect(page);

  await page.getByRole("button", { name: "创建用户" }).click();
  await page.locator("#user-account-id").fill("account-e2e");
  await page.locator("#user-display-name").fill("验收用户");
  await page.locator("#user-email").fill("user@example.com");
  await page.locator("#user-organization").fill("org-e2e");
  await page.locator("#user-token-limit").fill("200000");
  await page.locator("#user-image-limit").fill("100");
  const initialPassword = "e2e-user-password-123";
  await page.locator("#user-password").fill(initialPassword);
  await page.getByRole("button", { name: "保存用户" }).click();
  await expect(page.locator("#user-table-body")).toContainText("验收用户");
  expect(guard.userCreateBody).toMatchObject({
    account_id: "account-e2e",
    token_limit: 200000,
    image_limit: 100,
    password: initialPassword,
    client_request_id: expect.stringMatching(/^admin_[0-9a-f]{32}$/u),
  });
  await expect(page.locator("#user-table-body")).toContainText("密码已设置");

  const modelSecret = "sk-e2e-model-secret-123456";
  await page.getByRole("button", { name: "添加模型" }).click();
  await page.locator("#model-local-id").selectOption("ecorex-chat");
  await page.locator("#model-display-name").fill("GPT-5.6 SOL · 中等推理");
  await page.locator("#model-upstream-id").fill("gpt-5.6-sol");
  await page.locator("#model-api-key").fill(modelSecret);
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.locator("#model-table-body")).toContainText("GPT-5.6 SOL");
  await expect(page.locator("#model-api-key")).toHaveValue("");
  await expect(page.locator("body")).not.toContainText(modelSecret);
  expect(guard.modelCreateBody).toMatchObject({
    local_model_id: "ecorex-chat",
    modality: "chat",
    upstream_model_id: "gpt-5.6-sol",
    api_key: modelSecret,
  });

  await page.getByRole("button", { name: "测试并启用" }).click();
  await expect(page.getByRole("dialog", { name: /\u6d4b试并启用/u })).toBeVisible();
  await page.locator("#confirm-submit-button").click();
  await expect(page.locator("#model-table-body")).toContainText("已生效");
  expect(guard.modelTestBody).toMatchObject({
    revision: 1,
    client_request_id: expect.stringMatching(/^admin_[0-9a-f]{32}$/u),
  });

  await page.getByRole("button", { name: "发布候选" }).click();
  await page.locator("#confirm-submit-button").click();
  await expect(page.locator("#candidate-status")).toHaveText("published");
  await page.locator("#rollout-mode").selectOption("full");
  await expect(page.locator("#rollout-percentage")).toHaveValue("100");
  await expect(page.locator("#target-accounts")).toBeDisabled();
  await page.getByRole("button", { name: "创建全量推送" }).click();
  await expect(page.getByRole("dialog", { name: "确认全量推送" })).toBeVisible();
  await page.locator("#confirm-submit-button").click();
  await expect(page.locator("#rollout-status")).toHaveText("pending");
  expect(guard.rolloutBody).toMatchObject({
    percentage: 100,
    target_organization_ids: [],
    target_account_ids: [],
    client_request_id: expect.stringMatching(/^admin_[0-9a-f]{32}$/u),
  });
  expect(guard.externalRequests).toEqual([]);
  expect(guard.browserErrors).toEqual([]);
});

test("administrator gate table remains non-mutating and page-safe at 390px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const guard = await installAdminApi(page, { missing: ["live-image"] });
  await page.goto(`${ADMIN_ORIGIN}/admin/`, { waitUntil: "domcontentloaded" });
  await connect(page);

  await expect(page.getByRole("button", { name: "发布候选" })).toBeDisabled();
  await expect(page.locator("#gate-summary")).toContainText("1 项仍缺失");
  await expect(page.locator("#gate-table-body select, #gate-table-body input, #gate-table-body button")).toHaveCount(0);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  expect(guard.externalRequests).toEqual([]);
  expect(guard.browserErrors).toEqual([]);
});

import { expect, test, type Page, type Route } from "@playwright/test";

const ADMIN_ORIGIN = "http://127.0.0.1:4180";
const ADMIN_TOKEN = "admin-e2e-token-1234567890";
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

async function installAdminApi(
  page: Page,
  options: { missing?: string[]; onPublish?: (body: unknown) => void } = {},
): Promise<{ browserErrors: string[]; externalRequests: string[] }> {
  const guard = { browserErrors: [] as string[], externalRequests: [] as string[] };
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
    if (path === `/api/v1/admin/releases/${RELEASE_ID}/publish` && request.method() === "POST") {
      options.onPublish?.(request.postDataJSON());
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(candidate([], "published")) });
      return;
    }
    throw new Error(`unexpected administrator API request: ${request.method()} ${path}`);
  });
  return guard;
}

async function connect(page: Page): Promise<void> {
  await page.locator("#admin-token").fill(ADMIN_TOKEN);
  await page.getByRole("button", { name: "连接控制面" }).click();
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
  await expect(page.locator("#admin-token")).toHaveValue("");
  await expect(page.locator("#session-label")).toHaveText("未连接");
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

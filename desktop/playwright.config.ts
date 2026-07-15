import { defineConfig, devices } from "@playwright/test";

const host = "127.0.0.1";
const port = 4179;
const baseURL = `http://${host}:${port}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./tmp/playwright-results",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 45_000,
  expect: {
    timeout: 12_000,
  },
  reporter: [
    ["line"],
    ["html", { outputFolder: "./tmp/playwright-report", open: "never" }],
  ],
  use: {
    baseURL,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    permissions: ["clipboard-read", "clipboard-write"],
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        browserName: "chromium",
        viewport: { width: 1600, height: 1200 },
      },
    },
  ],
  webServer: [
    {
      command: `node tools/ga-mock-server.mjs --port=${port} --scenario=artifact`,
      url: `${baseURL}/__ga/viewport-matrix`,
      reuseExistingServer: false,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "python ../scripts/serve-v1-admin-e2e.py --port=4180",
      url: "http://127.0.0.1:4180/__admin_e2e/ready",
      reuseExistingServer: false,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});

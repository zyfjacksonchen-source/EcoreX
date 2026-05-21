const { defineConfig } = require('@playwright/test');

const localNoProxy = ['127.0.0.1', 'localhost'];
const existingNoProxy = process.env.NO_PROXY || process.env.no_proxy || '';
process.env.NO_PROXY = [...new Set([
  ...existingNoProxy.split(',').map((item) => item.trim()).filter(Boolean),
  ...localNoProxy
])].join(',');
process.env.no_proxy = process.env.NO_PROXY;

module.exports = defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.cjs',
  timeout: 60 * 1000,
  expect: {
    timeout: 10 * 1000
  },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  outputDir: 'test-results/e2e',
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off'
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5188 --strictPort',
    url: 'http://127.0.0.1:5188',
    reuseExistingServer: !process.env.CI,
    timeout: 120 * 1000
  }
});

#!/usr/bin/env node

const { spawn } = require('child_process');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');

function budgetMs(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function formatMs(value) {
  return `${(value / 1000).toFixed(2)}s`;
}

function runTimed(label, command, args, options = {}) {
  const budget = options.budgetMs;
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    console.log(`\n[responsive-smoke] ${label} started`);
    const child = spawn(command, args, {
      cwd: repoRoot,
      env: {
        ...process.env,
        ...(options.env || {})
      },
      stdio: 'inherit',
      windowsHide: true
    });

    child.on('error', reject);
    child.on('close', (code) => {
      const elapsedMs = Date.now() - startedAt;
      if (code !== 0) {
        reject(new Error(`${label} failed with exit code ${code} after ${formatMs(elapsedMs)}.`));
        return;
      }
      if (elapsedMs > budget) {
        reject(new Error(`${label} exceeded budget ${formatMs(budget)}; actual ${formatMs(elapsedMs)}.`));
        return;
      }
      console.log(`[responsive-smoke] ${label} passed in ${formatMs(elapsedMs)} (budget ${formatMs(budget)})`);
      resolve({ label, elapsedMs, budgetMs: budget });
    });
  });
}

async function main() {
  const buildBudget = budgetMs('ECOREX_RESPONSIVE_BUILD_BUDGET_MS', 120_000);
  const e2eBudget = budgetMs('ECOREX_RESPONSIVE_E2E_BUDGET_MS', 180_000);
  const viteCli = path.join(repoRoot, 'node_modules', 'vite', 'bin', 'vite.js');
  const playwrightCli = path.join(repoRoot, 'node_modules', '@playwright', 'test', 'cli.js');

  const results = [];
  results.push(await runTimed('Vite production build', process.execPath, [viteCli, 'build'], {
    budgetMs: buildBudget
  }));
  results.push(await runTimed('Electron E2E @responsive including large message ledger input and file preview', process.execPath, [
    playwrightCli,
    'test',
    '-c',
    'playwright.e2e.config.cjs',
    '--grep',
    '@responsive'
  ], {
    budgetMs: e2eBudget,
    env: {
      ECOREX_RESPONSIVE_SMOKE: '1',
      ECOREX_E2E_LARGE_LEDGER_INPUT_BUDGET_MS: process.env.ECOREX_E2E_LARGE_LEDGER_INPUT_BUDGET_MS || '1500'
    }
  }));

  console.log('\n[responsive-smoke] summary');
  for (const result of results) {
    console.log(`- ${result.label}: ${formatMs(result.elapsedMs)} / ${formatMs(result.budgetMs)}`);
  }
}

main().catch((error) => {
  console.error(`\n[responsive-smoke] ${error.message || error}`);
  process.exitCode = 1;
});

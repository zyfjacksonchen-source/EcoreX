#!/usr/bin/env node

const fs = require('fs');
const os = require('os');
const path = require('path');
const { closeEcorex, launchEcorex, login } = require('../tests/e2e/helpers/electron-app.cjs');

const rootDir = path.resolve(__dirname, '..');
const reportDir = path.join(rootDir, 'reports', 'qa');

function envInt(name, fallback, min, max) {
  const value = Number(process.env[name]);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(Math.max(Math.trunc(value), min), max);
}

function requiredConfig() {
  const config = {
    baseUrl: String(process.env.ECOREX_REAL_MODEL_BASE_URL || '').trim(),
    apiKey: String(process.env.ECOREX_REAL_MODEL_API_KEY || '').trim(),
    model: String(process.env.ECOREX_REAL_MODEL_NAME || '').trim(),
    imageModel: String(process.env.ECOREX_REAL_IMAGE_MODEL || 'image-2').trim(),
    concurrency: envInt('ECOREX_REAL_AGENT_CONCURRENCY', 2, 1, 4),
    timeoutMs: envInt('ECOREX_REAL_AGENT_TIMEOUT_MS', 10 * 60 * 1000, 60 * 1000, 30 * 60 * 1000),
    cancelAfterMs: envInt('ECOREX_REAL_AGENT_CANCEL_AFTER_MS', 8000, 2000, 60 * 1000),
    includeTimeoutProbe: process.env.ECOREX_REAL_AGENT_TIMEOUT_PROBE === '1'
  };
  const missing = [];
  if (!config.baseUrl) missing.push('ECOREX_REAL_MODEL_BASE_URL');
  if (!config.apiKey) missing.push('ECOREX_REAL_MODEL_API_KEY');
  if (!config.model) missing.push('ECOREX_REAL_MODEL_NAME');
  return { config, missing };
}

function largeAdvertisingPrompt(targetChars = 48000) {
  const seed = [
    '你正在为一个新品广告项目做长任务压测。',
    '请基于以下模拟数据完成：投放诊断、预算调整、素材策略、归因风险、下周执行清单。',
    '要求先拆解任务，再输出结构化结论。不要调用外部不可用资源。',
    '模拟数据字段：日期、渠道、计划、素材、展现、点击、消耗、转化、线索质量、备注。'
  ].join('\n');
  const row = '2026-05-22, 信息流, 新品A, 短视频开头钩子, 120000, 3821, 15800, 243, 高, 点击率稳定但夜间成本升高。\n';
  return `${seed}\n\n${row.repeat(Math.ceil((targetChars - seed.length) / row.length)).slice(0, targetChars)}`;
}

async function setupRealModel(page, config) {
  return page.evaluate(async (profile) => {
    const saved = await window.ecorex.saveModelProfile({
      name: 'real-agent-stress',
      label: 'Real Agent Stress',
      baseUrl: profile.baseUrl,
      apiKey: profile.apiKey,
      model: profile.model,
      imageModel: profile.imageModel,
      isActive: true,
      allowPrivateBaseUrl: true,
      confirmPrivateBaseUrl: true
    });
    if (!saved?.ok) throw new Error(saved?.error || 'saveModelProfile failed');
    const activated = await window.ecorex.activateModelProfile({ name: 'real-agent-stress' });
    if (!activated?.ok) throw new Error(activated?.error || 'activateModelProfile failed');
    const tested = await window.ecorex.testModelAdapterProfile({ name: 'real-agent-stress' });
    if (!tested?.ok) throw new Error(tested?.error || 'testModelAdapterProfile failed');
    const project = await window.ecorex.createProject({
      name: `真实模型压测 ${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`,
      client: 'EcoreX 压测客户',
      industry: '广告服务',
      scenario: '信息流 / 搜索 / 达人种草',
      goal: '验证并发、取消、超时、大上下文和项目记忆隔离',
      budget: '压测预算',
      period: '生产前验收',
      deliverables: ['压测报告', '异常清单', '上线建议']
    });
    if (!project?.ok) throw new Error(project?.error || 'createProject failed');
    return {
      modelTest: {
        ok: tested.ok,
        latencyMs: tested.latencyMs || tested.durationMs || null,
        status: tested.status || tested.message || 'ok'
      },
      project: {
        id: project.project?.id,
        name: project.project?.name,
        memoryLabel: project.project?.memoryLabel
      }
    };
  }, config);
}

async function runPrompt(page, payload) {
  return page.evaluate((input) => window.ecorex.runPrompt(input), payload);
}

async function runStress(page, config) {
  const startedAt = Date.now();
  const setup = await setupRealModel(page, config);
  const prompts = Array.from({ length: config.concurrency }, (_, index) => ({
    sessionId: `stress-${Date.now()}-${index}`,
    prompt: [
      `并发压测任务 ${index + 1}/${config.concurrency}。`,
      '请输出广告投放诊断、预算优化建议、素材实验计划和项目风险。',
      '要求给出可执行步骤，不要输出密钥或本地路径。'
    ].join('\n'),
    model: config.model,
    timeoutMs: config.timeoutMs
  }));

  const concurrentStartedAt = Date.now();
  const concurrent = await Promise.all(prompts.map((payload) => runPrompt(page, payload)));
  const concurrentMs = Date.now() - concurrentStartedAt;

  const largeStartedAt = Date.now();
  const large = await runPrompt(page, {
    sessionId: `stress-large-${Date.now()}`,
    prompt: largeAdvertisingPrompt(),
    model: config.model,
    timeoutMs: config.timeoutMs
  });
  const largeMs = Date.now() - largeStartedAt;

  const cancel = await page.evaluate(async (input) => {
    const promise = window.ecorex.runPrompt({
      sessionId: input.sessionId,
      prompt: input.prompt,
      model: input.model,
      timeoutMs: input.timeoutMs
    });
    await new Promise((resolve) => setTimeout(resolve, input.cancelAfterMs));
    const stopped = await window.ecorex.stopPrompt({ sessionId: input.sessionId });
    const result = await promise;
    return { stopped, result };
  }, {
    sessionId: `stress-cancel-${Date.now()}`,
    prompt: '请执行一个较长的广告项目分析任务，持续拆解预算、素材和归因风险，直到被取消。',
    model: config.model,
    timeoutMs: config.timeoutMs,
    cancelAfterMs: config.cancelAfterMs
  });

  let timeoutProbe = null;
  if (config.includeTimeoutProbe) {
    const timeoutStartedAt = Date.now();
    timeoutProbe = await runPrompt(page, {
      sessionId: `stress-timeout-${Date.now()}`,
      prompt: '请进行一个很长的逐步分析，持续输出直到超时，用于验证超时恢复。',
      model: config.model,
      timeoutMs: 30 * 1000
    });
    timeoutProbe.elapsedMs = Date.now() - timeoutStartedAt;
  }

  const diagnostics = await page.evaluate(() => Promise.all([
    window.ecorex.getAgentSessions({ includeHistory: true }),
    window.ecorex.getCrashRecoveryStatus({ limit: 10 }),
    window.ecorex.exportDiagnosticsPackage({ saveToFile: false, logLimit: 20, sessionLimit: 10 })
  ]).then(([sessions, crashes, exported]) => ({
    sessions,
    crashes,
    diagnosticsSchema: exported?.diagnosticsPackage?.schema,
    diagnosticsRedacted: exported?.diagnosticsPackage?.privacy
  })));

  return {
    schema: 'ecorex.real-agent-stress.v1',
    generatedAt: new Date().toISOString(),
    host: {
      platform: process.platform,
      arch: process.arch,
      release: os.release()
    },
    config: {
      baseUrlHost: new URL(config.baseUrl).host,
      model: config.model,
      imageModel: config.imageModel,
      concurrency: config.concurrency,
      timeoutMs: config.timeoutMs,
      includeTimeoutProbe: config.includeTimeoutProbe
    },
    setup,
    timings: {
      totalMs: Date.now() - startedAt,
      concurrentMs,
      largeMs
    },
    checks: {
      modelProfile: setup.modelTest.ok === true,
      concurrentCompleted: concurrent.every((item) => item?.ok === true || ['completed', 'failed', 'timeout'].includes(item?.status)),
      largeContextCompleted: large?.ok === true || ['completed', 'failed', 'timeout'].includes(large?.status),
      cancelReturned: cancel?.stopped?.ok === true || ['cancelled', 'stopped'].includes(cancel?.result?.status),
      diagnosticsExported: diagnostics.diagnosticsSchema === 'ecorex.diagnostics.v1',
      diagnosticsRedacted: diagnostics.diagnosticsRedacted?.includesApiKeys !== true
    },
    results: {
      concurrent: concurrent.map((item) => ({
        ok: item?.ok,
        status: item?.status,
        sessionId: item?.sessionId,
        durationMs: item?.durationMs || item?.elapsedMs || null,
        error: item?.error ? String(item.error).slice(0, 300) : ''
      })),
      large: {
        ok: large?.ok,
        status: large?.status,
        sessionId: large?.sessionId,
        error: large?.error ? String(large.error).slice(0, 300) : ''
      },
      cancel: {
        stopped: cancel?.stopped?.ok,
        status: cancel?.result?.status,
        sessionId: cancel?.result?.sessionId,
        error: cancel?.result?.error ? String(cancel.result.error).slice(0, 300) : ''
      },
      timeoutProbe
    },
    diagnostics
  };
}

async function main() {
  const { config, missing } = requiredConfig();
  if (missing.length) {
    console.error(`Missing real model environment variables: ${missing.join(', ')}`);
    console.error('Set ECOREX_REAL_MODEL_BASE_URL, ECOREX_REAL_MODEL_API_KEY, and ECOREX_REAL_MODEL_NAME in your shell, then run npm run test:real-agent.');
    console.error('npm run test:real-agent');
    process.exitCode = 2;
    return;
  }

  fs.mkdirSync(reportDir, { recursive: true });
  const instance = await launchEcorex();
  try {
    await login(instance.page);
    const report = await runStress(instance.page, config);
    const reportPath = path.join(reportDir, `real-agent-stress-${Date.now()}.json`);
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    const failed = Object.entries(report.checks).filter(([, ok]) => !ok);
    console.log(`real agent stress report written: ${path.relative(rootDir, reportPath)}`);
    for (const [name, ok] of Object.entries(report.checks)) {
      console.log(`${ok ? 'ok  ' : 'fail'} ${name}`);
    }
    if (failed.length) process.exitCode = 1;
  } finally {
    await closeEcorex(instance);
  }
}

main().catch((error) => {
  console.error(error?.message || error);
  process.exitCode = 1;
});

#!/usr/bin/env node

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const reportPath = path.join(releaseDir, 'security-audit-report.json');
const strict = process.argv.includes('--strict') || process.env.ECOREX_AUDIT_STRICT === '1';

function runAudit(args) {
  const command = process.platform === 'win32' ? 'cmd.exe' : 'npm';
  const commandArgs = process.platform === 'win32'
    ? ['/d', '/s', '/c', ['npm', 'audit', '--json', ...args].join(' ')]
    : ['audit', '--json', ...args];
  const result = spawnSync(command, commandArgs, {
    cwd: rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.error) {
    return {
      auditReportVersion: 2,
      commandError: result.error.message,
      metadata: { vulnerabilities: { total: 1, critical: 1 } }
    };
  }
  const text = result.stdout || result.stderr || '{}';
  try {
    return JSON.parse(text);
  } catch {
    return {
      auditReportVersion: 2,
      parseError: text.slice(0, 4000),
      metadata: { vulnerabilities: { total: 1, critical: 1 } }
    };
  }
}

function vulnCount(report, level) {
  return Number(report?.metadata?.vulnerabilities?.[level] || 0);
}

function summarize(report) {
  const counts = report?.metadata?.vulnerabilities || {};
  return {
    info: Number(counts.info || 0),
    low: Number(counts.low || 0),
    moderate: Number(counts.moderate || 0),
    high: Number(counts.high || 0),
    critical: Number(counts.critical || 0),
    total: Number(counts.total || 0),
    advisories: Object.entries(report?.vulnerabilities || {}).map(([name, item]) => ({
      name,
      severity: item.severity,
      isDirect: Boolean(item.isDirect),
      range: item.range,
      fixAvailable: item.fixAvailable || false
    }))
  };
}

function main() {
  fs.mkdirSync(releaseDir, { recursive: true });
  const prod = runAudit(['--omit=dev']);
  const all = runAudit([]);
  const report = {
    schema: 'ecorex.security-audit.v1',
    generatedAt: new Date().toISOString(),
    strict,
    production: summarize(prod),
    allDependencies: summarize(all),
    policy: {
      productionCriticalOrHighFails: true,
      productionAnyVulnerabilityFails: true,
      devCriticalOrHighFailsOnlyInStrict: true,
      electronRuntimeUpgradeRequiredBeforePublicRelease: summarize(all).advisories.some((item) => item.name === 'electron')
    }
  };
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');

  console.log(`security audit report written: ${path.relative(rootDir, reportPath)}`);
  console.log(`production vulnerabilities: ${report.production.total}`);
  console.log(`all dependency vulnerabilities: ${report.allDependencies.total}`);

  const prodHasVulns = report.production.total > 0;
  const strictDevHigh = strict && (vulnCount(all, 'critical') > 0 || vulnCount(all, 'high') > 0);
  if (prodHasVulns) {
    console.error('Production dependency audit failed.');
    process.exitCode = 1;
  } else if (strictDevHigh) {
    console.error('Strict audit failed because dev/runtime tooling has high or critical advisories.');
    process.exitCode = 1;
  } else if (report.allDependencies.high || report.allDependencies.critical) {
    console.warn('Dev/runtime tooling has high advisories; upgrade Electron/electron-builder before public production release.');
  }
}

main();

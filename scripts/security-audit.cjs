#!/usr/bin/env node

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const reportDir = path.join(rootDir, 'reports', 'qa');
const reportPath = path.join(reportDir, 'security-audit-report.json');
const strict = process.argv.includes('--strict') || process.env.ECOREX_AUDIT_STRICT === '1';
const secretScanMaxBytes = Number(process.env.ECOREX_SECRET_SCAN_MAX_BYTES || 1024 * 1024);
const secretScanRoots = ['electron', 'src', 'scripts', 'tests', 'release'];
const secretScanExtensions = new Set([
  '.cjs',
  '.css',
  '.html',
  '.js',
  '.json',
  '.jsx',
  '.log',
  '.map',
  '.md',
  '.mjs',
  '.txt',
  '.ts',
  '.tsx',
  '.yml',
  '.yaml'
]);
const secretScanSkipDirs = new Set([
  '.git',
  'coverage',
  'dist',
  'node_modules',
  'out',
  'win-unpacked'
]);
const secretPatterns = [
  { name: 'github-token', pattern: /\bgh[pousr]_[A-Za-z0-9_]{30,}\b/g },
  { name: 'openai-style-key', pattern: /\bsk-[A-Za-z0-9][A-Za-z0-9_-]{20,}\b/g },
  { name: 'anthropic-key', pattern: /\bsk-ant-[A-Za-z0-9_-]{20,}\b/g },
  { name: 'authorization-bearer', pattern: /\bAuthorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._-]{16,}/gi },
  { name: 'secret-assignment', pattern: /\b(api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|session[_-]?token)\s*[:=]\s*['"][^'"\r\n]{16,}['"]/gi }
];

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

function readTextIfPresent(relativePath) {
  try {
    return fs.readFileSync(path.join(rootDir, relativePath), 'utf8');
  } catch {
    return '';
  }
}

function staticSecurityChecks() {
  const main = readTextIfPresent('electron/main.cjs');
  const preload = readTextIfPresent('electron/preload.cjs');
  const app = readTextIfPresent('src/App.jsx');
  const artifactPreviewStart = app.indexOf('function ArtifactPreviewShelf');
  const artifactPreviewEnd = artifactPreviewStart >= 0 ? app.indexOf('function AttachmentPreviewList', artifactPreviewStart) : -1;
  const artifactPreviewSection = artifactPreviewStart >= 0 && artifactPreviewEnd > artifactPreviewStart
    ? app.slice(artifactPreviewStart, artifactPreviewEnd)
    : '';
  const checks = [
    {
      name: 'file preview IPC requires auth',
      passed: /handleSafe\('file:preview'[\s\S]*authRequired:\s*true/.test(main) && /previewFile:\s*\(payload\)[\s\S]*file:preview/.test(preload)
    },
    {
      name: 'file preview has a hard 512KB text limit',
      passed: /FILE_PREVIEW_MAX_BYTES\s*=\s*512\s*\*\s*1024/.test(main)
    },
    {
      name: 'file preview redacts content before returning it',
      passed: /function previewFile[\s\S]*redactSensitiveText\(rawText\)/.test(main)
    },
    {
      name: 'attachment picker IPC requires auth',
      passed: /handleSafe\('attachment:select-files'[\s\S]*authRequired:\s*true/.test(main) && /selectAttachmentFiles:\s*\(payload\)[\s\S]*attachment:select-files/.test(preload)
    },
    {
      name: 'agent attachment ingestion uses structured payloads',
      passed: /function ingestAgentAttachments/.test(main) && /payload\.attachments/.test(main) && /attachments:\s*cleanAttachments/.test(app)
    },
    {
      name: 'attachment previews are size limited',
      passed: /ATTACHMENT_PREVIEW_MAX_BYTES\s*=/.test(main) && /previewDataUrl/.test(main)
    },
    {
      name: 'logs are redacted through redactForLog',
      passed: /function writeLog[\s\S]*redactForLog\(meta\)/.test(main)
    },
    {
      name: 'diagnostics package declares secret and prompt redaction',
      passed: /includesApiKeys:\s*false/.test(main) && /includesPromptFullText:\s*false/.test(main)
    },
    {
      name: 'HTML artifact previews are sandboxed inside the renderer',
      passed: /className="artifact-html-frame"[\s\S]*sandbox=""/.test(app)
    },
    {
      name: 'artifact preview renderer does not call external shell open APIs',
      passed: Boolean(artifactPreviewSection) && !/\bshell\.(openExternal|openPath)\b|\bopenExternal\s*\(|\bopenPath\s*\(|['"]shell\.openPath['"]|['"]openPath['"]/.test(artifactPreviewSection)
    }
  ];
  return {
    total: checks.length,
    passed: checks.filter((check) => check.passed).length,
    failed: checks.filter((check) => !check.passed).map((check) => check.name),
    checks
  };
}

function shouldSkipDirectory(directoryPath) {
  const name = path.basename(directoryPath);
  if (secretScanSkipDirs.has(name)) return true;
  const relative = path.relative(rootDir, directoryPath).split(path.sep).join('/');
  return relative === 'release/win-unpacked' || relative.startsWith('release/win-unpacked/');
}

function shouldScanFile(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  if (!secretScanExtensions.has(extension)) return false;
  try {
    return fs.statSync(filePath).size <= secretScanMaxBytes;
  } catch {
    return false;
  }
}

function walkFiles(directoryPath, files = []) {
  if (!fs.existsSync(directoryPath) || shouldSkipDirectory(directoryPath)) return files;
  for (const entry of fs.readdirSync(directoryPath, { withFileTypes: true })) {
    const fullPath = path.join(directoryPath, entry.name);
    if (entry.isDirectory()) {
      walkFiles(fullPath, files);
    } else if (entry.isFile() && shouldScanFile(fullPath)) {
      files.push(fullPath);
    }
  }
  return files;
}

function allowedSecretMatch(value) {
  return /(sk-test|sk-example|placeholder|redacted|dummy|fake|your-api-key|example-token|process\.env|__ecorex)/i.test(String(value || ''));
}

function lineNumberForIndex(text, index) {
  return text.slice(0, index).split(/\r?\n/).length;
}

function redactSecretSample(value = '') {
  const text = String(value);
  if (text.length <= 12) return '[REDACTED]';
  return `${text.slice(0, 6)}...[${text.length} chars]...${text.slice(-4)}`;
}

function runSecretLeakScan() {
  const files = secretScanRoots.flatMap((relativeRoot) => walkFiles(path.join(rootDir, relativeRoot)));
  const findings = [];
  for (const file of files) {
    let text = '';
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const entry of secretPatterns) {
      entry.pattern.lastIndex = 0;
      let match;
      while ((match = entry.pattern.exec(text))) {
        const value = match[0] || '';
        if (allowedSecretMatch(value)) continue;
        findings.push({
          type: entry.name,
          file: path.relative(rootDir, file),
          line: lineNumberForIndex(text, match.index),
          sample: redactSecretSample(value)
        });
        if (findings.length >= 100) break;
      }
      if (findings.length >= 100) break;
    }
    if (findings.length >= 100) break;
  }
  return {
    scannedFiles: files.length,
    maxFileBytes: secretScanMaxBytes,
    roots: secretScanRoots,
    findings
  };
}

function main() {
  fs.mkdirSync(reportDir, { recursive: true });
  const prod = runAudit(['--omit=dev']);
  const all = runAudit([]);
  const securityChecks = staticSecurityChecks();
  const secretLeakScan = runSecretLeakScan();
  const report = {
    schema: 'ecorex.security-audit.v1',
    generatedAt: new Date().toISOString(),
    strict,
    production: summarize(prod),
    allDependencies: summarize(all),
    attachmentPreviewAndLogPolicy: securityChecks,
    secretLeakScan,
    policy: {
      productionCriticalOrHighFails: true,
      productionAnyVulnerabilityFails: true,
      devCriticalOrHighFailsOnlyInStrict: true,
      attachmentPreviewAndLogPolicyFails: true,
      secretLeakScanFails: true,
      electronRuntimeUpgradeRequiredBeforePublicRelease: summarize(all).advisories.some((item) => item.name === 'electron')
    }
  };
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');

  console.log(`security audit report written: ${path.relative(rootDir, reportPath)}`);
  console.log(`production vulnerabilities: ${report.production.total}`);
  console.log(`all dependency vulnerabilities: ${report.allDependencies.total}`);
  console.log(`attachment/preview/log policy checks: ${securityChecks.passed}/${securityChecks.total}`);
  console.log(`secret leak scan findings: ${secretLeakScan.findings.length} across ${secretLeakScan.scannedFiles} files`);

  const prodHasVulns = report.production.total > 0;
  const strictDevHigh = strict && (vulnCount(all, 'critical') > 0 || vulnCount(all, 'high') > 0);
  const policyFailed = securityChecks.failed.length > 0;
  const secretLeaksFound = secretLeakScan.findings.length > 0;
  if (prodHasVulns) {
    console.error('Production dependency audit failed.');
    process.exitCode = 1;
  } else if (policyFailed) {
    console.error(`Attachment/preview/log security policy failed: ${securityChecks.failed.join(', ')}`);
    process.exitCode = 1;
  } else if (secretLeaksFound) {
    console.error('Potential secret leakage found in attachments/previews/logs/code scan.');
    for (const finding of secretLeakScan.findings.slice(0, 10)) {
      console.error(`- ${finding.type}: ${finding.file}:${finding.line} ${finding.sample}`);
    }
    process.exitCode = 1;
  } else if (strictDevHigh) {
    console.error('Strict audit failed because dev/runtime tooling has high or critical advisories.');
    process.exitCode = 1;
  } else if (report.allDependencies.high || report.allDependencies.critical) {
    console.warn('Dev/runtime tooling has high advisories; upgrade Electron/electron-builder before public production release.');
  }
}

main();

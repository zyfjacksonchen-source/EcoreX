#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

let asar = null;
try {
  asar = require('@electron/asar');
} catch {
  asar = null;
}

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const failures = [];
const passes = [];

const dirtySegmentNames = new Set([
  '.codex',
  '.claude',
  '.mcp',
  '.agents',
  '.ecorex-memory',
  'test-results',
  'playwright-report',
  'sessions',
  'workspace',
  'EcoreX Diagnostics'
].map((item) => item.toLowerCase()));

const dirtyFilePatterns = [
  /^\.env(?:\..*)?$/i,
  /\.log$/i,
  /^settings\.json$/i,
  /^secrets\.json$/i,
  /^auth-session\.json$/i,
  /^auth-identity\.json$/i,
  /^auth-users\.json$/i,
  /^model-profiles\.json$/i,
  /^enterprise-admin-journal\.jsonl$/i,
  /^session-bindings\.json$/i,
  /^skill-packs\.json$/i,
  /^real-agent-stress-\d+\.json$/i,
  /^install-matrix-report\.json$/i,
  /^security-audit-report\.json$/i,
  /\.(?:p12|pfx|pem|key|crt|cer)$/i
];

function rel(target) {
  return path.relative(rootDir, target).replace(/\\/g, '/');
}

function pass(message) {
  passes.push(message);
}

function fail(message) {
  failures.push(message);
}

function isDirtyPath(relativePath) {
  const normalized = String(relativePath || '').replace(/\\/g, '/');
  const segments = normalized.split('/').filter(Boolean);
  if (segments.some((segment) => dirtySegmentNames.has(segment.toLowerCase()))) return true;
  const basename = segments.at(-1) || normalized;
  return dirtyFilePatterns.some((pattern) => pattern.test(basename));
}

function walkFiles(directoryPath, files = []) {
  if (!fs.existsSync(directoryPath)) return files;
  for (const entry of fs.readdirSync(directoryPath, { withFileTypes: true })) {
    const fullPath = path.join(directoryPath, entry.name);
    if (entry.isDirectory()) {
      walkFiles(fullPath, files);
    } else if (entry.isFile()) {
      files.push(fullPath);
    }
  }
  return files;
}

function packageBuildConfig() {
  return JSON.parse(fs.readFileSync(path.join(rootDir, 'package.json'), 'utf8')).build || {};
}

function verifyBuilderDenylist() {
  const build = packageBuildConfig();
  const files = Array.isArray(build.files) ? build.files : [];
  for (const pattern of [
    '!**/.env',
    '!**/.env.*',
    '!**/*.log',
    '!**/secrets.json',
    '!**/auth-session.json',
    '!**/auth-users.json',
    '!**/model-profiles.json',
    '!**/.codex/**/*',
    '!**/.claude/**/*',
    '!**/.mcp.json',
    '!**/.ecorex-memory/**/*'
  ]) {
    if (!files.includes(pattern)) fail(`build.files missing release denylist pattern ${pattern}`);
  }
  pass('electron-builder app files include local-state denylist patterns');

  const resources = Array.isArray(build.extraResources) ? build.extraResources : [];
  for (const resource of resources) {
    const filters = Array.isArray(resource.filter) ? resource.filter : [];
    const resourceTarget = String(resource.to || resource.from || '');
    const looksLikeSingleFile = Boolean(path.extname(resourceTarget)) && !/[*?]/.test(String(resource.from || ''));
    if (looksLikeSingleFile) continue;
    for (const pattern of ['!**/.env', '!**/.env.*', '!**/*.log']) {
      if (!filters.includes(pattern) && String(resource.to || '') !== 'vue-office') {
        fail(`extraResources ${resource.to || resource.from || '?'} missing ${pattern}`);
      }
    }
  }
  pass('extraResources include local-state denylist patterns');
}

function verifyReleaseDirectory() {
  if (!fs.existsSync(releaseDir)) {
    pass('release directory is absent before packaging');
    return;
  }

  const rootDirtyFiles = walkFiles(releaseDir)
    .filter((filePath) => {
      const relative = path.relative(releaseDir, filePath).replace(/\\/g, '/');
      if (relative.startsWith('win-unpacked/')) return false;
      return isDirtyPath(relative);
    })
    .map(rel);
  for (const item of rootDirtyFiles) fail(`dirty release artifact present: ${item}`);
  if (!rootDirtyFiles.length) pass('release root has no test/local diagnostic artifacts');

  const resourceRoot = path.join(releaseDir, 'win-unpacked', 'resources');
  if (fs.existsSync(resourceRoot)) {
    const resourceDirtyFiles = walkFiles(resourceRoot)
      .filter((filePath) => isDirtyPath(path.relative(resourceRoot, filePath)))
      .map(rel);
    for (const item of resourceDirtyFiles) fail(`dirty packaged resource present: ${item}`);
    if (!resourceDirtyFiles.length) pass('win-unpacked resources have no local state files');

    const asarPath = path.join(resourceRoot, 'app.asar');
    if (fs.existsSync(asarPath)) {
      if (!asar?.listPackage) {
        fail('cannot inspect app.asar because @electron/asar is unavailable');
      } else {
        const asarDirtyFiles = asar.listPackage(asarPath)
          .map((item) => String(item || '').replace(/^\/+/, ''))
          .filter(isDirtyPath);
        for (const item of asarDirtyFiles) fail(`dirty app.asar entry present: ${item}`);
        if (!asarDirtyFiles.length) pass('app.asar has no local state files');
      }
    }
  }
}

verifyBuilderDenylist();
verifyReleaseDirectory();

console.log('\nrelease cleanliness verification');
for (const item of passes) console.log(`  ok   ${item}`);
for (const item of failures) console.log(`  fail ${item}`);

if (failures.length) process.exitCode = 1;

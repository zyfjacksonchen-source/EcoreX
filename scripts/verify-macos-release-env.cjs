#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const failures = [];
const warnings = [];
const passes = [];

function rel(...segments) {
  return path.join(rootDir, ...segments);
}

function pass(message) {
  passes.push(message);
}

function warn(message) {
  warnings.push(message);
}

function fail(message) {
  failures.push(message);
}

function exists(relativePath) {
  return fs.existsSync(rel(relativePath));
}

function checkCommand(command, args = ['--version']) {
  const result = spawnSync(command, args, { encoding: 'utf8', windowsHide: true });
  return result.status === 0 ? String(result.stdout || result.stderr || '').trim() : '';
}

function packageJson() {
  return JSON.parse(fs.readFileSync(rel('package.json'), 'utf8'));
}

function validatePackageConfig() {
  const pkg = packageJson();
  const build = pkg.build || {};
  const mac = build.mac || {};
  if (!mac.target) fail('package.json build.mac.target is missing.');
  else pass('mac dmg/zip targets configured.');
  if (mac.hardenedRuntime !== true) fail('build.mac.hardenedRuntime must be true.');
  else pass('hardened runtime enabled for macOS.');
  for (const file of ['build/entitlements.mac.plist', 'build/entitlements.mac.inherit.plist']) {
    if (!exists(file)) fail(`${file} is missing.`);
    else pass(`${file} exists.`);
  }
  if (!String(pkg.scripts?.['dist:mac'] || '').includes('electron-builder --mac')) {
    fail('dist:mac script must call electron-builder --mac.');
  } else {
    pass('dist:mac script configured.');
  }
  const files = Array.isArray(build.files) ? build.files : [];
  for (const pattern of [
    'node_modules/@anthropic-ai/claude-code-darwin-arm64/**/*',
    'node_modules/@anthropic-ai/claude-code-darwin-x64/**/*'
  ]) {
    if (!files.includes(pattern)) fail(`build.files missing ${pattern}.`);
    else pass(`build.files includes ${pattern}.`);
  }
}

function validateHostEnvironment() {
  if (process.platform !== 'darwin') {
    warn('macOS installer creation, codesign, notarization, Gatekeeper, and Rosetta checks must run on a macOS host.');
    return;
  }

  for (const [command, args, label] of [
    ['xcrun', ['notarytool', '--help'], 'Apple notarytool'],
    ['codesign', ['--version'], 'codesign'],
    ['spctl', ['--version'], 'Gatekeeper assessment tool'],
    ['hdiutil', ['help'], 'DMG tooling'],
    ['iconutil', ['--help'], 'iconutil'],
    ['sips', ['--help'], 'sips']
  ]) {
    const output = checkCommand(command, args);
    if (output) pass(`${label} available.`);
    else fail(`${label} is unavailable on this host.`);
  }

  if (!exists('build/icon.icns')) fail('build/icon.icns is missing. Run npm run assets:mac-icon on macOS.');
  else pass('build/icon.icns exists.');

  const signingIdentity = checkCommand('security', ['find-identity', '-v', '-p', 'codesigning']);
  if (/Developer ID Application/.test(signingIdentity)) pass('Developer ID Application certificate is visible in keychain.');
  else warn('Developer ID Application certificate was not detected; unsigned local mac builds may run, but public distribution needs signing/notarization.');
}

function validateArtifacts() {
  const releaseDir = rel('release');
  if (!fs.existsSync(releaseDir)) {
    warn('release/ is missing; mac artifact checks skipped.');
    return;
  }
  const names = fs.readdirSync(releaseDir);
  const macArtifacts = names.filter((name) => /\.(dmg|zip)$/i.test(name));
  if (macArtifacts.length) pass(`mac release artifacts found: ${macArtifacts.join(', ')}`);
  else warn('No .dmg or .zip mac release artifacts found yet.');
}

validatePackageConfig();
validateHostEnvironment();
validateArtifacts();

console.log('\nmacOS release environment verification');
for (const item of passes) console.log(`  ok   ${item}`);
for (const item of warnings) console.log(`  warn ${item}`);
for (const item of failures) console.log(`  fail ${item}`);

if (failures.length) process.exitCode = 1;

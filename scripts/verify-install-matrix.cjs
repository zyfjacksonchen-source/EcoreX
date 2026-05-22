#!/usr/bin/env node

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const reportPath = path.join(releaseDir, 'install-matrix-report.json');

const platforms = [
  {
    id: 'win10-x64',
    name: 'Windows 10 x64',
    artifact: /^EcoreX Agent Setup .+\.exe$/i,
    checks: ['Defender/SmartScreen download', 'first launch', 'local owner binding', 'model connection test', 'project create/switch', 'uninstall', 'reinstall keeps no stale crash loop']
  },
  {
    id: 'win11-x64',
    name: 'Windows 11 x64',
    artifact: /^EcoreX Agent Setup .+\.exe$/i,
    checks: ['Defender/SmartScreen download', 'first launch', 'full access confirmation', 'diagnostics export', 'concurrent task cancel', 'uninstall', 'reinstall']
  },
  {
    id: 'macos-arm64',
    name: 'macOS Apple Silicon',
    artifact: /arm64\.(dmg|zip)$/i,
    checks: ['Gatekeeper open', 'first launch', 'keychain safeStorage', 'model connection test', 'project memory isolation', 'move to Applications', 'uninstall from Applications']
  },
  {
    id: 'macos-x64',
    name: 'macOS Intel',
    artifact: /x64\.(dmg|zip)$/i,
    checks: ['Gatekeeper open', 'first launch', 'keychain safeStorage', 'model connection test', 'project memory isolation', 'move to Applications', 'uninstall from Applications']
  }
];

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function fileSummary(file) {
  const stat = fs.statSync(file);
  return {
    name: path.basename(file),
    size: stat.size,
    sha256: sha256(file),
    updatedAt: stat.mtime.toISOString()
  };
}

function main() {
  fs.mkdirSync(releaseDir, { recursive: true });
  const artifacts = fs
    .readdirSync(releaseDir, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => path.join(releaseDir, entry.name))
    .filter((file) => /\.(exe|dmg|zip|pkg|msi)$/i.test(file))
    .map(fileSummary);

  const rows = platforms.map((platform) => {
    const matching = artifacts.filter((artifact) => platform.artifact.test(artifact.name));
    return {
      id: platform.id,
      name: platform.name,
      artifactReady: matching.length > 0,
      artifacts: matching,
      manualChecks: platform.checks,
      status: matching.length ? 'ready-for-clean-machine-test' : 'artifact-missing'
    };
  });

  const report = {
    schema: 'ecorex.install-matrix.v1',
    generatedAt: new Date().toISOString(),
    releaseDir,
    artifacts,
    platforms: rows,
    requiredManualEvidence: [
      'OS build and architecture screenshot',
      'installer hash before install',
      'first launch screenshot',
      'model connection result',
      'project create/switch result',
      'diagnostics export after run',
      'uninstall/reinstall result',
      'antivirus or Gatekeeper verdict'
    ]
  };

  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8');

  console.log(`install matrix report written: ${path.relative(rootDir, reportPath)}`);
  for (const row of rows) {
    console.log(`${row.artifactReady ? 'ok  ' : 'warn'} ${row.name}: ${row.status}`);
  }
}

main();

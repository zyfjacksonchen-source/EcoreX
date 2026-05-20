const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

function nativePackageName() {
  if (process.platform === 'win32' && process.arch === 'x64') return 'claude-code-win32-x64';
  if (process.platform === 'darwin' && process.arch === 'arm64') return 'claude-code-darwin-arm64';
  if (process.platform === 'darwin' && process.arch === 'x64') return 'claude-code-darwin-x64';
  if (process.platform === 'linux' && process.arch === 'x64') return 'claude-code-linux-x64';
  if (process.platform === 'linux' && process.arch === 'arm64') return 'claude-code-linux-arm64';
  return null;
}

const packageName = nativePackageName();
if (!packageName) {
  console.error(`Unsupported platform for bundled Claude Code native binary: ${process.platform}/${process.arch}`);
  process.exit(1);
}

const exeName = process.platform === 'win32' ? 'claude.exe' : 'claude';
const candidates = [
  path.join(__dirname, '..', 'node_modules', '@anthropic-ai', packageName, exeName),
  path.join(
    __dirname,
    '..',
    'node_modules',
    '@anthropic-ai',
    'claude-code',
    'node_modules',
    '@anthropic-ai',
    packageName,
    exeName
  )
];

const binary = candidates.find((candidate) => fs.existsSync(candidate));
if (!binary) {
  console.error(`Missing Claude Code native binary. Expected one of:\n${candidates.join('\n')}`);
  process.exit(1);
}

const result = spawnSync(binary, ['--version'], { encoding: 'utf8', windowsHide: true });
if (result.status !== 0) {
  console.error(`Claude Code native binary failed: ${result.stderr || result.stdout || result.error?.message}`);
  process.exit(result.status || 1);
}

console.log(`Verified Claude Code native binary: ${result.stdout.trim()}`);

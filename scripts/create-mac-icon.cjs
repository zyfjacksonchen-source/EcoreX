#!/usr/bin/env node

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const sourcePng = path.join(rootDir, 'build', 'icon.png');
const iconsetDir = path.join(rootDir, 'build', 'icon.iconset');
const outputIcns = path.join(rootDir, 'build', 'icon.icns');

function run(command, args) {
  const result = spawnSync(command, args, {
    cwd: rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed: ${result.stderr || result.stdout || result.error?.message || ''}`);
  }
}

function main() {
  if (process.platform !== 'darwin') {
    if (fs.existsSync(outputIcns)) {
      console.log('mac icon already exists; generation skipped on non-macOS host.');
      return;
    }
    console.log('mac icon generation skipped: run npm run assets:mac-icon on macOS before npm run dist:mac.');
    return;
  }

  if (!fs.existsSync(sourcePng)) throw new Error('build/icon.png is missing.');
  fs.rmSync(iconsetDir, { recursive: true, force: true });
  fs.mkdirSync(iconsetDir, { recursive: true });

  const sizes = [16, 32, 64, 128, 256, 512];
  for (const size of sizes) {
    run('sips', ['-z', String(size), String(size), sourcePng, '--out', path.join(iconsetDir, `icon_${size}x${size}.png`)]);
    run('sips', ['-z', String(size * 2), String(size * 2), sourcePng, '--out', path.join(iconsetDir, `icon_${size}x${size}@2x.png`)]);
  }
  run('iconutil', ['-c', 'icns', iconsetDir, '-o', outputIcns]);
  fs.rmSync(iconsetDir, { recursive: true, force: true });
  console.log(`mac icon generated: ${path.relative(rootDir, outputIcns)}`);
}

try {
  main();
} catch (error) {
  console.error(error.message || error);
  process.exitCode = 1;
}

const { spawnSync } = require('child_process');
const path = require('path');

const electronPath = require('electron');
const script = process.argv[2];

if (!script) {
  console.error('Usage: node scripts/run-electron-script.cjs <script> [...args]');
  process.exit(1);
}

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const result = spawnSync(electronPath, [path.resolve(script), ...process.argv.slice(3)], {
  cwd: process.cwd(),
  env,
  stdio: 'inherit'
});

if (result.error) {
  console.error(result.error.message || String(result.error));
  process.exit(1);
}

process.exit(typeof result.status === 'number' ? result.status : 1);

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const latestPath = path.join(releaseDir, 'latest.yml');
const packageJson = JSON.parse(fs.readFileSync(path.join(rootDir, 'package.json'), 'utf8'));

function sha512Base64(file) {
  return crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64');
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function replaceFirst(text, pattern, replacement, label) {
  if (!pattern.test(text)) throw new Error(`release metadata fix failed: ${label} not found in latest.yml`);
  return text.replace(pattern, replacement);
}

function main() {
  if (!fs.existsSync(releaseDir) || !fs.existsSync(latestPath)) {
    console.log('release metadata fix skipped: release/latest.yml not found');
    return;
  }

  const installers = fs
    .readdirSync(releaseDir)
    .filter((name) => new RegExp(`^EcoreX Agent Setup ${escapeRegExp(packageJson.version)}\\.exe$`, 'i').test(name))
    .map((name) => {
      const file = path.join(releaseDir, name);
      return { name, file, stat: fs.statSync(file) };
    })
    .sort((left, right) => right.stat.mtimeMs - left.stat.mtimeMs || right.stat.size - left.stat.size);

  const installer = installers[0];
  if (!installer) {
    console.log('release metadata fix skipped: installer not found');
    return;
  }

  let latest = fs.readFileSync(latestPath, 'utf8');
  const sha512 = sha512Base64(installer.file);
  const blockmapPath = path.join(releaseDir, `${installer.name}.blockmap`);
  if (!fs.existsSync(blockmapPath) || fs.statSync(blockmapPath).size <= 0) {
    throw new Error(`release metadata fix failed: ${path.basename(blockmapPath)} is missing or empty`);
  }
  latest = replaceFirst(latest, /^(version:\s*).+$/m, `$1${packageJson.version}`, 'version');
  latest = replaceFirst(latest, /^(\s*-\s*url:\s*).+$/m, `$1${installer.name}`, 'files[0].url');
  latest = replaceFirst(latest, /^(\s{4}sha512:\s*).+$/m, `$1${sha512}`, 'files[0].sha512');
  latest = replaceFirst(latest, /^(\s*size:\s*)\d+$/m, `$1${installer.stat.size}`, 'files[0].size');
  latest = replaceFirst(latest, /^(\s*path:\s*).+$/m, `$1${installer.name}`, 'path');
  latest = replaceFirst(latest, /^(sha512:\s*).+$/m, `$1${sha512}`, 'sha512');

  fs.writeFileSync(latestPath, latest, 'utf8');
  console.log(`release metadata points to ${installer.name} (${installer.stat.size} bytes)`);
}

main();

#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');

function ensureInsideRoot(target) {
  const resolved = path.resolve(target);
  if (resolved !== rootDir && !resolved.startsWith(`${rootDir}${path.sep}`)) {
    throw new Error(`Refusing to clean outside the repository: ${resolved}`);
  }
  return resolved;
}

function removeTarget(target) {
  const resolved = ensureInsideRoot(target);
  if (!fs.existsSync(resolved)) return false;
  fs.rmSync(resolved, { recursive: true, force: true });
  return true;
}

function removeRootMatches(pattern) {
  const removed = [];
  if (!fs.existsSync(rootDir)) return removed;
  for (const name of fs.readdirSync(rootDir)) {
    if (!pattern.test(name)) continue;
    const target = path.join(rootDir, name);
    if (removeTarget(target)) removed.push(path.relative(rootDir, target));
  }
  return removed;
}

const removed = [
  ...['release', 'test-results', 'playwright-report', path.join('reports', 'qa')]
    .filter((relativePath) => removeTarget(path.join(rootDir, relativePath))),
  ...removeRootMatches(/^index-[A-Za-z0-9_-]+\.(?:js|css)$/),
  ...removeRootMatches(/^main\.cjs$/)
];

if (removed.length) {
  console.log(`cleaned release/local artifacts:\n- ${removed.join('\n- ')}`);
} else {
  console.log('release/local artifacts already clean');
}

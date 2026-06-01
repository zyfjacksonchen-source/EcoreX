#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const targets = [
  path.join(root, 'electron'),
  path.join(root, 'scripts')
];

function listCjsFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name.startsWith('.')) return [];
      return listCjsFiles(fullPath);
    }
    return entry.isFile() && entry.name.endsWith('.cjs') ? [fullPath] : [];
  });
}

const files = targets.flatMap(listCjsFiles).sort();
if (!files.length) {
  console.log('No CommonJS files found for syntax lint.');
  process.exit(0);
}

let failed = false;
for (const file of files) {
  const result = spawnSync(process.execPath, ['--check', file], {
    cwd: root,
    stdio: 'inherit',
    windowsHide: true
  });
  if (result.status !== 0) failed = true;
}

if (failed) {
  process.exit(1);
}

console.log(`Syntax lint passed for ${files.length} CommonJS files.`);

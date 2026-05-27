const assert = require('assert');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const mainPath = path.join(repoRoot, 'electron', 'main.cjs');
const source = fs.readFileSync(mainPath, 'utf8');

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function snippetFrom(needle) {
  const start = source.indexOf(needle);
  assert.notStrictEqual(start, -1, `Missing snippet: ${needle}`);
  const next = source.indexOf('\nhandleSafe(', start + needle.length);
  return source.slice(start, next === -1 ? source.length : next);
}

function functionSnippet(name) {
  const start = source.indexOf(`function ${name}`);
  assert.notStrictEqual(start, -1, `Missing function ${name}`);
  const signatureEnd = source.indexOf(') {', start);
  assert.notStrictEqual(signatureEnd, -1, `Could not parse signature for ${name}`);
  const open = source.indexOf('{', signatureEnd);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Could not parse function ${name}`);
}

function assertHandle(channel, { authRequired = true, requiredPermission = null } = {}) {
  const snippet = snippetFrom(`handleSafe('${channel}'`);
  if (authRequired) {
    assert.match(snippet, /authRequired:\s*true/, `${channel} must require auth`);
  }
  if (requiredPermission) {
    assert.match(
      snippet,
      new RegExp(`requiredPermission:\\s*['"]${escapeRegExp(requiredPermission)}['"]`),
      `${channel} must require ${requiredPermission}`
    );
  }
}

function rolePermissions(role) {
  const match = source.match(new RegExp(`\\n\\s*${escapeRegExp(role)}:\\s*\\[([^\\]]*)\\]`));
  assert(match, `Missing role ${role}`);
  return match[1];
}

const superAdminPermissions = rolePermissions('super_admin');
const adminPermissions = rolePermissions('admin');
const userPermissions = rolePermissions('user');

for (const permission of ['settings:manage', 'secrets:manage', 'models:manage', 'mcp:manage', 'skills:manage']) {
  assert.match(superAdminPermissions, new RegExp(`['"]${escapeRegExp(permission)}['"]`), `super_admin missing ${permission}`);
  assert.match(adminPermissions, new RegExp(`['"]${escapeRegExp(permission)}['"]`), `admin missing ${permission}`);
}
assert.doesNotMatch(
  userPermissions,
  /settings:manage|secrets:manage|models:manage|mcp:manage|skills:manage/,
  'base user role must not get management permissions'
);

[
  'auth:logout',
  'auth:users:list',
  'auth:user:update',
  'secrets:status',
  'secrets:list',
  'listModelProfiles',
  'testModelProfile',
  'modelAdapter:testProfile',
  'modelAdapter:generateImage',
  'settings:get',
  'mcp:list',
  'mcp:status',
  'mcp:refresh',
  'mcp:get',
  'skill:list',
  'skill:status',
  'skill:refresh'
].forEach((channel) => assertHandle(channel));

[
  ['auth:user:create', 'users:manage'],
  ['auth:user:delete', 'users:manage'],
  ['auth:profile:update', 'profile:update'],
  ['enterprise:action', 'enterprise:manage'],
  ['secrets:set', 'secrets:manage'],
  ['secrets:delete', 'secrets:manage'],
  ['saveModelProfile', 'models:manage'],
  ['deleteModelProfile', 'models:manage'],
  ['activateModelProfile', 'models:manage'],
  ['settings:update', 'settings:manage'],
  ['mcp:update', 'mcp:manage'],
  ['mcp:update-config', 'mcp:manage'],
  ['mcp:enable', 'mcp:manage'],
  ['mcp:disable', 'mcp:manage'],
  ['skill:install', 'skills:manage'],
  ['skill:enable', 'skills:manage'],
  ['skill:disable', 'skills:manage'],
  ['skill:update', 'skills:manage']
].forEach(([channel, requiredPermission]) => assertHandle(channel, { requiredPermission }));

const readAuthUsers = functionSnippet('readAuthUsers');
assert.match(readAuthUsers, /if \(legacyIdentityExists\) throw new Error\('Legacy auth identity could not be read\.'\);/);
assert.match(readAuthUsers, /if \(!rawUsers\) throw new Error\('Invalid auth users file\.'\);/);
assert.match(readAuthUsers, /if \(!users\.length\) throw new Error\('Auth users file contains no valid users\.'\);/);
assert.match(readAuthUsers, /throw authUsersUnavailableError\(error\);/);

const publicAuthSession = functionSnippet('publicAuthSession');
assert.match(publicAuthSession, /return publicAuthUsersUnavailableSession\(options\);/);

console.log('main IPC security assertions passed');

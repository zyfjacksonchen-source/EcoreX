const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const electron = require('electron');

const { app, safeStorage } = electron;
const LOCAL_AUTH_HASH_ITERATIONS = 210_000;
const DEFAULT_OUTPUT = path.join(__dirname, '..', 'build', 'provisioning', 'ecorex-provisioning.json');

function fail(message) {
  console.error(`[provisioning] ${message}`);
  process.exitCode = 1;
  if (app?.quit) app.quit();
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function decryptSafeStorageValue(value) {
  if (!value || typeof value !== 'string') return '';
  return safeStorage.decryptString(Buffer.from(value, 'base64'));
}

function decryptEnvelope(raw) {
  if (raw?.encoding === 'safeStorage/v1') return JSON.parse(decryptSafeStorageValue(raw.data));
  return raw;
}

function normalizeEmail(value) {
  const email = String(value || '').trim().toLowerCase();
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error('Invalid email.');
  return email;
}

function stableUserId(email = '') {
  return crypto.createHash('sha256').update(`ecorex-user/v1:${String(email || '').toLowerCase()}`).digest('hex').slice(0, 24);
}

function hashPassword(password, salt, iterations = LOCAL_AUTH_HASH_ITERATIONS) {
  return crypto.pbkdf2Sync(password, salt, iterations, 32, 'sha256').toString('hex');
}

function fallbackOwnerFromEnv() {
  const email = process.env.ECOREX_PROVISION_OWNER_EMAIL || '';
  const password = process.env.ECOREX_PROVISION_OWNER_PASSWORD || '';
  if (!email || !password) return null;
  if (password.length < 8) throw new Error('ECOREX_PROVISION_OWNER_PASSWORD must be at least 8 characters.');
  const normalizedEmail = normalizeEmail(email);
  const salt = crypto.randomBytes(16).toString('hex');
  const now = new Date().toISOString();
  return {
    id: stableUserId(normalizedEmail),
    email: normalizedEmail,
    displayName: process.env.ECOREX_PROVISION_OWNER_NAME || normalizedEmail.split('@')[0],
    title: 'Local super administrator',
    team: 'EcoreX',
    role: 'super_admin',
    active: true,
    salt,
    passwordHash: hashPassword(password, salt),
    iterations: LOCAL_AUTH_HASH_ITERATIONS,
    digest: 'sha256',
    createdAt: now,
    updatedAt: now
  };
}

function readProvisionedUsers(sourceUserData) {
  const usersFile = path.join(sourceUserData, 'auth-users.json');
  if (fs.existsSync(usersFile)) {
    const raw = decryptEnvelope(readJson(usersFile));
    const users = Array.isArray(raw?.users) ? raw.users : Array.isArray(raw) ? raw : [];
    return users.map((user) => ({
      id: String(user.id || stableUserId(user.email || '')).slice(0, 80),
      email: normalizeEmail(user.email),
      displayName: String(user.displayName || user.name || String(user.email || '').split('@')[0] || '').slice(0, 80),
      title: String(user.title || '').slice(0, 120),
      team: String(user.team || '').slice(0, 120),
      avatarInitials: String(user.avatarInitials || '').slice(0, 4),
      role: String(user.role || 'user').trim().toLowerCase().replace(/[-\s]+/g, '_'),
      active: user.active !== false,
      passwordHash: String(user.passwordHash || ''),
      salt: String(user.salt || ''),
      iterations: Number(user.iterations) || LOCAL_AUTH_HASH_ITERATIONS,
      digest: user.digest || 'sha256',
      createdAt: user.createdAt || new Date().toISOString(),
      updatedAt: user.updatedAt || user.createdAt || new Date().toISOString()
    })).filter((user) => /^[a-f0-9]{64}$/i.test(user.passwordHash) && /^[a-f0-9]{32,}$/i.test(user.salt));
  }
  const fallback = fallbackOwnerFromEnv();
  return fallback ? [fallback] : [];
}

function readProvisionedModelProfile(sourceUserData) {
  const profilesFile = path.join(sourceUserData, 'model-profiles.json');
  if (!fs.existsSync(profilesFile)) return null;
  const raw = readJson(profilesFile);
  const profiles = Array.isArray(raw?.profiles) ? raw.profiles : [];
  const active = profiles.find((profile) => profile.name === raw.activeProfileName)
    || profiles.find((profile) => profile.isActive)
    || profiles[0]
    || null;
  if (!active) return null;
  const apiKey = active.apiKey?.encoding === 'safeStorage/v1'
    ? decryptSafeStorageValue(active.apiKey.data)
    : String(active.apiKey || '');
  return {
    name: active.name || active.label || active.model || 'EcoreX Default',
    label: active.label || active.name || 'EcoreX Default',
    baseUrl: active.baseUrl || '',
    apiKey,
    model: active.model || 'gpt-5.5',
    imageModel: active.imageModel || 'gpt-image-2',
    isActive: true
  };
}

async function main() {
  if (!app || !safeStorage) {
    console.error('[provisioning] Run this script with Electron, not plain Node.');
    process.exitCode = 1;
    return;
  }
  const sourceUserData = process.env.ECOREX_SOURCE_USER_DATA
    || path.join(process.env.APPDATA || '', 'ecorex-agent');
  app.setPath('userData', sourceUserData);
  await app.whenReady();
  if (!safeStorage.isEncryptionAvailable()) {
    fail('safeStorage is unavailable; cannot export local encrypted state.');
    return;
  }

  const outputFile = process.env.ECOREX_PROVISIONING_OUTPUT || DEFAULT_OUTPUT;
  const users = readProvisionedUsers(sourceUserData);
  if (!users.some((user) => user.active !== false && user.role === 'super_admin')) {
    fail('No active super administrator found. Set ECOREX_PROVISION_OWNER_EMAIL and ECOREX_PROVISION_OWNER_PASSWORD or create one in EcoreX first.');
    return;
  }

  const modelProfile = readProvisionedModelProfile(sourceUserData);
  const payload = {
    version: 1,
    generatedAt: new Date().toISOString(),
    auth: {
      mode: 'managed-local',
      users
    },
    modelProfile
  };
  fs.mkdirSync(path.dirname(outputFile), { recursive: true });
  fs.writeFileSync(outputFile, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  console.log(JSON.stringify({
    ok: true,
    output: outputFile,
    users: users.length,
    ownerEmail: users.find((user) => user.role === 'super_admin')?.email || '',
    modelProfile: modelProfile?.name || '',
    model: modelProfile?.model || '',
    imageModel: modelProfile?.imageModel || '',
    apiKeyConfigured: Boolean(modelProfile?.apiKey)
  }, null, 2));
  app.quit();
}

main().catch((error) => fail(error?.message || String(error)));

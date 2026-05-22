#!/usr/bin/env node

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const releaseDir = path.join(rootDir, 'release');
const policyPath = path.join(releaseDir, 'release-policy.json');
const pkg = JSON.parse(fs.readFileSync(path.join(rootDir, 'package.json'), 'utf8'));

function sha512Base64(file) {
  return crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64');
}

function artifactSummary(name) {
  const file = path.join(releaseDir, name);
  if (!fs.existsSync(file)) return null;
  const stat = fs.statSync(file);
  return {
    name,
    size: stat.size,
    sha512: sha512Base64(file),
    updatedAt: stat.mtime.toISOString()
  };
}

function envInt(name, fallback, min, max) {
  const value = Number(process.env[name]);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(Math.max(Math.trunc(value), min), max);
}

function main() {
  fs.mkdirSync(releaseDir, { recursive: true });
  const names = fs.readdirSync(releaseDir).filter((name) => /\.(exe|dmg|zip)$/i.test(name)).sort();
  const artifacts = names.map(artifactSummary).filter(Boolean);
  const channel = String(process.env.ECOREX_RELEASE_CHANNEL || 'stable').trim().toLowerCase();
  const stagedPercentage = envInt('ECOREX_STAGED_ROLLOUT_PERCENT', channel === 'stable' ? 10 : 100, 0, 100);
  const rollbackToVersion = String(process.env.ECOREX_ROLLBACK_TO_VERSION || '').trim();

  const policy = {
    schema: 'ecorex.release-policy.v1',
    generatedAt: new Date().toISOString(),
    appId: pkg.build?.appId || 'com.ecorex.agent',
    productName: pkg.build?.productName || 'EcoreX Agent',
    version: pkg.version,
    channel,
    stagedRollout: {
      enabled: stagedPercentage < 100,
      percentage: stagedPercentage,
      cohortKey: 'anonymousInstallId'
    },
    rollback: {
      enabled: Boolean(rollbackToVersion),
      toVersion: rollbackToVersion || null,
      reason: process.env.ECOREX_ROLLBACK_REASON || ''
    },
    updateFeed: {
      provider: process.env.ECOREX_UPDATE_PROVIDER || 'manual',
      url: process.env.ECOREX_UPDATE_FEED_URL || '',
      requiresSignedArtifacts: true,
      macRequiresNotarization: true
    },
    artifacts
  };

  fs.writeFileSync(policyPath, `${JSON.stringify(policy, null, 2)}\n`, 'utf8');
  console.log(`release policy written: ${path.relative(rootDir, policyPath)}`);
  console.log(`channel=${policy.channel} staged=${policy.stagedRollout.percentage}% artifacts=${artifacts.length}`);
}

main();

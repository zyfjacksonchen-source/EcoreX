#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const buildRoot = path.join(rootDir, 'build');
const managedSkillRoot = path.join(buildRoot, 'managed-skill-packs');
const managedToolRoot = path.join(buildRoot, 'managed-tools');
const maxCopyBytes = 80 * 1024 * 1024;

function fail(message) {
  console.error(`prepare-managed-skills failed: ${message}`);
  process.exit(1);
}

function assertInside(base, target) {
  const relative = path.relative(path.resolve(base), path.resolve(target));
  if (relative === '' || (relative && !relative.startsWith('..') && !path.isAbsolute(relative))) return;
  fail(`Refusing to write outside ${base}: ${target}`);
}

function readJson(file, fallback = {}) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function parseSkillFrontmatter(file) {
  try {
    const text = fs.readFileSync(file, 'utf8');
    const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if (!match) return {};
    const meta = {};
    for (const line of match[1].split(/\r?\n/)) {
      const simple = line.match(/^([A-Za-z0-9_.-]+):\s*(.+)$/);
      if (simple) meta[simple[1]] = simple[2].replace(/^['"]|['"]$/g, '').trim();
      const nestedVersion = line.match(/^\s+version:\s*(.+)$/);
      if (nestedVersion && !meta.version) meta.version = nestedVersion[1].replace(/^['"]|['"]$/g, '').trim();
    }
    return meta;
  } catch {
    return {};
  }
}

function shouldSkip(relativePath) {
  const normalized = String(relativePath || '').replace(/\\/g, '/');
  return /(^|\/)(\.git|node_modules|__pycache__|\.venv|venv|dist|build|release|test-results)(\/|$)/i.test(normalized)
    || /(^|\/)(\.env|\.env\..*|secrets\.json|auth-session\.json|auth-users\.json|settings\.json)$/i.test(normalized);
}

function copyDirectory(sourceDir, targetDir, label, state = { bytes: 0, files: 0 }) {
  for (const entry of fs.readdirSync(sourceDir, { withFileTypes: true })) {
    const source = path.join(sourceDir, entry.name);
    const relative = path.relative(sourceDir, source);
    if (shouldSkip(relative)) continue;
    const target = path.join(targetDir, entry.name);
    assertInside(targetDir, target);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      fs.mkdirSync(target, { recursive: true });
      copyDirectory(source, target, label, state);
      continue;
    }
    if (!entry.isFile()) continue;
    const stat = fs.statSync(source);
    state.bytes += stat.size;
    state.files += 1;
    if (state.bytes > maxCopyBytes) fail(`${label} resource copy is unexpectedly large.`);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(source, target);
  }
  return state;
}

function cleanGeneratedDir(target) {
  assertInside(buildRoot, target);
  fs.rmSync(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  fs.mkdirSync(target, { recursive: true });
}

function writeManifest(target, manifest) {
  fs.mkdirSync(path.join(target, '.claude-plugin'), { recursive: true });
  fs.writeFileSync(
    path.join(target, '.claude-plugin', 'plugin.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8'
  );
}

function verifyCli(exePath) {
  const result = spawnSync(exePath, ['--version'], {
    cwd: rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.status !== 0) {
    fail(`lark-cli executable check failed: ${result.stderr || result.stdout || result.error?.message || ''}`);
  }
  return String(result.stdout || result.stderr || '').trim();
}

function prepareFeishuSkill() {
  const defaultSource = process.platform === 'win32' ? 'C:\\cli-main' : '';
  const sourceRoot = path.resolve(process.env.ECOREX_LARK_CLI_SOURCE || defaultSource || '');
  if (!sourceRoot || !fs.existsSync(sourceRoot)) {
    fail(`Feishu source directory is missing. Set ECOREX_LARK_CLI_SOURCE or create ${defaultSource}.`);
  }

  const sourceSkills = path.join(sourceRoot, 'skills');
  const sourceExe = path.join(sourceRoot, 'bin', process.platform === 'win32' ? 'lark-cli.exe' : 'lark-cli');
  if (!fs.existsSync(path.join(sourceSkills, 'lark-shared', 'SKILL.md'))) {
    fail(`Feishu skill docs are missing under ${sourceSkills}.`);
  }
  if (!fs.existsSync(sourceExe)) fail(`lark-cli executable is missing: ${sourceExe}`);

  const packageInfo = readJson(path.join(sourceRoot, 'package.json'), {});
  const version = String(packageInfo.version || '1.0.40');
  const skillTarget = path.join(managedSkillRoot, 'lark-cli');
  const toolTarget = path.join(managedToolRoot, 'lark-cli');

  cleanGeneratedDir(skillTarget);
  writeManifest(skillTarget, {
    name: 'lark-cli',
    displayName: 'lark-cli',
    description: 'Feishu/Lark CLI skill collection managed by EcoreX Agent. Users authorize their own Feishu account before personal-resource access.',
    version,
    skills: './skills'
  });
  const copiedSkills = copyDirectory(sourceSkills, path.join(skillTarget, 'skills'), 'Feishu Skill');

  cleanGeneratedDir(toolTarget);
  const targetExe = path.join(toolTarget, path.basename(sourceExe));
  fs.copyFileSync(sourceExe, targetExe);
  fs.writeFileSync(
    path.join(toolTarget, 'manifest.json'),
    `${JSON.stringify({
      name: 'lark-cli',
      version,
      platform: process.platform,
      executable: path.basename(sourceExe),
      source: 'EcoreX managed Feishu CLI runtime'
    }, null, 2)}\n`,
    'utf8'
  );

  const versionText = verifyCli(targetExe);
  console.log(`Prepared managed Feishu Skill ${version} from ${sourceRoot}`);
  console.log(`  skills: ${copiedSkills.files} files, ${copiedSkills.bytes} bytes`);
  console.log(`  runtime: ${path.relative(rootDir, targetExe)} (${versionText})`);
}

function prepareAgentSkillCreator() {
  const defaultSource = process.platform === 'win32' ? 'C:\\agent-skill-creator-main' : '';
  const sourceRoot = path.resolve(process.env.ECOREX_AGENT_SKILL_CREATOR_SOURCE || defaultSource || '');
  if (!sourceRoot || !fs.existsSync(sourceRoot)) {
    fail(`Agent Skill Creator source directory is missing. Set ECOREX_AGENT_SKILL_CREATOR_SOURCE or create ${defaultSource}.`);
  }
  const sourceSkill = path.join(sourceRoot, 'SKILL.md');
  if (!fs.existsSync(sourceSkill)) fail(`Agent Skill Creator SKILL.md is missing: ${sourceSkill}`);

  const meta = parseSkillFrontmatter(sourceSkill);
  const name = 'agent-skill-creator';
  const version = String(meta.version || '4.0.0');
  const skillTarget = path.join(managedSkillRoot, name);
  const skillDir = path.join(skillTarget, 'skills', name);

  cleanGeneratedDir(skillTarget);
  writeManifest(skillTarget, {
    name,
    displayName: 'Agent Skill Creator',
    description: 'Create, validate, export, and install EcoreX managed skills from workflow descriptions.',
    version,
    skills: './skills'
  });
  fs.mkdirSync(skillDir, { recursive: true });
  const copied = copyDirectory(sourceRoot, skillDir, 'Agent Skill Creator');
  console.log(`Prepared managed Agent Skill Creator ${version} from ${sourceRoot}`);
  console.log(`  skill: ${copied.files} files, ${copied.bytes} bytes`);
}

prepareFeishuSkill();
prepareAgentSkillCreator();

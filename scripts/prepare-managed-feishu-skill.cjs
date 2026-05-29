#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const buildRoot = path.join(rootDir, 'build');
const managedSkillRoot = path.join(buildRoot, 'managed-skill-packs');
const managedToolRoot = path.join(buildRoot, 'managed-tools');
const maxCopyBytes = 80 * 1024 * 1024;
const retiredManagedNames = ['ppt-master', 'excel-mcp-server'];

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
    || /(^|\/)(\.lark|\.feishu|\.larksuite)(\/|$)/i.test(normalized)
    || /(^|\/)(\.env|\.env\..*|secrets\.json|auth-session\.json|auth-identity\.json|auth-users\.json|enterprise-admin-journal\.jsonl|session-bindings\.json|model-profiles\.json|settings\.json)$/i.test(normalized)
    || /(^|\/).*\.log$/i.test(normalized);
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

function copyRequiredFile(source, target, label) {
  if (!fs.existsSync(source) || !fs.statSync(source).isFile()) fail(`${label} is missing: ${source}`);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

function cleanGeneratedDir(target) {
  assertInside(buildRoot, target);
  fs.rmSync(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  fs.mkdirSync(target, { recursive: true });
}

function removeGeneratedDir(target) {
  assertInside(buildRoot, target);
  fs.rmSync(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
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

function commandName(name) {
  return process.platform === 'win32' ? `${name}.cmd` : name;
}

function verifyCommand(command, args, label, options = {}) {
  const isWindowsCmd = process.platform === 'win32' && /\.cmd$/i.test(command);
  const result = spawnSync(isWindowsCmd ? 'cmd.exe' : command, isWindowsCmd ? ['/d', '/c', command, ...args] : args, {
    cwd: options.cwd || rootDir,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.status !== 0) {
    fail(`${label} check failed: ${result.stderr || result.stdout || result.error?.message || ''}`);
  }
  return String(result.stdout || result.stderr || '').trim();
}

function patchFeishuSharedSkill(skillTarget) {
  const sharedSkillPath = path.join(skillTarget, 'skills', 'lark-shared', 'SKILL.md');
  if (!fs.existsSync(sharedSkillPath)) return;
  const marker = '<!-- ecorex-auth-handoff-v1 -->';
  let text = fs.readFileSync(sharedSkillPath, 'utf8');
  if (text.includes(marker)) return;
  const insert = [
    '',
    marker,
    '## EcoreX 桌面端授权交接规则',
    '',
    '- `config init`、`auth login`、扫码、OAuth 或浏览器授权不是业务完成，只是进入外部授权等待态。',
    '- 优先使用 split-flow / `--no-wait --json`。拿到用户可操作的链接后，原样展示链接并生成二维码，然后结束本轮并说明“待授权完成后继续”。',
    '- 如果命令只能阻塞等待，必须使用后台方式并给足至少 600 秒的 timeout；读取到链接/二维码后不要把后台等待输出当最终结果。',
    '- 用户回复“完成 / 扫码完成 / 授权完成”后，先执行只读状态检查或继续对应 device-code 流程，确认本机当前用户授权已保存，再恢复原任务。',
    '- 不要向用户展示 runner、harness、timeout、raw JSON token、base_token、folder_token、access_token、调试路径或完整命令日志。',
    ''
  ].join('\n');
  const heading = '## 配置初始化';
  if (text.includes(heading)) {
    text = text.replace(heading, `${insert}\n${heading}`);
  } else {
    text = `${text.trimEnd()}\n${insert}`;
  }
  fs.writeFileSync(sharedSkillPath, text, 'utf8');
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
  patchFeishuSharedSkill(skillTarget);

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

function removeRetiredManagedResources() {
  for (const name of retiredManagedNames) {
    removeGeneratedDir(path.join(managedSkillRoot, name));
    removeGeneratedDir(path.join(managedToolRoot, name));
  }
}

function findOfficeCliExecutable(sourceRoot) {
  const candidates = [
    process.env.ECOREX_OFFICECLI_EXE,
    path.join(sourceRoot, 'bin', process.platform === 'win32' ? 'officecli.exe' : 'officecli'),
    process.platform === 'win32' && process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'OfficeCLI', 'officecli.exe') : '',
    process.platform !== 'win32' ? '/usr/local/bin/officecli' : ''
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return path.resolve(candidate);
  }
  const whereCommand = process.platform === 'win32' ? 'where' : 'which';
  const result = spawnSync(whereCommand, ['officecli'], { encoding: 'utf8', windowsHide: true });
  const first = String(result.stdout || '').split(/\r?\n/).map((line) => line.trim()).find(Boolean);
  return first && fs.existsSync(first) ? path.resolve(first) : '';
}

function prepareOfficeCli() {
  const defaultSource = process.platform === 'win32' ? 'C:\\OfficeCLI-main' : '';
  const sourceRoot = path.resolve(process.env.ECOREX_OFFICECLI_SOURCE || defaultSource || '');
  if (!sourceRoot || !fs.existsSync(sourceRoot)) {
    fail(`OfficeCLI source directory is missing. Set ECOREX_OFFICECLI_SOURCE or create ${defaultSource}.`);
  }
  const sourceSkill = path.join(sourceRoot, 'SKILL.md');
  const sourceSkills = path.join(sourceRoot, 'skills');
  if (!fs.existsSync(sourceSkill)) fail(`OfficeCLI root SKILL.md is missing: ${sourceSkill}`);
  if (!fs.existsSync(path.join(sourceSkills, 'officecli-docx', 'SKILL.md'))) fail(`OfficeCLI child skills are missing under ${sourceSkills}.`);

  const meta = parseSkillFrontmatter(sourceSkill);
  const name = 'officecli';
  const version = String(meta.version || '1.0.102');
  const skillTarget = path.join(managedSkillRoot, name);
  const toolTarget = path.join(managedToolRoot, name);
  const sourceExe = findOfficeCliExecutable(sourceRoot);
  if (!sourceExe) fail('OfficeCLI executable is missing. Install OfficeCLI first or set ECOREX_OFFICECLI_EXE.');

  cleanGeneratedDir(skillTarget);
  writeManifest(skillTarget, {
    name,
    displayName: 'OfficeCLI',
    description: 'Create, inspect, validate, and modify DOCX, XLSX, and PPTX documents through the EcoreX-managed OfficeCLI runtime.',
    version,
    skills: './skills'
  });
  const copied = copyDirectory(sourceSkills, path.join(skillTarget, 'skills'), 'OfficeCLI Skill');
  fs.mkdirSync(path.join(skillTarget, 'skills', name), { recursive: true });
  fs.copyFileSync(sourceSkill, path.join(skillTarget, 'skills', name, 'SKILL.md'));

  cleanGeneratedDir(toolTarget);
  const targetExe = path.join(toolTarget, process.platform === 'win32' ? 'officecli.exe' : 'officecli');
  fs.copyFileSync(sourceExe, targetExe);
  fs.writeFileSync(
    path.join(toolTarget, 'manifest.json'),
    `${JSON.stringify({
      name,
      version,
      platform: process.platform,
      executable: path.basename(targetExe),
      source: 'EcoreX managed OfficeCLI runtime'
    }, null, 2)}\n`,
    'utf8'
  );

  const versionText = verifyCommand(targetExe, ['--version'], 'officecli');
  console.log(`Prepared managed OfficeCLI ${version} from ${sourceRoot}`);
  console.log(`  skills: ${copied.files} files, ${copied.bytes} bytes`);
  console.log(`  runtime: ${path.relative(rootDir, targetExe)} (${versionText})`);
}

function openCliPackageJson(sourceRoot) {
  const packageInfo = readJson(path.join(sourceRoot, 'package.json'), null);
  if (!packageInfo?.name || !packageInfo?.version) fail(`OpenCLI package.json is invalid under ${sourceRoot}.`);
  return packageInfo;
}

function writeOpenCliWrappers(binDir) {
  fs.mkdirSync(binDir, { recursive: true });
  const cmd = [
    '@echo off',
    'setlocal',
    'set "SCRIPT=%~dp0..\\package\\dist\\src\\main.js"',
    'set "ECOREX_NODE=%~dp0..\\..\\..\\..\\EcoreX Agent.exe"',
    'if exist "%ECOREX_NODE%" (',
    '  set "ELECTRON_RUN_AS_NODE=1"',
    '  "%ECOREX_NODE%" "%SCRIPT%" %*',
    '  exit /b %ERRORLEVEL%',
    ')',
    'if defined ECOREX_NODE_EXE (',
    '  "%ECOREX_NODE_EXE%" "%SCRIPT%" %*',
    '  exit /b %ERRORLEVEL%',
    ')',
    'node "%SCRIPT%" %*',
    'exit /b %ERRORLEVEL%',
    ''
  ].join('\r\n');
  fs.writeFileSync(path.join(binDir, 'opencli.cmd'), cmd, 'utf8');
  const ps1 = [
    '$script = Join-Path $PSScriptRoot "..\\package\\dist\\src\\main.js"',
    '$ecorexNode = Join-Path $PSScriptRoot "..\\..\\..\\..\\EcoreX Agent.exe"',
    'if (Test-Path $ecorexNode) {',
    '  $env:ELECTRON_RUN_AS_NODE = "1"',
    '  & $ecorexNode $script @args',
    '  exit $LASTEXITCODE',
    '}',
    'if ($env:ECOREX_NODE_EXE -and (Test-Path $env:ECOREX_NODE_EXE)) {',
    '  & $env:ECOREX_NODE_EXE $script @args',
    '  exit $LASTEXITCODE',
    '}',
    '& node $script @args',
    'exit $LASTEXITCODE',
    ''
  ].join('\n');
  fs.writeFileSync(path.join(binDir, 'opencli.ps1'), ps1, 'utf8');
  if (process.platform !== 'win32') {
    const sh = [
      '#!/usr/bin/env sh',
      'SCRIPT="$(CDPATH= cd -- "$(dirname -- "$0")/../package/dist/src" && pwd)/main.js"',
      'exec node "$SCRIPT" "$@"',
      ''
    ].join('\n');
    const shPath = path.join(binDir, 'opencli');
    fs.writeFileSync(shPath, sh, 'utf8');
    fs.chmodSync(shPath, 0o755);
  }
}

function prepareOpenCliRuntime(sourceRoot, toolTarget, packageInfo) {
  const packageTarget = path.join(toolTarget, 'package');
  const extensionTarget = path.join(toolTarget, 'extension');
  const runtimePackage = {
    name: packageInfo.name,
    version: packageInfo.version,
    description: packageInfo.description,
    type: packageInfo.type || 'module',
    main: packageInfo.main || 'dist/src/main.js',
    bin: packageInfo.bin || { opencli: 'dist/src/main.js' },
    dependencies: packageInfo.dependencies || {}
  };

  cleanGeneratedDir(toolTarget);
  fs.mkdirSync(packageTarget, { recursive: true });
  fs.writeFileSync(path.join(packageTarget, 'package.json'), `${JSON.stringify(runtimePackage, null, 2)}\n`, 'utf8');
  copyDirectory(path.join(sourceRoot, 'dist'), path.join(packageTarget, 'dist'), 'OpenCLI runtime dist');
  copyDirectory(path.join(sourceRoot, 'clis'), path.join(packageTarget, 'clis'), 'OpenCLI adapters');
  for (const fileName of ['cli-manifest.json', 'README.md', 'README.zh-CN.md', 'LICENSE', 'PRIVACY.md']) {
    const source = path.join(sourceRoot, fileName);
    if (fs.existsSync(source) && fs.statSync(source).isFile()) fs.copyFileSync(source, path.join(packageTarget, fileName));
  }

  const npm = commandName('npm');
  const npmArgs = ['install', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'];
  const install = spawnSync(process.platform === 'win32' ? 'cmd.exe' : npm, process.platform === 'win32' ? ['/d', '/c', npm, ...npmArgs] : npmArgs, {
    cwd: packageTarget,
    encoding: 'utf8',
    windowsHide: true
  });
  if (install.status !== 0) fail(`OpenCLI runtime dependencies install failed: ${install.stderr || install.stdout || install.error?.message || ''}`);

  const sourceExtension = path.join(sourceRoot, 'extension');
  copyRequiredFile(path.join(sourceExtension, 'manifest.json'), path.join(extensionTarget, 'manifest.json'), 'OpenCLI extension manifest');
  copyRequiredFile(path.join(sourceExtension, 'popup.html'), path.join(extensionTarget, 'popup.html'), 'OpenCLI extension popup');
  copyRequiredFile(path.join(sourceExtension, 'popup.js'), path.join(extensionTarget, 'popup.js'), 'OpenCLI extension popup script');
  copyDirectory(path.join(sourceExtension, 'dist'), path.join(extensionTarget, 'dist'), 'OpenCLI extension dist');
  copyDirectory(path.join(sourceExtension, 'icons'), path.join(extensionTarget, 'icons'), 'OpenCLI extension icons');

  writeOpenCliWrappers(path.join(toolTarget, 'bin'));
  fs.writeFileSync(
    path.join(toolTarget, 'manifest.json'),
    `${JSON.stringify({
      name: 'opencli',
      version: packageInfo.version,
      platform: process.platform,
      executable: process.platform === 'win32' ? 'bin/opencli.cmd' : 'bin/opencli',
      extension: 'extension',
      extensionInstallHint: 'Chrome Web Store ID ildkmabpimmkaediidaifkhjpohdnifk or bundled unpacked extension directory.',
      source: 'EcoreX managed OpenCLI runtime'
    }, null, 2)}\n`,
    'utf8'
  );
}

function prepareOpenCli() {
  const defaultSource = process.platform === 'win32' ? 'C:\\OpenCLI-main' : '';
  const sourceRoot = path.resolve(process.env.ECOREX_OPENCLI_SOURCE || defaultSource || '');
  if (!sourceRoot || !fs.existsSync(sourceRoot)) {
    fail(`OpenCLI source directory is missing. Set ECOREX_OPENCLI_SOURCE or create ${defaultSource}.`);
  }
  const packageInfo = openCliPackageJson(sourceRoot);
  if (!fs.existsSync(path.join(sourceRoot, 'dist', 'src', 'main.js'))) {
    fail(`OpenCLI dist is missing. Run "npm install --ignore-scripts && npm run build" in ${sourceRoot}.`);
  }
  if (!fs.existsSync(path.join(sourceRoot, 'skills', 'opencli-browser', 'SKILL.md'))) {
    fail(`OpenCLI skills are missing under ${path.join(sourceRoot, 'skills')}.`);
  }
  if (!fs.existsSync(path.join(sourceRoot, 'extension', 'dist', 'background.js'))) {
    fail(`OpenCLI extension build is missing under ${path.join(sourceRoot, 'extension')}.`);
  }

  const name = 'opencli';
  const skillTarget = path.join(managedSkillRoot, name);
  const toolTarget = path.join(managedToolRoot, name);

  cleanGeneratedDir(skillTarget);
  writeManifest(skillTarget, {
    name,
    displayName: 'OpenCLI',
    description: 'Drive Chrome, website adapters, and browser-based workflows through the EcoreX-managed OpenCLI runtime.',
    version: packageInfo.version || '1.0.0',
    skills: './skills'
  });
  const copied = copyDirectory(path.join(sourceRoot, 'skills'), path.join(skillTarget, 'skills'), 'OpenCLI Skill');
  prepareOpenCliRuntime(sourceRoot, toolTarget, packageInfo);
  const versionText = verifyCommand(path.join(toolTarget, 'bin', process.platform === 'win32' ? 'opencli.cmd' : 'opencli'), ['--version'], 'opencli');
  console.log(`Prepared managed OpenCLI ${packageInfo.version} from ${sourceRoot}`);
  console.log(`  skills: ${copied.files} files, ${copied.bytes} bytes`);
  console.log(`  runtime: ${path.relative(rootDir, toolTarget)} (${versionText})`);
}

removeRetiredManagedResources();
prepareFeishuSkill();
prepareAgentSkillCreator();
prepareOfficeCli();
prepareOpenCli();

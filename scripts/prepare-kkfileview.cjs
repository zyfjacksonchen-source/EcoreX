#!/usr/bin/env node

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const schema = 'ecorex.kkfileview.vendor.v1';
const defaultSourceDir = process.env.KKFILEVIEW_SOURCE_DIR || 'C:\\kkFileView-master';
const defaultVendorDir = path.join(rootDir, 'vendor', 'kkfileview');

function usage() {
  console.log(`Usage: node scripts/prepare-kkfileview.cjs [options]

Options:
  --source <dir>       kkFileView source checkout. Default: ${defaultSourceDir}
  --vendor <dir>       Vendor output directory. Default: vendor/kkfileview
  --jre <dir>          Optional Java 21 JRE/JDK directory to copy when --copy is used.
  --libreoffice <dir>  Optional LibreOffice directory. Defaults to source/server/LibreOfficePortable.
  --build              Run the detected Maven package command before scanning artifacts.
  --copy               Copy detected artifacts into vendor/kkfileview.
  --dry-run            Print planned writes and copies without touching files.
  --help               Show this help.

Typical flow:
  node scripts/prepare-kkfileview.cjs --dry-run
  node scripts/prepare-kkfileview.cjs --build --copy --jre C:\\path\\to\\jre-21
`);
}

function takeValue(args, index, name) {
  const value = args[index + 1];
  if (!value || value.startsWith('--')) {
    throw new Error(`${name} requires a value`);
  }
  return value;
}

function parseArgs(argv) {
  const options = {
    sourceDir: defaultSourceDir,
    vendorDir: defaultVendorDir,
    jreDir: process.env.KKFILEVIEW_JRE_DIR || '',
    libreOfficeDir: process.env.KKFILEVIEW_LIBREOFFICE_DIR || '',
    build: false,
    copy: false,
    dryRun: false
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--source') {
      options.sourceDir = takeValue(argv, index, '--source');
      index += 1;
    } else if (arg === '--vendor') {
      options.vendorDir = takeValue(argv, index, '--vendor');
      index += 1;
    } else if (arg === '--jre') {
      options.jreDir = takeValue(argv, index, '--jre');
      index += 1;
    } else if (arg === '--libreoffice') {
      options.libreOfficeDir = takeValue(argv, index, '--libreoffice');
      index += 1;
    } else if (arg === '--build') {
      options.build = true;
    } else if (arg === '--copy') {
      options.copy = true;
    } else if (arg === '--dry-run') {
      options.dryRun = true;
    } else {
      throw new Error(`Unknown option: ${arg}`);
    }
  }

  options.sourceDir = path.resolve(options.sourceDir);
  options.vendorDir = path.resolve(options.vendorDir);
  if (options.jreDir) options.jreDir = path.resolve(options.jreDir);
  if (options.libreOfficeDir) options.libreOfficeDir = path.resolve(options.libreOfficeDir);
  return options;
}

function toSlash(value) {
  return value.split(path.sep).join('/');
}

function relFromRoot(file) {
  const relative = path.relative(rootDir, file);
  if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) {
    return toSlash(relative);
  }
  return file;
}

function relFromSource(sourceDir, file) {
  const relative = path.relative(sourceDir, file);
  if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) {
    return toSlash(relative);
  }
  return file;
}

function existsFile(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

function existsDir(dir) {
  try {
    return fs.statSync(dir).isDirectory();
  } catch {
    return false;
  }
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function fileSummary(file, baseDir) {
  const stat = fs.statSync(file);
  return {
    path: baseDir ? relFromSource(baseDir, file) : file,
    name: path.basename(file),
    size: stat.size,
    sha256: sha256(file),
    updatedAt: stat.mtime.toISOString()
  };
}

function stripComments(xml) {
  return xml.replace(/<!--[\s\S]*?-->/g, '');
}

function decodeXml(value) {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function firstTag(xml, tagName) {
  const tag = escapeRegExp(tagName);
  const match = xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'i'));
  return match ? decodeXml(match[1].trim()) : null;
}

function allTags(xml, tagName) {
  const tag = escapeRegExp(tagName);
  const pattern = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'gi');
  const values = [];
  let match = pattern.exec(xml);
  while (match) {
    values.push(decodeXml(match[1].trim()));
    match = pattern.exec(xml);
  }
  return values;
}

function block(xml, tagName) {
  const tag = escapeRegExp(tagName);
  const match = xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>[\\s\\S]*?</${tag}>`, 'i'));
  return match ? match[0] : '';
}

function parsePom(pomPath) {
  if (!existsFile(pomPath)) {
    return { exists: false, path: pomPath };
  }

  const xml = stripComments(fs.readFileSync(pomPath, 'utf8'));
  const parent = block(xml, 'parent');
  const ownXml = xml.replace(parent, '');

  return {
    exists: true,
    path: pomPath,
    artifactId: firstTag(ownXml, 'artifactId') || firstTag(xml, 'artifactId'),
    groupId: firstTag(ownXml, 'groupId') || firstTag(parent, 'groupId'),
    version: firstTag(ownXml, 'version') || firstTag(parent, 'version'),
    packaging: firstTag(ownXml, 'packaging') || 'jar',
    modules: allTags(xml, 'module'),
    properties: {
      javaVersion: firstTag(xml, 'java.version'),
      springBootVersion: firstTag(xml, 'spring.boot.version')
    }
  };
}

function scanFlat(dir, matcher) {
  if (!existsDir(dir)) return [];
  return fs
    .readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && matcher(entry.name))
    .map((entry) => path.join(dir, entry.name));
}

function unique(values) {
  return Array.from(new Set(values));
}

function newestFirst(files) {
  return files.sort((left, right) => {
    const leftStat = fs.statSync(left);
    const rightStat = fs.statSync(right);
    return rightStat.mtimeMs - leftStat.mtimeMs || rightStat.size - leftStat.size;
  });
}

function findServerJars(sourceDir) {
  const candidateDirs = [
    path.join(sourceDir, 'server', 'target'),
    path.join(sourceDir, 'target'),
    path.join(sourceDir, 'server', 'bin')
  ];

  const files = unique(
    candidateDirs.flatMap((dir) =>
      scanFlat(dir, (name) => /^kkFileView-.+\.jar$/i.test(name) && !/-(sources|javadoc)\.jar$/i.test(name))
    )
  );
  return newestFirst(files);
}

function findServerArchives(sourceDir) {
  const candidateDirs = [path.join(sourceDir, 'server', 'target'), path.join(sourceDir, 'target')];
  const files = unique(candidateDirs.flatMap((dir) => scanFlat(dir, (name) => /^kkFileView-.+\.(zip|tar\.gz)$/i.test(name))));
  return newestFirst(files);
}

function defaultLibreOfficeDir(sourceDir) {
  return path.join(sourceDir, 'server', 'LibreOfficePortable');
}

function libreOfficeSummary(dir) {
  if (!dir) return { configured: false, exists: false, path: null };
  const candidates = [
    path.join(dir, 'App', 'libreoffice', 'program', 'soffice.exe'),
    path.join(dir, 'App', 'libreoffice', 'program', 'soffice.bin'),
    path.join(dir, 'program', 'soffice'),
    path.join(dir, 'program', 'soffice.bin')
  ];
  return {
    configured: true,
    exists: existsDir(dir),
    path: dir,
    soffice: candidates.find(existsFile) || null
  };
}

function jreSummary(dir) {
  if (!dir) return { configured: false, exists: false, path: null };
  const javaExe = process.platform === 'win32' ? path.join(dir, 'bin', 'java.exe') : path.join(dir, 'bin', 'java');
  return {
    configured: true,
    exists: existsDir(dir),
    path: dir,
    java: existsFile(javaExe) ? javaExe : null
  };
}

function quoteCmdArg(value) {
  return `"${String(value).replace(/"/g, '""')}"`;
}

function spawnCommand(executable, args, options = {}) {
  const commandOptions = { windowsHide: true, ...options, shell: false };
  if (process.platform === 'win32' && /\.(cmd|bat)$/i.test(executable)) {
    return spawnSync(process.env.ComSpec || 'cmd.exe', ['/d', '/c', executable, ...args], commandOptions);
  }
  return spawnSync(executable, args, commandOptions);
}

function detectMaven(sourceDir, rootPom, serverPom) {
  const wrapperName = process.platform === 'win32' ? 'mvnw.cmd' : 'mvnw';
  const wrapper = path.join(sourceDir, wrapperName);
  const executable = existsFile(wrapper) ? wrapper : findMavenExecutable();
  const args = rootPom.modules?.includes('server')
    ? ['-pl', 'server', '-am', '-DskipTests', 'package']
    : ['-f', path.relative(sourceDir, serverPom.path || path.join(sourceDir, 'server', 'pom.xml')), '-DskipTests', 'package'];
  const probe = existsDir(sourceDir)
    ? spawnCommand(executable, ['-v'], {
        cwd: sourceDir,
        encoding: 'utf8',
        timeout: 8000
      })
    : { status: null, stdout: '', stderr: '' };
  const output = `${probe.stdout || ''}${probe.stderr || ''}`.trim();
  return {
    executable,
    executableLabel: existsFile(wrapper) ? `.${path.sep}${wrapperName}` : path.basename(executable),
    available: probe.status === 0,
    versionLine: output.split(/\r?\n/).find(Boolean) || '',
    args,
    cwd: sourceDir,
    line: `${existsFile(wrapper) ? `.${path.sep}${wrapperName}` : path.basename(executable)} ${args.join(' ')}`
  };
}

function findMavenExecutable() {
  const exe = process.platform === 'win32' ? 'mvn.cmd' : 'mvn';
  const candidates = [
    process.env.KKFILEVIEW_MAVEN_EXE,
    process.env.MAVEN_HOME ? path.join(process.env.MAVEN_HOME, 'bin', exe) : '',
    process.env.M2_HOME ? path.join(process.env.M2_HOME, 'bin', exe) : '',
    path.join(rootDir, '.cache', 'kkfileview-tools', 'apache-maven-3.9.11', 'bin', exe),
    exe
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate === exe || existsFile(candidate)) return candidate;
  }
  return exe;
}

function expectedArtifacts(rootPom, serverPom) {
  const artifactId = serverPom.artifactId || 'kkFileView';
  const version = serverPom.version || rootPom.version || '5.0.0';
  const base = `server/target/${artifactId}-${version}`;
  return [`${base}.jar`, `${base}.zip`, `${base}.tar.gz`];
}

function discover(options) {
  const rootPom = parsePom(path.join(options.sourceDir, 'pom.xml'));
  const serverPom = parsePom(path.join(options.sourceDir, 'server', 'pom.xml'));
  const maven = detectMaven(options.sourceDir, rootPom, serverPom);
  const jars = existsDir(options.sourceDir) ? findServerJars(options.sourceDir) : [];
  const archives = existsDir(options.sourceDir) ? findServerArchives(options.sourceDir) : [];
  const detectedLibreOffice = options.libreOfficeDir || defaultLibreOfficeDir(options.sourceDir);

  return {
    sourceExists: existsDir(options.sourceDir),
    rootPom,
    serverPom,
    maven,
    jars,
    archives,
    libreOffice: libreOfficeSummary(detectedLibreOffice),
    jre: jreSummary(options.jreDir),
    sourceConfigDir: path.join(options.sourceDir, 'server', 'src', 'main', 'config'),
    sourceBinDir: path.join(options.sourceDir, 'server', 'src', 'main', 'bin')
  };
}

function runBuild(discovery, options) {
  if (options.dryRun) {
    console.log(`[dry-run] would run in ${discovery.maven.cwd}: ${discovery.maven.line}`);
    return;
  }
  if (!discovery.maven.available && !existsFile(discovery.maven.executable)) {
    throw new Error(`Maven was not found. Install Maven or add mvnw to ${options.sourceDir}`);
  }
  console.log(`running Maven build: ${discovery.maven.line}`);
  const result = spawnCommand(discovery.maven.executable, discovery.maven.args, {
    cwd: discovery.maven.cwd,
    stdio: 'inherit'
  });
  if (result.status !== 0) {
    throw new Error(`Maven build failed with exit code ${result.status || 1}`);
  }
}

function makeManifest(options, discovery) {
  const jar = discovery.jars[0] || null;
  const serverJarVendor = jar ? `server/bin/${path.basename(jar)}` : null;
  const copyWillMaterialize = options.copy && Boolean(jar);
  const state = jar
    ? copyWillMaterialize
      ? options.dryRun
        ? 'dry-run-ready'
        : 'ready'
      : 'draft-source-jar-detected'
    : 'needs-maven-build';

  return {
    schema,
    generatedAt: new Date().toISOString(),
    generatedBy: relFromRoot(__filename),
    status: {
      state,
      materialized: copyWillMaterialize && !options.dryRun,
      dryRun: options.dryRun,
      copiedArtifacts: options.copy
    },
    source: {
      root: options.sourceDir,
      exists: discovery.sourceExists,
      rootPom: discovery.rootPom.exists ? relFromSource(options.sourceDir, discovery.rootPom.path) : null,
      serverPom: discovery.serverPom.exists ? relFromSource(options.sourceDir, discovery.serverPom.path) : null,
      groupId: discovery.serverPom.groupId || discovery.rootPom.groupId || null,
      artifactId: discovery.serverPom.artifactId || 'kkFileView',
      version: discovery.serverPom.version || discovery.rootPom.version || null,
      javaVersion: discovery.rootPom.properties?.javaVersion || null,
      springBootVersion: discovery.rootPom.properties?.springBootVersion || null,
      modules: discovery.rootPom.modules || []
    },
    build: {
      requiresJavaMajor: Number(discovery.rootPom.properties?.javaVersion || 21),
      maven: discovery.maven,
      expectedArtifacts: expectedArtifacts(discovery.rootPom, discovery.serverPom)
    },
    detected: {
      serverJar: jar ? fileSummary(jar, options.sourceDir) : null,
      serverArchives: discovery.archives.map((archive) => fileSummary(archive, options.sourceDir)),
      sourceConfigDir: existsDir(discovery.sourceConfigDir) ? relFromSource(options.sourceDir, discovery.sourceConfigDir) : null,
      sourceBinDir: existsDir(discovery.sourceBinDir) ? relFromSource(options.sourceDir, discovery.sourceBinDir) : null,
      libreOffice: {
        exists: discovery.libreOffice.exists,
        path: discovery.libreOffice.path,
        soffice: discovery.libreOffice.soffice
      },
      jre: {
        configured: discovery.jre.configured,
        exists: discovery.jre.exists,
        path: discovery.jre.path,
        java: discovery.jre.java
      }
    },
    vendor: {
      root: relFromRoot(options.vendorDir),
      serverJar: serverJarVendor,
      serverBinDir: 'server/bin',
      configDir: 'server/config',
      logDir: 'runtime/log',
      runtimeDir: 'runtime',
      jreDir: 'jre',
      libreOfficeDir: 'libreoffice/LibreOfficePortable',
      launchers: {
        windowsCmd: 'bin/start-kkfileview.cmd',
        powershell: 'bin/start-kkfileview.ps1',
        shell: 'bin/start-kkfileview.sh'
      }
    },
    runtimeEnv: {
      KK_SERVER_PORT: '8012',
      KK_OFFICE_HOME: '%RESOURCE_ROOT%/kkfileview/libreoffice/LibreOfficePortable/App/libreoffice',
      KK_FILE_DIR: '%USER_DATA%/kkfileview/files',
      KK_LOCAL_PREVIEW_DIR: '%USER_DATA%/kkfileview/preview',
      KK_LOG_DIR: '%USER_DATA%/kkfileview/log',
      KK_BASE_URL: 'http://127.0.0.1:8012'
    },
    electronBuilder: {
      extraResources: [
        {
          from: 'vendor/kkfileview',
          to: 'kkfileview',
          filter: ['**/*', '!**/*.log', '!**/tmp/**/*', '!**/.DS_Store', '!**/Thumbs.db']
        }
      ]
    },
    nextSteps: [
      'If serverJar is null, build kkFileView with the detected Maven command.',
      'Run this script again with --copy and --jre <Java 21 runtime> to populate vendor/kkfileview.',
      'Run node scripts/verify-kkfileview-vendor.cjs --require-jre --require-libreoffice before enabling extraResources.',
      'Add the electronBuilder.extraResources entry to package.json in a later packaging change.'
    ]
  };
}

function ensureDir(dir, dryRun) {
  if (dryRun) {
    console.log(`[dry-run] would create directory: ${relFromRoot(dir)}`);
    return;
  }
  fs.mkdirSync(dir, { recursive: true });
}

function writeText(file, content, dryRun) {
  if (dryRun) {
    console.log(`[dry-run] would write: ${relFromRoot(file)}`);
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content, 'utf8');
}

function copyFile(source, target, dryRun) {
  if (dryRun) {
    console.log(`[dry-run] would copy file: ${source} -> ${relFromRoot(target)}`);
    return;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

function copyDir(source, target, dryRun) {
  if (!existsDir(source)) return false;
  if (dryRun) {
    console.log(`[dry-run] would copy directory: ${source} -> ${relFromRoot(target)}`);
    return true;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.cpSync(source, target, { recursive: true });
  return true;
}

function windowsLauncher() {
  return [
    '@echo off',
    'setlocal',
    'set "ROOT=%~dp0.."',
    'set "SERVER=%ROOT%\\server"',
    'set "JAVA_EXE=%ROOT%\\jre\\bin\\java.exe"',
    'if not exist "%JAVA_EXE%" set "JAVA_EXE=java"',
    'if "%KK_SERVER_PORT%"=="" set "KK_SERVER_PORT=8012"',
    'if "%KK_FILE_DIR%"=="" set "KK_FILE_DIR=%ROOT%\\runtime\\files"',
    'if "%KK_LOCAL_PREVIEW_DIR%"=="" set "KK_LOCAL_PREVIEW_DIR=%ROOT%\\runtime\\preview"',
    'if "%KK_LOG_DIR%"=="" set "KK_LOG_DIR=%ROOT%\\runtime\\log"',
    'if "%KK_BASE_URL%"=="" set "KK_BASE_URL=http://127.0.0.1:%KK_SERVER_PORT%"',
    'if "%KK_OFFICE_HOME%"=="" set "KK_OFFICE_HOME=%ROOT%\\libreoffice\\LibreOfficePortable\\App\\libreoffice"',
    'set "JAR_NAME="',
    'for %%F in ("%SERVER%\\bin"\\kkFileView-*.jar) do (',
    '  set "JAR_NAME=%%~fF"',
    '  goto :jar_found',
    ')',
    'echo kkFileView jar not found in "%SERVER%\\bin"',
    'exit /b 1',
    ':jar_found',
    'if not exist "%KK_LOG_DIR%" mkdir "%KK_LOG_DIR%"',
    '"%JAVA_EXE%" -Dfile.encoding=UTF-8 -Dspring.config.location="%SERVER%\\config\\application.properties" -jar "%JAR_NAME%" > "%KK_LOG_DIR%\\kkFileView.log" 2>&1'
  ].join('\r\n');
}

function powershellLauncher() {
  return [
    '$ErrorActionPreference = "Stop"',
    '$Root = Resolve-Path (Join-Path $PSScriptRoot "..")',
    '$Server = Join-Path $Root "server"',
    '$Java = Join-Path $Root "jre\\bin\\java.exe"',
    'if (-not (Test-Path $Java)) { $Java = "java" }',
    'if (-not $env:KK_SERVER_PORT) { $env:KK_SERVER_PORT = "8012" }',
    'if (-not $env:KK_FILE_DIR) { $env:KK_FILE_DIR = Join-Path $Root "runtime\\files" }',
    'if (-not $env:KK_LOCAL_PREVIEW_DIR) { $env:KK_LOCAL_PREVIEW_DIR = Join-Path $Root "runtime\\preview" }',
    'if (-not $env:KK_LOG_DIR) { $env:KK_LOG_DIR = Join-Path $Root "runtime\\log" }',
    'if (-not $env:KK_BASE_URL) { $env:KK_BASE_URL = "http://127.0.0.1:$($env:KK_SERVER_PORT)" }',
    'if (-not $env:KK_OFFICE_HOME) { $env:KK_OFFICE_HOME = Join-Path $Root "libreoffice\\LibreOfficePortable\\App\\libreoffice" }',
    '$Jar = Get-ChildItem -Path (Join-Path $Server "bin") -Filter "kkFileView-*.jar" | Sort-Object LastWriteTime -Descending | Select-Object -First 1',
    "if (-not $Jar) { throw \"kkFileView jar not found in $(Join-Path $Server 'bin')\" }",
    '$LogDir = $env:KK_LOG_DIR',
    'New-Item -ItemType Directory -Force -Path $LogDir | Out-Null',
    "$Config = Join-Path $Server 'config\\application.properties'",
    "$LogFile = Join-Path $LogDir 'kkFileView.log'",
    '& $Java "-Dfile.encoding=UTF-8" "-Dspring.config.location=$Config" -jar $Jar.FullName *> $LogFile'
  ].join('\n');
}

function shellLauncher() {
  return [
    '#!/usr/bin/env sh',
    'set -eu',
    'ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"',
    'SERVER="$ROOT/server"',
    'JAVA_EXE="$ROOT/jre/bin/java"',
    'if [ ! -x "$JAVA_EXE" ]; then JAVA_EXE="java"; fi',
    'export KK_SERVER_PORT="${KK_SERVER_PORT:-8012}"',
    'export KK_FILE_DIR="${KK_FILE_DIR:-$ROOT/runtime/files}"',
    'export KK_LOCAL_PREVIEW_DIR="${KK_LOCAL_PREVIEW_DIR:-$ROOT/runtime/preview}"',
    'export KK_LOG_DIR="${KK_LOG_DIR:-$ROOT/runtime/log}"',
    'export KK_BASE_URL="${KK_BASE_URL:-http://127.0.0.1:$KK_SERVER_PORT}"',
    'export KK_OFFICE_HOME="${KK_OFFICE_HOME:-$ROOT/libreoffice/LibreOfficePortable/App/libreoffice}"',
    'JAR_PATH="$(find "$SERVER/bin" -maxdepth 1 -name "kkFileView-*.jar" -print | sort | tail -n 1)"',
    'if [ -z "$JAR_PATH" ]; then echo "kkFileView jar not found in $SERVER/bin" >&2; exit 1; fi',
    'mkdir -p "$KK_LOG_DIR"',
    'exec "$JAVA_EXE" -Dfile.encoding=UTF-8 -Dspring.config.location="$SERVER/config/application.properties" -jar "$JAR_PATH" > "$KK_LOG_DIR/kkFileView.log" 2>&1'
  ].join('\n');
}

function readmeContent(manifest) {
  const lines = [
    '# kkFileView Vendor Draft',
    '',
    `Generated by \`${manifest.generatedBy}\` at ${manifest.generatedAt}.`,
    '',
    '## Source Discovery',
    '',
    `- Source root: \`${manifest.source.root}\``,
    `- Source exists: \`${manifest.source.exists}\``,
    `- Maven module: \`${manifest.source.modules.join(', ') || 'none detected'}\``,
    `- Version: \`${manifest.source.version || 'unknown'}\``,
    `- Required Java: \`${manifest.build.requiresJavaMajor}\``,
    `- Server jar: \`${manifest.detected.serverJar?.path || 'not found'}\``,
    `- LibreOffice: \`${manifest.detected.libreOffice.soffice || 'not found'}\``,
    `- Maven command: \`cd ${manifest.build.maven.cwd} && ${manifest.build.maven.line}\``,
    '',
    '## Prepare',
    '',
    '```powershell',
    'node scripts\\prepare-kkfileview.cjs --dry-run',
    `cd ${manifest.build.maven.cwd}`,
    manifest.build.maven.line,
    'cd C:\\EcoreX-Agent',
    'node scripts\\prepare-kkfileview.cjs --copy --jre C:\\path\\to\\java-21-runtime',
    'node scripts\\verify-kkfileview-vendor.cjs --require-jre --require-libreoffice',
    '```',
    '',
    '## Expected Vendor Layout',
    '',
    '```text',
    'vendor/kkfileview/',
    '  manifest.json',
    '  README.md',
    '  bin/start-kkfileview.cmd',
    '  bin/start-kkfileview.ps1',
    '  bin/start-kkfileview.sh',
    '  server/bin/kkFileView-*.jar',
    '  server/config/application.properties',
    '  runtime/log/',
    '  jre/bin/java(.exe)',
    '  libreoffice/LibreOfficePortable/App/libreoffice/program/soffice(.exe)',
    '```',
    '',
    '## Electron Builder Draft',
    '',
    'Do not edit package.json from this script. When the vendor is verified, add this entry in a later packaging change:',
    '',
    '```json',
    JSON.stringify(manifest.electronBuilder.extraResources, null, 2),
    '```'
  ];
  return `${lines.join('\n')}\n`;
}

function materialize(manifest, discovery, options) {
  const manifestPath = path.join(options.vendorDir, 'manifest.json');
  const readmePath = path.join(options.vendorDir, 'README.md');

  ensureDir(options.vendorDir, options.dryRun);
  writeText(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, options.dryRun);
  writeText(readmePath, readmeContent(manifest), options.dryRun);
  writeText(path.join(options.vendorDir, manifest.vendor.launchers.windowsCmd), `${windowsLauncher()}\r\n`, options.dryRun);
  writeText(path.join(options.vendorDir, manifest.vendor.launchers.powershell), `${powershellLauncher()}\n`, options.dryRun);
  writeText(path.join(options.vendorDir, manifest.vendor.launchers.shell), `${shellLauncher()}\n`, options.dryRun);

  if (!options.copy) return;
  if (!manifest.vendor.serverJar || !discovery.jars[0]) return;

  copyDir(discovery.sourceBinDir, path.join(options.vendorDir, manifest.vendor.serverBinDir), options.dryRun);
  copyDir(discovery.sourceConfigDir, path.join(options.vendorDir, manifest.vendor.configDir), options.dryRun);
  ensureDir(path.join(options.vendorDir, manifest.vendor.logDir), options.dryRun);
  copyFile(discovery.jars[0], path.join(options.vendorDir, manifest.vendor.serverJar), options.dryRun);

  if (discovery.libreOffice.exists) {
    copyDir(discovery.libreOffice.path, path.join(options.vendorDir, manifest.vendor.libreOfficeDir), options.dryRun);
  }
  if (discovery.jre.exists) {
    copyDir(discovery.jre.path, path.join(options.vendorDir, manifest.vendor.jreDir), options.dryRun);
  }
}

function printSummary(manifest, discovery, options) {
  console.log(`kkFileView source: ${options.sourceDir}`);
  console.log(`source exists: ${discovery.sourceExists ? 'yes' : 'no'}`);
  console.log(`server jar: ${manifest.detected.serverJar?.path || 'not found'}`);
  console.log(`maven build: ${manifest.build.maven.line}`);
  console.log(`maven available: ${manifest.build.maven.available ? manifest.build.maven.versionLine : 'no'}`);
  console.log(`libreoffice: ${manifest.detected.libreOffice.soffice || 'not found'}`);
  console.log(`jre: ${manifest.detected.jre.java || 'not configured'}`);
  console.log(`vendor state: ${manifest.status.state}`);
  console.log(`${options.dryRun ? 'planned' : 'wrote'} vendor draft: ${relFromRoot(options.vendorDir)}`);
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    usage();
    return;
  }

  let discovery = discover(options);
  if (options.build) {
    runBuild(discovery, options);
    discovery = discover(options);
  }

  const manifest = makeManifest(options, discovery);
  materialize(manifest, discovery, options);
  printSummary(manifest, discovery, options);

  if (options.copy && !manifest.detected.serverJar) {
    console.error('No kkFileView server jar was found. Run the Maven build command above, then rerun with --copy.');
    process.exitCode = 1;
  }
}

try {
  main();
} catch (error) {
  console.error(error.message || error);
  process.exit(1);
}

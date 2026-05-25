#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const defaultVendorDir = path.join(rootDir, 'vendor', 'kkfileview');
const expectedSchema = 'ecorex.kkfileview.vendor.v1';

function usage() {
  console.log(`Usage: node scripts/verify-kkfileview-vendor.cjs [options]

Options:
  --vendor <dir>          Vendor directory. Default: vendor/kkfileview
  --require-jre           Fail if vendor/jre/bin/java(.exe) is missing or too old.
  --require-libreoffice   Fail if LibreOffice soffice is missing.
  --allow-draft           Treat draft or needs-build manifests as warnings.
  --allow-missing         Exit 0 when vendor/kkfileview has not been generated yet.
  --json                  Print a JSON report.
  --help                  Show this help.
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
    vendorDir: defaultVendorDir,
    requireJre: false,
    requireLibreOffice: false,
    allowDraft: false,
    allowMissing: false,
    json: false
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--vendor') {
      options.vendorDir = takeValue(argv, index, '--vendor');
      index += 1;
    } else if (arg === '--require-jre') {
      options.requireJre = true;
    } else if (arg === '--require-libreoffice') {
      options.requireLibreOffice = true;
    } else if (arg === '--allow-draft') {
      options.allowDraft = true;
    } else if (arg === '--allow-missing') {
      options.allowMissing = true;
    } else if (arg === '--json') {
      options.json = true;
    } else {
      throw new Error(`Unknown option: ${arg}`);
    }
  }

  options.vendorDir = path.resolve(options.vendorDir);
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

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function makeReport(options) {
  const checks = [];
  return {
    vendorDir: options.vendorDir,
    checks,
    add(level, id, message, details = {}) {
      checks.push({ level, id, message, details });
    },
    ok(id, message, details) {
      this.add('ok', id, message, details);
    },
    warn(id, message, details) {
      this.add('warn', id, message, details);
    },
    fail(id, message, details) {
      this.add('fail', id, message, details);
    }
  };
}

function vendorPath(options, relativePath) {
  if (!relativePath) return null;
  return path.resolve(options.vendorDir, relativePath);
}

function listJars(dir) {
  if (!existsDir(dir)) return [];
  return fs
    .readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && /^kkFileView-.+\.jar$/i.test(entry.name))
    .map((entry) => path.join(dir, entry.name));
}

function javaMajor(versionText) {
  const match = versionText.match(/version "(?:(1)\.)?(\d+)/i);
  if (!match) return null;
  return Number(match[2]);
}

function checkJava(javaExe, requiredMajor) {
  const result = spawnSync(javaExe, ['-version'], {
    encoding: 'utf8',
    timeout: 8000,
    windowsHide: true
  });
  const output = `${result.stderr || ''}${result.stdout || ''}`.trim();
  return {
    status: result.status,
    output,
    major: javaMajor(output),
    error: result.error?.message || null,
    meetsRequirement: result.status === 0 && (!requiredMajor || (javaMajor(output) || 0) >= requiredMajor)
  };
}

function findSoffice(libreOfficeDir) {
  const candidates = [
    path.join(libreOfficeDir, 'App', 'libreoffice', 'program', 'soffice.exe'),
    path.join(libreOfficeDir, 'App', 'libreoffice', 'program', 'soffice.bin'),
    path.join(libreOfficeDir, 'program', 'soffice'),
    path.join(libreOfficeDir, 'program', 'soffice.bin')
  ];
  return candidates.find(existsFile) || null;
}

function isDraft(manifest) {
  const state = String(manifest.status?.state || '');
  return !manifest.status?.materialized || /draft|needs|dry-run|source-jar-detected/i.test(state);
}

function verify(options) {
  const report = makeReport(options);
  const manifestPath = path.join(options.vendorDir, 'manifest.json');

  if (!existsDir(options.vendorDir)) {
    const message = `${relFromRoot(options.vendorDir)} does not exist`;
    if (options.allowMissing) {
      report.warn('vendor.missing', message);
      return report;
    }
    report.fail('vendor.missing', message);
    return report;
  }
  report.ok('vendor.exists', `${relFromRoot(options.vendorDir)} exists`);

  if (!existsFile(manifestPath)) {
    const message = `${relFromRoot(manifestPath)} is missing`;
    if (options.allowMissing) {
      report.warn('manifest.missing', message);
      return report;
    }
    report.fail('manifest.missing', message);
    return report;
  }

  let manifest;
  try {
    manifest = readJson(manifestPath);
    report.ok('manifest.read', `${relFromRoot(manifestPath)} parsed`);
  } catch (error) {
    report.fail('manifest.parse', `manifest.json is not valid JSON: ${error.message}`);
    return report;
  }

  if (manifest.schema === expectedSchema) {
    report.ok('manifest.schema', `schema is ${expectedSchema}`);
  } else {
    report.fail('manifest.schema', `unexpected schema: ${manifest.schema || 'missing'}`);
  }

  const draft = isDraft(manifest);
  if (draft && options.allowDraft) {
    report.warn('manifest.draft', `vendor state is ${manifest.status?.state || 'unknown'}`);
  } else if (draft) {
    report.fail('manifest.draft', `vendor is not materialized yet: ${manifest.status?.state || 'unknown'}`);
  } else {
    report.ok('manifest.ready', `vendor state is ${manifest.status?.state || 'ready'}`);
  }

  const jarFromManifest = vendorPath(options, manifest.vendor?.serverJar);
  const jarCandidates = jarFromManifest && existsFile(jarFromManifest) ? [jarFromManifest] : listJars(vendorPath(options, manifest.vendor?.serverBinDir || 'server/bin'));
  if (jarCandidates.length) {
    report.ok('server.jar', `found ${relFromRoot(jarCandidates[0])}`);
  } else if (options.allowDraft && draft) {
    report.warn('server.jar', 'server jar is missing in draft vendor');
  } else {
    report.fail('server.jar', 'server/bin/kkFileView-*.jar is missing');
  }

  const configFile = path.join(vendorPath(options, manifest.vendor?.configDir || 'server/config'), 'application.properties');
  if (existsFile(configFile)) {
    report.ok('server.config', `found ${relFromRoot(configFile)}`);
  } else if (options.allowDraft && draft) {
    report.warn('server.config', 'application.properties is missing in draft vendor');
  } else {
    report.fail('server.config', 'server/config/application.properties is missing');
  }

  const readmePath = path.join(options.vendorDir, 'README.md');
  if (existsFile(readmePath)) {
    report.ok('vendor.readme', `found ${relFromRoot(readmePath)}`);
  } else {
    report.warn('vendor.readme', 'README.md is missing');
  }

  for (const [id, relativePath] of Object.entries(manifest.vendor?.launchers || {})) {
    const launcher = vendorPath(options, relativePath);
    if (existsFile(launcher)) {
      report.ok(`launcher.${id}`, `found ${relFromRoot(launcher)}`);
    } else if (options.allowDraft && draft) {
      report.warn(`launcher.${id}`, `${relativePath} is missing in draft vendor`);
    } else {
      report.warn(`launcher.${id}`, `${relativePath} is missing`);
    }
  }

  const javaExe = vendorPath(
    options,
    process.platform === 'win32' ? path.join(manifest.vendor?.jreDir || 'jre', 'bin', 'java.exe') : path.join(manifest.vendor?.jreDir || 'jre', 'bin', 'java')
  );
  if (existsFile(javaExe)) {
    const java = checkJava(javaExe, manifest.build?.requiresJavaMajor);
    if (java.meetsRequirement) {
      report.ok('jre.version', `Java ${java.major} is available at ${relFromRoot(javaExe)}`);
    } else {
      const message = `Java runtime failed requirement ${manifest.build?.requiresJavaMajor || 'unknown'}: ${java.output || java.error || 'no version output'}`;
      if (options.requireJre) report.fail('jre.version', message);
      else report.warn('jre.version', message);
    }
  } else {
    const message = `${relFromRoot(javaExe)} is missing`;
    if (options.requireJre) report.fail('jre.missing', message);
    else report.warn('jre.missing', message);
  }

  const libreOfficeDir = vendorPath(options, manifest.vendor?.libreOfficeDir || 'libreoffice/LibreOfficePortable');
  const soffice = findSoffice(libreOfficeDir);
  if (soffice) {
    report.ok('libreoffice.soffice', `found ${relFromRoot(soffice)}`);
  } else {
    const message = `LibreOffice soffice is missing under ${relFromRoot(libreOfficeDir)}`;
    if (options.requireLibreOffice) report.fail('libreoffice.soffice', message);
    else report.warn('libreoffice.soffice', message);
  }

  const extraResources = manifest.electronBuilder?.extraResources || [];
  const hasVendorResource = extraResources.some((entry) => entry.from === 'vendor/kkfileview' && entry.to === 'kkfileview');
  if (hasVendorResource) {
    report.ok('electron.extraResources', 'manifest includes kkfileview extraResources draft');
  } else {
    report.warn('electron.extraResources', 'manifest does not include an extraResources draft for vendor/kkfileview');
  }

  return report;
}

function printText(report) {
  const label = { ok: 'ok  ', warn: 'warn', fail: 'fail' };
  for (const check of report.checks) {
    console.log(`${label[check.level] || check.level} ${check.id}: ${check.message}`);
  }
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    usage();
    return;
  }

  const report = verify(options);
  const failures = report.checks.filter((check) => check.level === 'fail');
  if (options.json) {
    console.log(
      JSON.stringify(
        {
          vendorDir: report.vendorDir,
          ok: failures.length === 0,
          checks: report.checks
        },
        null,
        2
      )
    );
  } else {
    printText(report);
  }
  if (failures.length) process.exitCode = 1;
}

try {
  main();
} catch (error) {
  console.error(error.message || error);
  process.exit(1);
}

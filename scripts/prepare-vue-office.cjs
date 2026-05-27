const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const defaultSourceDir = process.env.VUE_OFFICE_SOURCE_DIR || 'C:\\vue-office-master';
const defaultVendorDir = path.join(rootDir, 'vendor', 'vue-office');
const templatePath = path.join(__dirname, 'vue-office-viewer-template.html');
const requiredAssets = [
  'docx.css',
  'docx.umd.js',
  'excel.css',
  'excel.umd.js',
  'pdf.umd.js',
  'pptx-preview.umd.js'
];

function usage() {
  console.log(`Usage: node scripts/prepare-vue-office.cjs [options]

Options:
  --source <dir>   vue-office checkout. Default: ${defaultSourceDir}
  --vendor <dir>   Vendor output directory. Default: vendor/vue-office
  --dry-run        Print the copy plan without writing files.
  -h, --help       Show this help.

This copies the lightweight static preview runtimes plus the EcoreX viewer wrapper.`);
}

function takeValue(argv, index, flag) {
  const value = argv[index + 1];
  if (!value || value.startsWith('--')) throw new Error(`${flag} requires a value.`);
  return value;
}

function parseArgs(argv) {
  const options = {
    sourceDir: defaultSourceDir,
    vendorDir: defaultVendorDir,
    dryRun: false
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--source') {
      options.sourceDir = takeValue(argv, index, arg);
      index += 1;
    } else if (arg === '--vendor') {
      options.vendorDir = takeValue(argv, index, arg);
      index += 1;
    } else if (arg === '--dry-run') {
      options.dryRun = true;
    } else if (arg === '-h' || arg === '--help') {
      usage();
      process.exit(0);
    } else {
      throw new Error(`Unknown option: ${arg}`);
    }
  }
  options.sourceDir = path.resolve(options.sourceDir);
  options.vendorDir = path.resolve(options.vendorDir);
  return options;
}

function fileSummary(filePath) {
  const stat = fs.statSync(filePath);
  return {
    path: path.relative(rootDir, filePath).replace(/\\/g, '/'),
    bytes: stat.size
  };
}

function ensureFile(filePath, label) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    throw new Error(`${label} is missing: ${filePath}`);
  }
}

function copyFile(source, target, dryRun) {
  if (dryRun) return;
  if (path.resolve(source) === path.resolve(target)) return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

function writeText(filePath, text, dryRun) {
  if (dryRun) return;
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, text);
}

function hardenExcelRuntime(filePath, dryRun) {
  if (dryRun) return [];
  let source = fs.readFileSync(filePath, 'utf8');
  const replacements = [
    {
      label: 'drawing lookup without anchors',
      from: /t\.drawings\[i\]\.anchors\.forEach/g,
      to: '(t.drawings[i]&&t.drawings[i].anchors||[]).forEach'
    },
    {
      label: 'drawing model without anchors',
      from: /e\.anchors\.forEach/g,
      to: '(e&&e.anchors||[]).forEach'
    },
    {
      label: 'render model without anchors',
      from: /t\.anchors\.forEach/g,
      to: '(t&&t.anchors||[]).forEach'
    }
  ];
  const applied = [];
  for (const replacement of replacements) {
    const matches = source.match(replacement.from);
    if (!matches?.length) continue;
    source = source.replace(replacement.from, replacement.to);
    applied.push({ label: replacement.label, count: matches.length });
  }
  fs.writeFileSync(filePath, source);
  return applied;
}

function readmeText() {
  return [
    '# EcoreX vue-office Vendor',
    '',
    'This directory contains the lightweight static document preview runtime used by the Electron app.',
    '',
    '- Source: `C:\\vue-office-master\\demo-cdn\\js-preview-lib` by default.',
    '- PPTX uses the browser-only `pptx-preview` runtime because upstream vue-office does not publish a PPTX js-preview CDN bundle.',
    '- Included formats: PDF, DOCX, XLS, XLSX, XLSM, PPTX, PPTM.',
    '- Excluded: the full upstream source tree, demos, test files, build cache, Java runtimes, and native office suites.',
    '',
    'Refresh with:',
    '',
    '```powershell',
    'npm run prepare:vue-office',
    'npm run verify:vue-office',
    '```',
    ''
  ].join('\n');
}

function firstExistingAsset(candidates, label) {
  for (const candidate of candidates.filter(Boolean)) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`${label} is missing. Checked:\n${candidates.filter(Boolean).map((candidate) => `- ${candidate}`).join('\n')}`);
}

function assetCandidates(asset, options, sourceLibDir) {
  if (asset === 'pptx-preview.umd.js') {
    return [
      process.env.PPTX_PREVIEW_UMD,
      path.join(sourceLibDir, asset),
      path.join(options.sourceDir, 'core', 'node_modules', 'pptx-preview', 'dist', asset),
      path.join(options.vendorDir, 'js-preview-lib', asset)
    ];
  }
  return [path.join(sourceLibDir, asset)];
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const sourceLibDir = path.join(options.sourceDir, 'demo-cdn', 'js-preview-lib');
  ensureFile(templatePath, 'viewer template');

  const copied = [];
  for (const asset of requiredAssets) {
    const source = firstExistingAsset(assetCandidates(asset, options, sourceLibDir), `vue-office asset ${asset}`);
    const target = path.join(options.vendorDir, 'js-preview-lib', asset);
    copyFile(source, target, options.dryRun);
    if (asset === 'excel.umd.js') {
      const patches = hardenExcelRuntime(target, options.dryRun);
      for (const patch of patches) {
        console.log(`  patched ${asset}: ${patch.label} (${patch.count})`);
      }
    }
    copied.push({ asset, source, target });
  }
  copyFile(templatePath, path.join(options.vendorDir, 'index.html'), options.dryRun);
  writeText(path.join(options.vendorDir, 'README.md'), readmeText(), options.dryRun);

  const manifest = {
    schema: 'ecorex.vue-office.vendor.v1',
    sourceDir: options.sourceDir,
    sourceLibDir,
    generatedAt: new Date().toISOString(),
    assets: options.dryRun
      ? requiredAssets.map((asset) => ({ path: `js-preview-lib/${asset}` }))
      : [
          fileSummary(path.join(options.vendorDir, 'index.html')),
          ...requiredAssets.map((asset) => fileSummary(path.join(options.vendorDir, 'js-preview-lib', asset)))
        ],
    packaging: {
      extraResourcesFrom: 'vendor/vue-office',
      extraResourcesTo: 'vue-office'
    }
  };
  writeText(path.join(options.vendorDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, options.dryRun);

  console.log(`${options.dryRun ? 'Would prepare' : 'Prepared'} vue-office preview vendor: ${path.relative(rootDir, options.vendorDir) || options.vendorDir}`);
  for (const item of copied) {
    console.log(`- ${path.relative(rootDir, item.target).replace(/\\/g, '/')}`);
  }
}

try {
  main();
} catch (error) {
  console.error(error?.message || String(error));
  process.exit(1);
}

const fs = require('fs');
const path = require('path');

const rootDir = path.resolve(__dirname, '..');
const vendorDir = path.join(rootDir, 'vendor', 'vue-office');
const requiredFiles = [
  'index.html',
  'README.md',
  'manifest.json',
  'js-preview-lib/docx.css',
  'js-preview-lib/docx.umd.js',
  'js-preview-lib/excel.css',
  'js-preview-lib/excel.umd.js',
  'js-preview-lib/pdf.umd.js',
  'js-preview-lib/pptx-preview.umd.js'
];

function rel(filePath) {
  return path.relative(rootDir, filePath).replace(/\\/g, '/');
}

function readText(filePath) {
  return fs.readFileSync(path.join(rootDir, filePath), 'utf8');
}

function fail(message) {
  console.error(`[fail] ${message}`);
  process.exitCode = 1;
}

function ok(message) {
  console.log(`[ok] ${message}`);
}

function assert(condition, message) {
  if (condition) ok(message);
  else fail(message);
}

function assertFile(relativePath) {
  const filePath = path.join(vendorDir, relativePath);
  let stat = null;
  try {
    stat = fs.statSync(filePath);
  } catch {
    // Report below.
  }
  assert(Boolean(stat?.isFile?.() && stat.size > 0), `${rel(filePath)} exists and is non-empty`);
}

function packageJson() {
  return JSON.parse(readText('package.json'));
}

function assertNoLegacyTerms() {
  const terms = [
    ['kk', 'FileView'].join(''),
    ['Libre', 'Office'].join(''),
    ['jod', 'converter'].join('')
  ];
  const files = [
    'package.json',
    'electron/main.cjs',
    'src/App.jsx',
    'src/styles.css',
    'scripts/verify-production.cjs',
    'electron/evaluation-framework.cjs',
    '.gitignore'
  ];
  for (const file of files) {
    if (!fs.existsSync(path.join(rootDir, file))) continue;
    const text = readText(file);
    for (const term of terms) {
      assert(!new RegExp(term, 'i').test(text), `${file} has no legacy preview term ${term}`);
    }
  }
}

function main() {
  for (const file of requiredFiles) assertFile(file);

  const manifest = JSON.parse(fs.readFileSync(path.join(vendorDir, 'manifest.json'), 'utf8'));
  assert(manifest.schema === 'ecorex.vue-office.vendor.v1', 'vendor manifest schema is current');
  assert(Array.isArray(manifest.assets) && manifest.assets.length >= 6, 'vendor manifest lists copied assets');

  const pkg = packageJson();
  const extraResources = Array.isArray(pkg.build?.extraResources) ? pkg.build.extraResources : [];
  const resource = extraResources.find((entry) => String(entry.to || '').replace(/\\/g, '/') === 'vue-office');
  assert(Boolean(resource), 'electron-builder extraResources includes vue-office');
  assert(resource?.from === 'vendor/vue-office', 'vue-office extraResources source is vendor/vue-office');
  assert(Array.isArray(resource?.filter) && resource.filter.includes('js-preview-lib/**/*'), 'vue-office extraResources filter keeps static assets only');

  const mainSource = readText('electron/main.cjs');
  assert(mainSource.includes("const VUE_OFFICE_VENDOR_DIR_NAME = 'vue-office'"), 'main process uses vue-office vendor root');
  assert(mainSource.includes('function ensureVueOfficePreviewServer'), 'main process serves vue-office viewer locally');
  assert(mainSource.includes("renderMode: 'vue-office'"), 'preview IPC returns vue-office render mode');
  assert(mainSource.includes("'.pptx'") && mainSource.includes("'pptx'"), 'preview IPC routes PPTX through the local vue-office viewer');
  assert(!/\bspawn\([^)]*vue-office/i.test(mainSource), 'vue-office preview does not spawn a sidecar process');

  const appSource = readText('src/App.jsx');
  assert(appSource.includes("preview.renderMode === 'vue-office'"), 'renderer has vue-office preview branch');
  assert(appSource.includes('artifact-vue-office-frame'), 'renderer uses vue-office iframe class');
  const viewerSource = fs.readFileSync(path.join(vendorDir, 'index.html'), 'utf8');
  assert(viewerSource.includes('pptx-preview.umd.js'), 'viewer loads the PPTX browser runtime');
  assert(viewerSource.includes('scrollbar-color') && viewerSource.includes('x-spreadsheet-scrollbar'), 'viewer applies EcoreX scrollbar styling inside document previews');
  const excelRuntime = fs.readFileSync(path.join(vendorDir, 'js-preview-lib', 'excel.umd.js'), 'utf8');
  assert(excelRuntime.includes('(t.drawings[i]&&t.drawings[i].anchors||[]).forEach'), 'Excel runtime tolerates missing drawing anchors');
  assert(excelRuntime.includes('(e&&e.anchors||[]).forEach'), 'Excel runtime tolerates empty drawing models');

  assertNoLegacyTerms();

  if (process.exitCode) process.exit(process.exitCode);
}

main();

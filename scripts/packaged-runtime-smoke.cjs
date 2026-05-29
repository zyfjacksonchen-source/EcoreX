const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { chromium } = require('@playwright/test');

const repoRoot = path.resolve(__dirname, '..');
const packagedExe = path.join(repoRoot, 'release', 'win-unpacked', 'EcoreX Agent.exe');
const secretEnvKeys = [
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_AUTH_TOKEN',
  'OPENAI_API_KEY',
  'ECOREX_LICENSE_KEY',
  'ANTHROPIC_BASE_URL',
  'OPENAI_BASE_URL',
  'ECOREX_REAL_MODEL_API_KEY'
];

function addNoProxy() {
  const entries = new Set(String(process.env.NO_PROXY || process.env.no_proxy || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean));
  entries.add('127.0.0.1');
  entries.add('localhost');
  process.env.NO_PROXY = [...entries].join(',');
  process.env.no_proxy = process.env.NO_PROXY;
}

function tempPaths() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ecorex-packaged-runtime-'));
  return {
    root,
    appData: path.join(root, 'AppData', 'Roaming'),
    localAppData: path.join(root, 'AppData', 'Local'),
    temp: path.join(root, 'Temp'),
    userData: path.join(root, 'UserData')
  };
}

function makeEnv(paths) {
  const env = {
    ...process.env,
    APPDATA: paths.appData,
    LOCALAPPDATA: paths.localAppData,
    TEMP: paths.temp,
    TMP: paths.temp,
    ECOREX_E2E: '1',
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true'
  };
  delete env.ELECTRON_RUN_AS_NODE;
  for (const key of secretEnvKeys) delete env[key];
  return env;
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
      });
      response.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.setTimeout(1000, () => request.destroy(new Error('timeout')));
  });
}

async function waitForDebugPort(port) {
  const deadline = Date.now() + 90_000;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      return await getJson(`http://127.0.0.1:${port}/json/version`);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
  throw lastError || new Error('Timed out waiting for packaged DevTools endpoint.');
}

async function findAppPage(browser) {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const pages = browser.contexts().flatMap((context) => context.pages());
    for (const page of pages) {
      const hasFrame = await page.locator('.app-frame').count().catch(() => 0);
      if (hasFrame) {
        page.setDefaultTimeout(10_000);
        return page;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Timed out waiting for packaged renderer page.');
}

async function login(page) {
  await page.locator('[data-testid="login-email-input"]').waitFor({ state: 'visible' });
  await page.locator('[data-testid="login-email-input"]').fill('e2e.owner@ecorex.local');
  await page.locator('[data-testid="login-secret-input"]').fill('EcoreX123!');
  await page.locator('[data-testid="login-submit-button"]').click();
  await page.locator('[data-testid="app-shell"]').waitFor({ state: 'visible', timeout: 20_000 });
}

function crc32(buffer) {
  const table = crc32.table || (crc32.table = Array.from({ length: 256 }, (_item, index) => {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    return value >>> 0;
  }));
  let crc = 0xffffffff;
  for (const byte of buffer) crc = table[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function dosTimeParts(date = new Date()) {
  const time = (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2);
  const day = date.getDate();
  const month = date.getMonth() + 1;
  const year = Math.max(1980, date.getFullYear()) - 1980;
  return { time, date: (year << 9) | (month << 5) | day };
}

function createStoreZip(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  const stamp = dosTimeParts();
  for (const [name, value] of entries) {
    const nameBytes = Buffer.from(name, 'utf8');
    const data = Buffer.isBuffer(value) ? value : Buffer.from(String(value), 'utf8');
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt16LE(stamp.time, 10);
    local.writeUInt16LE(stamp.date, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, nameBytes, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(stamp.time, 12);
    central.writeUInt16LE(stamp.date, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBytes);
    offset += local.length + nameBytes.length + data.length;
  }
  const centralSize = centralParts.reduce((sum, part) => sum + part.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...localParts, ...centralParts, end]);
}

function xmlEscape(value = '') {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function createDocx(text) {
  return createStoreZip([
    ['[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'],
    ['word/document.xml', `<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>${xmlEscape(text)}</w:t></w:r></w:p></w:body></w:document>`]
  ]);
}

function createXlsx(rows) {
  const rowXml = rows.map((row, rowIndex) => `<row r="${rowIndex + 1}">${row.map((cell, colIndex) => {
    const column = String.fromCharCode(65 + colIndex);
    return `<c r="${column}${rowIndex + 1}" t="inlineStr"><is><t>${xmlEscape(cell)}</t></is></c>`;
  }).join('')}</row>`).join('');
  return createStoreZip([
    ['[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'],
    ['xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="RuntimeSmoke" sheetId="1" r:id="rId1"/></sheets></workbook>'],
    ['xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'],
    ['xl/worksheets/sheet1.xml', `<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${rowXml}</sheetData></worksheet>`]
  ]);
}

function createPptx(title, body) {
  const textShape = (id, name, text, x, y, w, h, fontSize) => `
      <p:sp>
        <p:nvSpPr><p:cNvPr id="${id}" name="${name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="${x}" y="${y}"/><a:ext cx="${w}" cy="${h}"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          <a:noFill/><a:ln><a:noFill/></a:ln>
        </p:spPr>
        <p:txBody>
          <a:bodyPr wrap="square"><a:spAutoFit/></a:bodyPr>
          <a:lstStyle/>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="${fontSize}"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill></a:rPr><a:t>${xmlEscape(text)}</a:t></a:r>
            <a:endParaRPr lang="en-US" sz="${fontSize}"/>
          </a:p>
        </p:txBody>
      </p:sp>`;
  const emptyShapeTree = `
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>`;
  const themeXml = `<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="EcoreX">
  <a:themeElements>
    <a:clrScheme name="EcoreX">
      <a:dk1><a:srgbClr val="111111"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="1F2937"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2>
      <a:accent1><a:srgbClr val="FF5A00"/></a:accent1><a:accent2><a:srgbClr val="2563EB"/></a:accent2>
      <a:accent3><a:srgbClr val="16A34A"/></a:accent3><a:accent4><a:srgbClr val="9333EA"/></a:accent4>
      <a:accent5><a:srgbClr val="EAB308"/></a:accent5><a:accent6><a:srgbClr val="0F766E"/></a:accent6>
      <a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="9333EA"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="EcoreX">
      <a:majorFont><a:latin typeface="Arial"/></a:majorFont>
      <a:minorFont><a:latin typeface="Arial"/></a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="EcoreX">
      <a:fillStyleLst><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="9525"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:prstDash val="solid"/></a:ln>
        <a:ln w="12700"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill><a:prstDash val="solid"/></a:ln>
        <a:ln w="19050"><a:solidFill><a:schemeClr val="accent2"/></a:solidFill><a:prstDash val="solid"/></a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
      <a:bgFillStyleLst><a:solidFill><a:schemeClr val="bg1"/></a:solidFill></a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
</a:theme>`;
  return createStoreZip([
    ['[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>'],
    ['ppt/presentation.xml', '<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId2"/></p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/><p:defaultTextStyle><a:defPPr><a:defRPr lang="en-US" sz="2400"/></a:defPPr></p:defaultTextStyle></p:presentation>'],
    ['ppt/_rels/presentation.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/></Relationships>'],
    ['ppt/slides/slide1.xml', `<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld name="RuntimeSmoke"><p:spTree>${emptyShapeTree}${textShape(2, 'Title', title, 914400, 685800, 10363200, 914400, 3600)}${textShape(3, 'Body', body, 914400, 2057400, 10363200, 914400, 2400)}</p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>`],
    ['ppt/slides/_rels/slide1.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'],
    ['ppt/slideLayouts/slideLayout1.xml', `<?xml version="1.0" encoding="UTF-8"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" type="blank" preserve="1">
  <p:cSld name="Blank"><p:spTree>${emptyShapeTree}</p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>`],
    ['ppt/slideLayouts/_rels/slideLayout1.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>'],
    ['ppt/slideMasters/slideMaster1.xml', `<?xml version="1.0" encoding="UTF-8"?>
<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld><p:spTree>${emptyShapeTree}</p:spTree></p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>
</p:sldMaster>`],
    ['ppt/slideMasters/_rels/slideMaster1.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>'],
    ['ppt/theme/theme1.xml', themeXml]
  ]);
}

function createPdf(text) {
  const stream = `BT /F1 18 Tf 72 720 Td (${text.replace(/[()\\]/g, ' ')}) Tj ET`;
  const objects = [
    '1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n',
    '2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n',
    '3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n',
    '4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n',
    `5 0 obj << /Length ${stream.length} >> stream\n${stream}\nendstream endobj\n`
  ];
  let body = '%PDF-1.4\n';
  const offsets = [0];
  for (const object of objects) {
    offsets.push(Buffer.byteLength(body, 'utf8'));
    body += object;
  }
  const xrefOffset = Buffer.byteLength(body, 'utf8');
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (let index = 1; index < offsets.length; index += 1) body += `${String(offsets[index]).padStart(10, '0')} 00000 n \n`;
  body += `trailer << /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  return Buffer.from(body, 'utf8');
}

function createRuntimeFiles(workspaceRoot) {
  const dir = path.join(workspaceRoot, `packaged-runtime-smoke-${Date.now()}`);
  fs.mkdirSync(dir, { recursive: true });
  const marker = 'ECOREX_RUNTIME_PREVIEW_OK';
  const files = [
    ['txt', 'sample.txt', Buffer.from(`${marker}\nplain text preview line\n`, 'utf8')],
    ['md', 'sample.md', Buffer.from(`# ${marker}\n\n- markdown preview\n`, 'utf8')],
    ['json', 'sample.json', Buffer.from(JSON.stringify({ marker, value: 42 }, null, 2), 'utf8')],
    ['csv', 'sample.csv', Buffer.from(`name,value\n${marker},42\n`, 'utf8')],
    ['html', 'sample.html', Buffer.from(`<!doctype html><html><body><section><h1>${marker}</h1><p>HTML sandbox preview</p></section></body></html>`, 'utf8')],
    ['image', 'sample.png', Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axpRbkAAAAASUVORK5CYII=', 'base64')],
    ['pdf', 'sample.pdf', createPdf(marker)],
    ['docx', 'sample.docx', createDocx(`${marker} Word document`)],
    ['xlsx', 'sample.xlsx', createXlsx([['marker', 'metric'], [marker, '42']])],
    ['pptx', 'sample.pptx', createPptx(marker, 'PPT runtime preview')]
  ];
  return {
    dir,
    marker,
    files: files.map(([kind, name, content]) => {
      const filePath = path.join(dir, name);
      fs.writeFileSync(filePath, content);
      return { kind, name, path: filePath, size: content.length };
    })
  };
}

function assertPreviewResult(file, preview, marker) {
  if (!preview || preview.ok !== true || preview.previewable !== true) {
    throw new Error(`Preview failed for ${file.name}: ${JSON.stringify(preview)}`);
  }
  if (['txt', 'md', 'json', 'csv'].includes(file.kind) && !String(preview.content || preview.text || '').includes(marker)) {
    throw new Error(`Text preview did not read marker for ${file.name}: ${JSON.stringify(preview).slice(0, 500)}`);
  }
  if (file.kind === 'html') {
    if (preview.renderMode !== 'sandbox-srcdoc' || !String(preview.content || '').includes(marker)) {
      throw new Error(`HTML preview is not sandbox-srcdoc with marker: ${JSON.stringify(preview).slice(0, 500)}`);
    }
    if (preview.sandbox?.allowScripts !== false) throw new Error(`HTML preview should disable scripts: ${JSON.stringify(preview.sandbox)}`);
  }
  if (file.kind === 'image' && (preview.renderMode !== 'image' || !String(preview.previewUrl || preview.dataUrl || '').startsWith('data:image/png;base64,'))) {
    throw new Error(`Image preview is not inline native image data: ${JSON.stringify(preview).slice(0, 500)}`);
  }
  if (['pdf', 'docx', 'xlsx', 'pptx'].includes(file.kind)) {
    if (preview.renderMode !== 'vue-office' || !preview.previewUrl || preview.metadata?.previewEngine !== 'vue-office') {
      throw new Error(`Office/PDF preview is not using vue-office native-like viewer for ${file.name}: ${JSON.stringify(preview).slice(0, 500)}`);
    }
  }
}

async function verifyVueOfficePreview(context, file, preview) {
  const page = await context.newPage();
  try {
    await page.goto(preview.previewUrl, { waitUntil: 'domcontentloaded', timeout: 20_000 });
    await page.waitForFunction(() => {
      const status = document.querySelector('#status');
      const viewer = document.querySelector('#viewer');
      if (!viewer) return false;
      if (status?.classList.contains('error')) return true;
      return status?.classList.contains('ready') && (viewer.children.length > 0 || String(viewer.textContent || '').trim().length > 0);
    }, null, { timeout: 20_000 });
    const detail = await page.evaluate(() => {
      const status = document.querySelector('#status');
      const viewer = document.querySelector('#viewer');
      return {
        statusClass: status?.className || '',
        statusText: status?.textContent || '',
        viewerClass: viewer?.className || '',
        childCount: viewer?.children.length || 0,
        fallback: Boolean(viewer?.querySelector('.office-fallback')),
        pptSlides: viewer?.querySelectorAll('.pptx-preview-slide-wrapper').length || 0,
        text: String(viewer?.innerText || viewer?.textContent || '').slice(0, 500)
      };
    });
    if (detail.statusClass.includes('error')) {
      throw new Error(`vue-office rendered error for ${file.name}: ${JSON.stringify(detail)}`);
    }
    if (!detail.statusClass.includes('ready') || detail.childCount < 1) {
      throw new Error(`vue-office did not render user-visible content for ${file.name}: ${JSON.stringify(detail)}`);
    }
    if (file.kind === 'pptx' && (detail.fallback || detail.pptSlides < 1)) {
      throw new Error(`PPTX preview fell back instead of rendering slides: ${JSON.stringify(detail)}`);
    }
    return detail;
  } finally {
    await page.close().catch(() => {});
  }
}

async function runFilePreviewSmoke(browser, page) {
  const setup = await page.evaluate(async () => {
    const settings = await window.ecorex.getSettings();
    return { workspaceRoot: settings?.settings?.workspaceRoot || settings?.workspaceRoot || '' };
  });
  if (!setup.workspaceRoot) throw new Error('Workspace root was not available in packaged app.');
  const fixture = createRuntimeFiles(setup.workspaceRoot);
  const listResult = await page.evaluate((relativePath) => window.ecorex.listWorkspace({ relativePath }), path.basename(fixture.dir));
  const listedNames = (listResult?.entries || []).map((entry) => entry.name).sort();
  for (const file of fixture.files) {
    if (!listedNames.includes(file.name)) throw new Error(`Workspace list did not show created file ${file.name}: ${JSON.stringify(listResult)}`);
  }
  const previews = await page.evaluate(async (files) => {
    const results = [];
    for (const file of files) {
      const preview = await window.ecorex.previewFile({ path: file.path, filePath: file.path, name: file.name });
      results.push({ file, preview });
    }
    return results;
  }, fixture.files);
  for (const { file, preview } of previews) assertPreviewResult(file, preview, fixture.marker);
  const rich = [];
  const previewBrowser = await chromium.launch({ headless: true });
  try {
    const context = await previewBrowser.newContext();
    for (const { file, preview } of previews.filter((item) => ['pdf', 'docx', 'xlsx', 'pptx'].includes(item.file.kind))) {
      rich.push({ file: file.name, detail: await verifyVueOfficePreview(context, file, preview) });
    }
  } finally {
    await previewBrowser.close().catch(() => {});
  }
  return {
    dir: fixture.dir,
    listed: listedNames,
    previews: previews.map(({ file, preview }) => ({
      name: file.name,
      kind: file.kind,
      renderMode: preview.renderMode,
      previewable: preview.previewable,
      previewEngine: preview.metadata?.previewEngine || '',
      contentHasMarker: String(preview.content || preview.text || '').includes(fixture.marker),
      hasPreviewUrl: Boolean(preview.previewUrl)
    })),
    rich
  };
}

async function runPromptAndWait(page, payload, timeoutMs) {
  return page.evaluate(async ({ payload, timeoutMs }) => {
    const terminalKinds = new Set(['done', 'error', 'cancelled', 'timeout']);
    const events = [];
    let stop = null;
    const waitForTerminal = new Promise((resolve) => {
      const handler = (incoming) => {
        const batch = Array.isArray(incoming) ? incoming : (incoming?.events || [incoming]);
        for (const event of batch) {
          if (!event || event.sessionId !== payload.sessionId) continue;
          events.push({
            kind: event.kind,
            status: event.status,
            toolName: event.toolName || '',
            text: event.text || event.textPreview || '',
            tools: Array.isArray(event.tools) ? event.tools.map((tool) => ({ name: tool?.name || '', input: tool?.input || {} })) : []
          });
          if (terminalKinds.has(String(event.kind || '').toLowerCase())) resolve({ terminal: events[events.length - 1], events });
        }
      };
      if (typeof window.ecorex.onAgentEvents === 'function') stop = window.ecorex.onAgentEvents(handler);
      else if (typeof window.ecorex.onAgentEvent === 'function') stop = window.ecorex.onAgentEvent(handler);
    });
    const start = await window.ecorex.runPrompt(payload);
    const terminal = await Promise.race([
      waitForTerminal,
      new Promise((resolve) => setTimeout(() => resolve({ timeout: true, events }), timeoutMs))
    ]);
    if (typeof stop === 'function') stop();
    return { start, ...terminal };
  }, { payload, timeoutMs });
}

async function runWebSearchSmoke(page) {
  const sessionId = `packaged-web-search-${Date.now()}`;
  const prompt = [
    '请执行一次真实联网搜索验证，必须使用联网搜索工具。',
    '搜索关键词：EcoreX Agent ecoreai.cn',
    '请直接给出你搜索到的结果标题、链接或可核验结论。',
    '不要只凭记忆回答。'
  ].join('\n');
  const result = await runPromptAndWait(page, {
    sessionId,
    prompt,
    rawPrompt: prompt,
    userPrompt: prompt,
    model: 'gpt-5.5',
    timeoutMs: 8 * 60 * 1000
  }, 8 * 60 * 1000 + 20_000);
  const allText = (result.events || []).map((event) => [event.toolName, event.text, ...(event.tools || []).map((tool) => tool.name)].join(' ')).join('\n');
  const sawSearchTool = /(WebSearch|联网检索|准备联网检索|search)/i.test(allText);
  const finalText = (result.events || []).map((event) => event.text || '').join('\n');
  if (!result.start?.ok) throw new Error(`Web search run did not start: ${JSON.stringify(result.start)}`);
  if (result.timeout) throw new Error(`Web search run timed out. Events: ${allText.slice(-1200)}`);
  if (!result.terminal || result.terminal.kind !== 'done') throw new Error(`Web search run did not finish successfully: ${JSON.stringify(result.terminal)}`);
  if (!sawSearchTool) throw new Error(`Web search tool was not observed in events: ${allText.slice(-2000)}`);
  if (!/(https?:\/\/|ecoreai|EcoreX|芯助手|登录|search|result)/i.test(finalText)) {
    throw new Error(`Web search final answer did not include a user-visible result: ${finalText.slice(-2000)}`);
  }
  return {
    sessionId,
    eventCount: result.events?.length || 0,
    terminal: result.terminal,
    sawSearchTool,
    finalPreview: finalText.slice(-1200)
  };
}

async function main() {
  addNoProxy();
  if (!fs.existsSync(packagedExe)) {
    throw new Error(`Packaged app not found: ${packagedExe}. Run npm run dist first.`);
  }
  const paths = tempPaths();
  for (const dir of [paths.appData, paths.localAppData, paths.temp, paths.userData]) fs.mkdirSync(dir, { recursive: true });
  const port = 9900 + Math.floor(Math.random() * 400);
  const child = spawn(packagedExe, [`--remote-debugging-port=${port}`, `--user-data-dir=${paths.userData}`], {
    cwd: path.dirname(packagedExe),
    env: makeEnv(paths),
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: false
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });
  try {
    const version = await waitForDebugPort(port);
    const browser = await chromium.connectOverCDP(version.webSocketDebuggerUrl || `http://127.0.0.1:${port}`);
    try {
      const page = await findAppPage(browser);
      await page.bringToFront();
      await login(page);
      const filePreview = await runFilePreviewSmoke(browser, page);
      const webSearch = await runWebSearchSmoke(page);
      console.log(JSON.stringify({
        ok: true,
        packagedExe,
        filePreview,
        webSearch
      }, null, 2));
    } finally {
      await browser.close().catch(() => {});
    }
  } finally {
    child.kill();
    setTimeout(() => {
      try {
        fs.rmSync(paths.root, { recursive: true, force: true, maxRetries: 3, retryDelay: 300 });
      } catch {
        // The packaged app may release handles shortly after process exit.
      }
    }, 1000);
    const meaningfulStderr = stderr
      .split(/\r?\n/)
      .filter((line) => line.trim() && !line.includes('DevTools listening on'))
      .join('\n');
    if (meaningfulStderr) console.error(meaningfulStderr);
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});

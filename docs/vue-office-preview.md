# vue-office Preview Packaging

EcoreX uses a lightweight static vue-office preview runtime for PDF, DOCX, XLS, XLSX, XLSM, PPTX, and PPTM files.

The packaged resource is `vendor/vue-office`, not the full upstream checkout. Refresh it from the local upstream source:

```powershell
npm run prepare:vue-office
npm run verify:vue-office
```

By default the prepare script reads:

```text
C:\vue-office-master\demo-cdn\js-preview-lib
```

Only these files are copied:

```text
vendor/vue-office/index.html
vendor/vue-office/manifest.json
vendor/vue-office/README.md
vendor/vue-office/js-preview-lib/docx.css
vendor/vue-office/js-preview-lib/docx.umd.js
vendor/vue-office/js-preview-lib/excel.css
vendor/vue-office/js-preview-lib/excel.umd.js
vendor/vue-office/js-preview-lib/pdf.umd.js
vendor/vue-office/js-preview-lib/pptx-preview.umd.js
```

At runtime Electron starts a loopback-only HTTP server on demand. The server serves the static viewer and a temporary tokenized file URL with `Cache-Control: no-store`. The viewer rejects non-local preview sources and runs inside the existing artifact iframe sandbox.

PPTX/PPTM use the browser-only `pptx-preview` runtime inside the same static viewer because upstream vue-office does not publish a PPTX js-preview CDN bundle. Legacy binary Office formats that vue-office cannot render stay inside the metadata fallback.

# EcoreX vue-office Vendor

This directory contains the lightweight static document preview runtime used by the Electron app.

- Source: `C:\vue-office-master\demo-cdn\js-preview-lib` by default.
- PPTX uses the browser-only `pptx-preview` runtime because upstream vue-office does not publish a PPTX js-preview CDN bundle.
- Included formats: PDF, DOCX, XLS, XLSX, XLSM, PPTX, PPTM.
- Excluded: the full upstream source tree, demos, test files, build cache, Java runtimes, and native office suites.

Refresh with:

```powershell
npm run prepare:vue-office
npm run verify:vue-office
```

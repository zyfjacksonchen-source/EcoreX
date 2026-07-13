# WebUI Chromium GA gate

This directory exercises the content-addressed production `dist/` through the
loopback-only GA Runtime. The Playwright context rejects every request outside
`http://127.0.0.1:4179`; page errors, console errors, missing axe, accessibility
violations, and incomplete viewport reports fail the run.

Run from `desktop/`:

```powershell
npm ci
npx playwright install chromium
npm run typecheck
npm run build
npm run test:v1
npm run test:e2e
```

The browser matrix covers 1440×900, 1024×768, 768×900, and 390×844 in light
and dark themes. Interaction coverage includes fitted image preview and bounded
zoom, keyboard/focus restoration, forced colors, reduced motion, and the touch
artifact action sheet with one visible 44px action target.

Local locked-toolchain verification on 2026-07-12 (not a release receipt):

- Node.js 22.23.1, npm 10.9.8, `@playwright/test` 1.61.1, Chromium headless shell 1228.
- The official Node archive matched SHA-256 `7df0bc9375723f4a86b3aa1b7cc73342423d9677a8df4538aca31a049e309c29`.
- Production bundle: 16 chunks; bundle gate passed; entry SHA-256 `0aa2c19358be01bccfc452d6177de6962cd82810cbdc5a53104e220162d1aced`.
- Web unit/contract suite: 144/144 passed with zero skipped tests.
- Chromium E2E suite: 36 tests with zero skipped tests, including the 10-case light/dark viewport matrix and two real-browser administrator-console contracts.
- `npm audit`: 0 known vulnerabilities.

Protected release runners must execute the commands again with the pinned
release Node.js and browser cache; this record is only reproducible local
development evidence.

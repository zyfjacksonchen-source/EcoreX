# Responsive GA browser harness

The responsive GA harness renders the production WebUI bundle inside a fixed,
same-origin iframe. It is test-only infrastructure in
`desktop/tools/ga-mock-server.mjs`; it does not add viewport or theme branches
to the production React application.

## Run it

```powershell
cd desktop
npm run build
npm run ga:serve -- --scenario=artifact --port=4179
```

Open `http://127.0.0.1:4179/__ga/viewport-matrix` to obtain the canonical JSON
matrix. Each entry has a directly openable relative `url`. The fixed matrix is:

- 1440 × 900, light and dark
- 1024 × 768, light and dark
- 768 × 900, light and dark
- 390 × 844, light and dark
- 320 × 568, light and dark

For example:

```text
/__ga/viewport?viewport=390x844&theme=dark&scenario=artifact
```

The wrapper reports the iframe's real `contentWindow.innerWidth/innerHeight`,
the document content width and horizontal overflow, the applied theme, visible
clickable labels that render on more than one line, and the visibility of
navigation, model selection, composer, task type and Artifact controls. It also
runs the pinned, development-only `axe-core` build against that rendered frame;
overflow, wrapped clickable text or any accessibility violation makes the
matrix item fail.
The same object is exposed on the wrapper as
`window.__ECOREX_GA_VIEWPORT_REPORT__` for an in-app Browser inspection after
the report changes from “正在等待” to JSON.

## Security contract

- `/` and ordinary static fallback routes retain `frame-ancestors 'none'` and
  `X-Frame-Options: DENY`.
- Only `/__ga/frame-app` serves the built index with
  `frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN`. Its external GA
  bootstrap runs before the production module and only fixes the whitelisted
  `light` or `dark` preference.
- The wrapper itself cannot be framed. It loads external CSS/JavaScript under a
  nonce-free CSP; it contains no inline style, inline script or event handler.
- Viewport, theme and scenario are exact allowlists. Unknown or duplicate
  parameters return 422. Unknown `/__ga/` paths return JSON 404 rather than
  falling through to the production application, which prevents recursive
  wrapper construction.
- Every harness, matrix, axe and frame response is `no-store`. The frame uses the
  same origin so the approved in-app Browser can inspect its DOM and inject
  accessibility tooling without changing the production CSP.

`npm run test:e2e` captures all ten rendered frames into the local Playwright
report and separately exercises keyboard/touch, forced-colors and
reduced-motion behavior. This Chromium evidence does not replace real
assistive-technology or physical-device review.

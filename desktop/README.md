# EcoreX WebUI

This historically named directory contains only the v1 React WebUI. It is a
thin, same-origin browser client of the local Python Runtime; it is not an
Electron/native desktop application and does not own agent execution, policy,
tools, artifacts, or update state.

## Development

```powershell
npm install
npm run typecheck
npm run test:v1
npm run dev
```

The development server listens on `127.0.0.1`.

For deterministic Replay UI acceptance without external side effects, build the
WebUI and run `npm run ga:serve -- --scenario=replay`. The GA Runtime exposes a
read-only verified Mock Replay snapshot and an explicitly confirmed,
idempotent Live Replay path; it never calls a model, tool, or connector.

## Production build

```powershell
npm run build
```

The build is deliberately two-stage:

1. Vite writes files carrying an explicit `.unhashed-<rollup-hash>` staging
   marker.
2. `tools/rehash-dist.mjs` resolves the asset dependency graph, rejects cycles,
   missing/orphan files, inline script/style and legacy overlay references,
   rewrites every asset reference, and atomically activates a content-addressed
   `dist`.

Every production asset name contains the first 16 hexadecimal characters of
the SHA-256 of its final bytes. `index.html` remains non-immutable, contains
exactly one `<!--__ECOREX_RUNTIME_CONFIG__-->` marker in `<head>`, and refers
only to the final content-addressed files. Re-running the post-build stage is
idempotent.

The Python release builder scans the exact `index.html` plus `assets/`
allowlist again, creates and signs `web-manifest.json`, and the product server
verifies the release manifest, Web manifest, file sizes, and SHA-256 digests
before serving any byte.

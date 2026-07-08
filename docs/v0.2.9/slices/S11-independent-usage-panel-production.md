# S11 Independent Usage Panel Production Deploy

## Status

Completed.

## Intent

Deploy the v0.2.9 audit-surface upgrade to the production independent usage-panel slice at `/ecorex-agent/usage-panel/`.

## Finding

- The main v0.2.9 public release was deployed, but production nginx routes `/ecorex-agent/usage-panel/` to `/srv/ecorex-agent-usage-panel/current/`, not to `/srv/ecorex-agent-download/current/admin/`.
- The independent usage-panel slice was still pointing at the 2026-06-29 release and did not contain the v0.2.9 audit markers.
- Its API service on `127.0.0.1:18105` served the legacy `/api/data` contract and did not expose `/api/runtime-audit`.

## Implementation

- Added `deploy/ecorex-usage-panel/` as a tracked independent production slice.
- Added a v0.2.9 usage-panel page focused on:
  - user action categories
  - recent user actions
  - effective artifacts
  - thumbs-down feedback traces
  - collapsed technical request/event details
- Added an independent `usage_panel_api.py` service that keeps `/api/data` compatibility and exposes `/api/runtime-audit` plus `/api/state`.
- The service reuses the deployed Admin API `AdminStore.runtime_audit` projection against the production admin SQLite database.
- Deployed to `/srv/ecorex-agent-usage-panel/releases/20260705003507-v0.2.9-audit-panel` and atomically moved `/srv/ecorex-agent-usage-panel/current`.
- Restarted `ecorex-usage-panel-api.service`.

## Evidence

- `docs/v0.2.9/artifacts/production-independent-usage-panel-inspect.json`
- `docs/v0.2.9/artifacts/production-independent-usage-panel-deploy.json`
- `docs/v0.2.9/artifacts/production-independent-usage-panel-postdeploy-smoke.json`

## Verification

- Local:
  - `python -m py_compile deploy/ecorex-usage-panel/usage_panel_api.py`
  - `node --check deploy/ecorex-usage-panel/app.js`
- Production:
  - independent usage-panel API health returned `200`, version `0.2.9`
  - `/api/runtime-audit` returned `200`, version `0.2.9`, and includes `actionTypeCounts`, `userActions`, `effectiveArtifacts`, and `feedbackTraces`
  - `/ecorex-agent/usage-panel/` remains protected by Basic Auth with unauthenticated status `401`
  - deployed static files contain the v0.2.9 audit markers for effective artifacts and thumbs-down feedback traces

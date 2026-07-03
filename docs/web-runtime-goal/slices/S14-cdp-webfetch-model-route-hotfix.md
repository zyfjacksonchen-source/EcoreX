# S14: CDP / WebFetch / Model Route Hotfix

## Scope

Web-only hotfix for the production v0.2.6 package after user-reported issues:

- `browser is not available` / CDP not enabled by default.
- `web_fetch` image URLs rendered as binary text instead of becoming usable artifacts.
- Model-switch route observability still pointed at stale/default model in error paths.
- Old `agent_runs` SQLite tables would miss the new `model` / `provider` columns after upgrade.

## Changes

- Browser/CDP:
  - Web runtime defaults force `cdp_auto_launch`, `cdp_fallback`, and `persistent` on unless explicitly disabled by env.
  - Playwright Chromium is discovered from managed `PLAYWRIGHT_BROWSERS_PATH` / `$ECOREX_STATE_DIR/playwright-browsers` in Web runtime.
  - Browser read-only snapshot/get-text cannot start a new process. If it inspects an already reachable CDP endpoint, the created service disables auto-launch and fallback.
  - Web installer runs `python -m playwright install --with-deps chromium`, validates a real headless launch, and includes `playwright_chromium` in core baseline.

- WebFetch:
  - Low-risk HTTP/HTTPS fetch is allowed through the shared broker.
  - Image URLs and `image/*` / image suffix responses are saved as workspace artifacts.
  - Signed URLs are redacted in logs and tool errors.

- Runtime/observability:
  - `agent_runs` migrates legacy tables to add `model` / `provider`.
  - Web message runs write provider/model route into run ledger metadata.
  - Agent error diagnostics keep public redaction while logging typed route details.

- Packaging:
  - Synced runtime fixes into `desktop/runtime/ecorex-runtime` so Windows/macOS WebUI packages contain the same runtime behavior.
  - Updated WebUI local packager default to `0.2.6`.

## Validation

- Local focused tests:
  - `tests/test_web_runtime_goal.py`: 63 passed.
  - CDP/OCR external group: 97 passed.
  - Run ledger / permission focused group: 4 passed.
  - Cached enterprise model policy focused test: 1 passed.

- Production deploy:
  - `scripts/deploy-v024-production.py`: PASS.
  - Server `check-ecorex-web-release.sh`: PASS, including runtime baseline, release gate, systemd, HTTP, SSE.
  - Server live tool smoke: browser navigate, read-only no-launch, web_fetch image artifact, run-ledger legacy migration all PASS.
  - Download page browser smoke: v0.2.6 page loads and Windows/macOS links point at new package names.

## Status

PASS_WITH_NOTES. All reviewer blockers resolved. Remaining notes are non-blocking:

- Browser discovery still prefers external Chrome/Edge before managed Playwright Chromium when available; managed Chromium is the Web fallback and install gate.
- Historical `smoke-web-hotfix-contracts.py` still contains old v0.2.2 expectations and should stay out of current release gates or be rewritten separately.

# v0.1.16 Status

## Current Branch

`codex/ecorex-v0.1.16`

## Current Phase

Production-grade Windows local hand-test candidate is built, re-smoke-tested, and re-packaged after the hand-test fixes. Current goal scope is code push and local hand-test readiness; Windows code signing is intentionally deferred. Public production promotion is still gated by signing, macOS/WebUI/Linux artifact generation, production download-page deployment, and remaining automated UI performance/focus traces.

## Build Outputs

- Unpacked desktop: `desktop/release/win-unpacked/EcoreX.exe`
- Windows installer: `desktop/release/EcoreX_0.1.16_x64-setup.exe`
- Public release zip: `release-artifacts/EcoreX_0.1.16-public-release.zip`
- Public zip SHA256: `966942A3660F2573155973965FCEB1580D04E25F520164095E9FFFC679BCFD02`
- Windows installer SHA256: `A7984AA3EBA379A8ED4B1553DBD38481DBD82487B0A460E1FC131DCDC0E65D18`
- Windows installer signing: `NotSigned` (release promotion blocker, local hand-test allowed)

## Validation Evidence

- `npm run typecheck` from `desktop/`: PASS.
- `npm run build` from `desktop/`: PASS.
- `npm run package:dir` from `desktop/`: PASS.
- `npm run package:win` from `desktop/`: PASS.
- `python -m py_compile ...`: PASS for touched backend/runtime files.
- `python scripts/validate-ecorex-release-artifacts.py --desktop-dir desktop/release/win-unpacked --desktop-only --version 0.1.16`: PASS.
- `python scripts/validate-ecorex-release-artifacts.py --version 0.1.16`: PASS with Windows artifact ready and macOS/WebUI/Linux pending.
- Trim-boundary smoke for context persistence: PASS.
- Unpacked launch smoke on port `19161`: PASS; `/auth/check` ready, `/api/version` does not leak runtime details, cleanup PASS.
- Unsigned installer smoke on port `19162`: PASS; silent install, app launch, sidecar ready, version `0.1.16`, cleanup PASS.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py -q`: PASS, 107 passed.
- Final unpacked hand-test API smoke on port `9899`: PASS after final package; `/auth/check` ready, `/api/version` returns `0.1.16`, POST `/api/file-stat` and `/api/file` return the Windows image path `C:\Users\user\EcoreX\images\codex-final-smoke-preview.png` as `image/png`, `/api/diagnostics/bundle` returns privacy flags false with local paths redacted and no raw image path/name leak, `/api/active-requests` empty.
- Online production verification: BLOCKED; `https://www.ecoreai.cn/ecorex-agent/manifest.json` still returns `0.1.15`.

## Feature Status

| ID | Status | Notes |
| --- | --- | --- |
| F01 | PARTIAL | Incomplete code/table tail hiding and long-stream render throttling implemented. Needs automated 200k visual/perf trace. |
| F02 | PASS/PARTIAL | Global/plugin skill discovery and broader @skill search implemented. Invalid-skill diagnostics still need fixture coverage. |
| F03 | PARTIAL | Composer focus retry hardened. Needs 20-session P95 focus trace. |
| F04 | PARTIAL | Optimistic send/cancel, local completion filtering, post-done SSE short tail, and active-request filtering implemented. API smoke shows no false active requests after hand-test launch. Needs repeated non-first-round send visual smoke. |
| F05 | PASS/PARTIAL | Session-owned artifact paths, `/api/file` runtime preview URLs, missing artifact UI, and Windows absolute image preview bridge fixed. Full path matrix beyond the smoke image remains. |
| F06 | PARTIAL | Delta flush and render throttling reduce long-response work. Needs renderer trace/soak. |
| F07 | PASS/PARTIAL | Trim persistence, TTS seq attach, SSE locks, old boot stale pending grace implemented. Runtime kill/rapid-send smoke remains. |
| F08 | PASS/PARTIAL | Per-boot sidecar token, unauthenticated version privacy, and process-tree cleanup implemented and smoke-tested. Repeated P95 ready sampling remains. |
| F09 | PARTIAL/BLOCKED | Release validation, diagnostic bundle, pytest backend gate, and Windows local hand-test smoke pass. Code signing, production download-page deployment, non-Windows artifacts, and UI perf/focus traces remain blockers for public production promotion. |

## Open Risks

- Windows v0.1.16 installer is unsigned by current scope ("先不签名"). It is suitable for local hand testing but not public production promotion.
- macOS DMG, WebUI local packages, and Linux service package remain `pending` in `deploy/ecorex-site/manifest.json`.
- Production download page still serves v0.1.15 until the regenerated public zip is deployed with server credentials.
- Automated frontend visual/performance traces are not yet implemented.
- Windows UI automation via the Computer Use plugin was unavailable due to a missing internal module; manual-style validation was performed through packaged app launch plus runtime API/file/diagnostic checks.

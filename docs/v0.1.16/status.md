# v0.1.16 Status

## Current Branch

`codex/ecorex-v0.1.16`

## Current Phase

Production-grade local hand-test candidate is built and smoke-tested on Windows. Public promotion is still gated by code signing, macOS/WebUI/Linux artifact generation, pytest availability, and remaining automated UI performance/focus traces.

## Build Outputs

- Unpacked desktop: `desktop/release/win-unpacked/EcoreX.exe`
- Windows installer: `desktop/release/EcoreX_0.1.16_x64-setup.exe`
- Public release zip: `release-artifacts/EcoreX_0.1.16-public-release.zip`
- Public zip SHA256: `095341161203957949994790A5AD0FA867097CFED0DBA9B6CBE3D9291923B953`
- Windows installer SHA256: `90E45AFF3DE797B57D21D79C688C79DD3E697F5C3E1EE94EC191B951810BA5C0`
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
- `pytest`: BLOCKED because pytest is not installed in the available Python environments.

## Feature Status

| ID | Status | Notes |
| --- | --- | --- |
| F01 | PARTIAL | Incomplete code/table tail hiding and long-stream render throttling implemented. Needs automated 200k visual/perf trace. |
| F02 | PASS/PARTIAL | Global/plugin skill discovery and broader @skill search implemented. Invalid-skill diagnostics still need fixture coverage. |
| F03 | PARTIAL | Composer focus retry hardened. Needs 20-session P95 focus trace. |
| F04 | PARTIAL | Optimistic send/cancel and SSE/history recovery implemented. Needs repeated non-first-round send smoke. |
| F05 | PASS/PARTIAL | Session-owned artifact paths and missing artifact UI fixed. Full Windows path matrix remains. |
| F06 | PARTIAL | Delta flush and render throttling reduce long-response work. Needs renderer trace/soak. |
| F07 | PASS/PARTIAL | Trim persistence, TTS seq attach, SSE locks, old boot stale pending grace implemented. Runtime kill/rapid-send smoke remains. |
| F08 | PASS/PARTIAL | Per-boot sidecar token, unauthenticated version privacy, and process-tree cleanup implemented and smoke-tested. Repeated P95 ready sampling remains. |
| F09 | PARTIAL/BLOCKED | Release validation and docs exist. Full diagnostic bundle, pytest, code signing, and non-Windows artifacts remain blockers for public production promotion. |

## Open Risks

- Windows v0.1.16 installer is unsigned. It is suitable for local hand testing but not public production promotion.
- macOS DMG, WebUI local packages, and Linux service package remain `pending` in `deploy/ecorex-site/manifest.json`.
- Automated frontend visual/performance traces are not yet implemented.
- Pytest is unavailable locally, so backend automated test gate is compile/smoke based for this run.

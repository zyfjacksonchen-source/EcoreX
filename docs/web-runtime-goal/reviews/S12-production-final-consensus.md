# S12 Production Final Gate Consensus

Generated: 2026-07-02

## Scope

Production v0.2.6 final verification after the Web-only fixes for model switching, OCR URL handoff, provider logos, imagegen routing, `/client/model-config` proxy fallback, Win/Mac/Web package refresh, downloads, and deployment.

## Evidence

- `docs/v0.2.6/artifacts/production-deploy-online.json`: PASS.
- `docs/v0.2.6/artifacts/production-200-user-behavior.json`: 200/200 PASS.
- `docs/v0.2.6/artifacts/production-32-image-ocr-vision-toolchain.json`: 32/32 PASS.
- `docs/v0.2.6/artifacts/production-browser-ui-v026-smoke.json`: 18/18 PASS.
- `docs/v0.2.6/artifacts/production-agent-product-acceptance.json`: 450/450 PASS, 0 skips.
- `docs/web-runtime-goal/artifacts/S12-production-final-gate.json`: 15/15 PASS.

## Release Artifacts

- Windows WebUI: `B618A0980A91C539476BD402F0195A542C6F02CB549358C95F5125E21A4EE2D9`, size `126927348`, `updatedAt=2026-07-02`.
- macOS WebUI: `AA2D8169CFA1F6AEA4616DF384F73C2456C9C263D855D0BA11B89318A27B0E1D`, size `301168164`, `updatedAt=2026-07-02`.
- Web Linux service: `D2B6A5EBE92318019BA3A7C31BF2FF24742A3203A44698FD3352B26D1E3FDD70`, size `3953632`, `updatedAt=2026-07-02`.

## Multi-Agent Review

- Architecture/toolchain reviewer: PASS_WITH_NOTES, no blocking issue. Note: keep enterprise token cache as auth-only, not a second model state source.
- Safety/permissions/user-perception reviewer: PASS_WITH_NOTES, no blocking issue. Note: `/client/*` public proxy exception should stay documented and narrow.
- Runtime/image/OCR/toolchain reviewer: PASS_WITH_NOTES, no blocking issue. Note: OCR/vision fixture is smoke coverage, not a broad accuracy benchmark.
- Web UX/observability reviewer: PASS_WITH_NOTES after fix. Initial note found redaction status could be overwritten by quality gates; fixed in `scripts/smoke-v026-production-agent-product-acceptance.py`, then reran 450/450 PASS with empty redaction violations.
- Release/download reviewer: PASS_WITH_NOTES, no blocking issue. Note: use `release-artifacts/EcoreX_0.2.6-public-release.zip` and manifest-matched top-level packages as source of truth, not stale local `deploy/ecorex-site/downloads` contents.

## Consensus

PASS_WITH_NOTES accepted as final consensus. All notes are non-blocking after the redaction-gate fix and rerun.

Confirmed points:

- The production server is active/enabled and reports v0.2.6.
- Public manifest and local release artifacts agree for Windows WebUI, macOS WebUI, and Web Linux service.
- The 32-check production toolchain test confirms chat model switching is actually applied through `/api/models`, then image generation and image edit still route through `gpt-image-2-pro` native image APIs with no shell/Python fallback, then the original chat model is restored and verified.
- OCR now repairs common OCR URL scheme separator loss such as `https/example.com/...` and still supports bare-domain browser handoff.
- Browser UI smoke confirms provider logos load, model switching works through the UI, the model switch notice is a normal message, and unexpected console/resource errors are absent.
- `/client/model-config` no longer leaks a 404 into the browser console when the enterprise bridge is absent or returns not found; Web falls back to local model configuration through a typed response.
- The legacy 200-check manifest date check no longer hardcodes a calendar day; it validates artifact dates against the manifest release date and release floor.
- The final 450-check aggregate now enforces redaction failures after quality gates are computed, ignores child redaction metadata false positives, and avoids treating the ordinary check name `bridge sends bearer authorization` as a raw bearer token.

## Non-Blocking Notes

- The final gate now has a repeatable script: `scripts/smoke-v026-production-final-gate.py`.

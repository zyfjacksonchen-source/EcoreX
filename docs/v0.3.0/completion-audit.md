# e-Mate v0.3.0 Completion Audit

Date: 2026-08-05 (Asia/Shanghai)

## Local implementation status

| Slice | Status | Evidence |
|---|---|---|
| Five-robot home, dark/light themes, retained Composer and Settings | Passed | `design-qa.md`; same-viewport Browser comparison; frontend production build |
| Automatic image create/edit/follow-up routing | Passed | Central intent router and focused image-routing tests |
| Luna high default; GPT-5.5 removed from active code/catalog/current fixtures | Passed | Model suite `65 passed`; only a persisted legacy migration string and explicit negative guards remain |
| Immediate/streamed message flow, real reasoning/search/image states, Task List | Passed | Runtime reducer/event contracts and production composition |
| Continuous execution, browser scripts and empty-terminal recovery | Passed locally | Browser Pack schema aligned to handler; conversation facts survive format recovery; merged continuity gate `46 passed` |
| Usage/Audit shared projection | Passed in production | Asia/Shanghai `[2026-08-01, 2026-08-05)` exact KPI equality; canonical records `186`; production receipt in `artifacts/usage-audit-production-reconciliation.json` |
| Password/session revocation | Passed locally | Password/session focused suite; production self-change remains part of the upgrade smoke gate |
| Full-access migration and verified permission bridge | Passed locally | Backend completion audit and focused permission suite |
| ExtensionService Skill authority, progressive disclosure, controlled Runner and hot generation refresh | Passed locally | Digest-bound Python Runner, exact CAS/workspace sandbox roots, Skill authority/execution tests and Runtime composition |
| Capability Center / Astro Skill Hub and authenticated Runtime/Control Plane paths | Passed locally | Fixed upstream Astro page at `/ecorex-agent/skills/`; 53/53 source ZIPs locked; 28 canonical CAS packages + 5 native aliases; no Cow runtime dependency |
| v0.2.9.2-compatible update bridge, background verified notification and single 0.3.0 product version | Passed locally | Banner only after verified `awaiting_user`; durable Settings entry; health-polled same-tab replacement; frontend suite `214 passed` |
| Terminal download page, Windows shortcut convergence and retained-candidate upgrade | Passed locally | Approved five-robot e-Mate page; exact shortcut replaced in place; real 1.38 GB legacy migration; fixed Runtime served `/api/version` and addressed WebUI from retained candidate dependencies |
| Windows x64 and macOS universal WebUI package producers | Passed locally | Candidate-bound offline Bootstrap path; final Windows ZIP reopened independently; strict cross-platform receipts; Bootstrap Go package and package/manifest contract gate passed; no Electron, `.app` or native product UI |
| Generated Web contracts, TypeScript and production bundle | Passed | deterministic venv contract check; `tsc --noEmit`; frontend suite `216 passed`; 38 addressed assets; initial gzip `149.61 KiB` |

## Honest limitations

- Controlled Skill execution now uses the existing signed sandbox authority with exact Runtime, CAS revision and workspace roots. Node remains `missing_runtime` until a signed Node runtime is shipped; macOS `sandbox-exec`, Developer ID and notarization still require the protected Apple runner.
- The fixed Cow seed now mirrors all 53 audited source ZIPs and canonically packages 28, while 5 bind native e-Mate Skills. Twenty remain fail-closed because their runtime/effect/root/config contracts are genuinely ambiguous; none were guessed.
- Frontend contract commands use `desktop/tools/run-python.mjs`, which deterministically selects the repository virtual environment. `npm run contracts:check`, TypeScript, `214` frontend tests and the production build pass.
- The user explicitly authorized the current HTTP Luna BaseURL as a previously verified endpoint. A local metered acceptance request using the supplied current provider configuration returned HTTP `200`, reported `gpt-5.6-luna`, honored `reasoning_effort=high`, and returned provider usage. The acceptance harness remains fail-closed for HTTP unless explicitly authorized and bound to the exact provider host digest.
- The current source now has Candidate-bound Windows x64 and macOS universal WebUI producers, the isolated Go 1.26.5 Bootstrap suite passes, and the local Windows/package/rollback contracts pass. This checkout has ready current-user DPAPI release/publication keys for the explicit direct-waiver path, but no current-source cross-platform Candidate, protected production signer or Apple release runner. No production package, Developer ID/notary result or real two-platform upgrade receipt exists yet.
- The retained Windows drill used an ephemeral local signer and reused its frozen pre-fix package for diagnosis. Fixed source was then composed and served through that package's exact CPython/dependency closure and migrated slot; the immutable final package must still be regenerated by the production signing workflow. The user's installed v0.2.9.2 files and desktop shortcut were not modified.

## External release gates still open

1. Resolve the GitHub account billing lock. The main repository was changed from private to public on 2026-08-05 as authorized, and run `30978342014` then progressed from workflow startup into four concrete hosted jobs; every job was rejected before runner assignment with GitHub's exact annotation `The job was not started because your account is locked due to a billing issue.` Public visibility therefore removed the private-minute ambiguity but cannot bypass the account lock.
2. Choose either release admission route without weakening its checks: restore hosted Actions plus the protected signer authorities, or provide a real macOS build/notary host so the existing DPAPI direct-waiver path can consume fresh `windows-x64`, `macos-arm64` and `macos-x64` stages. The retained Windows drill predates the final root fixes and there are no current-source macOS stages, so it is not reusable as a release.
3. Provide Apple Developer ID/notary credentials, missing platform/live-acceptance execution authority and a scoped `EcoreX-installers` publication credential. The GitHub environments and production self-hosted Runner currently contain none of these external authorities.
4. Run staging/Candidate and the fixed Windows/macOS WebUI producers; execute real Windows x64 and macOS arm64/x64 upgrades from 0.2.9.2, including rollback.
5. Publish the signed 0.3.0 release so the live Runtime adopts the already verified `gpt-5.6-luna` high default; repeat digest-bound acceptance, management/download-page smoke and exact package hash/size readback, then atomically publish the public manifest last. Both serving origins still return the identical `0.2.9.2` manifest.

The long Goal remains active. Local implementation is not being represented as a production release until these external facts exist.

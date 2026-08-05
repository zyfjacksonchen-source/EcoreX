# e-Mate v0.3.0 macOS And Online Release Audit

Date: 2026-08-04

Host: Windows; no macOS artifact was available for `codesign`, `spctl`, `lipo`, `stapler` or `notarytool` inspection.

Mutation boundary: no workflow dispatch, push, upload, release edit, secret edit, CDN write or production activation was performed.

## Decision

The current checkout is **not yet releasable as the requested macOS universal WebUI update**. The product remains browser-based WebUI: the universal ZIP must carry both exact signed arm64/x64 Runtime/Bootstrap slots, select the host slot during install and launch the React WebUI in the browser. It must not introduce an Electron, SwiftUI or `.app` product shell. The repository has useful per-architecture v1 staging and signed publication primitives, but it still needs the WebUI ZIP producer, Apple distribution-signing/notarization evidence for its executable payloads, and a production workflow that publishes the v0.2.9.2-compatible legacy manifest after package readback.

The July 8 GitHub `v0.3.0` assets are historical bytes from an older source state. They must not be used as evidence for the current dirty checkout.

## Existing Verified Paths

| Surface | Existing path | What it actually proves |
|---|---|---|
| macOS native stage | `.github/workflows/ecorex-v1-platform-stage.yml` → `scripts/run-v1-repo-platform-stage.py` → `platform-staging/stager.py` and `platform-staging/native/macos/build.sh` | Separate `macos-arm64` and `macos-x64` Core/Packs/Bootstrap stages. It does not build one universal WebUI package. |
| macOS Mach-O closure | `platform-staging/stager.py` | Relocates dependencies, checks slices with `lipo`, then applies deterministic **ad-hoc** signatures and verifies copy/archive stability. This is not Developer ID signing, hardened-runtime distribution signing or notarization. |
| Release archives | `ecorex/release/builder.py`, `ecorex/release/candidate.py` | Deterministic, Ed25519-signed v1 ZIP artifacts for Windows x64, macOS arm64 and macOS x64. Supported targets do not include `macos/universal`. |
| v1 immutable publication | `.github/workflows/ecorex-v1-candidate.yml`, `.github/workflows/ecorex-v1-promote-candidate.yml`, `ecorex/release/github.py`, `ecorex/release/public_pointer.py` | Exact-byte GitHub/mirror/CDN publication and signed public-bootstrap pointer activation for v1 artifacts. |
| Legacy update pointer | `ecorex/release/legacy_webui_manifest.py` | Re-hashes verified Windows/macOS WebUI ZIPs and atomically replaces `legacy-pointer/manifest.json` only after both exist. Nginx/Caddy route only that pointer. |

## Missing Release Evidence And Integration

1. **No universal producer.** Active v1 targets are `windows/x64`, `macos/arm64`, and `macos/x64`. No active script/workflow builds `webui-macos-universal` or writes `emate.webui-build-receipt.v1`.
2. **No Apple distribution contract.** Repository search found no Developer ID identity, hardened-runtime signing or notarization submission in active workflows. The only current codesigning is ad-hoc `--sign - --timestamp=none` on individual Mach-O files. Stapling is not required or possible for the retained ZIP distribution; the accepted notarization receipt and exact signed inner bytes must instead be retained and verified.
3. **No current WebUI ZIP to inspect.** The required deliverable is not an app/DMG/PKG. It is the existing-format ZIP containing both exact architecture slots, Web assets and browser installer/launcher. No current-source ZIP, signed inner-byte inventory or accepted notarization receipt is available on this Windows host.
4. **Legacy pointer generator is orphaned from publication.** `legacy_webui_manifest.py` is referenced by tests but not by candidate/promotion workflows. The v1 publish/activate sequence never stages or atomically activates `/ecorex-agent/manifest.json` for v0.2.9.2 clients.
5. **The old dispatch helper is non-executable.** `scripts/dispatch-ecorex-macos-dmg-workflow.ps1` defaults to nonexistent `.github/workflows/ecorex-desktop-release.yml`, old version/ref values, and a missing `scripts/update-ecorex-desktop-release-manifest.ps1`. Dry-run proves it would dispatch that missing workflow.
6. **No current local artifacts.** Neither `release-artifacts` nor `deploy/ecorex-site/downloads` contains current-source 0.3.0 macOS universal/DMG/PKG output or a verified build receipt.
7. **Current source cannot be reproduced by CI yet.** Local `main` and remote `main` both point to `a202303531085d90deac3cd460e2582bd0c9b2d7`, while the v0.3.0 implementation is an extensive dirty/untracked working tree.

## CI And Credential Readiness

- The active public source is `zyfjacksonchen-source/EcoreX`; publication assets target `zyfjacksonchen-source/EcoreX-installers`. The CLI/session used for release must have workflow access to the former and scoped release-write access to the latter; access to the now-private legacy account is not accepted as publication authority.
- The new source repository is public. Active release workflows use exact repository, `refs/heads/main` and `ECOREX_V030_RELEASE_COMMIT_SHA == github.sha` admission; they no longer depend on the unavailable `github.ref_protected` signal. The same variables, environments and Runner bindings must still be configured and audited in the new repository before dispatch.
- Environment/Secret observations collected from the legacy repository do not transfer authority to `zyfjacksonchen-source/EcoreX`. The new repository must independently prove the required stage, signing, publication, production and Apple notarization configuration.
- The only registered self-hosted runner is online Linux ARM64 with release/cloud labels. There is no self-hosted Windows runner for the platform-stage or live-acceptance jobs. macOS jobs use GitHub-hosted arm64/Intel runners.
- Legacy-account workflow runs are retained only as historical diagnostics. No successful platform-stage, Candidate or promotion run in `zyfjacksonchen-source/EcoreX` is yet recorded by this audit.

## Current Online Readback

- `https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json` still returns product `EcoreX`, version `0.2.9.2`, updated `2026-07-07`; this is correctly unchanged because no deployment was authorized.
- GitHub installers release `v0.3.0` exists and contains a historical `EcoreX_0.3.0-webui-macos-universal.zip` (`652,443,004` bytes, GitHub digest `78c2b096...`). Its publication date is 2026-07-08 and it is not bound to the current source tree or current build receipt.

## Local Gate Results

- Legacy last-step atomic manifest and server-route tests: `2 passed`.
- Update manifest and release-asset publication tests: `24 passed`.
- Release exact-byte promotion/schema slice: `1 passed, 1 skipped`.
- Real macOS codesign tests: `2 skipped` on Windows, as expected; there is no exported artifact to substitute for them.
- ReleaseBuilder suite: `17 passed, 1 skipped`. The stale delta fixture now represents the four-part legacy `0.2.9.2` product baseline as valid signed-manifest SemVer `0.2.9+legacy.2`; the delta remains correctly ordered before `0.3.0`.
- Candidate pipeline suite exceeded the 90-second local bound and was terminated without a result; it is not counted as passing.
- PowerShell parser accepted the legacy dispatch helper. `actionlint`, PyYAML and a usable local Bash were unavailable, so no local workflow/actionlint or shell syntax pass is claimed. GitHub itself currently exposes the three v1 workflows as active YAML.

## Minimum Executable Release Path

1. Commit the current v0.3.0 source as one immutable release candidate and restore a real protected-ref authority (GitHub plan/repository setting or an equivalent signed admission that does not falsely require `github.ref_protected`).
2. Add an active macOS packaging job that consumes the exact arm64/x64 v1 Candidate artifacts and creates the legacy-compatible `EcoreX_0.3.0-webui-macos-universal.zip`; bind every contained Runtime/Bootstrap/Web byte and emit `emate.webui-build-receipt.v1` with the Windows package. The installer selects the exact slot by `uname -m`; it must not merge signed binaries or add a native UI shell.
3. On macOS, Developer ID-sign the executable payloads with hardened runtime before Candidate hashing, verify their exact signatures, submit the final ZIP with `notarytool --wait`, require an accepted notarization receipt, and repeat exact-byte verification on the downloadable ZIP. Stapling is intentionally not claimed for ZIP distribution.
4. Execute clean 0.2.9.2 → 0.3.0 upgrade/rollback tests on Windows x64 and both macOS architectures; retain source SHA, package SHA/size, signing identity, notarization result and installed-version/data-preservation receipts.
5. Configure the missing release/publication/control-plane secrets and signer variables, register the required Windows self-hosted runner, then run CI → platform stage → Candidate → verify-only promotion.
6. Upload immutable artifacts first and read back exact hashes from GitHub, mirror and CDN. Only after both legacy packages and all v1 artifacts match should the server stage `legacy_webui_manifest.py` output and atomically replace `legacy-pointer/manifest.json` as the final compatibility step.
7. Run public readback and real 0.2.9.2 update discovery after activation. Any hash, signing, health or upgrade failure must leave the 0.2.9.2 pointer active.

## Post-audit implementation note (2026-08-04)

The repository now contains a protected, fail-closed producer contract at `.github/workflows/emate-v030-macos-universal.yml` and `scripts/build-v030-macos-universal-webui.py`. It consumes the Ed25519-verified stable Candidate rather than raw stages, keeps exact arm64/x64 Candidate archives, selects the matching Bootstrap at install time, invokes its authenticated `--local-release` path, verifies Developer ID/hardened-runtime readback, requires an Apple `Accepted` ZIP notarization response, binds the matching production Windows receipt, and atomically emits the compatibility ZIP plus distribution/final WebUI receipts. ZIP stapling is correctly marked inapplicable; no `.app` or native product UI was introduced.

This closes the missing local producer/wiring gap only. It does not turn this audit into a release approval: real Apple credentials, protected Candidate bytes, both-platform upgrade evidence, upload readback and final production pointer activation are still external gates, and no workflow was dispatched or online manifest changed in this implementation slice.

# EcoreX v0.1.10 Release Manifest Notes

## Current Release Metadata

- Product: EcoreX
- Version: `0.1.10`
- Date: 2026-06-11
- Windows artifact: `desktop/release/EcoreX_0.1.10_x64-setup.exe`
- Windows size: `117,529,360` bytes
- Windows SHA256: `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`
- Public deployment zip: `release-artifacts/EcoreX_0.1.10-public-release.zip`
- Public deployment zip size: `120,277,051` bytes
- Public deployment zip SHA256: `EAD857656A7399DCCC7D5052049DF889D22BA0C4B38D25658DA04CB7D76571F1`
- Local git branch: `codex/ecorex-v0.1.10-productization`
- GitHub SSH push target prepared: `git@github.com:zhangyifanjackson-dotcom/EcoreX.git`
- GitHub HTTPS push target prepared: `https://github.com/zhangyifanjackson-dotcom/EcoreX.git`
- GitHub push status: succeeded via HTTPS token using Git for Windows `schannel` TLS backend.
- GitHub pushed branches: `main` and `codex/ecorex-v0.1.10-productization` both point to the same clean EcoreX v0.1.10 source snapshot.
- Usage/SSE GitHub source sync commit: `45c5a7dfc92be9933c8895732c36469ed3a85e4b`; created via GitHub Git Data API after normal Git transport hit transient port 443 failures.
- GitHub overwrite note: `main` was force-updated with lease to replace the previous repository contents, matching the product handoff requirement.
- Git bundle handoff: `release-artifacts/EcoreX_0.1.10-productization.bundle`, size `6,518,904`, SHA256 `B53B9CBB8276E9D5FF1D9A589571FA565D605DD9AD74730C27901A0BFE611A1A`.
- Git patch handoff: `release-artifacts/EcoreX_0.1.10-productization.patch`, size `15,109,034`, SHA256 `A2D8B5731648D2566F6E7571A8540A1DF0A8B732A29CDCAEA3BA83C2ABC0AA9C`.
- macOS arm64/x64 artifacts: metadata prepared; signing, notarization, and Gatekeeper validation skipped for this Windows round.
- Public host state: `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned HTTP 404 during the latest 2026-06-11 verification retry after proxy was enabled. Earlier verification had seen v0.1.7. The local release directory is v0.1.10, but production deployment/routing is still pending.

## Verification Evidence

- `npm run typecheck` passed in `desktop/`.
- `npm run build` passed in `desktop/`.
- `desktop/dist/index.html` uses relative `./assets/...` paths to avoid Electron `file://` white screen.
- Admin API temporary HTTP smoke passed:
  - `/state`
  - `/users`
  - `/client/auth/login`
  - `/client/quota/check`
  - `/model-credentials/global`
  - `/client/model-config`
  - `/client/events`
- Admin API security smoke confirmed `/client/model-config` returns 401 without a valid user token and returns model config only for an authenticated enterprise session.
- Admin static-site route smoke confirmed `/admin/api/*` works for health, state, user creation, client login, and quota checks.
- Release verification script now mirrors the same security boundary: client-key-only model config must return 401, and configured model delivery requires `-ClientUserToken`.
- Admin DOM static smoke confirmed all 28 `data-*` hooks are represented by JavaScript bindings/render logic.
- Edge headless visual smoke rendered the local download page and Admin page. It caught and fixed an Admin first-load modal bug caused by CSS overriding `[hidden]`; the follow-up screenshot verified a clean first load.
- Download directory hash smoke confirmed `deploy/ecorex-site/downloads/EcoreX_0.1.10_x64-setup.exe` matches the manifest hash and size.
- Agent stream usage normalizer smoke passed for `prompt_tokens`/`completion_tokens`, `input_tokens`/`output_tokens`, and camelCase usage envelopes.
- Renderer telemetry path now reports provider usage from SSE `done` once per completed turn, with estimated-token fallback; `done` replaces final assistant text instead of duplicating streamed deltas.
- Packaged Windows runtime contains `enterprise-policy.json` with the public EcoreX Admin API URLs and public desktop channel key; no model API key is embedded in the installer.
- `npm run stage:runtime:win` passed in `desktop/`.
- `npm run package:win:signed` passed in `desktop/` after retrying a transient Electron Builder download timeout.
- `scripts/smoke-installed-win.ps1 -Port 19142` passed:
  - installed app found: true
  - app started: true
  - sidecar ready: true
  - cleanup completed: true
- After the usage/SSE fix, full Electron Builder directory packaging hit transient GitHub 443 timeouts twice. The release was rebuilt by safely updating the existing `win-unpacked` app.asar/runtime, re-signing, and running `electron-builder --win nsis --x64 --prepackaged release/win-unpacked --publish never`; the regenerated setup exe signature is valid and installed smoke passed again.
- Cross-agent review completed for Admin API, Desktop UX, and Electron/runtime. Blocking findings were fixed before final packaging.
- Public verification script was run with `-SkipGitRemoteCheck`; it correctly failed because the public manifest route currently returns HTTP 404.
- `scripts/prepare-ecorex-public-release.ps1` generated a clean public deployment zip. Zip inspection confirmed it contains `site/manifest.json`, `site/downloads/EcoreX_0.1.10_x64-setup.exe`, `admin-api/ecorex_admin_api.py`, and `checksums.json`, excludes old v0.1.4 installers and `__pycache__`, and the zipped Windows installer hash matches `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`.
- `scripts/verify-ecorex-release.ps1 -LocalWindowsInstaller desktop\release\EcoreX_0.1.10_x64-setup.exe -SkipGitRemoteCheck` was rerun after the rebuild. It correctly still reports public blockers because `https://www.ecoreai.cn/ecorex-agent/manifest.json` returns HTTP 404.
- `scripts/verify-ecorex-release.ps1` now supports `-ExpectedGitHubCommit` for snapshot/API GitHub handoff verification, so release checks can validate remote `main` and `codex/ecorex-v0.1.10-productization` without requiring the local shallow CowAgent commit SHA to match the remote clean snapshot SHA. If Git transport is unstable, the verifier can fall back to GitHub refs API using `ECOREX_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN` from the environment.
- Git bundle verification passed: bundle contains `refs/heads/codex/ecorex-v0.1.10-productization` at `f7861062df11b88aa783ff1e736ca92253532363` and records a complete history.
- Second cross-agent audit smoke passed after the admin/desktop hardening pass:
  - `python -m py_compile deploy\ecorex-admin-api\ecorex_admin_api.py`
  - `npm run typecheck`
  - `npm run build`
  - Admin API HTTP security smoke covering admin auth, disabled demo users, login, password change, client-key gating for the password alias, trusted session device attribution, over-quota model denial, invalid client event denial, and raw event admin gate.
  - `desktop/scripts/smoke-renderer-visual.ps1` screenshot smoke for auth, light main, and dark main states.
- Latest Windows package rebuild passed `npm run package:win:signed`; Authenticode status is `Valid` for `release/win-unpacked/EcoreX.exe` and `release/EcoreX_0.1.10_x64-setup.exe`.
- Latest installed-app smoke passed: installer found, app started, sidecar ready, cleanup completed on port `19142`.
- Latest public handoff zip was regenerated after the second hardening pass with SHA256 `EAD857656A7399DCCC7D5052049DF889D22BA0C4B38D25658DA04CB7D76571F1`.
- Added server-side deployment helpers after the public route still returned 404: `scripts/install-ecorex-public-release.sh`, `deploy/ecorex-site/nginx/ecorex-agent.conf.example`, and `deploy/ecorex-admin-api/systemd/ecorex-admin-api.service.example`.
- Local Linux/WSL install smoke found and fixed a release-blocking handoff issue: Windows `Compress-Archive` had produced backslash zip entries that Linux extracted as literal backslash filenames. The release zip generator now writes `/` entries, the installer normalizes legacy entries, and temp install verified the expected release/current layout.
- Added `scripts/test-ecorex-v0.1.10-acceptance.ps1` as a consolidated acceptance harness for local package integrity, Linux install smoke, GitHub refs, and public route status. It supports `-AllowPublicBlocked` so current 404 routing remains visible without hiding local/package evidence.
- Live route diagnosis shows Caddy is the active server: Admin/API paths are reachable, but static `/ecorex-agent/*` is not. Added `deploy/ecorex-site/caddy/Caddyfile.example` and release-zip inclusion for the Caddy route template.
- Added `scripts/check-ecorex-server-release.sh`, copied by the install script into `$ADMIN_ROOT/server`, to verify release/current files, Admin API files, server helpers, and public route status from the server.
- Added import-safe Caddy route snippet `deploy/ecorex-site/caddy/ecorex-agent.routes.caddy` and release-zip inclusion, so existing Caddy site blocks can import only the EcoreX routes.

## Pending Release Steps

- Upload or sync the final Windows artifact to the public release/download host when ready. The local `deploy/ecorex-site/downloads/` directory is intentionally ignored by git.
- Use `docs/ecorex/v0.1.10/public-deployment-runbook.md` for the public host sync and post-deploy verification sequence.
- Run a human visual pass on the installed desktop UI for login, first chat, stop, paste attachment, quota block, and error telemetry.
- Run macOS arm64/x64 signing, notarization, Gatekeeper, and installed-app smoke later on a Mac.
- Public live model chat still needs to be checked after the production host serves v0.1.10.

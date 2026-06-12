# EcoreX v0.1.10 Release Manifest Notes

## Current Release Metadata

- Product: EcoreX
- Version: `0.1.10`
- Date: 2026-06-12
- Windows artifact: `desktop/release/EcoreX_0.1.10_x64-setup.exe`
- Windows size: `117,442,400` bytes
- Windows SHA256: `BE89ADCEAE56D8097D54B1394B2CA47E752F3BD93D51F3F8374D2E0B9F14A308`
- Public deployment zip: `release-artifacts/EcoreX_0.1.10-public-release.zip`
- Public deployment zip size: `120,192,890` bytes
- Public deployment zip SHA256: `DC8BE4CF01E77F7C40962E572A35638636D70448AF820E4DA5474AB4B724AE15`
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
- Public host state: `https://www.ecoreai.cn/ecorex-agent/manifest.json` serves v0.1.10. Production Caddy static routing was repaired on 2026-06-12; after the desktop enterprise-policy BOM fix, the public package was regenerated for redeploy.

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
- Windows runtime staging now writes `enterprise-policy.json` without UTF-8 BOM. Electron policy readers also strip a BOM before `JSON.parse`, covering manually placed override files.
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
- `scripts/prepare-ecorex-public-release.ps1` generated a clean public deployment zip. Zip inspection confirmed it contains `site/manifest.json`, `site/downloads/EcoreX_0.1.10_x64-setup.exe`, `admin-api/ecorex_admin_api.py`, and `checksums.json`, excludes old v0.1.4 installers and `__pycache__`, and the zipped Windows installer hash matches `BE89ADCEAE56D8097D54B1394B2CA47E752F3BD93D51F3F8374D2E0B9F14A308`.
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
- Latest installed-app smoke passed after the preload and enterprise-policy BOM fixes: installer found, app started, packaged policy was no-BOM, and CDP confirmed `window.ecorexDesktop.enterpriseLogin` and `enterpriseLogout` are available.
- Latest public handoff zip was regenerated after the desktop enterprise-policy BOM fix with SHA256 `DE7941408D79D663CF58057AFB97158C84FC40EF5558EB79385635236B9FCEB6`.
- Latest desktop UX correction rebuild passed `npm run package:win:signed` on 2026-06-12. The signed setup exe is `117,442,400` bytes with SHA256 `BE89ADCEAE56D8097D54B1394B2CA47E752F3BD93D51F3F8374D2E0B9F14A308`; the regenerated public handoff zip is `120,192,890` bytes with SHA256 `DC8BE4CF01E77F7C40962E572A35638636D70448AF820E4DA5474AB4B724AE15`.
- Added server-side deployment helpers after the public route still returned 404: `scripts/install-ecorex-public-release.sh`, `deploy/ecorex-site/nginx/ecorex-agent.conf.example`, and `deploy/ecorex-admin-api/systemd/ecorex-admin-api.service.example`.
- Local Linux/WSL install smoke found and fixed a release-blocking handoff issue: Windows `Compress-Archive` had produced backslash zip entries that Linux extracted as literal backslash filenames. The release zip generator now writes `/` entries, the installer normalizes legacy entries, and temp install verified the expected release/current layout.
- Added `scripts/test-ecorex-v0.1.10-acceptance.ps1` as a consolidated acceptance harness for local package integrity, Linux install smoke, GitHub refs, and public route status. It supports `-AllowPublicBlocked` so current 404 routing remains visible without hiding local/package evidence.
- Live route diagnosis shows Caddy is the active server: Admin/API paths are reachable, but static `/ecorex-agent/*` is not. Added `deploy/ecorex-site/caddy/Caddyfile.example` and release-zip inclusion for the Caddy route template.
- Added `scripts/check-ecorex-server-release.sh`, copied by the install script into `$ADMIN_ROOT/server`, to verify release/current files, Admin API files, server helpers, and public route status from the server.
- Added import-safe Caddy route snippet `deploy/ecorex-site/caddy/ecorex-agent.routes.caddy` and release-zip inclusion, so existing Caddy site blocks can import only the EcoreX routes.
- The acceptance-log verification note was first synced to GitHub snapshot commit `66139aebfaa6e613d3295aa427665f56af1c8e59` on both `main` and `codex/ecorex-v0.1.10-productization`; that note captured the earlier public `manifest.json` HTTP 404 before the 2026-06-12 server repair.
- Fixed the GitHub API fallback used by the release and acceptance verifiers so refs such as `codex/ecorex-v0.1.10-productization` are requested through singular slash-preserving `git/ref/{ref}` paths instead of `%2F`-encoded paths or plural `git/refs`; when env tokens are unset, the fallback can use Git credential helper tokens for private repository checks.
- Production `www.ecoreai.cn/ecorex-agent` was repaired on 2026-06-12. The server now serves v0.1.10 from `/srv/ecorex-agent-download/releases/20260612014244-v0.1.10`; Docker Caddy has the static read-only mount, and the Docker Admin API service was rebuilt with the v0.1.10 source and the packaged `ecorex-desktop-v0.1.10` client key.
- Public verification after the repair passed with 0 blockers: release verifier, consolidated acceptance harness, server-side `check-ecorex-server-release.sh`, capability policy key check, and a temporary public user create/login/model-config/quota/delete smoke all passed.

## Pending Release Steps

- Run a human visual pass on the installed desktop UI for login, first chat, stop, paste attachment, quota block, and error telemetry.
- Run macOS arm64/x64 signing, notarization, Gatekeeper, and installed-app smoke later on a Mac.
- Public live model chat should be checked from the installed desktop app with a real retained enterprise user; temporary public API login/model-config/quota smoke already passed.

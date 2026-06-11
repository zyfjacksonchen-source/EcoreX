# EcoreX Goal Ledger

## Active Goal

- Version: v0.1.10
- Objective: productize EcoreX desktop and admin for ordinary Windows/macOS users, while keeping the agent core compatible.
- Current branch/worktree note: `deploy/`, `desktop/`, Admin API, and EcoreX docs are active product files. Generated release/runtime folders remain ignored and should not be hand-edited as source of truth.

## Work Ownership

| Area | Owner | Write scope | Status | Notes |
| --- | --- | --- | --- | --- |
| Admin API | Main agent + review agent | `deploy/ecorex-admin-api/` | Implemented | User auth, password reset/change, quotas, filtered logs, authenticated global model config. |
| Admin Web | Main agent | `deploy/ecorex-site/admin/` | Implemented | User management, usage by user, error filters, model edit modal. |
| Desktop Renderer | Main agent + review agent | `desktop/src/` | Implemented | One-screen Codex-style layout, login, Chinese settings, composer UX, fixed approval bar. |
| Electron Runtime | Main agent + review agent | `desktop/electron/`, `desktop/scripts/` | Implemented | Login context, sanitized renderer session, policy cache, capability install feedback, Windows installed smoke. |
| Release/Docs | Main agent | `docs/ecorex/`, manifest/package metadata | Implemented | v0.1.10 records, acceptance evidence, artifact hash/size, macOS skipped boundary. |

## Guardrails

- Do not modify agent core behavior unless required for a desktop bridge contract and recorded here.
- Do not expose real model API keys in renderer, manifest, logs, or docs.
- Prefer soft-delete users so usage and error history remain auditable.
- Every visible button must either perform an action, open a useful state, or be disabled with a clear reason.
- Main desktop layout must fit one screen. Only the session list and chat transcript may scroll.
- macOS signing, notarization, and Gatekeeper validation are intentionally skipped in this Windows round and will be run later on a Mac.

## Running Notes

- 2026-06-11: Plan converted to implementation. Initial inspection found version sources still at `0.1.5`/`0.1.4`, Admin API without password/quota/login, and desktop UI concentrated in a large `App.tsx`.
- 2026-06-11: Admin API/Web, Desktop UI, Electron runtime bridge, capability feedback, and v0.1.10 metadata implemented.
- 2026-06-11: Blocking review findings fixed: model config now requires a user token, renderer no longer receives enterprise token, API bridge uses an exact route whitelist, file-open confirmation is fixed above the composer, local image thumbnails render, and disabled capability packs no longer show install actions.
- 2026-06-11: Windows installer `EcoreX_0.1.10_x64-setup.exe` built and signed locally. Installed-app smoke passed with sidecar ready. macOS signing/notarization/Gatekeeper remains intentionally skipped for later Mac execution.
- 2026-06-11: Admin route compatibility strengthened for `/admin/api/*` and `/api/admin/*`. Download page now formats artifact sizes, disables non-ready macOS downloads, and the local v0.1.10 Windows installer was copied into the ignored download directory with hash/size verified against `manifest.json`.
- 2026-06-11: Fixed an actual out-of-box gap: `stage-runtime-win.ps1` and `stage-runtime-mac.sh` now stage a default non-secret enterprise policy when no env override is provided. Admin API accepts the same default public desktop channel key unless `ECOREX_CLIENT_EVENT_KEY` overrides it. Rebuilt signed Windows installer with packaged `enterprise-policy.json`.
- 2026-06-11: Public release verification against `https://www.ecoreai.cn/ecorex-agent` still reports v0.1.7. Treat production deployment as a remaining external release step; do not mark the goal complete from local evidence alone.
- 2026-06-11: Local browser smoke with Edge rendered the v0.1.10 download and Admin pages. It exposed an Admin first-load empty modal caused by CSS overriding `[hidden]`; fixed in `deploy/ecorex-site/admin/admin.css` and verified with a second screenshot.
- 2026-06-11: Added `scripts/prepare-ecorex-public-release.ps1` and generated an initial `release-artifacts/EcoreX_0.1.10-public-release.zip` for server handoff. That initial zip SHA256 was `CE05311BE1FE949ACA1483349EC543E5A945C4D45211DF533EA1AA0B6F068429` and has been superseded by the later hardening build.
- 2026-06-11: Created local git branch `codex/ecorex-v0.1.10-productization` and local commit for the v0.1.10 productization work. Added remote `ecorex` -> `git@github.com:zhangyifanjackson-dotcom/EcoreX.git`; SSH push failed on this machine with `Permission denied (publickey)`, so HTTPS token push is the current GitHub handoff path.
- 2026-06-11: Generated offline Git handoff artifacts in `release-artifacts/`: `EcoreX_0.1.10-productization.bundle` and `.patch`. Bundle verification passed and can recreate the v0.1.10 productization branch from the pre-HTTPS-push handoff commit on a GitHub-authorized machine.
- 2026-06-11: GitHub HTTPS token push succeeded after switching Git for Windows to the `schannel` TLS backend. Because the local CowAgent checkout is shallow and could not push full history to a different repository, a clean source snapshot was exported and pushed as a root commit to both `main` and `codex/ecorex-v0.1.10-productization` in `zhangyifanjackson-dotcom/EcoreX`. The remote `main` update used `--force-with-lease` to intentionally replace the old repository contents with the EcoreX v0.1.10 snapshot.
- 2026-06-11: Fixed desktop usage accounting path after completion audit. Agent stream now normalizes provider `usage` envelopes, Web SSE `done` carries usage to the renderer, and renderer reports real token usage once per completed turn with estimated-token fallback. Also fixed SSE `done` handling so final text replaces the assistant bubble instead of duplicating streamed deltas, and Stop clears immediately after `done`.
- 2026-06-11: Rebuilt the Windows package after the usage/SSE fix via manual prepackaged NSIS path because full Electron Builder packaging hit transient GitHub 443 timeouts. That signed installer SHA256 was `14D57A4F15D2F99DDC04975D5E636707F648864665D4F3F4D5A011516626DB55`, size `120050856`, and has been superseded by the later hardening build.
- 2026-06-11: Synced the usage/SSE source fix to GitHub through the GitHub Git Data API because normal Git transport was still unstable on port 443. The source sync commit is `45c5a7dfc92be9933c8895732c36469ed3a85e4b`; documentation-only follow-ups may advance the remote refs beyond that commit.
- 2026-06-11: Strengthened `scripts/verify-ecorex-release.ps1` for the clean snapshot/API GitHub handoff path. It can now validate remote `main` and `codex/ecorex-v0.1.10-productization` against an expected remote commit instead of incorrectly requiring the local shallow CowAgent commit SHA to equal the remote snapshot SHA. It also falls back to GitHub refs API when `git ls-remote` is unstable and a token is provided through the environment.
- 2026-06-11: Second cross-agent audit closed additional blockers. Desktop stream now accepts provider `delta` frames and stream-level `error` frames, Ctrl/Cmd+Z writes the backend `user_seq` onto the original user bubble before deleting history, auth check failures no longer trap the user on the loading screen, paste/file failures show toast feedback, and local image preview URLs follow the active sidecar web port instead of assuming `9899`.
- 2026-06-11: Electron now gates `openPath` in the main process with the same local-file permission store used by the renderer approval card. The renderer can request a password change, but it still never receives the enterprise user token.
- 2026-06-11: Admin API now has an admin boundary. Admin routes require Basic auth, bearer token, or admin API key from environment configuration; CORS is same-origin or explicit allow-list only; fixed default users are disabled unless explicitly seeded with an environment password; client events require a valid user token; and over-quota users are denied model config before credentials are returned.
- 2026-06-11: Admin Web now surfaces mutating failures through an inline notice, can filter logs by time window, keeps soft-deleted users visible for usage/error history filters, and wraps model editing/reset/delete flows in handled promises.
- 2026-06-11: Added renderer visual smoke script `desktop/scripts/smoke-renderer-visual.ps1`. It injects a temporary desktop bridge mock into the built renderer and captures auth, light main, and dark main screenshots with Edge headless so white-screen/layout regressions are caught without a running sidecar.
- 2026-06-11: Rebuilt and re-signed the Windows installer after the second hardening pass. New installer SHA256 `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`, size `117529360`. Installed-app smoke passed with sidecar ready on port `19142`. Public release zip regenerated with server helpers included; SHA256 `DDF69409D5E3183644A11D11089E883419409BC705DFCFCD8C86CAA46359FD31`, size `120274162`.
- 2026-06-11: Public release verification was retried after proxy was enabled. `https://www.ecoreai.cn/ecorex-agent/manifest.json` now returns HTTP 404, so production deployment/routing is still the remaining external blocker.
- 2026-06-11: Server handoff was hardened after local Linux/WSL smoke found Windows `Compress-Archive` stored backslash zip entries. `prepare-ecorex-public-release.ps1` now writes zip entries with `/`, and `install-ecorex-public-release.sh` also normalizes legacy backslash entries. Temporary install smoke under `tmp/deploy-smoke` verified release `index.html`, `manifest.json`, `admin/index.html`, and the Windows installer exist after install.

## Known Follow-Up

- Deploy or point the product build at the production Admin API before final live enterprise login/model chat acceptance.
- Provider usage is now normalized when upstream returns a standard usage envelope; estimated tokens remain as fallback for providers that omit usage in streaming responses.
- Run the skipped macOS signing/notarization/Gatekeeper pass on Mac hardware.

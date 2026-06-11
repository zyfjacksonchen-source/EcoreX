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
- 2026-06-11: Added `scripts/prepare-ecorex-public-release.ps1` and generated `release-artifacts/EcoreX_0.1.10-public-release.zip` for server handoff. Current zip SHA256 `CE05311BE1FE949ACA1483349EC543E5A945C4D45211DF533EA1AA0B6F068429`; contents verified to exclude stale v0.1.4 installer and pycache.
- 2026-06-11: Created local git branch `codex/ecorex-v0.1.10-productization` and local commit for the v0.1.10 productization work. Added remote `ecorex` -> `git@github.com:zhangyifanjackson-dotcom/EcoreX.git`; SSH push failed on this machine with `Permission denied (publickey)`, so HTTPS token push is the current GitHub handoff path.
- 2026-06-11: Generated offline Git handoff artifacts in `release-artifacts/`: `EcoreX_0.1.10-productization.bundle` and `.patch`. Bundle verification passed and can recreate the v0.1.10 productization branch from the pre-HTTPS-push handoff commit on a GitHub-authorized machine.
- 2026-06-11: GitHub HTTPS token push succeeded after switching Git for Windows to the `schannel` TLS backend. Because the local CowAgent checkout is shallow and could not push full history to a different repository, a clean source snapshot was exported and pushed as a root commit to both `main` and `codex/ecorex-v0.1.10-productization` in `zhangyifanjackson-dotcom/EcoreX`. The remote `main` update used `--force-with-lease` to intentionally replace the old repository contents with the EcoreX v0.1.10 snapshot.
- 2026-06-11: Fixed desktop usage accounting path after completion audit. Agent stream now normalizes provider `usage` envelopes, Web SSE `done` carries usage to the renderer, and renderer reports real token usage once per completed turn with estimated-token fallback. Also fixed SSE `done` handling so final text replaces the assistant bubble instead of duplicating streamed deltas, and Stop clears immediately after `done`.
- 2026-06-11: Rebuilt the Windows package after the usage/SSE fix via manual prepackaged NSIS path because full Electron Builder packaging hit transient GitHub 443 timeouts. New signed installer SHA256 `14D57A4F15D2F99DDC04975D5E636707F648864665D4F3F4D5A011516626DB55`, size `120050856`. Installed smoke passed again with sidecar ready.
- 2026-06-11: Synced the usage/SSE source fix to GitHub through the GitHub Git Data API because normal Git transport was still unstable on port 443. The source sync commit is `45c5a7dfc92be9933c8895732c36469ed3a85e4b`; documentation-only follow-ups may advance the remote refs beyond that commit.

## Known Follow-Up

- Deploy or point the product build at the production Admin API before final live enterprise login/model chat acceptance.
- Provider usage is now normalized when upstream returns a standard usage envelope; estimated tokens remain as fallback for providers that omit usage in streaming responses.
- Run the skipped macOS signing/notarization/Gatekeeper pass on Mac hardware.

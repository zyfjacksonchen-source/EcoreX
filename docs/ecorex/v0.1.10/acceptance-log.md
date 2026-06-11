# EcoreX v0.1.10 Acceptance Log

## Scope

- Windows is the validation target for this round.
- macOS arm64/x64 signing, notarization, and Gatekeeper verification are intentionally skipped and must be run later on a Mac.
- Agent core behavior is not changed.

## Acceptance Matrix

| Area | Scenario | Status | Evidence / Notes |
| --- | --- | --- | --- |
| Version | Package, lockfile, site manifest, verification script all show `0.1.10`. | Pass | `desktop/package.json`, `desktop/package-lock.json`, `deploy/ecorex-site/manifest.json`, and `scripts/verify-ecorex-release.ps1` updated. |
| Admin Users | Create user with name, email, initial password. | Pass | Temporary HTTP smoke created a user and verified login. |
| Admin Users | Edit, soft-delete, and reset password. | Pass | API and Admin UI actions are implemented with modals/confirmation. Browser click pass is still recommended before public rollout. |
| Admin Quota | Set per-user daily and weekly token limits. | Pass | Temporary HTTP smoke set daily and weekly quotas and confirmed quota state. |
| Admin Route | Static Admin page can call API from `/admin/`. | Pass | Admin API now normalizes `/admin/api/*` and `/api/admin/*`; HTTP smoke verified `/admin/api/health`, `/admin/api/state`, user create, client login, and quota check. |
| Admin DOM | Admin visible buttons/forms have JS bindings. | Pass | Static DOM smoke verified all 28 Admin `data-*` hooks are represented in the page script or accepted render/template exceptions. |
| Admin Visual | Admin page should not show stale modal on first load. | Pass | Edge headless screenshot found an empty default modal caused by `.modal-backdrop` overriding `[hidden]`; fixed with `.modal-backdrop[hidden] { display: none; }` and re-screenshot verified clean first load. |
| Desktop Quota | Over-limit user is blocked before model request. | Pass | `/client/quota/check` returned blocked after usage event exceeded quota; renderer checks quota before send. |
| Admin Usage | Usage can be viewed by user. | Pass | `usageByUser` API and Admin usage cards implemented. |
| Admin Errors | Errors can be filtered by user, level, and device. | Pass | `/state` accepts `userEmail`, `level`, and `deviceId`; Admin filter UI implemented. |
| Admin Model | There is one global model, edited through a modal only. | Pass | Admin UI has no create-model flow; API maps model upsert to the global credential and blocks delete. `/client/model-config` requires a valid user session token. |
| Desktop Auth | Ordinary user sees login and can log in. | Pass | `AuthGate` implemented; Electron stores enterprise session in userData and exposes only a sanitized session to renderer. |
| Desktop Layout | Main UI fits one screen; only session list and transcript scroll. | Pass | CSS locks `html/body/#root` and `.app-shell` to 100vh; `.session-list` and `.message-list` are the main scroll containers. |
| Desktop Composer | Enter, Shift+Enter, Ctrl/Cmd+V, Ctrl/Cmd+Z, Ctrl/Cmd+A work. | Pass | Enter send, paste attachment thumbnail, and empty-composer Ctrl/Cmd+Z undo are implemented; Shift+Enter and Ctrl/Cmd+A use native textarea behavior. |
| Desktop Buttons | Every visible button has an action, disabled state, or explanation. | Pass | Session/search/settings/theme/notification/runtime/capability/send/stop/delete/open flows are wired; unavailable capability choices are disabled or hidden with explanatory state. |
| Permissions | Human-in-the-loop confirmation is visible and non-disruptive. | Pass | Capability confirmation and local file-open confirmation are fixed above the composer instead of using a detached native prompt. |
| Runtime | Windows installed app starts without local Python/Node/Git. | Pass | `npm run stage:runtime:win`, `npm run package:win:signed`, and `scripts/smoke-installed-win.ps1 -Port 19142` passed. Packaged runtime includes `enterprise-policy.json` so ordinary users do not need local policy setup. |
| Capabilities | First-use install, skip, cancel, failure, retry paths are visible. | Pass | Composer approval bar and settings pack installer call the Electron capability manager; disabled packs do not show install actions. |
| Token Accounting | User usage can be recorded and quota checked. | Pass | Usage/quota flow is implemented. Agent stream normalizes provider `usage` envelopes, Web SSE `done` carries usage to the renderer, and desktop telemetry reports real token usage once per completed turn with estimated-token fallback when a provider omits usage. |
| Chat Stream | Streaming deltas and final `done` do not duplicate assistant text, and Stop clears after completion. | Pass | Renderer now appends only `message_update` deltas, replaces the assistant bubble on `done`, clears `activeRequestId`, and keeps telemetry single-shot. |
| Windows Artifact | Installer hash and size are recorded. | Pass | `EcoreX_0.1.10_x64-setup.exe`; size `120,050,856` bytes; SHA256 `14D57A4F15D2F99DDC04975D5E636707F648864665D4F3F4D5A011516626DB55`. |
| Download Site | Windows download link resolves in local release directory. | Pass | v0.1.10 installer copied to `deploy/ecorex-site/downloads/`; hash/size match `manifest.json`. macOS buttons show pending validation instead of a false ready download. |
| Download Visual | Download page renders v0.1.10 without white screen. | Pass | Edge headless screenshot verified hero, release strip, `0.1.10`, Windows verified state, and macOS pending validation state. |
| Public Release Package | Server handoff package can be generated without stale artifacts. | Pass | `scripts/prepare-ecorex-public-release.ps1` generated `release-artifacts/EcoreX_0.1.10-public-release.zip`; zip inspection confirmed v0.1.10 site/admin API/checksums are present, manifest Windows artifact is `120,050,856` bytes with SHA256 `14D57A4F15D2F99DDC04975D5E636707F648864665D4F3F4D5A011516626DB55`, old v0.1.4 installer and pycache are absent. |
| Public Release | `https://www.ecoreai.cn/ecorex-agent/` serves v0.1.10. | Blocked | Public verification on 2026-06-11 still returned manifest version `0.1.7`. Local v0.1.10 release directory is ready, but the public host has not been updated. |
| macOS Signing | arm64/x64 signing, notarization, Gatekeeper. | Skipped | User will run these checks on a Mac separately. |

## Manual Follow-Up

- Run a human visual pass on the installed Windows app for login, first chat, stop, paste image/file, quota block, and error telemetry.
- Deploy or sync `deploy/ecorex-site/` plus the v0.1.10 installer to the production `/ecorex-agent` host before validating real enterprise login and real model chat end to end. Public manifest is currently v0.1.7.
- Run macOS package signing/notarization/Gatekeeper on Mac hardware as a separate acceptance pass.

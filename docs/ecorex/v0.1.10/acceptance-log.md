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
| Admin Users | Edit, soft-delete, reset password, and user self password change. | Pass | API and Admin UI actions are implemented with modals/confirmation. Client smoke verified `/client/auth/change-password`. Browser click pass is still recommended before public rollout. |
| Admin Quota | Set per-user daily and weekly token limits. | Pass | Temporary HTTP smoke set daily and weekly quotas and confirmed quota state. |
| Admin Route | Static Admin page can call API from `/admin/`. | Pass | Admin API now normalizes `/admin/api/*` and `/api/admin/*`; HTTP smoke verified `/admin/api/health`, `/admin/api/state`, user create, client login, and quota check. |
| Admin DOM | Admin visible buttons/forms have JS bindings. | Pass | Static DOM smoke verified all 28 Admin `data-*` hooks are represented in the page script or accepted render/template exceptions. |
| Admin Security | Admin routes are not public and client telemetry is bound to a real user token. | Pass | HTTP smoke confirmed `/state` and raw `/events` return 401 without admin auth, invalid client events return 401, default seeded users are disabled by default, and arbitrary Origin is no longer reflected. |
| Admin Visual | Admin page should not show stale modal on first load. | Pass | Edge headless screenshot found an empty default modal caused by `.modal-backdrop` overriding `[hidden]`; fixed with `.modal-backdrop[hidden] { display: none; }` and re-screenshot verified clean first load. |
| Desktop Quota | Over-limit user is blocked before model request. | Pass | `/client/quota/check` returned blocked after usage event exceeded quota; renderer checks quota before send. |
| Admin Usage | Usage can be viewed by user, including users later soft-deleted. | Pass | `usageByUser` includes deleted users with `deletedAt`; Admin usage cards preserve audit history. |
| Admin Errors | Errors can be filtered by user, level, device, and time window. | Pass | `/state` accepts `userEmail`, `level`, `deviceId`, `from`, and `to`; Admin filter UI uses `logUsers` so soft-deleted users remain selectable. |
| Admin Model | There is one global model, edited through a modal only. | Pass | Admin UI has no create-model flow; API maps model upsert to the global credential and blocks delete. `/client/model-config` requires a valid user session token and returns 401 for over-quota users. |
| Desktop Auth | Ordinary user sees login and can log in. | Pass | `AuthGate` implemented; Electron stores enterprise session in userData and exposes only a sanitized session to renderer. |
| Desktop Layout | Main UI fits one screen; only session list and transcript scroll. | Pass | CSS locks `html/body/#root` and `.app-shell` to 100vh; `.session-list` and `.message-list` are the main scroll containers. |
| Desktop Composer | Enter, Shift+Enter, Ctrl/Cmd+V, Ctrl/Cmd+Z, Ctrl/Cmd+A work. | Pass | Enter send, paste attachment thumbnail, paste failure toast, and empty-composer Ctrl/Cmd+Z undo are implemented; Shift+Enter and Ctrl/Cmd+A use native textarea behavior. Ctrl/Cmd+Z now deletes backend history using the original user message `user_seq`. |
| Desktop Buttons | Every visible button has an action, disabled state, or explanation. | Pass | Session/search/settings/theme/notification/runtime/capability/send/stop/delete/open flows are wired; unavailable capability choices are disabled or hidden with explanatory state. |
| Permissions | Human-in-the-loop confirmation is visible and non-disruptive. | Pass | Capability confirmation and local file-open confirmation are fixed above the composer instead of using a detached native prompt. Electron main process also denies `openPath` without an approved local-file permission. |
| Desktop Stream | Provider `delta`, `message_update`, `done`, and `error` frames produce stable chat UI. | Pass | Renderer handles `delta` and `message_update` as streaming text, replaces final text on `done`, reports stream `error` once, clears the active request, and writes error telemetry. |
| Desktop Visual | Built renderer does not white-screen and keeps light/dark one-screen layout. | Pass | `desktop/scripts/smoke-renderer-visual.ps1` captured auth, main-light, and main-dark screenshots through Edge headless with a temporary desktop bridge mock. |
| Runtime | Windows installed app starts without local Python/Node/Git. | Pass | `npm run stage:runtime:win`, `npm run package:win:signed`, and `scripts/smoke-installed-win.ps1 -Port 19142` passed. Packaged runtime includes `enterprise-policy.json` so ordinary users do not need local policy setup. |
| Capabilities | First-use install, skip, cancel, failure, retry paths are visible. | Pass | Composer approval bar and settings pack installer call the Electron capability manager; disabled packs do not show install actions. |
| Token Accounting | User usage can be recorded and quota checked. | Pass | Usage/quota flow is implemented. Agent stream normalizes provider `usage` envelopes, Web SSE `done` carries usage to the renderer, and desktop telemetry reports real token usage once per completed turn with estimated-token fallback when a provider omits usage. |
| Chat Stream | Streaming deltas and final `done` do not duplicate assistant text, and Stop clears after completion. | Pass | Renderer now appends only `message_update` deltas, replaces the assistant bubble on `done`, clears `activeRequestId`, and keeps telemetry single-shot. |
| Windows Artifact | Installer hash and size are recorded. | Pass | `EcoreX_0.1.10_x64-setup.exe`; size `117,529,360` bytes; SHA256 `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`. |
| Download Site | Windows download link resolves in local release directory. | Pass | v0.1.10 installer copied to `deploy/ecorex-site/downloads/`; hash/size match `manifest.json`. macOS buttons show pending validation instead of a false ready download. |
| Download Visual | Download page renders v0.1.10 without white screen. | Pass | Edge headless screenshot verified hero, release strip, `0.1.10`, Windows verified state, and macOS pending validation state. |
| Public Release Package | Server handoff package can be generated without stale artifacts. | Pass | `scripts/prepare-ecorex-public-release.ps1` generated `release-artifacts/EcoreX_0.1.10-public-release.zip`; zip SHA256 `DDF69409D5E3183644A11D11089E883419409BC705DFCFCD8C86CAA46359FD31`, size `120,274,162`; manifest Windows artifact is `117,529,360` bytes with SHA256 `ACA52B7ACF7D73FBCA62F3F5AB92C057AB50B8FBD188C3AD7105B665569D482B`, old v0.1.4 installer and pycache are absent, server helper scripts/config examples are included, and zip entries use Linux-safe `/` paths. |
| Public Install Script | Server release zip can be installed into release/current layout. | Pass | Local Linux/WSL smoke ran `scripts/install-ecorex-public-release.sh` with temp `RELEASE_ROOT`/`ADMIN_ROOT`; verified release `index.html`, `manifest.json`, `admin/index.html`, installer, Admin API files, and env file exist. |
| Acceptance Harness | Local/package/GitHub/public status can be checked from one command. | Pass | `scripts/test-ecorex-v0.1.10-acceptance.ps1 -AllowPublicBlocked` verifies manifest, installer hash/signature, release zip entries, Linux install smoke, public route status, and GitHub refs. |
| Public Release | `https://www.ecoreai.cn/ecorex-agent/` serves v0.1.10. | Blocked | Public verification on 2026-06-11 was retried after proxy was enabled; `https://www.ecoreai.cn/ecorex-agent/manifest.json` returned HTTP 404. Local v0.1.10 release package is ready, but the public host routing/deployment is not updated. |
| macOS Signing | arm64/x64 signing, notarization, Gatekeeper. | Skipped | User will run these checks on a Mac separately. |

## Manual Follow-Up

- Run a human visual pass on the installed Windows app for login, first chat, stop, paste image/file, quota block, and error telemetry.
- Deploy or sync `deploy/ecorex-site/` plus the v0.1.10 installer to the production `/ecorex-agent` host before validating real enterprise login and real model chat end to end. Public manifest currently returns HTTP 404.
- Run macOS package signing/notarization/Gatekeeper on Mac hardware as a separate acceptance pass.

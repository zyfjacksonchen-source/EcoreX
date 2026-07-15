# v0.2.0 Development Log

## 2026-06-23

- Created v0.2.0 execution branch `codex/ecorex-v0.2.0` from checkpoint `702072fa`.
- Started version migration from v0.1.19 to v0.2.0 for runtime/package/admin/WebUI defaults.
- Preserved v0.1.19 as a compatibility client key instead of removing it from rollout allowlists.
- Restored CowAgent's `ChatChannel.cancel_session` / `cancel_all_session` missing-futures guard and added a focused regression test.
- Added stale active run recovery: orphaned message runs with no cancel token, no SSE state, no live session lock, and no update past `web_active_run_stale_seconds` are marked `interrupted` and no longer block backpressure.
- Ran three read-only parallel agent slices:
  - Discovery slice confirmed channels, extensions, tools, and knowledge graph were split across inconsistent discovery surfaces.
  - Performance slice confirmed WebUI lag is structural: stream deltas drive full `App.tsx` renders, markdown reparse, token estimation, and sidebar recomputation.
  - Install/persistence slice confirmed project drift risk comes from early replace-mode UI-state writes before runtime hydration completes.
- Implemented shared channel catalog and wired it into `/api/channels`, `/api/extensions`, frontend runtime capabilities, and desktop bridge allowlist.
- Updated `/api/tools` to use `ToolManager.list_tools()` so loaded MCP/dynamic tools appear in the runtime capability snapshot.
- Hardened WebUI project/session persistence:
  - Runtime UI-state hydration now merges runtime projects/session mappings into local state instead of replacing local state with an empty or partial snapshot.
  - Automatic UI-state sync now uses merge mode; explicit project deletion is the only WebUI path that sends replace mode.
  - Backend `save_ui_state` ignores empty replace project payloads unless the caller explicitly sets `allowEmptyProjectState`.
- Improved WebUI streaming responsiveness:
  - Pending assistant answers now render through `LiveStreamingText`, which appends to a DOM text node on animation frames and skips Markdown parsing while the stream is live.
  - Long-stream display throttling was tightened from 110/48ms to 48/24ms.
  - History context token estimation moved out of render-time `useMemo` into a debounced effect so live deltas do not rescan all messages/tool output on every render.
- Hardened WebUI install/update entry points:
  - Removed the admin-page manifest link from ordinary Web UI.
  - Windows and macOS web installers now print script and manifest versions plus fallback instructions when the browser does not auto-open.
  - The package generator now emits versioned package installers and rejects generated macOS installers containing retired `resume_args` code.
  - macOS package installer writes desktop shortcuts before attempting to open the browser.
- Prepared v0.2.0 WebUI release artifacts:
  - Added an explicit `-PromoteVersion` gate to the public manifest updater so advancing `deploy/ecorex-site/manifest.json` from v0.1.19 to v0.2.0 is intentional and test-covered.
  - Built v0.2.0 WebUI Windows and macOS local packages plus the combined public release zip.
  - Verified package contents include the WebUI installers, desktop shortcut creation paths, v0.2.0 version markers, and no retired macOS `resume_args` retry code.
- Fixed WebUI local auth fallback after package smoke found the release page could stop at the login panel on loopback when the admin client bridge returned `403 invalid client key`.
  - No-password loopback WebUI now creates a local fallback session before probing admin model config.
  - Missing/invalid admin client keys are treated as local-client-unavailable for fallback purposes.
  - Rebuilt v0.2.0 WebUI packages and public release artifacts after the fix.
- Fixed independent review P1 for WebUI project/session state merging.
  - Merge-mode `sessionProjects`, `pinnedProjects`, `sessionTitles`, and `pinnedSessions` now let incoming explicit values override existing values while preserving keys omitted by partial clients.
  - Added a regression test for moving a session between project folders, updating titles, and explicitly unpinning project/session entries.
  - Rebuilt v0.2.0 WebUI packages and public release artifacts again so the packaged runtime contains the corrected merge semantics.
- Completed final v0.2.0 release closure.
  - Three independent read-only review agents reached PASS consensus across UI/performance, runtime/state, and cross-platform/security.
  - Published GitHub release `v0.2.0` with Windows WebUI, macOS WebUI, combined Win/Mac, and public-release zip assets.
  - Deployed the public release bundle to `https://www.ecoreai.cn/ecorex-agent/` and verified the public manifest and Win/Mac WebUI downloads.
  - Ran a Windows package installer live smoke; the local desktop shortcut was generated and the installed WebUI returned version `0.2.0` on port `9909`.

## 2026-06-24 Hotfix: Send/Interrupt UX

- Removed the user-visible English technical status `Sending while stopping the previous response`.
- Localized retry/reconnect/recovery controls and network interruption explanations in the chat surface.
- Fixed a live-placeholder merge race: when saved history contains the accepted user turn but the assistant answer is still running, WebUI now preserves the local pending assistant instead of hiding it.
- Tightened the live-placeholder merge race for repeated identical user prompts by matching accepted user turns from the comparable history tail instead of using the first content match.
- Fixed artifact/media-only completion UX: when a visible artifact, image, video, audio, or file event arrives, WebUI clears transient connecting/thinking phases and marks the visible assistant bubble settled while leaving the underlying stream open for later `done` or tail events.
- Added persisted `visibleOutputSettled` UI state so a pre-`done` visible artifact/media bubble remains recoverable after reload instead of being misclassified as a terminal assistant answer.
- Independent read-only review found and rechecked two UI-state P1 issues: premature terminal classification during reconnect and missing `visibleOutputSettled` persistence. Both were fixed before packaging.
- Rebuilt v0.2.0 WebUI packages and public release artifacts with the hotfix.
- Updated the public download page so the installation guide appears before package download cards, with one-click copy buttons for both Windows PowerShell and macOS Terminal install/update commands.

## 2026-06-24 Hotfix: WebUI Model Config Admission

- Investigated user `shzhoujiehuan@ecoremedia.net` seeing `消息未发送 / 当前网页版没有可用模型配置`.
- Root cause: the production `/ecorex-agent/client/*` route is served by the Docker Compose `ecorex-admin-api` container under `/opt/xhs-report`, not the copied `/srv/ecorex-agent-admin/app` files alone. The container still carried v0.1.15-era client keys, so v0.2.0 WebUI requests were rejected as `403 invalid client key` before model policy delivery.
- Hot-fixed production by syncing the v0.2.0 Admin API into `/opt/xhs-report/_ecorex_admin_api` and rebuilding/recreating the Compose service. The v0.2.0 WebUI client key now passes the client gate and returns `401 missing user token` without a user token, which is the expected authenticated boundary.
- Confirmed production has an enabled global model policy (`openai` / `gpt-5.5`) and `shzhoujiehuan@ecoremedia.net` is an active member, so a valid enterprise session can receive model configuration.
- Hardened `scripts/install-ecorex-public-release.sh` so future public deployments copy Admin API files into the Compose build context and run `docker compose up -d --build --force-recreate ecorex-admin-api` when that production layout is present.
- Hardened WebUI model admission errors with stable `MODEL_CONFIG_UNAVAILABLE`, `ENTERPRISE_LOGIN_REQUIRED`, `ENTERPRISE_POLICY_SYNC_FAILED`, and `ENTERPRISE_POLICY_UNAVAILABLE` codes.
- Added a user-facing recovery path: model-config send failures now preserve the draft/attachments and include a `重新登录` recovery action in addition to retry/keep-draft.
- Follow-up user validation still showed the old `当前网页版没有可用模型配置` copy. Root cause: public `/ecorex-agent/app/` was still served by `ecorex-web.service` from `/opt/ecorex-web/releases/20260619130611-v0.1.15`; only the static download/admin bundle had been updated.
- Rebuilt the v0.2.0 renderer, Win/Mac WebUI packages, Web Linux service tarball, manifest, and public release zip. Deployed both chains:
  - static download/admin bundle to `/srv/ecorex-agent-download/releases/20260624032435-v0.2.0`;
  - live WebUI runtime to `/opt/ecorex-web/releases/20260624032517-v0.2.0`.
- Verified public `/ecorex-agent/api/version` now returns `0.2.0`, public `/app/` references `assets/index-oN65WZHT.js`, and the old model-config copy is absent from the served HTML.
- Independent review found one remaining P1 after the first model-config hotfix: `/message` still had the old fallback copy `请先登录企业账号，或在设置 > 模型中配置可用的 API Key 后再发送。`
- Removed that fallback, added regression assertions that both old model-config copies stay out of `channel/web/web_channel.py`, and kept the recoverable model-config response shape (`code`, `error_type`, `recoverable`) intact.
- Rebuilt and redeployed both production chains again:
  - static download/admin bundle to `/srv/ecorex-agent-download/releases/20260624044918-v0.2.0`;
  - live WebUI runtime to `/opt/ecorex-web/releases/20260624045012-v0.2.0`.
- Verified public `/ecorex-agent/api/version` returns `0.2.0`, the v0.2.0 client key reaches `401 missing user token` on `/client/model-config`, public manifest hashes match the rebuilt packages, and the active WebUI runtime source has no old model-config fallback copy.

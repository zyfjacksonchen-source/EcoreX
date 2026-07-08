# EcoreX v0.3.0 Development Log

## 2026-07-07

- Scope: WebUI-only v0.3.0 hardening.
- Target branch: `codex/ecorex-v0.3.0-hardening`.
- Target version: `0.3.0`.
- Release title: `EcoreX 0.3.0 生产级任务控制与在线更新稳定性版本`.
- Safety snapshot created before implementation:
  - `git stash push -u -m "v0.3.0 pre-implementation dirty tree snapshot"`
  - Snapshot retained as the latest stash entry at creation time.
  - Snapshot includes tracked dirty files and untracked files.
- Workspace cleanup completed:
  - Removed regenerable cache directories only: `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.parcel-cache`, `.vite`.
  - Verified delete targets were inside the workspace before removal.
  - Did not delete unknown release/docs artifacts or user-visible generated assets.

## Standing Rules

- Every implementation slice must update this log.
- Do not silently revert pre-existing dirty worktree changes.
- WebUI validation must include user-path evidence, not only source-level checks.
- Any release/update pipeline failure must stop the v0.3.0 release chain and avoid producing half-trusted artifacts.

## Slice Status

- S00 workspace safety and cleanup: complete.
- S01 version metadata and release copy: complete.
- S02 active turn control: complete.
- S03 WebUI stability fixes: complete.
- S04 release/update chain hardening: complete.
- S05 admin management productization: complete.
- S06 external connector discovery and preservation: complete.
- S07 real-user acceptance evidence: complete for packaged/local smoke; credentialed and destructive-environment residuals are tracked in the acceptance checklist.
- S10 retouch/infinite canvas completion: complete.

## S06 External Connector Discovery And Preservation

- Product scope correction:
  - Removed frontend-only planned connector placeholders from the external connections quick panel.
  - `/api/external-connections` now returns `ecorex.external-connectors.implemented.v1` and only catalogs real implemented connectors.
  - Tencent Meeting, Tencent Survey, QQ Mail, Lexiang, ima, and finance connectors are documented as researched/not-yet-implemented rather than shown as connectable UI.
- Stable discovery:
  - Added `ToolManager.ensure_mcp_configured_loaded()` so workspace `mcp.json` connectors can be started/refreshed by runtime discovery without relying on a one-time settings page state.
  - Wired the ensure path into runtime capabilities, extension registry, skill service, Web channel tool snapshots, agent initialization, streaming turns, and MCP hot reload.
  - Tencent Docs attachments now trigger a bounded MCP ready check before agent execution.
- Online update preservation:
  - `manifest.json` now requires connector health checks for WebUI online updates.
  - Windows and macOS package installers capture external connector snapshots before update and after new runtime start.
  - Update activation fails with `rollback` when previously connected/callable connectors disappear after update.
  - `update-state.json` exposes redacted `externalConnections` health details for WebUI and acceptance evidence.
- Research:
  - Added `docs/v0.3.0/external-connectors-real-connectivity.md`.
  - Official API/OAuth routes and original CowAgent/current EcoreX implementation status are recorded there.
- Verification:
  - `python -m py_compile agent/tools/tool_manager.py agent/runtime_capabilities.py agent/extensions/registry.py agent/skills/service.py bridge/agent_initializer.py bridge/agent_bridge.py agent/protocol/agent_stream.py channel/web/web_channel.py`

## S05 Admin Management Productization

- Admin release backend:
  - Added release-index validation to staged/current release validation.
  - Promotion now blocks when v0.3.0 release-index is missing, not ready, mismatched with manifest artifacts, missing smoke pass evidence, or missing required artifact signatures.
  - Release entries now expose release-index status, rollout, kill-switch, rollback, state machine, background update policy, risks, and next actions.
  - Admin release notifications no longer mutate immutable v0.3.0 manifest/release-index packages; they write admin release notice and local update-state instead.
- Admin page:
  - Release panel now shows release-index trust status, risk count, rollout percent, kill-switch, rollback health check, online update state machine, and release risks.
  - Current stable and staged candidates show release-index status and expandable validation failures.
  - Disabled `通知用户` state is enforced in the click handler, not only visually.
- Verification:
  - `python -m py_compile deploy/ecorex-admin-api/ecorex_admin_api.py channel/web/web_channel.py`
  - `node -e "const fs=require('fs'); new Function(fs.readFileSync('deploy/ecorex-site/admin/admin.js','utf8')); console.log('admin js syntax ok')"`

## S04 Release And Online Update Chain Hardening

- Online update state machine:
  - Runtime update state now accepts `available`, `downloading`, `verified`, `staged`, `deferred`, `installed`, `activated`, `failed`, and `rollback`.
  - Release notices use `available` instead of the old ambiguous `ready`.
  - Installed/activated states require health check pass before the WebUI offers immediate switch.
  - Runtime update banner is visible again and distinguishes download, deferred, failed, rollback, retry, switch, and log-view states.
- Installer state output:
  - Windows and macOS package installers now write `available -> downloading -> verified -> staged -> installed/activated` into `update-state.json`.
  - Background updates still defer when active requests exist or active request state is unavailable.
  - Manual installs finish as `activated`; background installs finish as `installed` so existing tabs can soft-refresh after health check.
- Release orchestrator:
  - Added `scripts/release-ecorex-webui-orchestrator.ps1`.
  - Orchestrator gates version alignment, typecheck, renderer build, WebUI package build, web service package build, artifact hash, signature presence, manifest trust, smoke evidence, and atomic `release-index.json` promotion.
  - `desktop/package.json` exposes `webui:release`.
- Verification:
  - `npm run typecheck`
  - `python -m py_compile channel/web/web_channel.py deploy/ecorex-admin-api/ecorex_admin_api.py agent/tools/browser/browser_service.py agent/protocol/image_job_service.py agent/protocol/runtime_projection.py agent/tools/imagegen/imagegen.py`
  - PowerShell scriptblock parsing for `scripts/release-ecorex-webui-orchestrator.ps1` and `scripts/prepare-ecorex-webui-local-release.ps1`
  - Node JSON parsing for `deploy/ecorex-site/manifest.json`, `deploy/ecorex-site/release-index.json`, and `desktop/package.json`
  - `npm run build:renderer`
- Packaging and release-index status:
  - `scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` completed for Windows and macOS WebUI packages.
  - `scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` promoted `deploy/ecorex-site/release-index.json`.
  - `deploy/ecorex-site/manifest.json` download source order is now domestic GitHub mirror first, origin CDN fallback second.
  - Windows package: size `551244443`, SHA256 `8E2FEA63006B9518FF05BE4FD1D4967A9B4C981DC44B5DF31901FFD925CEAC5D`.
  - macOS package: size `652419412`, SHA256 `A05AF02233E7B1F4498CAEC1410EC75CDE8C719C1E912955199210889BD3BE52`.
  - Release-index status: `ready`, smoke status: `pass`.

## S07 Real-User Acceptance Evidence

- Completed source/build checks:
  - `tests/test_v030_webui_hardening.py`
  - `tests/test_v029_webui_followups.py::test_webui_online_update_uses_ready_dialog_instead_of_confusing_banner`
  - `tests/test_ecorex_web_parallel_backend.py::TestProjectSessionSourceContracts::test_react_project_session_composer_autosize_and_general_isolation`
  - Result: `8 passed`.
- Completed build checks:
  - `npm run typecheck`
  - `npm run build:renderer`
- Completed user-path evidence:
  - Packaged WebUI runtime smoke passed against `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`.
  - User online update local smoke passed through the public install script, manifest download, package hash check, background install, runtime `/api/version`, runtime `/api/update-check`, external connector preservation policy, and no-browser background update check.
  - Release-package CDP smoke passed through a real headless Chrome/Edge browser against the packaged WebUI: session open, image artifact shelf, precise retouch entry, right-side current artifacts, two-image selection, text target, rectangle selection, lasso selection, uploaded reference image, marker attachment, and composer draft with imagegen-only constraints.
  - Evidence:
    - `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`
    - `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`
    - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.json`
    - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.png`
- Remaining real-user evidence:
  - Credentialed connector quick panel in a packaged browser path, live CDP reconnect, provider-level imagegen output, and rollback-on-failed-runtime still need environment-backed validation before public promotion.

## S08 Office-Agent UX And ImageGen Routing Hardening

- Entry:
  - User reported that both single-character poster fixes and the `精准修图` entry still routed through repeated local `bash`/Python image processing.
  - User also reported first-message new-session pending state gaps, occasional output truncation after streaming, persistent circular tool icons, code artifacts showing in an office-agent chat, and top bar text-chip noise.
- Frontend runtime state:
  - Added local `PendingPreflightTurn` tracking so the first message's assistant pending state remains stable before the backend returns a server `request_id`.
  - New same-session sends now supersede an unaccepted local preflight turn with an explicit "已被新消息替换" state instead of leaving an empty/vanishing thinking state.
  - Active-turn controls now appear whenever a local pending assistant exists, not only after a server request id exists.
  - Done-event content merge now preserves already-streamed longer content unless a true `final_text` is provided, reducing accidental final-packet truncation.
  - History merge keeps a locally richer visible answer when the refreshed history projection is shorter.
- Image generation and retouch routing:
  - `精准修图` drafts now explicitly require `imagegen` / image-editing capability and forbid bash, Python, PIL, OpenCV, ImageMagick, SVG/canvas, or coordinate scripts as the semantic edit path.
  - `agent_stream` imagegen intent detection now includes `精准修图`, `局部修图`, `精修标注`, `标注图`, `箭头尖端`, single-character text-fix phrases, and poster/image edit phrases.
  - Tool schema selection continues to expose only `imagegen` for imagegen intent when available; if unavailable, it exposes diagnostic/enablement tools only (`host_diagnostics`, `optional_abilities`, `agent_capability`, `ecorex_cli`), not bash.
  - Execution layer hard-blocks bash/shell/terminal during semantic image retouch tasks unless a prior `imagegen` call succeeded and the shell command is deterministic post-processing such as copy, rename, zip, checksum, or reveal.
- Office-agent artifact and UI polish:
  - Chat artifact shelf filters implementation/code files such as `.py`, `.js`, `.ts`, `.sh`, `.ps1`, native code, logs, lockfiles, etc.
  - Markdown artifacts remain visible and keep a local-open action pinned.
  - Tool-step icons no longer render a persistent circular chip; state is conveyed by the plain icon color.
  - Project add-folder uses a plain icon style.
  - Header runtime/account state now uses icon-only status controls with tooltip/aria labels.
  - Sidebar "通用会话" copy is simplified to "会话".
- Verification:
  - `python -m py_compile agent/protocol/agent_stream.py channel/web/web_channel.py`
  - `npm run typecheck`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `15 passed`
  - Focused existing regressions:
    - `tests/test_ecorex_web_parallel_backend.py::TestProjectSessionSourceContracts::test_react_project_session_composer_autosize_and_general_isolation`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_prioritizes_imagegen_for_multi_image_requests`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_uses_diagnostics_when_imagegen_is_missing_not_bash`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_does_not_restore_recent_bash_for_imagegen_intent`
    - `tests/test_ecorex_web_parallel_backend.py::TestAgentHostBoundary::test_tool_schema_budget_does_not_restore_other_tools_for_imagegen_intent`
    - Result: `5 passed`
  - `npm run build:renderer` passed with the existing large chunk warning.

## S03 WebUI Stability Fixes

- CDP/browser:
  - Added stale-browser detection for structured action return values such as `{"error": "Target closed"}`.
  - CDP mode now reconnects once when an action returns a stale connection result, matching the existing exception-based recovery path.
- Image generation:
  - Added stable artifact ordering by `(task_index, artifact_index)` in image job state, runtime projection, and frontend rendering.
  - Image job artifacts now carry `task_index`, `artifact_index`, and `task_id`.
  - Imagegen output names now encode task and artifact ordinals (`tXX/iXX`) instead of relying on timestamp/order alone.
  - Incremental image batch events carry `artifact_index`.
- Composer/scroll stability:
  - Reworked composer autosize to avoid unconditional `height = auto`.
  - Stop and pause state updates preserve message-list scroll position when the user is not already pinned to bottom.
  - New active-turn transient phases are cleaned like other preflight phases.
- Session list:
  - Project and general session lists show the first six rows by default and expose `查看更多(N)`.
  - Search mode still shows all matching rows.
- Share:
  - Share thumbnails are bounded to smaller JPEG data URLs.
  - Share payloads are capped by byte budget and message/artifact count.
  - Recent messages and recent images are kept first; older media degrades to metadata if needed.
  - `payload too large` retries once with media stripped.
- Visual boundary:
  - Composer zone now has a completed top divider and rounded top corners.
- Verification so far:
  - `python -m py_compile agent/tools/browser/browser_service.py agent/protocol/image_job_service.py agent/tools/imagegen/imagegen.py agent/protocol/runtime_projection.py channel/web/web_channel.py`

## S09 Package Evidence Refresh

- Entry:
  - After S08 imagegen-routing and office-agent UX changes, v0.3.0 WebUI packages had to be rebuilt and package smoke evidence had to point to the rebuilt artifacts, not the previous 17:00 package hash.
- Rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551244443`
    - SHA256: `8E2FEA63006B9518FF05BE4FD1D4967A9B4C981DC44B5DF31901FFD925CEAC5D`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652419412`
    - SHA256: `A05AF02233E7B1F4498CAEC1410EC75CDE8C719C1E912955199210889BD3BE52`
- Added repeatable package runtime smoke:
  - `scripts/smoke-v030-webui-package-runtime.ps1`
  - The script extracts the release zip into a bounded `tmp/` smoke directory, writes an isolated local `config.json`, starts package-internal `runtime/app.py`, verifies `/api/version` and `/app/`, records package hash/size/runtime manifest/release metadata, then stops the process.
  - Recursive cleanup is guarded so it can only operate under the repo `tmp/` directory.
- Verification:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-smoke -ExpectedVersion 0.3.0 -Port 9929`
  - Result: pass.
  - Evidence: `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`.

## S09B User Online Update Smoke

- Entry:
  - User requested a final check that the user-side online update path is actually effective.
  - User also requested the first download source to use a domestic GitHub mirror for speed.
- Changes:
  - `manifest.json` download source order is `ghproxy.net` GitHub release mirror first and `https://dl.ecoremedia.net/ecorex-agent` fallback second.
  - Public Windows install script now honors `ECOREX_DOWNLOAD_DISABLE_PARALLEL=1` and skips adaptive Range download for localhost smoke sources.
  - Local online-update smoke uses a deterministic `fileName` mirror so Python's local static server does not create false Range failures.
  - Windows package runtime launch keeps no-browser/background behavior and restores runtime stdout/stderr logs for diagnosability.
- Verification:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600`
  - Result: `PASS`.
  - Checks passed: `7/7`.
  - Evidence: `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`.

## S09C Final Package Evidence Refresh

- Entry:
  - After the release-package CDP smoke found that Markdown image paths could render as previews without entering the actionable artifact shelf, `MessageContent` was hardened so inline image paths become legacy image artifacts and image artifacts with preview sources remain available for preview/retouch even if local stat falls back to preview-only.
  - The release runtime sanitizer previously spent too long scanning third-party vendor trees. It now keeps business/runtime files under strict sanitization while skipping full-text scans for vendor trees such as Python site-packages, Node, Playwright browsers, and wheelhouse, and skips very large text files.
- Final rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551259992`
    - SHA256: `058C7BAC58592664A5F2FAB952A3FFACD1CC7126BF0EC905F6C117351AAECF4D`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652435170`
    - SHA256: `7C52854B6909BC16DED8CC848CE83EA2DB20A41E45F2572AD5D8D910346401DB`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204941831`
    - SHA256: `7ECC0C459AD22F3627D9E8B27C69A4DBB746CB51C4AF4A4495A892C0C6869494`
- Release-index status:
  - `deploy/ecorex-site/release-index.json` status: `ready`.
  - Manifest hash in release-index: `52790D0AB712ABA44439854E2ED8FA6E45F549099C954EA6EAC358C4F6DCF701`.
  - Signatures are marked `not-required` for WebUI packages because this iteration intentionally does not develop/sign the desktop app.
- Final verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `19 passed`
  - `npm run typecheck`
  - `npm run build:renderer`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 180` -> `PASS`

## S10 Retouch And Infinite Canvas Completion

- Entry:
  - User requested the independent retouch/infinite-canvas slice after v0.3.0 release/update hardening.
  - Required capabilities: rectangle selection, lasso/circle selection, text annotations, uploaded image references, current-round artifact panel, multi-image selection, and T text-edit flow that routes to imagegen rather than local bash/Python editing.
- Changes:
  - `ImageRetouchCanvas` now uses a typed annotation layer model: `arrow`, `rect`, `lasso`, `text`, and `image`.
  - Bottom toolbar now exposes arrow, rectangle selection, lasso/circle selection, T text target, hand/pan, zoom, upload reference image, and undo/erase.
  - Uploaded images are added as visible reference layers on the infinite canvas and included in the transparent marker export.
  - Right-side `本轮产物` panel lists images from the current assistant response; if only one image is available, the current image is auto-added.
  - Users can select multiple images before submission. The generated draft includes all selected original image paths.
  - T text targets generate prompt constraints to preserve original font style, color, shadow, perspective, and layout while changing text content through imagegen.
  - The transparent marker layer still does not draw or mutate the original image; it only draws user annotations and uploaded reference images.
- Verification:
  - `npm run typecheck`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `18 passed`
  - `npm run build:renderer`
  - Vite preview served the built renderer at `http://127.0.0.1:5174/` and returned the built root document; the preview process was stopped afterward.
- Known follow-up:
  - Automatic OCR detection for all text boxes is not wired because the current WebUI has no production OCR endpoint. The T flow is implemented as user-placed text targets and imagegen style-preserving text edit instructions.

## S02 Active Turn Control

- Frontend send API now sends `interrupt_mode` with `/message`.
- Supported modes: `replace`, `amend`, `queue`, `branch`.
- Default composer send while the same session is running uses `replace`, displayed as updating the current task.
- Explicit running-task menu added to composer:
  - `更新任务`
  - `排队稍后执行`
  - `新开分支`
- `新开分支` creates a new WebUI session locally and sends the new message there while the original task keeps running.
- Queued message action UI no longer shows the ambiguous primary `引导` action.
- Queued messages now expose concrete actions:
  - `提到队首`
  - `取消排队`
- Backend `/message` now parses `interrupt_mode`.
- Backend default same-session behavior is active-turn control:
  - `replace` and `amend` cancel the active request and wait for the session lock.
  - `queue` explicitly accepts the message into the session queue.
  - `branch` refuses same-session admission so the frontend must use a distinct session.
- Fixed a concurrency edge: explicit queue/branch no longer increments the replacement ticket and therefore cannot supersede an active replacement wait.
- Verification so far:
  - `python -m py_compile channel/web/web_channel.py`

## S01 Version Metadata And Release Copy

- Updated current WebUI-facing version metadata to `0.3.0`:
  - `cli/VERSION`
  - `desktop/package.json`
  - `desktop/package-lock.json`
  - `common/ecorex_release_notes.py`
  - `deploy/ecorex-site/index.html`
  - `deploy/ecorex-site/install-webui.ps1`
  - `deploy/ecorex-site/install-webui.sh`
  - `deploy/ecorex-site/admin/index.html`
  - `deploy/ecorex-site/admin/admin.js`
  - `channel/web/web_channel.py`
- Release notes title set to `EcoreX 0.3.0 生产级任务控制与在线更新稳定性版本`.
- Added `deploy/ecorex-site/release-index.json` as the v0.3.0 release-index contract.
- Updated `deploy/ecorex-site/manifest.json` with:
  - current version `0.3.0`
  - `releaseIndex`
  - signature trust metadata
  - rollout metadata
  - kill-switch metadata
  - rollback metadata
  - online-update state machine metadata
- Marked v0.3.0 downloadable artifacts as `pending` with `sha256: pending` until the release orchestrator builds and validates real packages.
- Preserved v0.2.9.2 only as backward-compatible Web client keys and rollback previous version metadata.
- Verification:
  - Parsed `deploy/ecorex-site/manifest.json` and `deploy/ecorex-site/release-index.json` with Node JSON parsing.
  - Searched active WebUI release files for remaining `0.2.9.2` references; remaining references are compatibility keys or rollback metadata.

## S11 Final v0.3.0 Seal And Re-acceptance

- Entry:
  - Multi-agent review found two additional product issues during final acceptance: queued messages could show queue guidance without rendering the `提到队首` / `取消排队` action buttons, and the release CDP smoke did not yet cover active-turn UI paths.
  - User also requested domestic GitHub mirror as the first download source; this remains in the final manifest source order.
- Fixes after review:
  - Queued assistant messages now keep queued action state through a `queued` recovery fallback, so queue actions render even when stream/preflight merging drops the original transient `sendAttempt`.
  - CDP release smoke now validates:
    - `查看更多(N)` and `收起` session list behavior.
    - stop action preserves scroll position.
    - long composer input autosizes without page jump.
    - default active-turn send uses `interrupt_mode: replace`.
    - explicit `排队稍后执行` uses `interrupt_mode: queue` and `取消排队` is clickable.
    - explicit `新开分支` sends `interrupt_mode: branch`.
    - inline local image previews render with non-zero natural size.
    - precise retouch/infinite canvas supports two-image selection, text target, rectangle, lasso, uploaded reference image, marker attachment, and imagegen-only draft.
- Final effective package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551265395`
    - SHA256: `1E9050CF15E1FF3169CA3805B8639FE0AC9A4B984C4AD2F02FCAC4AA9AF15522`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652441202`
    - SHA256: `3063898AF17593162F9EC3B876941BC63E60225A0E71209A7B058B522063ED37`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204953267`
    - SHA256: `73FE83EF80BAD5A2B6C92CB6430F6848DCDED1A6D2810E9596B63DF78EF89471`
- Final verification:
  - `npm run typecheck` -> pass.
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. pytest -q tests/test_v030_webui_hardening.py` -> `22 passed, 1 warning`.
  - `npm run build:renderer` -> pass, built renderer asset `index-C7zTVepT.js`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` -> pass; `deploy/ecorex-site/release-index.json` status `ready`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-runtime-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 300` -> `PASS`.
- Final residual environment gates:
  - Credentialed external connector quick panel and post-update preservation still require a machine with real configured MCP/connector credentials.
  - Destructive online-update rollback on failed service or failed external-connector health check is implemented and smoke-policy covered, but not executed against a credentialed production-like machine in this workspace.
  - Automatic OCR text-box detection for retouch T mode still needs a production OCR endpoint; current v0.3.0 supports user-placed T text targets with style-preservation imagegen instructions.

## S12 Final Shell Polish And Release Re-seal

- Entry:
  - User clarified that the composer top divider should be removed, while the rounded top shell belongs to the main chat/session panel boundary.
- Changes:
  - Removed the extra top divider above the composer area.
  - Added the rounded/top-bordered main chat panel shell and matching rounded header treatment, with mobile reset.
- Final rebuilt package outputs:
  - `release-artifacts/EcoreX_0.3.0-webui-windows-x64.zip`
    - Size: `551265386`
    - SHA256: `3BF9A6546A294C2A96AC32786B7B893848BF3DC6A30897F228AD13CCDC49A48C`
  - `release-artifacts/EcoreX_0.3.0-webui-macos-universal.zip`
    - Size: `652441190`
    - SHA256: `1E7EC8295EEE2736A711AD1C152CB7EABF32A12D61C27DF5CC8EF060A211AA28`
  - `release-artifacts/EcoreX_0.3.0-webui-win-mac.zip`
    - Size: `1204953245`
    - SHA256: `D4E9E0D2EDE0AA200818F9D20E628AB7A9456A962E00EDA1C7DC4A69714543AF`
- Final verification after visual polish:
  - `npm run build:renderer` -> pass, built renderer assets `index-VHJzJbCn.js` and `index-PsAbIX8T.css`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-ecorex-webui-local-release.ps1 -Version 0.3.0` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/release-ecorex-webui-orchestrator.ps1 -Version 0.3.0 -SkipBuild -SkipPackage -AllowDirtyTree -Force` -> pass; `deploy/ecorex-site/release-index.json` status `ready`.
  - Synced Windows, macOS, and combined v0.3.0 WebUI packages into `deploy/ecorex-site/downloads/` and wrote `.sha256` sidecars.
  - Verified `deploy/ecorex-site/downloads/` package size/SHA256 against `deploy/ecorex-site/manifest.json` and `deploy/ecorex-site/release-index.json` for `webui-windows-x64` and `webui-macos-universal`.
  - Pushed branch `codex/ecorex-v0.3.0-hardening` to `origin`.
  - Created source release `https://github.com/zhangyifanjackson-dotcom/EcoreX/releases/tag/v0.3.0`.
  - Created installer asset release `https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/tag/v0.3.0`.
  - Uploaded Windows, macOS, combined WebUI packages and `.sha256` sidecars to the installer asset release.
  - Verified the manifest primary mirror (`ghproxy.net` -> `EcoreX-installers/releases/download/v0.3.0`) returns HTTP 200 and expected `Content-Length` for all three packages.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-package-runtime.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-package-runtime-smoke.json -SmokeRoot tmp\v030-webui-package-runtime-smoke -ExpectedVersion 0.3.0 -Port 9929` -> pass.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-online-update-local.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\user-online-update-local-smoke.json -SmokeRoot tmp\v030-user-online-update-smoke -Version 0.3.0 -SourcePort 9970 -RuntimePort 9939 -TimeoutSeconds 600` -> `PASS`, `7/7`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke-v030-webui-release-cdp.ps1 -PackagePath release-artifacts\EcoreX_0.3.0-webui-windows-x64.zip -OutputPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.json -ScreenshotPath docs\v0.3.0\artifacts\webui-release-cdp-smoke.png -SmokeRoot tmp\v030-webui-release-cdp-smoke -Port 9949 -TimeoutSeconds 300` -> `PASS`.
- Evidence:
  - `docs/v0.3.0/artifacts/webui-hardening-verification.json`
  - `docs/v0.3.0/artifacts/webui-package-runtime-smoke.json`
  - `docs/v0.3.0/artifacts/user-online-update-local-smoke.json`
  - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.json`
  - `docs/v0.3.0/artifacts/webui-release-cdp-smoke.png`
  - `docs/v0.3.0/artifacts/github-release-v030.json`

## S12 User-Observed Hotfix: Image Preview, Update Notice, And Multi-Image Count

- Entry:
  - User reported in the installed v0.3.0 WebUI that image generation could take about 10 minutes without visible return, then the generated image row rendered broken.
  - The update banner kept showing an older `EcoreX 0.2.9.2` admin notice after the runtime was already on `0.3.0`.
  - Generated/retouch artifacts showed a duplicate `preview only` row with only the filename; choosing that row could open an empty precision-retouch canvas.
  - User also reported that imagegen used to support generating two images at once, but the new build only produced images one by one.
- Root causes:
  - Provider HTTP waits still used 300s per provider route and fallback could compound the perceived wait.
  - Runtime-generated images under `runtime-*/images` were valid PNGs but outside `/api/file` preview roots, so the frontend could not load them.
  - The artifact shelf merged legacy filename-only image detections beside absolute-path artifacts and allowed retouch from preview-only rows.
  - `OpenAIProvider._create` hard-coded `n: 1`; direct imagegen schema did not expose `n/count/output_count`, and Web image-job API expanded `output_count=2` into two one-image tasks.
  - Admin release notices were shown as update-state banners even when the notice version was older than or equal to the running version.
- Changes:
  - Added dismissible update-state banners and filtered stale `admin-release-notice` versions at or below the running app version.
  - Added runtime `images/` as an internal read-only preview root, with file-stat/file-serve bypassing the workspace permission broker only for that internal generated-image root.
  - New relative `output_dir=images` resolves to the user workspace rather than the runtime install directory.
  - Artifact shelf now removes bare filename-only duplicate image rows when a concrete local/absolute image artifact with the same basename exists, hides missing source-only images after verification, and only enables precision retouch for verified ready images.
  - Restored native multi-image count: direct `imagegen` schema accepts `n`, `count`, `output_count`, and `num_images`; provider runner and standalone script pass the count; OpenAI sends `n` to generations/edits; non-native providers are looped inside imagegen rather than routed through bash.
  - Imagegen output files from OpenAI multi-image responses now include ordered `01`, `02`, etc. filename segments and artifact indexes.
  - Web image-job no-tasks API now creates one task with `output_count=count` instead of splitting the request into many one-image tasks.
- Verification:
  - `python -m py_compile desktop\runtime\ecorex-runtime\channel\web\web_channel.py desktop\runtime\ecorex-runtime\channel\web\files.py desktop\runtime\ecorex-runtime\agent\tools\imagegen\imagegen.py desktop\runtime\ecorex-runtime\agent\tools\imagegen\provider_runner.py desktop\runtime\ecorex-runtime\skills\image-generation\scripts\generate.py` -> pass.
  - `npm run build:renderer` -> pass.
  - Mocked OpenAI provider smoke with `n=2` -> pass, generated two files named with ordered `01` and `02` segments.
  - Internal preview-root import smoke -> pass for `runtime/images`.
  - `git diff --check` -> pass.
- Residual:
  - The currently running installed runtime must be refreshed through the rebuilt package or update flow before these source fixes affect that live browser tab.

## S13 Public Release Mirror Re-seal

- Entry:
  - User requested the download first source to prefer domestic GitHub mirrors for faster WebUI package updates.
  - Online manifest still exposed the domestic mirror URLs as generic `asset-base` entries, so the user-facing update chain could not clearly treat them as the primary mirror tier.
- Changes:
  - Extended public-release mirror classification so `ghproxy.net` and `ghfast.top` are recognized as `github-release-cn-mirror`, alongside the previous `gh-proxy.com` pattern.
  - Repacked `release-artifacts/EcoreX_0.3.0-public-release.zip` with download mode `github-cn-primary` and mirror order: `ghproxy.net`, `ghfast.top`, GitHub origin fallback.
  - Redeployed the public release with WebUI large package upload skipped, preserving manifest-mirror delivery for Windows/macOS WebUI packages.
- Published public zip:
  - `release-artifacts/EcoreX_0.3.0-public-release.zip`
    - Size: `2874502`
    - SHA256: `F1A9E84796E13020EBEF6884F3662FAE5E531F0AB96DF671382295844DF8AB69`
- Verification:
  - `tar -xOf release-artifacts\EcoreX_0.3.0-public-release.zip site/manifest.json` -> `download.mode = github-cn-primary`; mirrors are `ghproxy.net`, `ghfast.top`, GitHub origin fallback.
  - `python scripts\deploy-v024-production.py --promote-public-release` with `ECOREX_SKIP_WEBUI_DOWNLOAD_UPLOAD=1` -> `PASS`.
  - Online `https://mvdcm.ecoremedia.net/ecorex-agent/manifest.json` -> version `0.3.0`, `download.mode = github-cn-primary`.
  - Online mirror HEAD checks -> `ghproxy.net` and `ghfast.top` return HTTP `200` and expected `Content-Length` for both `webui-windows-x64` and `webui-macos-universal`.
- Evidence:
  - `docs/v0.3.0/artifacts/public-release-mirror-online-check.json`
  - `docs/v0.3.0/artifacts/production-deploy-online.json`

# EcoreX v0.2.9 Development Log

## 2026-07-04 21:16 +08:00

- Started long goal for v0.2.9 WebUI-focused release.
- Initial `git status --short` showed a pre-existing dirty worktree across runtime, WebUI, deploy, scripts, docs, and tests.
- Cleaned temporary workspace artifacts:
  - `.tmp-fs-profile-*`
  - `.tmp-pip-probe-*`
  - `.tmp-runtime-*`
  - `.tmp-nonexistent-skills-review`
  - `.tmp-win-sign-preflight-*.log`
  - most of top-level `tmp`
- Cleanup left top-level `tmp` because two log files under `tmp/v0272-gemini-runtime-source/` were locked by another process. No process was killed.
- Preserved source files, docs, release artifacts, and all unrelated dirty changes.

## Standing Verification Rules

- Run focused tests/checks for changed subsystems.
- Do not run `scripts/真实发布校验.py`.
- Validate v0.2.8 to v0.2.9 online upgrade before closing the goal.

## 2026-07-04 22:12 +08:00

- Continued v0.2.9 development in the current thread after the previous thread hit an invalid historical image context.
- Implemented S01/S02/S03 audit and usage-panel work:
  - Added runtime audit business action projection with `actionTypeCounts` and `userActions`.
  - Counts image processing for `image_job.*` events and `tool.*` events whose detail references `imagegen`.
  - Kept local file processing out of top-level action metrics.
  - Added automatic effective artifact projection from `sync_artifacts`.
  - Effective artifact rule is thumbs up, or default/no feedback with a final artifact.
  - Invalid artifact rule is thumbs down or explicit invalid marker.
  - Added `feedbackTraces` with marking user identity, artifact hash/label, feedback time, and session-share trace URL.
  - Added `feedbackShareId` and `feedbackShareUrl` passthrough from WebUI feedback to WebChannel sync and Admin projection.
  - Added `/ecorex-agent/usage-panel/` static alias and `/ecorex-agent/usage-panel/api/*` Admin API alias for Caddy/Nginx and AdminHandler path normalization.
  - Reworked the admin runtime audit panel into summary cards, action categories, recent user actions, effective artifacts, feedback traces, and collapsed technical details.
- Focused verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_admin_device_id.py -q` passed: 26 tests.
  - `npm run typecheck` in `desktop/` passed.
- Initial pytest run without disabling plugin autoload failed before collecting tests because the local `langsmith` pytest plugin loaded an incompatible `pydantic-core` version. The focused test passed with external pytest plugins disabled.

## 2026-07-04 22:18 +08:00

- Read source thread `019f2d72-6829-76a1-9a15-1d4daad0386c`.
- Added S10 as an independent pending slice for Tencent Docs MCP WebUI out-of-box capability.
- Preserved the source thread boundary: WebUI-only, WorkBuddy-style document selection, and selected Tencent Docs files added to the current conversation/task as remote attachments.
- Captured product constraints:
  - use official remote MCP endpoint `https://docs.qq.com/openapi/mcp`
  - store user token only in local MCP config as the `Authorization` header
  - never echo token in API/UI/log summaries
  - avoid local-file reads for Tencent Docs remote attachments
  - avoid proactive document mutation unless explicitly requested

## 2026-07-04 22:28 +08:00

- Implemented S04 knowledge graph WebUI display in the desktop WebUI settings memory area.
- Added graph API client types and calls for `/api/knowledge/graph` and `/api/knowledge/read`.
- Added a stable SVG knowledge graph with category colors, links, node labels, keyboard-selectable nodes, legend, and selected-node detail/excerpt panel.
- Kept scope limited to existing knowledge-base graph data.
- Focused verification:
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build:renderer` in `desktop/` passed.
  - Vite emitted a chunk-size warning for the existing renderer bundle size.

## 2026-07-04 22:40 +08:00

- Implemented S05 default identity injection.
- Updated default runtime persona to `小芯 / 同学 / 专业严谨`.
- Added migration from previous default EcoreX persona to the new `小芯` persona while preserving custom personas.
- Updated workspace `AGENT.md`, `USER.md`, and `BOOTSTRAP.md` templates so first-run no longer proactively asks users to define the assistant name, user address, or style.
- Updated WebUI conversation-facing copy to use `小芯` where it refers to chatting with the assistant.
- Updated legacy assistant self-name sanitization so old `CowAgent` self-references become `小芯`.
- Focused verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_default_identity.py -q` passed: 5 tests.
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_ecorex_web_parallel_backend.py::TestWebParallelHandlers::test_v022_hotfix_auth_identity_feishu_and_artifact_contracts -q` passed: 1 test.
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build:renderer` in `desktop/` passed.
  - Vite emitted a chunk-size warning for the existing renderer bundle size.

## 2026-07-04 22:45 +08:00

- Implemented S06 thinking motion upgrade.
- Main message flow now uses a restrained pulse for the `思考中` indicator.
- Expanded process details now use staged icons for reasoning, search/browse, tool execution, artifact/media generation, and generic phase rows.
- Motion remains CSS-only and respects `prefers-reduced-motion`.
- Focused verification:
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build:renderer` in `desktop/` passed.
  - Vite emitted a chunk-size warning for the existing renderer bundle size.

## 2026-07-04 22:47 +08:00

- Completed S07 scheduler module UI readability upgrade.
- Kept scheduler API behavior unchanged.
- Confirmed generated scheduler tasks render as visual task cards with separated main info, schedule/action type, next/last run metadata, errors, and actions.
- Refined scheduler task-card CSS so desktop uses distinct main/meta/action regions and mobile collapses to one column.
- Focused verification:
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build:renderer` in `desktop/` passed.
  - Vite emitted a chunk-size warning for the existing renderer bundle size.

## 2026-07-04 23:03 +08:00

- Completed S08 version and release metadata.
- Updated current WebUI version anchors to `0.2.9`:
  - `cli/VERSION`
  - desktop `package.json` and root `package-lock.json` versions
  - WebUI local packager default version
  - public install script User-Agent and version banners
  - Admin API version and current client event key
  - WebChannel enterprise client key list and current WebUI User-Agent strings
- Updated current WebUI release notes for v0.2.9 audit, effective artifacts, feedback traces, knowledge graph, default identity, thinking motion, and scheduler UI improvements.
- Updated public manifest root version, mirrors, and WebUI Windows/macOS artifact names to `0.2.9`.
- Marked v0.2.9 WebUI manifest artifacts as `pending-build` with empty size/SHA until S09 generates real artifact evidence; this avoids reusing v0.2.8 hashes for v0.2.9 file names.
- Focused verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_release_metadata.py tests/test_v025_runtime_manifest.py::test_v027_webui_installers_keep_windows_macos_user_flow_consistent tests/test_ecorex_admin_device_id.py::AdminReleaseStateTest::test_client_release_notice_endpoint_returns_admin_data_notice tests/test_ecorex_web_parallel_backend.py::TestWebParallelHandlers::test_enterprise_release_notice_uses_current_client_key_after_legacy_key -q` passed: 6 tests.
  - `npm run typecheck` in `desktop/` passed.
  - Plain pytest collection without disabling plugin autoload still fails before collection because local `langsmith` loads an incompatible `pydantic-core`.

## 2026-07-04 23:33 +08:00

- Completed S10 Tencent Docs MCP WebUI out-of-box capability.
- Added WebUI backend endpoints for Tencent Docs status, connect, disconnect, files, and search.
- Connection writes only local workspace `mcp.json` for server `tencent-docs` with the official endpoint `https://docs.qq.com/openapi/mcp` and an `Authorization` header; status/list responses remain redacted.
- Added runtime MCP status/tool-count projection and heuristic normalization for discovered Tencent Docs MCP file-list/search tools.
- Added permission-broker default allow for noninteractive Tencent Docs MCP startup only when `server=tencent-docs` and the URL exactly matches the official endpoint.
- Extended WebUI attachments with remote provider metadata and preserved Tencent Docs remote attachments through persistence and retry-draft recovery.
- Added composer Tencent Docs entry point, token connection dialog, recent/my/search picker, multi-select add-to-chat flow, attachment tray rendering, and remote open-link handling.
- Added hidden context so Tencent Docs attachments are treated as remote documents, not local file paths, and content reads go through discovered `tencent-docs` MCP tools unless the user asks for document mutation.
- Focused verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_tencent_docs_mcp.py -q` passed: 4 tests.
  - `npm run typecheck` in `desktop/` passed.
  - `npm run build:renderer` in `desktop/` passed; Vite emitted the existing renderer chunk-size warning.
  - Combined focused Python check passed: 10 tests across Tencent Docs MCP, v0.2.9 release metadata, installer metadata, Admin release notice, and WebChannel release-notice key compatibility.

## 2026-07-05 00:23 +08:00

- Completed S09 focused verification and online upgrade smoke.
- Built local v0.2.9 WebUI packages:
  - `release-artifacts/EcoreX_0.2.9-webui-windows-x64.zip`
  - `release-artifacts/EcoreX_0.2.9-webui-macos-universal.zip`
- Copied v0.2.9 WebUI artifacts into `deploy/ecorex-site/downloads/`.
- Promoted public WebUI Windows/macOS artifacts in `deploy/ecorex-site/manifest.json` to `ready` with real size/SHA:
  - Windows size `550795622`, SHA256 `3323BD22C920C7AA5CD42D4F42D2C1F8322CF76BCF08DD2F90CEDE5EC813FC73`
  - macOS size `652254333`, SHA256 `6EEC23D9FB9781F7699BE1B8D7FB5F1EEE5F502861B2DD5107D31F629A14F7E1`
- Downloaded `EcoreX_0.2.8-webui-windows-x64.zip` from the v0.2.8 release mirror and verified its documented size/SHA before using it as the legacy package.
- Ran local online upgrade smoke from v0.2.8 to v0.2.9 using local static release site `http://127.0.0.1:9808` and `ECOREX_RELEASE_MANIFEST_URL=http://127.0.0.1:9808/manifest.json`.
- Fixed `scripts/smoke-v028-legacy-webui-online-upgrade.ps1` so the downloaded online installer receives `-BaseUrl $BaseUrl`; first attempt failed because it fell back to the production v0.2.8 manifest.
- Upgrade smoke evidence:
  - `docs/v0.2.9/artifacts/legacy-webui-online-upgrade.json`
  - Status `PASS`, check count `4`, pass count `4`, target `0.2.9`, upgraded runtime `runtime-0.2.9-b33f92af`.
- Cleanup note: `C:\ecx-upgrade-smoke-v029` could not be fully removed because Edge cache files under the isolated test profile remained locked.
- `scripts/真实发布校验.py` was not run.

## 2026-07-05 08:23 +08:00

- Deployed v0.2.9 online to production.
- Built missing deployment artifacts:
  - `release-artifacts/EcoreX_0.2.9-web-linux-service.tar.gz`
  - `release-artifacts/EcoreX_0.2.9-public-release.zip`
- Promoted the public manifest `web-linux-service` artifact to v0.2.9 ready metadata:
  - Linux service size `4151742`, SHA256 `D6930A82DF7C5302E4CC3E95930CF4AFF8B2E370679525A8E954270E956AC36E`
- Created GitHub Release `v0.2.9` in `zhangyifanjackson-dotcom/EcoreX-installers` and uploaded:
  - `EcoreX_0.2.9-webui-windows-x64.zip`
  - `EcoreX_0.2.9-webui-macos-universal.zip`
  - `EcoreX_0.2.9-web-linux-service.tar.gz`
  - `EcoreX_0.2.9-web-linux-service.tar.gz.sha256`
- Ran production deployment with stable public-release promotion.
- Production deploy evidence:
  - `docs/v0.2.9/artifacts/production-deploy-online.json`
  - Status `PASS`; public manifest, staged manifest, installation manifest, and Web service version are all `0.2.9`; service is active and enabled; `/api/version` returned 200.
- Triggered Admin API release notification for the current stable v0.2.9 release.
  - `docs/v0.2.9/artifacts/production-release-notify.json`
  - Status `PASS`; notice file and runtime update-state both report version `0.2.9`.
- Ran external production HTTP smoke.
  - `docs/v0.2.9/artifacts/production-online-smoke.json`
  - Status `PASS`; manifest is `0.2.9`; origin download `Content-Length` values match all three ready artifacts; install scripts contain `0.2.9`; admin and usage-panel require auth; client model config rejects unauthenticated access; Web API version and update-state are both `0.2.9`.
- Verified GitHub Release assets and download mirrors.
  - `docs/v0.2.9/artifacts/github-release-assets-v029.json`
  - `docs/v0.2.9/artifacts/production-download-mirror-head-smoke.json`
  - Both report `PASS`; `ghproxy` and GitHub fallback HEAD probes return 200 with matching `Content-Length` for the three public download artifacts.
- `scripts/真实发布校验.py` was not run during deployment.

## 2026-07-05 08:39 +08:00

- Fixed the missed independent production usage-panel slice.
- Investigation found the real production nginx route for `/ecorex-agent/usage-panel/` points to `/srv/ecorex-agent-usage-panel/current/` and proxies `/ecorex-agent/usage-panel/api/*` to `127.0.0.1:18105/api/*`; it is separate from `/srv/ecorex-agent-download/current/admin/`.
- The independent usage-panel `current` symlink was still on the 2026-06-29 release and did not include v0.2.9 audit markers or `/api/runtime-audit`.
- Added tracked independent slice files under `deploy/ecorex-usage-panel/`.
- Deployed the v0.2.9 independent audit panel to `/srv/ecorex-agent-usage-panel/releases/20260705003507-v0.2.9-audit-panel` and atomically moved `/srv/ecorex-agent-usage-panel/current`.
- Updated and restarted `ecorex-usage-panel-api.service`; the service now exposes `/api/runtime-audit`, `/api/state`, and compatible `/api/data` using the deployed Admin API `AdminStore.runtime_audit` projection.
- Evidence:
  - `docs/v0.2.9/artifacts/production-independent-usage-panel-inspect.json`
  - `docs/v0.2.9/artifacts/production-independent-usage-panel-deploy.json`
  - `docs/v0.2.9/artifacts/production-independent-usage-panel-postdeploy-smoke.json`
- Postdeploy smoke status `PASS`:
  - `currentTarget` is `/srv/ecorex-agent-usage-panel/releases/20260705003507-v0.2.9-audit-panel`
  - service is `active`
  - unauthenticated public `/ecorex-agent/usage-panel/` returns `401`
  - local `/api/runtime-audit` returns `200`, version `0.2.9`
  - response includes `actionTypeCounts`, `userActions`, `effectiveArtifacts`, and `feedbackTraces`
  - deployed static files include `EcoreX v0.2.9`, `有效产物`, and `下拇指回溯` markers

## 2026-07-05 08:50 +08:00

- Re-audited thread `019f2d26-2e04-7ce0-a136-c0eee16deb7f` against the current v0.2.9 plan and evidence.
- Confirmed the original plan items are covered by the current acceptance checklist and production evidence:
  - audit action taxonomy
  - imagegen as image processing
  - local file processing excluded from top-level visible metrics
  - effective artifact automation
  - thumbs-down feedback traceability
  - usage-panel production route
  - knowledge graph frontend display
  - default Xiaoxin identity
  - thinking motion
  - scheduler readability
  - Tencent Docs MCP WebUI capability
  - version/release metadata
  - v0.2.8 to v0.2.9 online upgrade
  - production deploy and independent usage-panel deploy
- Found one missed test maintenance item: `tests/test_v029_release_metadata.py` still expected `web-linux-service` to remain at v0.2.8 from the earlier WebUI-only packaging stage.
- Updated the test to match the actual production deployment shape: `web-linux-service` is now v0.2.9 ready and its local download source size/SHA must match the manifest.
- Added release metadata test coverage for the independent usage-panel slice and its production postdeploy smoke evidence.
- Focused verification passed:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest ... -q`
  - Result: `19 passed`, with only existing pydub/ffmpeg warnings.
  - `python -m py_compile deploy/ecorex-usage-panel/usage_panel_api.py` passed.
  - `node --check deploy/ecorex-usage-panel/app.js` passed.
- Residual non-functional note: slice document filenames still have historical numbering drift (`S07-version-release.md` carries the plan's S08 content, and `S08-verification-upgrade.md` carries the plan's S09 content). No product or deployment gap was found from this.

## 2026-07-05 15:35 +08:00

- Implemented WebUI-only follow-up slice `S12-webui-followups-tencent-retouch-knowledge`.
- Tencent Docs:
  - Removed the composer Tencent Docs entry from the React WebUI.
  - Added Tencent Docs under Settings > External Connections with an agent-chat connection action.
  - Swapped the settings card logo to the official Tencent Docs asset.
  - Disabled the static v0.2.9 overlay injections that previously re-added the composer entry and memory star-map tab.
- Session recovery:
  - Added readable-title filtering so icon-only or image-only session summaries do not become sidebar titles.
  - Added readable title seeding for attachment-only Tencent Docs sends.
  - Retrying active empty-session selection now triggers history recovery to reduce blank-screen regressions.
- Precise retouch:
  - Reworked the image retouch modal into a canvas-editor layout with top controls, centered canvas, bottom floating toolbar, and right style panel.
  - Replaced oversized filled arrows/boxed labels with curved open-arrow annotations and smaller inline text labels.
- Memory and knowledge graph:
  - Removed knowledge graph from Settings > Memory.
  - Added an independent Settings > Knowledge Graph page with larger graph canvas, node path/category/degree, longer excerpt, and related-node list.
- Verification:
  - `npm run build:renderer` in `desktop/`: PASS.
  - `npm run typecheck` in `desktop/`: PASS.
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_webui_followups.py -q`: PASS, `4 passed`.
  - Chrome headless rendered the local handtest page with mock bridge: PASS; DOM includes the handtest user, readable Tencent Docs session row, and composer local-attachment-only control.
- Evidence:
  - `docs/v0.2.9/artifacts/webui-followups-local-smoke.json`
- Deployment status: not deployed. Waiting for user hand test confirmation before production deployment.

## 2026-07-05 16:02 +08:00

- Applied the latest WebUI follow-up corrections before server deployment:
  - Restored the composer grid to the compact v0.2.9-style shape after removing the Tencent Docs composer button.
  - Removed the extra new-session bottom offset that made the composer appear to float.
  - Changed Knowledge Graph so the knowledge network is full-width by default.
  - The right-side knowledge detail/summary panel now appears only after a node is clicked.
  - Removed the lower related-node card list from the knowledge detail panel.
  - Translated knowledge categories such as `memory`, `session`, `image`, and `external` into Chinese labels.
- Verification:
  - `npm run typecheck` in `desktop/`: PASS.
  - `npm run build:renderer` in `desktop/`: PASS.
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_v029_webui_followups.py -q`: PASS, `4 passed`.
  - `node scripts/smoke-v029-webui-followups-cdp.mjs`: PASS; real-click CDP smoke covered composer, Tencent Docs external connection, Knowledge Graph default/click states, Memory page, and the retouch editor.
- Evidence updated:
  - `docs/v0.2.9/artifacts/webui-followups-local-smoke.json`

## 2026-07-05 17:26 +08:00

- Completed the WebUI-only production rollout for the follow-up slice.
- Composer rollback:
  - Restored the chat composer to the compact v0.2.9-style grid after the Tencent Docs composer entry was removed.
  - Verified the online composer no longer floats: CDP measured `composerHeight = 91`, `zonePaddingBottom = 12`, and the textarea/model/send controls remain inside the composer.
- Production packaging and asset sync:
  - Rebuilt v0.2.9 WebUI packages with the latest follow-up fixes.
  - Uploaded and clobbered the GitHub release assets so domestic mirror and origin sizes match the current manifest.
  - Current key package hashes:
    - `EcoreX_0.2.9-webui-windows-x64.zip`: `0E7870C5A784F958EF7EFB2770ADFF05A7467C55C2C50D0690444AAAFA8147C1`
    - `EcoreX_0.2.9-webui-macos-universal.zip`: `580E4D2385BCA2E53149FC85D65822107CEB08504C2854E269183B674A1B92E4`
    - `EcoreX_0.2.9-web-linux-service.tar.gz`: `C918F74388A0B9C3C7EFD60E9BD028CCF56BB525F95A854C0A1E1073E6368566`
- Production deployment:
  - Deployed the v0.2.9 web service and promoted the public release manifest.
  - Confirmed `/api/version`, public manifest, staged manifest, installation manifest, and web service all report `0.2.9`.
- Real online verification:
  - `WEBUI_HANDTEST_URL=https://mvdcm.ecoremedia.net/ecorex-agent/app/ node scripts/smoke-v029-webui-followups-cdp.mjs`: PASS.
  - `docs/v0.2.9/artifacts/webui-followups-server-cdp-smoke.json`: PASS for composer rollback, Tencent Docs external connection, Knowledge Graph default/click states, Memory page graph removal, and retouch editor interactions.
  - `docs/v0.2.9/artifacts/production-online-smoke.json`: PASS.
  - `docs/v0.2.9/artifacts/production-download-mirror-head-smoke.json`: PASS; ghproxy and GitHub origin HEAD sizes match for all WebUI release artifacts.
  - `docs/v0.2.9/artifacts/production-online-update-real-smoke.json`: PASS; a real install fetched the Windows WebUI package from ghproxy first, verified SHA256, installed runtime `0.2.9`, launched an isolated WebUI on `9910`, and reported background update `installed`.
- Cleanup:
  - Stopped only the isolated smoke-test runtime/browser processes and removed `tmp/online-update-smoke-final-20260705164337`.
  - Left the existing user WebUI listener on `9909` untouched.
- Test maintenance:
  - Hardened `scripts/smoke-v029-production-online-update.ps1` so it checks stable production bundle markers instead of minified Chinese UI text.
  - Fixed the script's runtime readiness URL to query `/api/version` instead of `/app/api/version`.
- Safety note:
  - Did not run `scripts/真实发布校验.py`; that checker is intentionally skipped because it can expose credentials in logs.

## 2026-07-05 18:10 +08:00

- Started the post-deploy WebUI hotfix slice for the latest user findings.
- Precise retouch:
  - Changed retouch submit from immediate image-job creation to a chat-composer staging flow.
  - The annotated image is uploaded as an attachment, a thumbnail appears in the composer tray, and the structured edit prompt is appended to the composer so users can stage multiple retouch images before sending once.
- Session share:
  - Added share-safe image thumbnail generation for real artifact media.
  - Added click-to-enlarge lightbox and save/open actions in shared session pages.
- Knowledge Graph:
  - Added blank-canvas click handling to close the selected node detail panel.
- Local WebUI desktop entry:
  - Replaced direct URL desktop shortcuts with restart-safe launchers.
  - Windows now writes an `EcoreX WebUI.lnk` target to `Launch EcoreX WebUI.ps1`, which starts or reuses the local runtime before opening the browser.
  - macOS now writes an `EcoreX WebUI.command` launcher that restarts the local runtime after reboot and then opens the saved WebUI URL.
  - Updated public installer copy so users are not told to rely on a URL-only shortcut after shutdown.
- Verification so far:
  - `scripts/prepare-ecorex-webui-local-release.ps1` parses as PowerShell.
  - Focused WebUI/share static tests passed: `7 passed`, with only existing pydub/ffmpeg warnings.
- Deployment status: not deployed yet. Full typecheck, build, browser smoke, packaging, upload, and production smoke remain pending.

## 2026-07-05 18:35 +08:00

- Completed local verification for the post-deploy hotfix slice.
- Tencent Docs:
  - Strengthened the Settings > External Connections agent prompt so it explicitly guides token acquisition, `/api/tencent-docs/connect`, `/api/tencent-docs/status?start=1`, and MCP-based read/search without echoing tokens.
  - Added a backend ready-wait after explicit Tencent Docs MCP start/connect so the UI does not return a stale pending state immediately after token configuration.
  - Checked the latest local WebUI logs; no recent Tencent Docs action records were present, matching the earlier “no effective connection attempt” symptom.
- Precise retouch:
  - Fixed composer attachment thumbnails so uploaded `preview_url` images render as real thumbnails, not generic file icons.
- Local WebUI desktop entry:
  - Generated Windows/macOS launcher scripts both parse successfully; macOS validation uses LF normalization matching release packaging.
- Verification:
  - `npm run typecheck` in `desktop/`: PASS.
  - `npm run build:renderer` in `desktop/`: PASS.
  - `python -m py_compile channel/web/web_channel.py deploy/ecorex-admin-api/ecorex_admin_api.py`: PASS.
  - Focused Tencent Docs/WebUI pytest: PASS, `7 passed`.
  - Local CDP real-click smoke: PASS; composer has retouch thumbnail + batch draft, Knowledge Graph blank click closes detail, Tencent Docs remains only in Settings > External Connections.
- Evidence updated:
  - `docs/v0.2.9/artifacts/webui-followups-local-smoke.json`
- Deployment status: ready to package and deploy.

## 2026-07-05 23:27 +08:00

- Completed the final v0.2.9 WebUI hotfix deployment after the GitHub/mirror mismatch.
- GitHub release upload:
  - Re-uploaded the Windows and macOS v0.2.9 packages with `gh release upload` only; the interrupted REST/curl upload was stopped and not used.
  - Verified GitHub release asset size/digest for all published files:
    - `EcoreX_0.2.9-webui-windows-x64.zip`: `550836551`, `39E5DA8C1A4477AE09D14261125F88A2BC861BD1E561AF58DFB805EE51D4AA06`
    - `EcoreX_0.2.9-webui-macos-universal.zip`: `652294908`, `317F3B062954C45984550E99F04C7B6E6FF2CC77CC54EB4CCD5F16E28E08C945`
    - `EcoreX_0.2.9-web-linux-service.tar.gz`: `4214588`, `AE86A325A8975C36CC50FF0B1DC2E343FEA230AB17C04223001112DADD446DA3`
- Download mirror policy:
  - Fixed the stable public manifest so the first download source is the hosted cache `https://mvdcm.ecoremedia.net/ecorex-agent/downloads`.
  - Kept `ghproxy.net` as the second mirror and GitHub Release as the final fallback.
  - Promoted the refreshed public release to stable with `python scripts/deploy-v024-production.py --promote-public-release`.
- Verification:
  - `python scripts/validate-ecorex-release-artifacts.py --version 0.2.9 --public-zip release-artifacts/EcoreX_0.2.9-public-release.zip`: PASS.
  - Focused release metadata pytest: PASS, `1 passed`.
  - Online manifest now lists `ecorex-download-cache-v0.2.9` as mirror index 0 and the final package sizes/hashes above.
  - Online Range probes returned `206` for hosted-cache Windows/macOS/Linux downloads and ghproxy fallback, with expected total sizes.
  - Online CDP WebUI smoke: PASS for Tencent Docs external connection/logo, compact composer, Knowledge Graph default/detail/blank-close, Memory page graph removal, and retouch image+prompt staging.
  - Online update evidence: PASS; installer stdout shows first source `https://mvdcm.ecoremedia.net/ecorex-agent/downloads/EcoreX_0.2.9-webui-windows-x64.zip`, package SHA256 matched manifest, runtime `/api/version` returned `0.2.9`, and `update-state.json` reported background update `installed` with health check `pass`.
- Evidence updated:
  - `docs/v0.2.9/artifacts/online-manifest-after-download-cache-promote.json`
  - `docs/v0.2.9/artifacts/webui-followups-server-cdp-smoke-after-download-cache-rerun.json`
  - `docs/v0.2.9/artifacts/production-online-update-real-smoke-after-download-cache.json`
- Cleanup note:
  - Stopped the isolated update-smoke runtime and parent process. A small Edge profile residue of about 7.6 MB remained under `tmp/online-update-smoke-final-20260705230040` because Edge held profile files; the 550 MB downloaded package and runtime were removed.

## 2026-07-06 08:10 +08:00

- Changed the v0.2.9 public download priority back to `ghproxy` first, including online/background update flows.
- Manifest mirror order is now:
  - `asset-mirror-v0.2.9`: `https://ghproxy.net/https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v0.2.9`
  - `ecorex-download-cache-v0.2.9`: `https://mvdcm.ecoremedia.net/ecorex-agent/downloads`
  - `github-release-v0.2.9`: GitHub Release origin.
- Updated release generation scripts so future public-release packages preserve this order.
- Updated the online update smoke expectation so the first download source is `ghproxy`.
- Verification:
  - Rebuilt `release-artifacts/EcoreX_0.2.9-public-release.zip`; new SHA256 `8885D18128CF9FE0E3A9CE8E38A89E6082FF60133B03905F7ED16EE512676AF3`.
  - `python scripts/validate-ecorex-release-artifacts.py --version 0.2.9 --public-zip release-artifacts/EcoreX_0.2.9-public-release.zip`: PASS.
  - Focused release metadata pytest: PASS, `1 passed`.
  - Production deploy with `python scripts/deploy-v024-production.py --promote-public-release`: PASS.
  - Online manifest now has `ghproxy` as mirror index 0.
  - Online update first source resolves to `https://ghproxy.net/.../EcoreX_0.2.9-webui-windows-x64.zip`.
  - `ghproxy` Range probes returned `206` for Windows, macOS, and Linux service artifacts with expected total sizes.
- Evidence:
  - `docs/v0.2.9/artifacts/ghproxy-first-download-source-online.json`

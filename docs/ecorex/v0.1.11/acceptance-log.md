# EcoreX v0.1.11 Acceptance Log

## Desktop
| Item | Status | Evidence |
| --- | --- | --- |
| Version | Pass | `desktop/package.json` and `desktop/package-lock.json` are `0.1.11`. |
| Windows installer | Pending signature | Current rebuilt installer is `EcoreX_0.1.11_x64-setup.exe`, size `117,572,805`, SHA256 `CF0C5FFAFDF8A0C7FC0991BDFCBE5609917375D232AA419AAFCAE6329321CD18`. It contains the latest fixes but is not signed yet. |
| Windows signature | Blocked | The previous installer was Authenticode `Valid`, but the current rebuilt installer is `NotSigned`. The certificate is visible in CurrentUser/My. Windows SDK `signtool /debug` finds the certificate, then drops from `After Hash filter, 1 certs were left` to `After Private Key filter, 0 certs were left`, proving the private-key provider is not currently exposed. `SimplySign CSP` and `SimplySign KSP` are installed, but `certutil -key` lists no active key container. Smart Card services are stopped and require UAC/admin approval to start. |
| Installed Windows smoke | Pass unsigned | Silent install, app launch, sidecar ready, `/auth/check` success, and `/api/tools` returned `bash`, `web_fetch`, and `browser` for the current rebuilt installer. Signed smoke must be repeated after Authenticode signing. |
| Renderer visual smoke | Pass | Playwright screenshots passed for auth, main, settings, abilities, light and dark. |
| Left sidebar latest control | Pass | Left sidebar keeps new chat and search only; "回到最新消息" remains inside the chat pane and appears when the user scrolls away from the latest message. |
| SSE restoration | Pass | Desktop stream handler supports reasoning/thinking, message_end, tool_start, tool_end, media/file events, phase, delta/message_update, done, cancelled, and error. |
| Long reply collapse | Pass | `MessageContent` collapses long assistant replies by default; user messages are not collapsed. |
| Factory persona | Pass | Runtime config templates require EcoreX identity, professional/rigorous tone, and address users as "同学". |
| Tool call noise | Pass unsigned installed smoke | Same-name streamed tools now update/compact to one visible row, avoiding repeated `web_fetch`/bash rows in the chat transcript. Failed tools open by default so safety warnings are visible. |
| Desktop tool execution permission | Pass local simulation | Root cause for "bash/shell/browser unavailable but no confirmation popup" was that Electron permissions only covered `openPath`. Added a sidecar permission broker and SSE `tool_permission_request` flow so `bash`, `shell`, `terminal`, and `browser` pause for Allow once / Always allow / Deny decisions, write to the shared permission audit, and return a clear tool error on deny/timeout/cancel. `py_compile`, desktop `npm run typecheck`, and a broker allow-once simulation passed. |
| Local artifacts | Pass unsigned installed smoke | Structured file SSE events now include local `path`; generated artifacts render as local-open controls instead of inert/download links. Electron `openPath` auto-allows managed EcoreX workspaces such as `~/cow` for non-dangerous files. |
| Images in chat | Pass | DOM smoke verified Markdown images render as `.markdown-image`; local image media steps use `/api/file` preview URLs. |
| Web links in chat | Pass | DOM smoke verified Markdown HTTP links render with the correct `href` and `target="_blank"`, which Electron routes to `shell.openExternal`. |
| Usage accounting | Pass unsigned installed smoke | Desktop telemetry now reports the max of provider usage and local stream estimate, including streamed text, reasoning, tool payloads, and inline replies, preventing obvious undercounts such as 45 tokens for multi-tool tasks. |
| Clean release boundary | Pass unsigned package scan | `desktop/runtime/ecorex-runtime` and `desktop/release/win-unpacked/resources` were scanned for local DB/log/session/cache/userData artifacts after rebuilding. No AppData, session history, permission audit, pasted-files cache, or `~/cow` artifacts were found. |

## Admin And Download
| Item | Status | Evidence |
| --- | --- | --- |
| Admin API version | Pass | Production container rebuilt with `VERSION = "0.1.11"` and container `xhs-report-ecorex-admin-api-1` is healthy. |
| Client key compatibility | Pass | Public `/client/capability-policy` accepts `ecorex-desktop-v0.1.11` and returns version `0.1.11`. `/client/model-config` requires a user token after client-key validation, as expected. |
| Capability policy default | Pass | Production DB row is migrated to `mode = preinstall`, so login-time preload/preinstall and default-on capability selection can proceed without being stuck in ask mode. |
| Windows manifest | Pass | `deploy/ecorex-site/manifest.json` records the latest Windows installer size/hash. |
| WebUI Linux artifact | Pass | `EcoreX_0.1.11-web-linux-service.tar.gz`, size `2,846,017`, SHA256 `7C08D86502943275E40E1924D6283D5419C2A2BF769078EB9AABC9B3E3AE9FC2`; tar scan has `0` desktop entries and checksum validation covered `561` tar entries. |
| WebUI desktop visual shell | Pass | `/app/` packages `channel/web/static/app` with the existing desktop renderer static assets. Smoke verified bridge/base injection and `assets/index-CfGWwpfy.js` plus `assets/index-BtzBH59C.css` both return HTTP 200. |
| WebUI package runtime smoke | Pass | Extracted the final tarball to a temp directory, started its `runtime/app.py` with a temp venv/workspace, and verified `/auth/login`, `/app/`, JS/CSS assets, `/api/ui-state`, and `/api/installations`. |
| WebUI co-install state | Pass | Web backend exposes `/api/installations` and `/api/ui-state`, stores under `<agent_workspace>/.ecorex/`, and `/message` uses cross-process per-session locks. |
| WebUI concurrent install guard | Pass production | `scripts/install-ecorex-web.sh` serializes installs with `/var/lock/ecorex-web-install.lock` and stale-lock cleanup. Production install completed on Ubuntu 22.04 after adding `python3.10-venv`. |
| macOS DMG artifacts | Pass unsigned | GitHub Actions run `27412042545` produced DMGs with `notarize=false`. arm64 size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`; x64 size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`. |
| Public release zip | Pass Web + macOS unsigned | `EcoreX_0.1.11-public-release.zip`, size `311,225,935`, SHA256 `71D2196AEEF4A331F321D996839E09D8D9A03B70DB113C959B4048F48B6C9DE7`; structure validation found static site, Admin API, server helpers, strict JSON `checksums.json`, the ready Web tarball, and both macOS unsigned DMGs. Server install check skips Windows as `pending-signature` and validates ready Web/macOS artifacts. |
| Public deployment | Pass Web-first | `https://www.ecoreai.cn/ecorex-agent/manifest.json` is live as v0.1.11. Windows is disabled as `pending-signature`; macOS DMGs are downloadable as `ready-unsigned`; WebUI tarball is `ready` and matches SHA256 `7C08D86502943275E40E1924D6283D5419C2A2BF769078EB9AABC9B3E3AE9FC2`. `ecorex-web.service` is active, public `/app/`, `/auth/check`, `/api/version`, and `/stream` checks pass. |
| Public verifier | Pass Web-first | `scripts/verify-ecorex-release.ps1 -ExpectedVersion 0.1.11 -ClientEventKey ecorex-web-v0.1.11-web.1 -SkipGitRemoteCheck` returned `0` blockers. Warnings are expected for Windows `pending-signature`, macOS local validation, missing user token, and skipped GitHub sync. |

## Notes
- A hand-test reported `invalid client key` after installing v0.1.11. Root cause: production Admin API was still v0.1.10 and accepted only the old client key. The production container was rebuilt with the v0.1.11 Admin API and env compatibility keys.
- A later hand-test reported noisy tool rows, multiple orange waiting rows, inert artifact clicks, possible image/link rendering gaps, and suspiciously low usage accounting. The renderer, Electron permission manager, Web SSE file events, desktop usage telemetry, and capability preinstall policy were patched; a new signed installer must supersede the unsigned Windows artifact before Windows public download is enabled.
- The v0.1.11 public package must be a clean release build. Do not include local AppData, local conversation history, runtime logs, or files generated under the developer machine's `~/cow` workspace.
- macOS signing/notarization/Gatekeeper validation is not proven by Windows tests. If unsigned GitHub Actions DMGs are used, the download page must not claim notarization.

# EcoreX v0.1.11 Acceptance Log

## Desktop
| Item | Status | Evidence |
| --- | --- | --- |
| Version | Pass | `desktop/package.json` and `desktop/package-lock.json` are `0.1.11`. |
| Windows installer | Pending signature | Current rebuilt installer is `EcoreX_0.1.11_x64-setup.exe`, size `117,428,366`, SHA256 `0E18A6FC935EA37D93452B238BD3A313673BF3970B2BE7AF381ABB3AA4F06851`. It contains the latest fixes but is not signed yet. |
| Windows signature | Blocked | The previous installer was Authenticode `Valid`, but the current rebuilt installer is `NotSigned`. The certificate is visible in CurrentUser/My; `signtool` and `Set-AuthenticodeSignature` are blocked on cloud-signing/private-key provider interaction. |
| Installed Windows smoke | Pass unsigned | Silent install, app launch, sidecar ready, `/auth/check` success, and `/api/tools` returned `bash`, `web_fetch`, and `browser` for the current rebuilt installer. Signed smoke must be repeated after Authenticode signing. |
| Renderer visual smoke | Pass | Playwright screenshots passed for auth, main, settings, abilities, light and dark. |
| Left sidebar latest control | Pass | Left sidebar keeps new chat and search only; "回到最新消息" remains inside the chat pane and appears when the user scrolls away from the latest message. |
| SSE restoration | Pass | Desktop stream handler supports reasoning/thinking, message_end, tool_start, tool_end, media/file events, phase, delta/message_update, done, cancelled, and error. |
| Long reply collapse | Pass | `MessageContent` collapses long assistant replies by default; user messages are not collapsed. |
| Factory persona | Pass | Runtime config templates require EcoreX identity, professional/rigorous tone, and address users as "同学". |
| Tool call noise | Pass unsigned installed smoke | Same-name streamed tools now update/compact to one visible row, avoiding repeated `web_fetch`/bash rows in the chat transcript. Failed tools open by default so safety warnings are visible. |
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
| WebUI Linux artifact | Pass | `EcoreX_0.1.11-web-linux-service.tar.gz`, size `2,845,730`, SHA256 `2C991A1F5D6EF885C98F25AD5C3502A79D260830A7C106C96E77B53633359828`; tar scan has `0` desktop entries and checksum validation covered `413` files. |
| WebUI desktop visual shell | Pass | `/app/` packages `channel/web/static/app` with the existing desktop renderer static assets. Smoke verified bridge/base injection and `assets/index-CfGWwpfy.js` plus `assets/index-BtzBH59C.css` both return HTTP 200. |
| WebUI package runtime smoke | Pass | Extracted the final tarball to a temp directory, started its `runtime/app.py` with a temp venv/workspace, and verified `/auth/login`, `/app/`, JS/CSS assets, `/api/ui-state`, and `/api/installations`. |
| WebUI co-install state | Pass | Web backend exposes `/api/installations` and `/api/ui-state`, stores under `<agent_workspace>/.ecorex/`, and `/message` uses cross-process per-session locks. |
| WebUI concurrent install guard | Pass static | `scripts/install-ecorex-web.sh` serializes installs with `/var/lock/ecorex-web-install.lock` and stale-lock cleanup. Linux runtime execution still needs host validation. |
| macOS DMG artifacts | Pass unsigned | GitHub Actions run `27412042545` produced DMGs with `notarize=false`. arm64 size `150,067,486`, SHA256 `3A93E7F10E59E52D99C69C8AB9590B98D3BB7E5BBC7C1E54894F41472EDECB4D`; x64 size `156,273,299`, SHA256 `3D00CD7A5BE63E1BD33ED9A6F8CD2213A988F30267A5A2A5412C09D83B9318A5`. |
| Public release zip | Pass Web-only | `EcoreX_0.1.11-public-release.zip`, size `5,648,452`, SHA256 `5826F726869ABC9907CC243800E1F4A2372DE6AB77B5A948CD4CFBC9443B1256`; structure validation found static site, Admin API, server helpers, `checksums.json`, and the ready Web tarball. Regenerate after Windows signing/macOS artifacts are ready. |
| Public deployment | Pending production access | Web-ready public release zip is generated and structure-validated; production upload, `install-ecorex-public-release.sh`, `install-ecorex-web.sh`, and public `check-ecorex-web-release.sh` still need to run on the server. |

## Notes
- A hand-test reported `invalid client key` after installing v0.1.11. Root cause: production Admin API was still v0.1.10 and accepted only the old client key. The production container was rebuilt with the v0.1.11 Admin API and env compatibility keys.
- A later hand-test reported noisy tool rows, multiple orange waiting rows, inert artifact clicks, possible image/link rendering gaps, and suspiciously low usage accounting. The renderer, Electron permission manager, Web SSE file events, desktop usage telemetry, and capability preinstall policy were patched; a new signed installer must supersede the earlier local artifact before public deployment.
- The v0.1.11 public package must be a clean release build. Do not include local AppData, local conversation history, runtime logs, or files generated under the developer machine's `~/cow` workspace.
- macOS signing/notarization/Gatekeeper validation is not proven by Windows tests. If unsigned GitHub Actions DMGs are used, the download page must not claim notarization.

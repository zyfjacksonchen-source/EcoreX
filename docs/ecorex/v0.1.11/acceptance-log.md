# EcoreX v0.1.11 Acceptance Log

## Desktop
| Item | Status | Evidence |
| --- | --- | --- |
| Version | Pass | `desktop/package.json` and `desktop/package-lock.json` are `0.1.11`. |
| Windows installer | Pass | `EcoreX_0.1.11_x64-setup.exe`, size `117,469,216`, SHA256 `5ADF10F90DB64E46C6A92CB9FC0730F0A37D0C45B2F55B7EC566E25CF12E3685`. |
| Windows signature | Pass | Authenticode status `Valid`, signer certificate present. |
| Installed Windows smoke | Pass | Silent install, app launch, sidecar ready, `/auth/status` success. |
| Renderer visual smoke | Pass | Playwright screenshots passed for auth, main, settings, abilities, light and dark. |
| Left sidebar latest control | Pass | Left sidebar keeps new chat and search only; "回到最新消息" remains inside the chat pane and appears when the user scrolls away from the latest message. |
| SSE restoration | Pass | Desktop stream handler supports reasoning/thinking, message_end, tool_start, tool_end, media/file events, phase, delta/message_update, done, cancelled, and error. |
| Long reply collapse | Pass | `MessageContent` collapses long assistant replies by default; user messages are not collapsed. |
| Factory persona | Pass | Runtime config templates require EcoreX identity, professional/rigorous tone, and address users as "同学". |

## Admin And Download
| Item | Status | Evidence |
| --- | --- | --- |
| Admin API version | Pass | Production container rebuilt with `VERSION = "0.1.11"`. |
| Client key compatibility | Pass | Public capability-policy route returns HTTP 200 with both v0.1.10 and v0.1.11 desktop keys. |
| Windows manifest | Pass | `deploy/ecorex-site/manifest.json` records the latest Windows installer size/hash. |
| macOS DMG artifacts | Pending | Must be produced by GitHub Actions/macOS runner and then added to manifest/downloads with real hashes. |
| Public release zip | Pending | Regenerate after DMG artifacts are available, so the zip contains Windows and macOS downloads. |
| Public deployment | Pending | Deploy latest Admin/download package after release zip is regenerated. |

## Notes
- A hand-test reported `invalid client key` after installing v0.1.11. Root cause: production Admin API was still v0.1.10 and accepted only the old client key. The production container was rebuilt with the v0.1.11 Admin API and env compatibility keys.
- macOS signing/notarization/Gatekeeper validation is not proven by Windows tests. If unsigned GitHub Actions DMGs are used, the download page must not claim notarization.

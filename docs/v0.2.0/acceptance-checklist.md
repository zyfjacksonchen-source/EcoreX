# v0.2.0 Acceptance Checklist

| ID | Area | Requirement | Status | Evidence |
| --- | --- | --- | --- | --- |
| R20-01 | Version | Runtime, WebUI, admin API, package metadata, build scripts, and release scripts default to `0.2.0`. | IN PROGRESS | Version migration started; final proof requires rg check plus build/package output. |
| R20-02 | Compatibility | v0.1.19 client keys remain accepted during rollout. | IN PROGRESS | Compat entries added in admin API, Electron policy, Web bridge, and stage/install scripts; tests pending. |
| R20-03 | Runtime Stability | Accepted requests have a single recognized terminal state and cannot leave infinite pending UI. | IN PROGRESS | CowAgent cancel regression fixed; stale orphan active runs now terminalize as `interrupted` and release backpressure. Selected runtime tests passed; broader recovery smoke still pending. |
| R20-04 | Performance | WebUI typing, switching, deleting, folding, and streaming output are responsive on Win/Mac. | IN PROGRESS | Pending assistant output now uses text-node streaming and history token estimation is debounced outside render; typecheck, source regression, and renderer build passed. Playwright/runtime smoke still pending. |
| R20-05 | State Persistence | WebUI update preserves project folders and project sessions. | IN PROGRESS | Runtime hydrate and auto-sync are merge-by-default; backend blocks accidental empty replace; explicit project deletion keeps replace semantics. Targeted backend/source tests and desktop typecheck passed. |
| R20-06 | Discovery | Knowledge/graph/channels/tools are discoverable after configuration. | IN PROGRESS | Shared channel catalog added; `/api/extensions` includes `channel:*`; frontend runtime capabilities merge `/api/channels`; bridge allowlist includes channel POST, Feishu/Weixin auth, and knowledge graph; selected tests passed. |
| R20-07 | Install | Windows/macOS WebUI installers generate desktop entry, update correctly, and open browser. | IN PROGRESS | Installer scripts now log script/manifest versions and fallback instructions; mac package installer writes desktop shortcut before browser open and package generator blocks retired `resume_args`; syntax/source tests passed. Package smoke still pending. |
| R20-08 | Review | Independent parallel review reaches PASS consensus. | TODO | Pending review agents. |

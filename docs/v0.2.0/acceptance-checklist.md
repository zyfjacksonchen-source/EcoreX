# v0.2.0 Acceptance Checklist

| ID | Area | Requirement | Status | Evidence |
| --- | --- | --- | --- | --- |
| R20-01 | Version | Runtime, WebUI, admin API, package metadata, build scripts, and release scripts default to `0.2.0`. | IN PROGRESS | Version migration started; final proof requires rg check plus build/package output. |
| R20-02 | Compatibility | v0.1.19 client keys remain accepted during rollout. | IN PROGRESS | Compat entries added in admin API, Electron policy, Web bridge, and stage/install scripts; tests pending. |
| R20-03 | Runtime Stability | Accepted requests have a single recognized terminal state and cannot leave infinite pending UI. | TODO | Pending implementation/tests. |
| R20-04 | Performance | WebUI typing, switching, deleting, folding, and streaming output are responsive on Win/Mac. | TODO | Pending implementation/Playwright smoke. |
| R20-05 | State Persistence | WebUI update preserves project folders and project sessions. | TODO | Pending implementation/tests. |
| R20-06 | Discovery | Knowledge/graph/channels/tools are discoverable after configuration. | TODO | Pending implementation/tests. |
| R20-07 | Install | Windows/macOS WebUI installers generate desktop entry, update correctly, and open browser. | TODO | Pending package smoke. |
| R20-08 | Review | Independent parallel review reaches PASS consensus. | TODO | Pending review agents. |

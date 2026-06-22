# v0.1.19 Acceptance Checklist

Status values: TODO, PARTIAL, PASS, BLOCKED.

| ID | Area | Acceptance Standard | Status | Evidence |
| --- | --- | --- | --- | --- |
| R19-01 | Artifacts | Unverified local artifacts are hidden after retry exhaustion and cannot be opened through menus. | TODO |  |
| R19-02 | Artifacts | Artifact menus are unclipped on desktop, narrow, and mobile-sized renderer widths. | TODO |  |
| R19-03 | Reconnect | Network stalls show Codex-like inline status and recovery controls. | TODO |  |
| R19-04 | Reconnect | Replay gap, interrupted run, sidecar restart, and retryable conflict never leave infinite pending bubbles. | TODO |  |
| R19-05 | Interrupt Send | Sending while a task runs cleanly interrupts/replaces or restores the unsent draft. | TODO |  |
| R19-06 | Interrupt Send | Rapid multiple sends preserve latest-wins semantics and create no duplicate accepted turns. | TODO |  |
| R19-07 | Interrupt Send | Backend conflict/backpressure never leaves a user message that looks persisted but was not accepted. | TODO |  |
| R19-08 | Add To Chat | Right-click supported chat images/files can add to current composer, deduped. | TODO |  |
| R19-09 | Run Center | Run Center is absent from ordinary UI by default while recovery logic still works. | TODO |  |
| R19-10 | Sidebar | Sidebar collapse persists and auto-reveals active/search/running/unread items. | TODO |  |
| R19-11 | Cross-platform | Windows, macOS, and Web/Electron fallback paths avoid platform-specific assumptions. | TODO |  |
| R19-12 | Review | Independent multi-agent review reaches PASS consensus; writer does not review own work. | TODO |  |

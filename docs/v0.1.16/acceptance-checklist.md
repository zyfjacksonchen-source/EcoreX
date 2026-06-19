# v0.1.16 Acceptance Checklist

Status values: TODO, PARTIAL, PASS, BLOCKED.

| ID | Area | Acceptance Standard | Status | Evidence |
| --- | --- | --- | --- | --- |
| F01-A | Streaming Markdown | Long Markdown stream does not expose raw incomplete headings/code/table syntax in stable rendered content. | PARTIAL | Implemented streaming tail split, incomplete table holdback, and long-stream render throttle in `desktop/src/components/MessageContent.tsx`; `npm run typecheck` PASS. Needs automated 200k visual smoke. |
| F01-B | Streaming Markdown | Completion does not cause a full-message visual jump or delayed one-shot formatting pass. | PARTIAL | Stable/tail rendering retained; active pending content throttled at 12k+ chars. Needs browser visual timing capture. |
| F01-C | Streaming Markdown | `SSE delta -> paint` P95 is under 100ms in the long-response smoke. | TODO | Playwright/perf trace not yet added. |
| F02-A | Skill Discovery | Project, workspace, global Codex/agents, and plugin-cache skills are all discoverable. | PASS | `agent/skills/manager.py` discovers `~/.codex/skills`, `~/.agents/skills`, `.system`, and plugin-cache skill roots; Python compile PASS. |
| F02-B | Skill Discovery | Duplicate skill names are deterministically de-duplicated and invalid skills show diagnostics. | PARTIAL | Loader ordering is deterministic via case-insensitive sort. Invalid-skill UI diagnostic still needs fixture coverage. |
| F02-C | Skill Discovery | `@skill` search can find every enabled skill without a hard six-item-only result cap. | PASS | Search now covers name/displayName/description/source/path and returns up to 24 candidates; `npm run typecheck` PASS. |
| F03-A | Composer Focus | Creating 20 sessions in a row shows the input caret within 100ms P95 after UI commit. | PARTIAL | Multi-frame/timebox focus retry implemented in `desktop/src/App.tsx`; no automated 20-session focus trace yet. |
| F03-B | Composer Focus | Slow sidecar startup, history refresh, and session switching cannot steal focus from the new composer. | PARTIAL | `focusComposerSoon()` retries immediate/rAF/timeouts and sets cursor end. Needs UI focus regression smoke. |
| F04-A | First Response | 50 non-first-round sends show accepted/phase UI within 250ms. | PARTIAL | Send path is optimistic and cancellation is backgrounded. Needs repeated-send timing smoke. |
| F04-B | First Response | Switching away and back is not required for results to appear. | PARTIAL | SSE attach/history recovery timers added; installed/unpacked sidecar smoke PASS. Needs conversation replay test. |
| F04-C | First Response | SSE disconnect reconnects or recovers from history within 2s when the run is still active. | PARTIAL | SSE replay maps now locked and history recovery scheduled on attach/error. Needs network-disconnect smoke. |
| F05-A | Artifacts | Open/stat/preview works for Windows absolute paths, relative paths, spaces, Chinese characters, and deleted files. | PARTIAL | Session-owned path resolution, file URL decode, and stat broker path added. Needs full path matrix smoke. |
| F05-B | Artifacts | Relative artifact paths resolve against the artifact's owning project/session, not whichever project is currently active. | PASS | Message callbacks resolve via message/session project path instead of active project. |
| F05-C | Artifacts | Missing local files produce a clear UI error without breaking the artifact shelf. | PASS | Artifact shelf keeps missing/denied/error rows with status labels instead of filtering them out; `npm run typecheck` PASS. |
| F06-A | Frontend Performance | 200 sessions x 60 cached messages keeps input latency P95 under 50ms. | TODO | Large-session performance trace not yet automated. |
| F06-B | Frontend Performance | Streaming produces no sustained >50ms renderer long tasks in smoke testing. | PARTIAL | Delta flush delay scales for 30k+/100k+ content and render throttles pending content. Needs trace validation. |
| F06-C | Frontend Performance | Renderer memory does not grow continuously during a 30-minute idle/streaming soak. | TODO | Soak test not yet automated. |
| F07-A | Request Lifecycle | Killing runtime mid-run and restarting does not leave a false running state. | PARTIAL | Old boot pending messages get a short stale grace and history recovery is scheduled. Needs kill-mid-run test. |
| F07-B | Request Lifecycle | Late artifact/TTS events after done are not lost or attached to the wrong assistant message. | PASS | TTS extras attach by assistant `bot_seq`; Python compile PASS. |
| F07-C | Request Lifecycle | Three rapid sends in one session do not deadlock the session lock. | PARTIAL | Optimistic cancel/background cancel added. Needs rapid-send integration smoke. |
| F07-D | Request Lifecycle | Context trim persistence keeps the whole current run, including early tool/assistant messages. | PASS | Targeted trim-boundary smoke PASS: final_len 43, original_len 42, persisted current run 33 messages. |
| F08-A | Sidecar | A stale process occupying the desktop port is not mistaken for the current runtime. | PASS | Desktop sidecar probes `/api/version` with per-boot runtime token; unauthenticated smoke returns `desktopRuntimeVerified:false` and no token/root. |
| F08-B | Sidecar | App exit leaves no sidecar child process tree after 5s. | PASS | Unpacked smoke and installer smoke both cleaned all processes rooted under the app/install directory. |
| F08-C | Sidecar | Sidecar ready P95 is under 8s on the packaged smoke path. | PARTIAL | Unpacked smoke ready in one local run; installer smoke ready within 90s timeout. Needs repeated P95 sampling. |
| F09-A | Diagnostics | Diagnostic bundle includes boot id, request id, session id, active requests, recent errors, sidecar status, and logs. | TODO | Full diagnostic bundle not implemented in this slice. |
| F09-B | Diagnostics | Diagnostic bundle excludes prompt text, file contents, and artifact contents by default. | TODO | Full diagnostic bundle not implemented in this slice. |
| F09-C | Release Gate | `npm run typecheck`, `npm run build`, backend compile/tests, desktop smoke, artifact smoke, and sidecar smoke pass. | PARTIAL | PASS: `npm run typecheck`, `npm run build`, `npm run package:dir`, `npm run package:win`, Python compile, desktop-unpacked validator, public artifact validator, unpacked smoke, unsigned installer smoke. BLOCKED: pytest unavailable; Windows installer is NotSigned; macOS/WebUI/Linux v0.1.16 artifacts pending. |

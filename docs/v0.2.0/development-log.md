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

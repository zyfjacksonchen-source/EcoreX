# v0.2.0 Development Log

## 2026-06-23

- Created v0.2.0 execution branch `codex/ecorex-v0.2.0` from checkpoint `702072fa`.
- Started version migration from v0.1.19 to v0.2.0 for runtime/package/admin/WebUI defaults.
- Preserved v0.1.19 as a compatibility client key instead of removing it from rollout allowlists.
- Restored CowAgent's `ChatChannel.cancel_session` / `cancel_all_session` missing-futures guard and added a focused regression test.
- Added stale active run recovery: orphaned message runs with no cancel token, no SSE state, no live session lock, and no update past `web_active_run_stale_seconds` are marked `interrupted` and no longer block backpressure.

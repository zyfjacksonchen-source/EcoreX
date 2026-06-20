# v0.1.18 Development Log

## 2026-06-21 Kickoff

- Confirmed v0.1.17 code was committed before starting v0.1.18:
  `f8ff1db4 chore: stabilize EcoreX v0.1.17 gates`.
- Created branch `codex/ecorex-v0.1.18`.
- Started v0.1.18 tracking docs so the larger runtime/model work can be checked
  against explicit acceptance rows rather than memory.
- User-provided GitHub credential was not used in commands or written to files;
  token-pattern scan before the base commit found no `ghp_` token in the
  worktree.
- Added the first P0 durable run ledger slice:
  - `agent/protocol/run_ledger.py` stores `agent_runs` in SQLite with active and
    terminal status tracking.
  - WebChannel creates ledger rows for `/message`, updates phase from selected
    SSE events, writes cancelling/cancelled/failed/completed terminal state, and
    prefers ledger state in `active_requests_snapshot()`.
  - Focused pytest passed: 8 run-ledger/active-request/finalize/busy-session
    tests.
- Added the first P0 SSE contract slice:
  - WebChannel now normalizes stream events with `protocol_version`,
    `event_type`, `state`, `terminal`, and terminal reason/error fields.
  - Worker exceptions, pre-worker produce exceptions, and agent stream errors now
    emit machine-readable `type=error` / `event_type=run.failed` terminal events
    before legacy fallback code can emit a success-shaped `done`.
  - `stream_response()` emits `type=replay_gap` when `last_event_id` is older
    than the retained replay window.
  - Renderer `StreamItem` types include the new protocol fields and EventSource
    cursor cleanup treats `error` as terminal.
  - Focused pytest passed: 14 SSE/terminal/replay/active-request tests; desktop
    `npm run typecheck` passed.

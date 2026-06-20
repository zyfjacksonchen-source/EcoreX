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

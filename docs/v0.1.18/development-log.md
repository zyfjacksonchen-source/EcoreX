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
- Added the first R18-03 cancellation/concurrency slice:
  - `active_requests_snapshot()` no longer treats the SSE terminal-once guard as
    proof that backend work is inactive.
  - If a cancellation terminal has already been sent to the UI but the cancel
    token remains registered, `/api/active-requests` still exposes the request
    as `state=cancelling` from `cancel_registry`.
  - Desktop `RuntimeActiveRequest` types now include durable run and fallback
    source fields used by the future Run Center.
  - Focused pytest passed: 7 active-request/cancel/busy-session tests.
- Added the first R18-04 model capability slice:
  - `models/model_capabilities.py` centralizes provider inference, model
    capability flags, and OpenAI-compatible chat payload sanitization.
  - AgentBridge routes agent chat models through the shared provider inference
    helper.
  - OpenAI-compatible tool calls strip unsupported sampling parameters for
    fixed-sampling OpenAI models and request stream usage when supported.
  - `/api/models` chat capability now exposes the same capability object used by
    backend payload sanitization.
  - Focused pytest passed: 9 model-capability/ModelsHandler tests and 3
    qianfan AgentBridge routing tests.
- Added the second R18-03 cancellation/concurrency slice:
  - WebChannel now marks request-scoped cancel tokens as owned by
    `web_channel`.
  - AgentBridge no longer unregisters Web-owned request tokens immediately after
    `agent.run_stream()` returns; WebChannel finalization remains the owner that
    releases the token and records the final run state.
  - Non-Web AgentBridge tokens still self-clean, preserving IM/scheduler bounded
    registry behavior.
  - Focused pytest passed: 6 token-owner/active-request/busy-session tests.
- Added the third R18-03 pre-worker abort cleanup slice:
  - WebChannel now has a single `_abort_pre_worker_request()` cleanup path for
    `/message` failures that happen after `request_id` allocation but before a
    worker owns finalization.
  - Context composition failures, filtered contexts, and thread-start failures
    now write a failed ledger terminal, unregister the cancel token, release the
    session lock, and remove SSE/request replay state.
  - Focused pytest passed: 7 pre-worker-abort/active-request/worker-completion
    tests.

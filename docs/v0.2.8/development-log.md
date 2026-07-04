# EcoreX v0.2.8 Development Log

## Goal

Build production-grade runtime behavior for long agent tasks:

- task observation with health, timeout, and user intervention decisions;
- Codex-style same-session queueing so new user messages do not implicitly cancel the running task;
- durable evidence in code, tests, and release notes.

## Decisions

- Same-session input policy: queue-first. A new message sent during a running task is accepted as a queued run. It must not cancel the running request unless the user explicitly stops it.
- Queue storage: reuse RunLedger and runtime event ledger for v0.2.8. Do not introduce a second database in this slice.
- User surface: chat stream plus Run Center. Queued requests must appear as queued in active request snapshots and stream phases.
- Task observation policy: additive event model first, then progressively wire long tools such as image generation into provider-level health.

## Execution Notes

- 2026-07-04: Created long goal and began implementation.
- 2026-07-04: Confirmed existing WebChannel behavior uses `interrupt_previous` and `_interrupt_and_wait_for_session_lock()` for busy same-session sends.
- 2026-07-04: Confirmed `RunLedger` already recognizes `queued` as active but did not provide real queue lifecycle semantics.
- 2026-07-04: Updated `RunLedger` so queued runs have no `started_at` until they leave queued state, and added `queued_snapshot()`.
- 2026-07-04: Added WebChannel same-session queue state, queue-first busy-session admission, automatic queued-run start after session lock release, and `/api/requests/{request_id}/queue-action`.
- 2026-07-04: Updated Desktop send behavior so queued messages do not steal the currently running stream.
- 2026-07-04: Added `TaskObserver` and wired tool execution heartbeat/deadline/timeout/end into additive `task.*` events.
- 2026-07-04: Added runtime projection support for `task_observations`.

## Acceptance Anchors

- Sending a second message while a request is running returns `same_session.policy = "queue"` and `decision = "queued"`.
- The previous request is not cancelled and no cancelled SSE event is pushed for it.
- The queued request starts automatically after the current request releases its session lock.
- Run Center can show queued requests with stable request ids.
- Long image generation must eventually emit observation/intervention events instead of silently waiting.

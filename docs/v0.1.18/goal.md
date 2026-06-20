# EcoreX v0.1.18 Production Agent Goal

## Objective

v0.1.18 turns the v0.1.17 desktop/runtime hardening work into a production-grade
agent runtime. The release must make every long-running agent request observable,
durable enough to recover from UI/SSE/runtime interruptions, and safe under
concurrency, cancellation, tool output pressure, and model-provider failures.

This iteration starts from v0.1.17 commit `f8ff1db4`:
`chore: stabilize EcoreX v0.1.17 gates`.

v0.1.17 still has release-production blockers around Windows signing, macOS DMG
artifacts, and privileged symlink evidence. Those are recorded in v0.1.17 and do
not redefine the v0.1.18 agent-runtime goal.

## User-Visible Problems

- R18-01: The user must always know whether an agent run is queued, running,
  waiting for permission, cancelling, finalizing, completed, failed, cancelled,
  interrupted, or recoverable.
- R18-02: Refreshing the UI, switching sessions, reconnecting SSE, or restarting
  the sidecar must not leave false "thinking" bubbles or lose terminal state.
- R18-03: Same-session rapid sends, cancel during tool execution, subagent
  fan-out, scheduler jobs, and long tool output must be bounded and recoverable.
- R18-04: Provider timeouts, 429/5xx, context overflow, model capability
  mismatches, and malformed tool streams must become typed, observable errors
  with retry/fallback policy.
- R18-05: Large prompts, tools, reasoning, and artifacts must be budgeted instead
  of silently bloating context or freezing the renderer.

## Production Hardening

- R18-06: Add a durable Run/Job ledger as the authoritative state source for
  main requests, subagents, scheduler jobs, and long-running tools.
- R18-07: Replace loose SSE terminal semantics with a versioned stream event
  contract where success, failure, cancellation, replay gaps, and post-final
  tail events are distinct.
- R18-08: Centralize active run snapshots so the desktop UI reads backend truth
  instead of inferring from scattered local refs and timers.
- R18-09: Introduce a model capability catalog and model-call telemetry for
  reasoning effort, verbosity, token usage, cached tokens, retry count, latency,
  and provider request IDs.
- R18-10: Preserve v0.1.17 release gates and add v0.1.18 gates that prove the
  new runtime state machine, concurrency limits, and model gateway behavior.

## Agent Process

- Every implementation item must map to an acceptance row in
  `acceptance-checklist.md`.
- Every verification command must be recorded in `evidence-ledger.md`.
- P0/P1 review findings block completion until they are closed or explicitly
  moved to a documented follow-up.
- Do not mark this release complete from typecheck alone. The audit must prove
  request lifecycle, SSE recovery, cancellation, concurrency, and model-call
  behavior against current code and evidence.

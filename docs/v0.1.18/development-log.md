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
- Added the fourth R18-03 cancellation visibility slice:
  - `CancelTokenRegistry` now records `cancelled_at` and exposes
    `cancel_age_seconds` in active snapshots.
  - Desktop active-request filtering now uses `cancel_age_seconds` before
    falling back to request `age_seconds`, so a long-running request remains
    visible during the immediate post-cancel grace window.
  - Run-ledger active rows merge cancel-registry `cancelled_at` and
    `cancel_age_seconds` so ledger-preferred snapshots keep the same UI
    visibility semantics.
  - Focused pytest passed: 6 cancel-registry/active-request tests; desktop
    `npm run typecheck` passed.
- Added the fifth R18-03 concurrency visibility slice:
  - WebChannel busy-session fallback now returns
    `code=REQUEST_CONFLICT_RETRYABLE`, `error_type=concurrency_conflict`,
    `retryable=true`, and `retry_after_ms` instead of exposing raw
    `session_busy`.
  - Desktop chat send handling recognizes the typed retryable conflict,
    displays the stable retry message, and emits structured warning telemetry
    with retry metadata.
  - Focused pytest passed: 6 busy-session/active-request tests; desktop
    `npm run typecheck` passed.
- Added the first R18-03-C subagent coordination slice:
  - Subagent start now reserves bounded slots inside the shared state lock, so
    concurrency checking and queued task creation are atomic.
  - Subagent queued/running/cancelling/completed/failed/cancelled transitions
    now mirror into the run ledger with `run_type=subagent` and parent/session
    metadata.
  - Queued subagents are visible through backend active request snapshots, and
    parent cancellation marks running children as `cancelling` while terminally
    cancelling not-yet-running children.
  - Existing desktop chat sidebar and stream recovery paths filter out
    `run_type=subagent` rows so subagents remain backend-visible without being
    misrepresented as normal chat sessions before the Run Center exists.
  - Registry-only `subagent-*` active fallback rows are marked as
    `run_type=subagent`, and the desktop filter also excludes `subagent-`
    request/session prefixes for older or degraded runtime snapshots.
  - Focused pytest passed: 5 subagent tests; broader subagent/active/busy
    regression passed: 12 tests; desktop `npm run typecheck` passed.
- Added the first R18-04-B model telemetry slice:
  - `models/model_telemetry.py` adds a bounded in-memory model-call event
    collector, usage normalization for input/output/total/reasoning/cached
    tokens, and a small error taxonomy for rate limit, timeout, network,
    server/client, cancellation, and context overflow failures.
  - The shared OpenAI-compatible sync and streaming tool-call path now records
    provider/model, stream flag, retry count field, first-token latency, total
    latency, usage buckets, and failed-call taxonomy without changing the
    existing chunk/error contract consumed by AgentStream.
  - Stream telemetry now closes exactly once as `cancelled` when a consumer has
    already received model output and then cancels/closes the generator before
    provider completion.
  - Focused pytest passed: 11 model telemetry/capability tests.
- Added the first R18-04-C retry/fallback slice:
  - `models/model_retry.py` defines retryability from the shared model error
    taxonomy, parses `Retry-After` seconds or HTTP dates, and annotates final
    error responses with `retryable`, `retry_attempt`, `retry_exhausted`, and
    `error_taxonomy` evidence.
  - The OpenAI-compatible sync path retries rate-limit, timeout, network, and
    server errors using Retry-After when available and deterministic backoff
    otherwise; non-retryable 4xx errors return immediately with typed evidence.
  - The streaming path retries only before first model output. Once content,
    reasoning, or tool-call output has started, retryable errors are marked
    `retry_suppressed_reason=stream_output_started` so AgentStream does not
    restart the whole turn and duplicate partial UI output.
  - AgentStream now passes its outer retry count through `LLMRequest`, and
    AgentBridge forwards it into bot calls so model-call telemetry records the
    actual attempt number. AgentBridge also forwards AgentStream's
    cancel-aware sleep helper so provider-level retry backoff can be interrupted
    by user cancellation in agent runs.
  - Focused pytest passed: 20 model telemetry/capability tests; Web
    agent-stream error regression passed: 5 tests; Qianfan route subset passed:
    3 tests.
- Added the first R18-03-D backpressure slice:
  - Web `/message` now lets `/cancel` bypass pressure and gives the
    SessionLock/busy-session interrupt path first chance to replace an older
    same-session request, then checks active run pressure before request id
    allocation, cancel-token registration, run-ledger creation, and SSE setup.
  - The admission check enforces configurable global and per-session active-run
    limits and returns typed `BACKPRESSURE_GLOBAL_LIMIT` or
    `BACKPRESSURE_SESSION_LIMIT` errors with `retryable`, `recoverable`,
    `retry_after_ms`, active counts, active request ids, and the current SSE
    replay retention limit.
  - Same-session replacement requests ignore the old request id for admission
    accounting after first giving the busy-session path a chance to cancel it,
    while different-session requests still see the newly admitted request
    through the cancel registry/run ledger before they are accepted.
  - `/cancel` remains a fast path that bypasses admission pressure so users can
    always stop an overloaded session.
  - Focused pytest passed: 12 backpressure/busy/pre-worker/active snapshot tests.
- Added the second R18-03-D tool output/artifact budget slice:
  - Web SSE `tool_end` events now bound stdout/stderr/output/tail fields,
    generic long strings, large collections, and final result preview before
    sending them to the browser, while preserving typed
    `TOOL_OUTPUT_LIMIT` metadata for recoverable truncation.
  - Artifact metadata now has configurable max item, string length, and path
    length caps. Stored and streamed artifact metadata includes its limits and
    per-field truncation evidence.
  - When artifact metadata exceeds the item cap, WebChannel emits a typed
    `ARTIFACT_METADATA_LIMIT` warning event instead of silently growing the
    request artifact list.
  - Focused pytest passed: 20 backpressure/busy/pre-worker/active snapshot/tool
    budget tests.
- Added the first R18-05-B tool schema budget slice:
  - AgentStream now selects a budgeted subset of model-visible tool schemas per
    call. Plain turns keep core host/file tools, while Feishu, browser/MCP, web,
    scheduler, subagent, vision, memory, and diagnostics groups are deferred
    until user intent, explicit tool names, or recent tool-chain recovery require
    them.
  - Every model request records `tool_schema_budget` metadata and emits a
    `tool_schema_budget` event so future Run Center/telemetry work can explain
    which schemas were selected or deferred.
  - The budget can be disabled through `agent_tool_schema_budget_enabled=false`
    to force legacy full-schema behavior.
  - Focused pytest passed: 10 tool-schema/forced-text budget tests; 13 adjacent
    convergence/tool-chain tests; 75 AgentHostBoundary tests excluding two
    existing image-generation fake-byte fixture failures.
- Added the first R18-04-D Responses adapter slice:
  - `models/openai/responses_adapter.py` plans official OpenAI Responses API
    calls from chat-style messages/tools, including `previous_response_id`,
    compaction payloads, `/responses/input_tokens` payloads, prompt cache
    key/retention fields, service tier, max-output-token translation, tool-call
    history conversion, compaction output handoff that omits the compaction
    response id as `previous_response_id` while appending the fresh turn, and
    non-stream output normalization back to the existing chat-completions shape.
  - `OpenAIHTTPClient` now exposes narrow helpers for `/responses`,
    `/responses/compact`, and `/responses/input_tokens`.
  - `OpenAICompatibleBot.plan_responses_api_call()` is an explicit planning
    hook only: it returns a plan when `use_responses_api` or
    `openai_responses_api_enabled` is true and the provider/base URL resolve to
    official OpenAI; default production agent calls still use `/chat/completions`.
  - Focused pytest passed: 8 Responses adapter tests; adjacent model gateway
    regression passed: 28 capability/telemetry/Responses tests.
- Added the first R18-05-A context budget slice:
  - AgentStream now computes request-level `context_budget` metadata for each
    model call, including system prompt, message text, reasoning blocks,
    tool-use inputs, tool-result payloads, tool-schema cost, artifact metadata,
    media estimates, runtime artifacts, selected/deferred schema counts, and
    effective context-limit evidence.
  - The effective limit clamps oversized `agent_max_context_tokens` values to
    the model context window minus a response reserve by default, preventing a
    large global config from overfilling smaller model windows.
  - Response reserve is capped within the model window so 4K/8K models do not
    inherit an oversized 10K default reserve and collapse to a 1-token input
    budget.
  - AgentStream emits a `context_budget` event before the model call and stores
    the same metadata on `LLMRequest` so future Run Center/model telemetry work
    can explain near-limit or over-budget turns.
  - Context trimming now uses the same effective limit and focused coverage
    proves old turns are trimmed while the current run remains preserved.
  - Focused pytest passed: 16 context/tool-schema/forced-text/tool-chain budget
    tests.
- Added the first R18-06-A desktop Run Center slice:
  - Desktop diagnostics settings now surface a Run Center panel backed by the
    existing `/api/active-requests` snapshot.
  - The panel summarizes running, stopping, failed, and stale runtime state and
    lists active request rows, including subagent rows that were previously
    hidden from the primary chat sidebar.
  - Per-run actions reuse existing runtime contracts: chat-session rows merge
    the current Run Center request scope before opening/recovering, normal
    active runs can stop through `/cancel`, subagent rows stop through
    `/api/subagents/{task_id}/cancel` when a task id is available, and all run
    rows can export request-scoped diagnostics bundles.
  - Subagent stop falls back to request-scoped `/cancel` only when the dedicated
    task route fails and the fallback reports `cancelled > 0`, covering
    registry-only degraded rows without reintroducing fake success.
  - Run Center uses its own visibility predicate instead of the chat sidebar's
    30-second cancelled-request grace filter, so long-running stopping rows
    remain visible for recovery and diagnostics.
  - Subagent rows are visible in Run Center but diagnostics-only for
    open/recover, preserving the existing boundary that keeps subagents out of
    primary chat restoration.
  - Source-contract coverage now locks the Run Center-specific visibility
    predicate, scoped chat-session recovery row, subagent diagnostics-only
    Open behavior, subagent-specific cancel path, and guarded request-cancel
    fallback.
  - Stale session locks are shown separately so cleanup evidence is visible
    without being mixed into normal chat sessions.
  - Desktop `npm run typecheck` passed; focused source-contract pytest passed.
- Added the first R18-02-C desktop SSE replay-gap recovery slice:
  - Desktop stream consumers now detect backend `type=replay_gap` and
    `event_type=stream.replay_gap` events on both normal sends and resumed
    EventSource attaches.
  - Replay-gap handling marks the local stream attach failed, closes the stale
    EventSource, clears pending request/timer state, and tries to refresh the
    saved session history for the current request before falling back to an
    explicit non-pending retry message.
  - The replay-gap recovery success check is request-scoped, so an older final
    assistant message in the same session cannot suppress the current turn's
    retry/recovery fallback.
  - The fallback records `stream_replay_gap` telemetry with requested,
    retained-from, and next event cursor ids so Run Center/diagnostics can
    explain why a live stream stopped.
  - Desktop `npm run typecheck` passed; focused desktop source-contract pytest
    and adjacent backend SSE replay-gap tests passed.
- Added the first R18-01-B sidecar interruption terminal-ledger slice:
  - `active_requests_snapshot()` now inspects session-lock diagnostics before
    reading active durable run rows and removes locks only when the local owner
    process is confirmed dead.
  - When diagnostics prove a session lock owner is gone, active
    `run_type=message` rows for that session are marked terminal `interrupted` with
    `sidecar_interrupted` and `SIDECAR_INTERRUPTED` before the active snapshot
    response is built.
  - The active snapshot is re-read after terminal marking, so desktop recovery
    and Run Center no longer keep a dead-lock message request visible forever
    as `running`.
  - A sidecar-interrupted request is also suppressed from cancel-registry
    fallback rows, so stale in-process registry state cannot re-add the same
    durable terminal request as active.
  - Stale-only locks whose owner is still alive remain diagnostic evidence and
    do not trigger interruption, preventing long-running message tasks from
    being mislabelled as sidecar crashes.
  - The rule is intentionally scoped to message runs in this slice; subagent
    rows keep their independent lifecycle and remain active for the dedicated
    subagent/scheduler interruption policy slice.
  - Focused backend pytest passed for dead-lock cleanup, message-run
    interruption with registry fallback suppression, stale-live
    non-interruption, subagent non-interruption, cancelling fallback
    preservation, and durable active snapshot behavior.
- Added the first R18-07-A promotion-gate slice:
  - `scripts/check-ecorex-v0.1.18-promotion-gate.py` aggregates the production
    agent runtime gate into one machine-readable JSON report.
  - The gate requires all mapped acceptance rows for run ledger, SSE,
    cancellation/concurrency, model calls, context budgeting, Run Center, and
    evidence gates to reach `PASS` before it reports GO.
  - It also verifies evidence-ledger markers for run-ledger terminal semantics,
    SSE replay/failure semantics, cancellation/backpressure/subagent coverage,
    model-call telemetry/retry/Responses evidence, context/tool budget evidence,
    Run Center evidence, and multi-agent review consensus markers.
  - A built-in GitHub-token pattern scan checks tracked and unignored worktree
    files using `git ls-files -co --exclude-standard`, avoiding slow scans of
    ignored build/dependency artifacts.
  - If the token scan is skipped, the gate now emits a blocker instead of a
    possible GO report, so credential-leakage evidence remains mandatory.
  - `--allow-no-go` writes the current report without pretending the release is
    complete; `docs/v0.1.18/promotion-gate.json` is currently `no-go` with seven
    blocker groups because the broad v0.1.18 acceptance rows are still PARTIAL.
  - Focused pytest covers GO, PARTIAL/NO-GO, token-pattern failure, and
    token-scan-skipped blocker behavior.
- Added the first scheduler execution run-ledger slice:
  - Scheduler due-task execution now flows through `_execute_scheduled_task()`,
    creating one durable `run_type=scheduler` row per execution attempt after
    the outbound channel readiness check passes.
  - Scheduler attempt rows use a stable per-attempt request id that is also
    passed to web-channel contexts, so active snapshots and delivery mappings
    describe the same backend run.
  - Each call to `_execute_scheduled_task()` forces a fresh attempt id even if a
    caller reuses the same task dictionary, so a prior terminal row cannot block
    a retry or recurring execution attempt.
  - The ledger records `queued` then `running` before the action dispatch, and
    terminal-once semantics close attempts as `completed` on delivery success or
    `failed` on scheduler permission denial, unknown action, execution/delivery
    failure, malformed consumed tasks, empty agent/skill results, missing tools,
    and scheduled tool permission denial.
  - `active_requests_snapshot()` consumes durable scheduler rows but keeps them
    out of primary chat-session grouping; desktop primary-chat recovery also
    filters scheduler rows, while Run Center keeps them visible as
    diagnostics-only until scheduler cancellation/recovery contracts exist.
  - Focused backend pytest passed for scheduler active/terminal ledger state,
    fresh attempt ids for reused task dictionaries, scheduler permission-denied
    terminal rows, scheduled tool permission-denied terminal rows,
    scheduler-out-of-primary-session snapshots, Run Center diagnostics-only
    source contract, and the existing scheduler fail-closed permission tests;
    desktop `npm run typecheck` passed.

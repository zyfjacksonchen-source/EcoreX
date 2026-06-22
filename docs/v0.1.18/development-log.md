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
- Added the second R18-05-A/B context overflow recovery slice:
  - AgentStream now recognizes provider `error_taxonomy=context_overflow` in
    addition to provider text, so overflow recovery is not dependent on one
    vendor's exact error wording.
  - On the first context overflow, AgentStream emits a structured
    `context_overflow_recovery` event with before/after budget estimates,
    removed turn count, truncated block counts, and
    `current_turn_preserved` evidence.
  - Overflow retry now forces a text-only model call with
    `force_text_reason=context_overflow_recovery`, so recovery does not resend
    tool schemas into an already over-budget request.
  - Aggressive overflow trimming keeps the latest user run, removes older
    turns, truncates historical pasted messages, and marks bulky current-run
    tool payloads as truncated instead of dropping the current run boundary.
  - Recovery is limited to the pre-output stream boundary; if an overflow
    arrives after model text/reasoning/tool output starts, AgentStream fails the
    attempt without retrying so partial deltas are not duplicated.
  - Schema-only overflow recovery now retries text-only even when there is no
    historical message trim to apply, preventing tool-schema bloat from
    destructive history clearing.
  - The overflow-recovery marker now survives ordinary retry and empty-response
    retry recursion, so a second overflow after recovery cannot be mistaken for
    the first overflow attempt.
  - Partial-output overflow emits a closing `message_end` and does not retry,
    and stream message-format failures with generic "too large" text remain in
    the dirty-history recovery path instead of being promoted to overflow.
  - Focused pytest passed: 22 context/tool-schema/forced-text/tool-chain
    tests; adjacent context-overflow taxonomy tests passed.
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
  - `--allow-no-go` writes the report without pretending the release is
    complete; at that point `docs/v0.1.18/promotion-gate.json` was `no-go` with
    seven blocker groups because the broad v0.1.18 acceptance rows were still
    PARTIAL.
  - Focused pytest covers GO, PARTIAL/NO-GO, token-pattern failure, and
    token-scan-skipped blocker behavior.
- Added the second R18-07-A promotion-gate hardening slice:
  - Promotion gate review checks now cover every gated acceptance family, adding
    cancellation/concurrency, context-budget, and promotion-gate self-review
    consensus requirements alongside existing run-ledger, SSE, model-gateway,
    and Run Center review checks.
  - Focused unit coverage now proves missing context-budget,
    cancellation/concurrency, and promotion-gate-hardening review consensus rows
    are blockers, enforces same-line review markers, and covers all GitHub token
    pattern families used by the production scan.
  - R18-07-A is promoted to PASS because the gate itself is now complete and
    still correctly reports NO-GO while runtime acceptance rows remain PARTIAL.
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
    filters scheduler rows, while Run Center kept them visible as
    diagnostics-only at that point; the cancellation slice below later enables
    Stop while recovery remains diagnostics-only.
  - Focused backend pytest passed for scheduler active/terminal ledger state,
    fresh attempt ids for reused task dictionaries, scheduler permission-denied
    terminal rows, scheduled tool permission-denied terminal rows,
    scheduler-out-of-primary-session snapshots, Run Center diagnostics-only
    source contract, and the existing scheduler fail-closed permission tests;
    desktop `npm run typecheck` passed.
- Added the first scheduler cancellation slice:
  - Scheduler execution attempts now register an in-process cancel token keyed
    by the scheduler attempt request id before the durable run row becomes
    visible, and release it when the wrapper exits.
  - Scheduled `agent_task` / `skill_call` runs reuse that request id through
    AgentBridge, so Run Center `/cancel` can interrupt model streams, retry
    sleeps, permission waits, and Agent-managed tool execution through the
    existing AgentStream cancellation checkpoints.
  - Direct scheduled `tool_call` runs now inject the same cancel event into the
    tool object before `execute()`, covering cancel-aware tools such as bash,
    MCP, browser, and Feishu CLI without changing their public API.
  - If the scheduler attempt's cancel event is set, the wrapper writes terminal
    `cancelled` with `scheduler_cancelled` / `SCHEDULER_CANCELLED` instead of
    misclassifying the partial reply or cancelled tool result as completed.
    Cancelled failed results and cancelled exceptions are consumed as the
    stopped attempt, so the scheduler does not immediately retry the same due
    task after a Run Center Stop.
  - Run Center keeps scheduler rows diagnostics-only for Open/recover but now
    enables Stop, using the existing request-scoped `/cancel` contract and a
    scheduler-specific success toast.
  - Focused backend pytest passed for cancelled scheduler agent tasks,
    first-visible scheduler cancel token registration, cancel-visible scheduled
    tool calls, cancelled false-return and exception paths, scheduler run-ledger
    terminal state, primary-chat isolation, Run Center source contract, and
    existing scheduler fail-closed permission tests; desktop `npm run typecheck`
    passed.
- Added the scheduler AgentBridge cancel-token owner hardening slice:
  - `AgentBridge.agent_reply()` now treats pre-existing request tokens marked
    with `cancel_token_owner=web_channel` or `cancel_token_owner=scheduler` as
    externally owned, so it reuses the same event but leaves final unregister to
    the owner. If a caller marks an external owner without pre-registering the
    token, or agent initialization fails before the stream starts, AgentBridge
    still owns and cleans up the fallback token to avoid leaks.
  - Scheduled web `agent_task` and `skill_call` contexts now mark
    `cancel_token_owner=scheduler`, keeping scheduler attempts cancellable
    after AgentStream returns and while the scheduler is delivering the reply.
  - Focused tests cover web-owned token preservation, scheduler-owned token
    preservation, external-owner fallback cleanup, ordinary AgentBridge-owned
    cleanup, init-failure cleanup/preservation paths, scheduled skill-call
    owner context wiring, and scheduler agent-task cancellation during the
    post-Agent delivery window.
- Added the sidecar interruption stream-recovery slice:
  - `stream_response()` now handles the sidecar-restart window where the
    durable run ledger still knows about a message run but the new sidecar has
    no in-memory SSE queue for the request id.
  - When the ledger row is already terminal `interrupted/sidecar_interrupted`,
    or a still-active message run has a confirmed dead-owner session lock,
    reconnecting SSE clients receive a typed `type=interrupted`,
    `event_type=run.interrupted`, `terminal=true` stream event instead of a
    vague `invalid request_id` error or a long keepalive wait.
  - The same stream path marks the run terminal exactly once with
    `SIDECAR_INTERRUPTED`, re-reads the durable row before emitting the
    interrupted terminal so another terminal winner cannot be mislabeled,
    removes the dead lock, leaves stale-live locks alone, and keeps the
    recovery scoped to primary message runs so subagent and scheduler rows do
    not masquerade as chat bubbles.
  - SSE replay cursor handling also treats replay-log `interrupted` events as
    terminal, matching `done` / `error` / `cancelled` cleanup behavior if a
    future path stores interrupted events in the replay buffer.
  - Desktop stream consumers now treat `interrupted` / `run.interrupted` /
    `state=interrupted` as a first-class terminal phase, clear the active
    request state, run request-scoped history recovery, and otherwise finish
    the pending assistant bubble with a non-pending sidecar interruption
    fallback plus `stream_interrupted` telemetry.
  - Focused backend and desktop source-contract tests cover the lost-SSE
    sidecar stream terminal and the request-scoped desktop recovery path;
    replay-gap and sidecar-interruption recovery tests passed together, and
    desktop TypeScript still compiles.
- Added the SSE terminal contract closure slice:
  - Backend terminal normalization now forcibly maps terminal legacy types to
    their versioned contract fields, so callers cannot accidentally turn
    `done` into `run.failed`, `error` into `run.completed`, or make a terminal
    event non-terminal by passing stale fields.
  - `done`, `error`, `cancelled`, `interrupted`, and `replay_gap` now share a
    central stream-terminal set for reconnect cleanup and stream tail handling.
    Replay gaps are explicit stream-terminal recovery boundaries with
    `terminal=true` / `terminal_reason=replay_gap`, while still keeping their
    `stream.replay_gap` event type separate from run failure.
  - Generated replay-gap streams now end the current subscriber immediately
    after the terminal recovery event instead of continuing to emit retained
    data events after a terminal boundary; backend SSE state is left intact for
    the still-running request rather than being cleaned as a completed run.
  - Desktop stream cursor cleanup now includes `replay_gap`, matching the
    request-scoped replay-gap recovery handler that already stops the local
    attach and refreshes saved history.
  - Focused tests prove terminal normalization overrides conflicting legacy
    fields, replay-gap terminal metadata is present, agent-stream and worker
    failures emit only machine-readable `run.failed` output without a queued
    `done`, and the desktop replay-gap source contract includes cursor cleanup.
- Added the AgentBridge native model gateway slice:
  - `models/model_gateway.py` now wraps native provider `call_with_tools`
    implementations that do not use `OpenAICompatibleBot.call_with_tools`,
    giving DashScope/Zhipu/Gemini/Claude/Moonshot-style Agent paths the same
    bounded `ModelCallSpan`, retry-after/backoff, retry taxonomy, and
    first-output retry boundary used by the shared OpenAI-compatible path.
  - `AgentLLMModel.call()` and `call_stream()` now route native bot calls
    through the gateway while leaving shared OpenAI-compatible calls unwrapped,
    preventing duplicate telemetry. They also forward explicit
    `model_max_retries` / `max_model_retries` from `LLMRequest`.
  - `AgentLLMModel.call_stream()` now closes the inner model stream when the
    outer consumer closes, so user cancellation or consumer teardown records
    native model spans as `cancelled` and gives the provider iterator a chance
    to release network resources.
  - Native sync calls also accept provider implementations that return a
    single-result generator for `stream=False` (for example DeepSeek/Moonshot
    style adapters), converting the first yielded dict into the sync response
    instead of surfacing an unsupported-response error.
    If that generator raises while producing the first sync result, the gateway
    converts the exception into typed retry evidence and finishes telemetry
    instead of leaking a raw exception past the span.
  - Native stream iterators that raise exceptions now return typed error
    chunks instead of raw exceptions. If output already started, retryable
    exceptions are marked `retry_suppressed=stream_output_started`; if output
    never started and gateway retries are exhausted, the final error carries
    `retry_exhausted`, preventing AgentStream's outer string retry from
    bypassing the model gateway boundary.
  - Usage normalization now understands Gemini-style
    `promptTokenCount` / `candidatesTokenCount` / `totalTokenCount`,
    `thoughtsTokenCount`, and `cachedContentTokenCount`.
  - Focused tests cover native sync 429 retry with `Retry-After`, native sync
    400 fail-closed evidence, native single-result generator sync responses,
    native sync generator exception retry/exhaustion,
    native stream retry before first token, native stream exception suppression
    after first output, native stream exhausted exception markers before first
    output, native stream close/cancel exactly-once telemetry, and no double
    wrapping of the shared OpenAI-compatible gateway.
- Added the legacy `reply_text` telemetry coverage slice:
  - `models/legacy_reply_gateway.py` now wraps bot-factory-created legacy
    provider `reply_text` methods with `/legacy/reply_text` `ModelCallSpan`
    telemetry, covering normal chat/title-generation paths that still bypass
    `call_with_tools`.
  - The wrapper is intentionally transparent: provider-local retry recursion
    remains owned by the adapter, nested recursive `self.reply_text(...)` calls
    bypass the wrapper through a thread-local guard, return values/exceptions
    are preserved, and the top-level public legacy request records one bounded
    span instead of duplicate retry spans.
  - Legacy token envelopes using `total_tokens`, `completion_tokens`, and
    optional `prompt_tokens` are normalized into input/output/total buckets.
    Explicit `error`/4xx/5xx responses and empty `completion_tokens=0`
    completions are recorded as failures. The only zero-token non-empty text
    success exception is currently scoped to ModelScope responses that still
    have the successful response shape (`total_tokens` is present), because
    that normal chat path explicitly treats such responses as `ReplyType.TEXT`;
    ModelScope fallback sentinels without `total_tokens` and other provider
    failure sentinels remain failed telemetry.
  - AgentBridge native `call_with_tools` attempts now suppress inner legacy
    `reply_text` telemetry while the native gateway span is active. This keeps
    ModelScope-style adapters that implement sync `call_with_tools` via
    `self.reply_text(...)` from double-recording `/native/call_with_tools` and
    `/legacy/reply_text` spans for one public model call.
  - Focused tests cover one-span telemetry for provider-internal recursive
    retry, failure-sentinel classification, empty completion failures,
    ModelScope zero-token text success with `total_tokens`, ModelScope fallback
    failure without `total_tokens`, ModelScope status-error precedence,
    non-ModelScope zero-token text failure, and native AgentBridge suppression
    of inner legacy reply spans. Qianfan factory/reply-text payload regressions
    still pass after bot_factory wrapping.
- Added the permission-wait cancellation hardening slice:
  - `ToolPermissionBroker.authorize()` now marks cancellation during an
    interactive permission wait with `cancelled=true` while still cleaning the
    pending request.
  - `AgentStreamExecutor` now upgrades that structured permission decision to
    `AgentCancelledError`, so a user stop during a shell/browser/MCP approval
    prompt follows the normal `agent_cancelled` / `run.cancelled` terminal path
    instead of becoming a permission-denied tool result and another model turn.
  - The permission broker path also preserves cancellation exception passthrough
    if a future broker implementation raises `AgentCancelledError` directly.
  - Focused backend unittest coverage passed for broker pending cleanup,
    AgentStream cancellation upgrade, run-stream cancelled terminal/history
    repair, existing permission denial behavior, capability permission
    shortcuts, active-request waiting-permission snapshots, worker
    cancellation/finalization, scheduler cancellation, and bash cancel-aware
    subprocess behavior.
- Added the non-message orphan interruption slice:
  - WebChannel now records a process boot boundary at module load and uses it
    during active-request recovery to distinguish current-process background
    rows from durable rows left active by a previous sidecar process.
  - Pre-boot `run_type=subagent` and `run_type=scheduler` rows that have no
    in-process cancel token now terminal exactly once as
    `subagent_sidecar_interrupted` / `scheduler_sidecar_interrupted` instead
    of staying indefinitely active in Run Center after a sidecar restart.
  - Pre-boot orphan subagents also mark their `.ecorex/subagents.json` task row
    `interrupted`, which keeps the durable subagent state aligned with the run
    ledger and releases bounded subagent slots after a sidecar restart.
  - Current-process rows with a cancel token are preserved even when their
    timestamp is older than the local boot boundary, and rows created after the
    module boot boundary stay active. This keeps live scheduler/subagent work
    from being misclassified during normal operation or tests with lazy
    WebChannel instantiation.
  - Focused pytest passed for subagent bounded slot reservation, queued
    run-ledger visibility, subagent completion/cancel, scheduler run-ledger
    state, scheduler first-visible cancel token registration, pre-boot orphan
    interruption, current-token preservation, and Run Center subagent/scheduler
    source contracts.
- Added the recent terminal run truth slice:
  - The run ledger now exposes a bounded `terminal_snapshot()` for recent
    completed/failed/cancelled/interrupted rows, including
    `terminal_age_seconds` for diagnostics surfaces.
  - `/api/active-requests` still keeps `requests` active-only, but now adds
    `recentTerminalRequests` / `recent_terminal_requests` and
    `runStatusCounts` / `run_status_counts` so backend recovery surfaces can
    distinguish queued/running/cancelling/finalizing and recent
    completed/failed/cancelled/interrupted states from durable truth.
  - Durable terminal rows now suppress same-request cancel-registry fallback
    rows, preventing stale in-process registry state from reanimating a run
    that the ledger already knows is terminal.
  - Current cancellation remains visible as `cancelling` when a recent
    `cancelled` terminal event has closed the SSE stream but the request's
    cancel token is still registered; older durable terminal rows still prevent
    stale registry rows from being reanimated.
  - Desktop API typing preserves these fields in `RuntimeSnapshot` for future
    Run Center/diagnostics affordances without changing the current active-row
    UI semantics.
- Added the `/message` durable admission hardening slice:
  - `RunLedger.create_run()` now returns whether the row was actually persisted,
    using a same-transaction SQLite existence check before commit. This removes
    the separate post-create read verification window from `/message`
    admission.
  - Web `/message` now fails closed with typed `RUN_LEDGER_UNAVAILABLE` if the
    durable run row cannot be created or confirmed. The worker is not started,
    no SSE stream is exposed, the internal request id is not returned to the
    desktop, and pre-worker cleanup releases the session lock, cancel token,
    request mapping, and any request state.
  - Focused tests cover successful `/message` admission row fields and
    metadata, ledger-create exceptions, and ledger false-return failures. The
    broader admission/run-ledger regression set passed with 15 tests.
  - Descartes, Boole, and Lovelace confirmed no P0/P1 after the final
    same-transaction persistence check and empty-error-request-id contract.
- Added the subagent execution cancellation hardening slice:
  - Subagent child runs now register their child-session cancel token before
    becoming visible as `running` and before entering AgentBridge, closing the
    race where `/api/subagents/{id}/cancel` or parent-session cancellation could
    mark the task terminal while the child still proceeded into model/tool work.
  - Subagent AgentBridge calls now carry `cancel_token_owner=subagent`; the
    bridge borrows that pre-existing token without unregistering it, and the
    `_run_child` finalizer unregisters the child token after completed, failed,
    or cancelled terminal state is written.
  - Direct subagent cancel and parent cascade cancel both surface `cancelling`
    while the child is inside AgentBridge, then settle to durable `cancelled`
    with terminal ledger state and no registry residue after the child exits.
  - Boole found a P1 interleaving where a direct/parent cancel between the
    second `_run_child` status check and the delayed `running` phase write could
    downgrade the ledger from `cancelling` back to `running`. The fix adds a
    `preserve_cancelling` guard to `RunLedger.mark_phase()` and a regression
    that forces that interleaving.
  - Lovelace found a P1 escape hatch where non-Web `/cancel` entrypoints only
    cancelled the parent session token and left child `subagent-*` tokens
    running. ChatChannel, Slack, Telegram, Discord, CowCli fallback, and
    WebChannel now route parent-session stop through the same subagent cascade
    helper, with ChatChannel and CowCli fallback regressions.
  - Focused pytest passed for direct running-child cancellation, parent cascade
    cancellation, phase-downgrade prevention, ChatChannel/CowCli non-Web
    parent stop cascade, and subagent-owned AgentBridge token preservation.
    R18-03-A is now PASS; R18-03-B remains PARTIAL because the broader
    same-session queue policy is still pending.
- Added the same-session rapid resend policy hardening slice:
  - Web `/message` now returns a machine-readable `same_session` decision block
    on success and retryable conflict responses. The policy is explicit:
    `interrupt_previous`, `queue=disabled`.
  - Normal first-writer admission reports `decision=accepted`; rapid resend that
    cancels the previous request and obtains the session lock reports
    `decision=replacement_accepted` with replaced request ids and cancellation
    counts; finalize-window lock acquisition without a live token reports
    `accepted_after_finalize_wait`; and still-busy sessions report
    `retryable_conflict`.
  - Per-session backpressure only ignores existing active request ids after a
    real replacement succeeded. A free lock plus stale active token/durable row
    now rejects before allocating a second request id instead of silently
    becoming a second writer: it returns session backpressure when the session
    active limit is reached and typed same-session retryable conflict otherwise.
  - Competing rapid-resend waiters now use per-session replacement tickets; a
    newer resend supersedes older lock waiters so `queue=disabled` does not
    degrade into an implicit serialized queue under fast clicks.
  - Focused pytest passed for replacement success, retryable conflict, the
    no-interrupt/no-ignore guard under default session limits, superseded
    replacement waiters, regular successful admission, and global/session
    backpressure regressions. R18-03-B is now PASS.
- Added the OpenAI Responses runtime-state slice:
  - Added durable per-workspace Responses state storage at
    `.ecorex/model-responses-state.json`, keyed by provider/model/session hash
    so the runtime can carry `previous_response_id`, prompt-cache settings,
    service tier, truncation, store, and compacted input without writing raw
    session ids into the state index.
  - `plan_responses_api_call()` now loads persisted `previous_response_id` only
    for explicit `responses_input_scope=fresh` calls, so the current full-history
    agent path does not combine `previous_response_id` with duplicate transcript
    input. Full-history calls still reuse the stable prompt-cache/service
    settings, merge config defaults such as `responses_service_tier` and
    `responses_prompt_cache_retention`, and generate a stable hashed
    `prompt_cache_key` when none was supplied.
  - Official OpenAI, explicitly enabled non-stream `use_responses_api=True`
    calls now execute `/responses`, normalize the response back into the
    existing chat-completion shape, record model telemetry with
    `api_path=/responses`, and persist the next-turn state after successful
    responses. HTTP-200 Responses statuses `failed`, `cancelled`, and
    `incomplete` fail closed and do not persist next-turn state. Streaming
    remains on `/chat/completions` until Responses stream event normalization is
    wired.
  - Session delete, service/Web clear-context, clear-history, and dirty-session
    recovery paths now clear stored Responses state so `previous_response_id`
    cannot cross a user-visible history reset boundary; `clear_history=True`
    clears the adapter state even when DB conversation persistence is disabled.
  - Focused pytest passed for Responses state storage privacy, persisted-state
    planning, cleanup lifecycle, non-stream runtime normalization/state
    persistence, failure-state fail-closed handling, stream fallback, existing
    adapter payloads, capability catalog, and model telemetry regressions.
- Added the Responses streaming runtime slice:
  - `normalize_responses_stream_events_to_chat()` maps official Responses
    streaming events into the existing chat-completion stream chunk contract,
    including text/refusal deltas, reasoning deltas, function-call argument
    delta/done events, failed/incomplete/error terminals, completed usage, and
    final-response fallback content when no prior output delta was emitted.
  - Explicitly enabled official OpenAI `stream=True` calls now use
    `/responses` instead of falling back to `/chat/completions`, while keeping
    non-official or disabled providers on the existing chat-completions path.
  - Responses streams reuse the shared model retry/telemetry wrapper with
    `api_path=/responses`, so Retry-After backoff is honored before first output
    and retry remains suppressed after output starts through the existing stream
    state machine.
  - `response.completed` is the only streaming terminal that persists
    next-turn Responses state; failed/incomplete/error streams do not advance
    `previous_response_id`.
  - Model telemetry now recognizes official Responses
    `input_tokens_details.cached_tokens` usage buckets.
  - Focused pytest passed: 25 Responses adapter/runtime tests and 67 adjacent
    model telemetry/capability/Responses tests. R18-04-D is now PASS; R18-04-A,
    R18-04-B, and R18-04-C remain PARTIAL pending capability inventory,
    vision/image telemetry coverage, and provider-local retry ownership.
- Added the explicit model fallback routing slice:
  - Added `models/model_fallback.py` to parse a default-off `model_fallbacks`
    chain from config. Entries can be simple model names or objects with
    `model`/`bot_type`; duplicate primary routes and disabled entries are
    ignored.
  - `AgentLLMModel` now builds a primary route plus configured fallback routes
    for the agent model path. Sync calls try the next route only after the
    current model returns an exhausted retryable error. Streaming calls try the
    next route only before any model text, reasoning, or tool-call output has
    started, so EcoreX never splices partial output from two providers into one
    answer.
  - Fallback responses/chunks are annotated with `model_fallback` metadata
    naming the primary and fallback model/provider, while the existing
    telemetry span per attempt records the actual provider/model used.
  - AgentBridge now binds the explicit fallback route `bot_type` onto created
    bot instances, and `ChatGPTBot` refreshes its OpenAI/custom API key/base
    from that route binding instead of the global `bot_type`; this covers both
    `deepseek -> custom` and `custom -> openai` fallback chains.
  - `config.py` and `config-template.json` document `model_fallbacks` as an
    explicit opt-in stability setting. Focused pytest passed for config parsing,
    sync fallback, route-scoped OpenAI-compatible key/base binding, pre-output
    stream fallback with primary stream closeout, post-output no-fallback, and
    the existing model telemetry/capability suite.
- Added the ModelScope provider-local retry ownership slice:
  - ModelScope agent sync calls now pass `retry_count` through to `reply_text`
    for diagnostics while explicitly disabling provider-local recursive retry,
    so the shared native gateway owns retry attempts, Retry-After/backoff,
    retry exhaustion, telemetry, and fallback routing.
  - ModelScope `reply_text` still preserves legacy direct-chat local retry by
    default, but returns typed `error` / `message` / `status_code` evidence when
    a retryable or non-retryable provider failure reaches the public result.
  - `_handle_sync_response()` now returns typed errors directly instead of
    wrapping failure sentinels as assistant `choices`, closing the false-success
    path that prevented shared fallback after exhausted retryable failures.
  - ModelScope chat payload construction strips AgentBridge control fields such
    as `retry_count`, `model_max_retries`, `model_retry_sleep`, `session_id`,
    `channel_type`, `thinking`, and `reasoning_effort` before calling the
    provider, while preserving the Agent system prompt by merging it into the
    provider message list first.
  - Sync non-JSON HTTP errors now preserve the provider status code and message
    through the same typed error parser used by stream non-200 responses, so
    400/401/429/5xx failures are not misclassified as transport errors.
  - ModelScope stream non-200 and provider error chunks now preserve typed
    status and Retry-After evidence, and stream exceptions yield a typed error
    chunk instead of returning an ignored nested generator.
  - Focused pytest passed for ModelScope shared retry/backoff, real HTTP
    Retry-After propagation without provider-local sleep, non-JSON HTTP status
    preservation, non-retryable 4xx fail-closed behavior, exhausted retryable
    fallback routing, legacy local retry preservation, system prompt merging,
    control-arg stripping, stream non-200 typed errors, and the full model
    telemetry suite. R18-04-C remains PARTIAL pending the remaining
    non-ModelScope provider-local retry loop migrations.
- Added the DeepSeek/MiMo native HTTP error normalization slice:
  - Added `models/model_provider_errors.py` so native HTTP adapters can share
    tolerant non-200 response parsing, nested provider error extraction, and
    Retry-After header propagation without duplicating provider-local logic.
  - DeepSeek and MiMo agent sync/stream paths now return typed provider errors
    with `error`, `message`, `status_code`, code/type, and `retry_after`
    evidence for HTTP non-200 responses and SSE error chunks. The normalizer
    preserves `retry_after`, `retry_after_seconds`, and `retry_after_ms` as
    distinct fields so the shared gateway keeps millisecond Retry-After values
    in the correct unit. The existing AgentBridge/native gateway remains the
    retry owner for those errors.
  - The slice intentionally leaves direct `reply_text` chat behavior unchanged;
    provider-local legacy retry loops there remain visible for later migration.
  - Focused tests cover DeepSeek and MiMo real adapter sync 429 Retry-After
    retry through `model_retry_sleep`, sync non-JSON 400 fail-closed behavior,
    stream 429 Retry-After retry before output, pre-output SSE provider errors
    with `retry_after_ms=500` retrying with a 0.5-second sleep, stream provider
    errors after output being marked `retry_suppressed=stream_output_started`
    without a second provider attempt, control-arg stripping, telemetry
    taxonomy, and adjacent model capability/Responses/gate regression coverage.
    R18-04-C remains PARTIAL pending the remaining native provider and legacy
    retry-loop migrations.
- Extended native HTTP error normalization to Doubao, Moonshot, and MiniMax:
  - Reused `models/model_provider_errors.py` for the three OpenAI-compatible
    native agent adapters so HTTP non-200 responses and SSE provider error
    chunks return typed `error` / `message` / `status_code` evidence instead of
    raw text-only failures.
  - MiniMax SSE errors now preserve provider `http_code` as retry status
    evidence, including top-level and nested MiniMax error shapes, while all
    three adapters preserve separate `retry_after`, `retry_after_seconds`, and
    `retry_after_ms` fields for the shared retry parser. Error payloads with a
    textual `status` or `status_code` such as `status=error` no longer hide
    numeric `http_code` evidence.
  - Focused native HTTP provider tests now cover DeepSeek, MiMo, Doubao,
    Moonshot, and MiniMax across sync 429 Retry-After retry, sync non-JSON 400
    fail-closed behavior, stream HTTP retry before output, pre-output SSE
    `retry_after_ms` retry, top-level `http_code` retry evidence, textual
    status/status_code plus numeric `http_code` precedence, and
    post-output retry suppression without a second provider call. R18-04-C
    remains PARTIAL pending the remaining special native providers, direct-chat
    local retry-loop migration, and vision/image model-call surfaces.
- Added the first vision model-call telemetry slice:
  - `models/legacy_reply_gateway.py` now wraps legacy `call_vision` methods in
    addition to `reply_text`, recording `/legacy/call_vision` spans with
    provider/model, latency, usage tokens, error taxonomy, and exception
    evidence without changing provider behavior.
  - `models/bot_factory.py` now applies the combined legacy model-surface
    wrapper to every created bot, so bot-backed vision routes created by the
    Vision tool inherit telemetry automatically.
  - Focused tests cover successful vision usage/model recording, error-dict
    taxonomy, empty-content failures aligned with the Vision tool fallback
    semantics, HTTP status extraction from error messages, exception telemetry
    with re-raise, bot_factory wrapping for Qianfan `call_vision`, and adjacent
    Qianfan vision routing regressions. R18-04-B remains PARTIAL pending
    image-generation/create_img and remaining raw vision HTTP surface inventory.
- Added the image-generation model-call telemetry slice:
  - `models/legacy_reply_gateway.py` now wraps legacy `create_img` methods in
    addition to `reply_text` and `call_vision`, recording `/legacy/create_img`
    spans with provider/model, latency, retry-count field, tuple failure
    taxonomy, and exception evidence without changing provider return values or
    provider-local retry behavior.
  - `models/bot_factory.py` continues to apply the combined legacy
    model-surface wrapper to every created bot, so OpenAI-compatible image
    generation, LinkAI image generation, ZhipuAI image generation, and similar
    bot-backed `create_img` routes inherit telemetry automatically when created
    through the factory.
  - Focused tests cover successful tuple result telemetry, `HTTP 429` tuple
    failure taxonomy, provider-internal recursive retry de-duplication, and the
    combined legacy surface wrapper. R18-04-B remains PARTIAL pending remaining
    raw vision HTTP surface inventory; R18-04-C remains PARTIAL because this
    slice records image generation calls but does not migrate provider-local
    `create_img` retry loops to the shared retry policy.
- Closed the remaining raw vision telemetry inventory:
  - `agent/tools/vision/vision.py` now records `/vision/chat/completions`
    spans around the Vision tool's OpenAI/LinkAI raw HTTP path, including
    success usage, HTTP non-200 failures, provider error bodies, timeouts,
    connection failures, and fallback attempts. Bot-backed Vision providers
    continue to use `bot.call_vision` and therefore remain covered by the
    legacy `/legacy/call_vision` wrapper instead of double-recording.
  - `models/chatgpt/chat_gpt_bot.py` now records `/legacy/reply_image`
    telemetry for the old non-Agent `ContextType.IMAGE` recognition path that
    calls the OpenAI-compatible HTTP client directly.
  - `models/linkai/link_ai_bot.py` now records `/legacy/linkai_chat`
    telemetry for the legacy LinkAI direct chat path, including the production
    image-cache flow where `ChatChannel` stores an image and LinkAI later sends
    a multimodal `/v1/chat/completions` request from `_chat()`.
  - Focused tests cover raw Vision HTTP success, non-200 failure taxonomy with
    JSON error bodies, error-body `http_code` precedence, non-dict usage,
    string error bodies, timeout re-raise telemetry, fallback attempts, legacy
    `reply_image` success/HTTP-error/malformed-content behavior, and LinkAI
    cached-image multimodal, non-200 JSON, timeout retry, and connection retry
    telemetry. With the inventory cross-check finding no remaining production
    raw vision HTTP surfaces, R18-04-B is promoted to PASS. R18-04-C remains
    PARTIAL because provider-local retry loops and retry policy migration are
    still pending.
- Migrated OpenAI-compatible image generation retry ownership:
  - `models/openai/open_ai_image.py` now routes `OpenAIImage.create_img`
    retry decisions through `models/model_retry.py`, honoring Retry-After or
    shared backoff for retryable 408/429/5xx/timeout/network failures while
    failing closed on non-retryable 4xx errors. The existing
    `gpt-image-2-pro` to `gpt-image-2` unavailable-model fallback remains a
    model-selection fallback rather than a retry loop.
  - `models/legacy_reply_gateway.py` now reads provider-supplied thread-local
    create-image error details before recording `/legacy/create_img`, so
    OpenAI image failures keep typed status, code, type, and message evidence
    while preserving the existing tuple return shape used by channels. Legacy
    bot classes that do not call `OpenAIImage.__init__()` lazily initialize the
    image client and DALL-E token bucket instead of escaping the tuple-return
    contract.
  - Focused tests cover Retry-After retry success, non-retryable 400
    fail-closed behavior with typed telemetry, retryable 503 exhaustion,
    injected retry-sleep behavior, model-unavailable fallback without retry,
    lazy token-bucket initialization, local image rate-limit short-circuiting
    without an upstream image request, thread-local failure evidence under
    concurrent requests, and the existing legacy create_img telemetry
    regressions. R18-04-C remains PARTIAL because LinkAI/Zhipu/Azure/ModelScope
    image generation, remaining native provider error normalization, and legacy
    direct-chat retry loops are still pending.
- Extended special native provider error normalization to Claude, Zhipu,
  Gemini, DashScope, and LinkAI:
  - Claude, Gemini, and LinkAI raw HTTP agent sync/stream paths now return
    typed `provider_error_response` / `http_error_response` dictionaries for
    non-200 responses, provider SSE error events, transport exceptions, and
    post-output stream errors. This preserves status, code/type, message, and
    Retry-After evidence for the shared native gateway instead of throwing
    text-only exceptions or hiding retry evidence inside provider-specific
    shapes.
  - Zhipu SDK exceptions now normalize response-backed HTTP failures, nested
    provider error bodies, top-level `status/http_code/type/error_type`, and
    `retry_after` / `retry_after_seconds` / `retry_after_ms` values before they
    reach AgentBridge. DashScope SDK responses now use the same typed error
    shape for sync and streaming non-OK chunks, including timeout/network
    classifications and retry-after fields from SDK proxy objects.
  - Focused tests cover Claude/Gemini/LinkAI sync 429 Retry-After retry,
    non-JSON 400 fail-closed behavior, stream HTTP retry before output,
    pre-output SSE provider errors with millisecond Retry-After evidence,
    post-output retry suppression, timeout and connection retry taxonomy,
    `stream_output_started` suppression reason, Gemini `data:{...}` SSE
    prefixes, Zhipu SDK exception retry/fail-closed behavior, and DashScope SDK
    sync/stream error normalization, including proxy objects that raise from
    direct attribute probes. Claude and Zhipu stream setup exception fallbacks
    now snapshot typed error responses before returning generators, avoiding
    Python 3 cleared-exception-variable `NameError` paths. The full model
    telemetry suite and
    model-capability/Responses/promotion-gate regression set passed. R18-04-C
    remains PARTIAL pending non-OpenAI image retry-loop migration, legacy
    direct-chat retry-loop migration, and provider-specific edges outside the
    AgentBridge native gateway.
- Hardened the shared model capability catalog and model-call control path:
  - `models/model_capabilities.py` now records explicit support flags for
    `reasoning_effort`, `verbosity`, provider thinking controls, provider-safe
    reasoning effort values, and the token-limit parameter name. Official
    OpenAI fixed-sampling models map `max_tokens` to `max_completion_tokens`,
    strip unsupported sampling params, support OpenAI reasoning effort and
    verbosity controls, and keep stream usage enabled only for official OpenAI
    chat providers. Non-official OpenAI-compatible bases no longer inherit
    official OpenAI stream-usage or fixed-sampling behavior only because their
    provider id says `openai`; `capabilities_for_config()` and `/api/models`
    use the same base-aware downgrade so the UI/catalog no longer overstates
    official OpenAI-only controls for custom compatible endpoints.
  - AgentBridge now checks capability flags before adding `thinking`,
    `reasoning_effort`, or `verbosity` controls, so models such as Gemini do
    not receive unsupported thinking payloads. DeepSeek V4 keeps its
    `thinking` and `max` effort semantics, while official OpenAI normalizes
    overly strong local `reasoning_effort=max` to provider-safe `high`.
    Unsupported effort values for `high/max` providers now conservatively
    fall back to `high` instead of escalating to `max`; explicit `max` remains
    available for providers that support it.
  - The shared OpenAI-compatible bot path now derives official-vs-compatible
    capability identity from both provider id and API base, coerces system
    messages to user messages for o1-style models, forwards reasoning and
    verbosity into both chat completions and the default-disabled Responses
    adapter, and reuses sanitized token limits across chat and Responses
    planning. Subclasses that omit a provider id now explicitly resolve to
    official OpenAI only when the API base is the official OpenAI host, and to
    generic OpenAI-compatible behavior otherwise.
  - The legacy `ChatGPTBot` initialization now uses the same capability
    catalog instead of a hardcoded model list, and derives the capability
    provider from the actual route/API base instead of hardcoding official
    OpenAI. Future official `gpt-5.x` variants inherit unsupported-parameter
    stripping, while custom OpenAI-compatible direct-chat routes keep ordinary
    sampling controls.
  - Focused tests cover ModelScope namespace inference, official OpenAI
    token/reasoning/verbosity sanitization, non-official OpenAI-compatible
    bases preserving ordinary sampling and dropping unsupported controls, o1
    system-message coercion, Responses reasoning/text/max-output mapping,
    AgentBridge model-control gating, legacy ChatGPT args, and `/api/models`
    capability exposure, including custom OpenAI-compatible base downgrades.
    R18-04-A remains PARTIAL pending a machine-readable provider capability
    matrix and broader native/direct-call coverage.
- Migrated LinkAI and Zhipu legacy image retry ownership:
  - Added `models/model_image_retry.py` as a shared helper for legacy
    image-generation surfaces. It reuses `models/model_retry.py` for
    Retry-After parsing/backoff, retryable taxonomy, bounded retry attempts,
    and thread-local `/legacy/create_img` sidecar evidence.
  - `models/linkai/link_ai_bot.py` now treats non-2xx image-generation
    responses as typed provider errors, preserves status/code/type/message and
    Retry-After evidence, and retries 408/429/5xx/timeout/network failures
    through the shared helper instead of a single opaque catch-all failure.
  - `models/zhipuai/zhipu_ai_image.py` now routes SDK image-generation
    exceptions through the same helper, uses existing Zhipu exception
    normalization when available, and records local image rate-limit failures
    as typed 429 sidecar evidence without making an upstream request.
  - `models/legacy_reply_gateway.py` and `models/model_telemetry.py` now
    preserve retry metadata (`retry_attempt`, `max_retries`,
    `retry_exhausted`, `retry_after*`) from provider image sidecars into
    model-call telemetry, so Run Center/diagnostics can distinguish
    non-retryable failures from exhausted retryable failures.
  - Focused tests cover LinkAI image 503 Retry-After retry exhaustion with
    typed telemetry and Zhipu image 429 Retry-After retry success. Full model
    telemetry plus Responses/capability/model-handler regressions passed.
    R18-04-C remains PARTIAL pending remaining image edge coverage,
    legacy direct-chat retry-loop migration, and provider-specific edges
    outside the AgentBridge native gateway.
- Hardened ModelScope legacy image generation retry and terminal-state evidence:
  - `models/modelscope/modelscope_bot.py` now exposes the legacy image method
    with the same `retry_count` / `model_retry_sleep` contract used by other
    legacy image providers. Async task creation goes through
    `models/model_image_retry.py`, so 408/429/5xx/timeouts honor shared
    Retry-After/backoff and non-retryable create-task 4xx responses fail closed
    with typed sidecar evidence.
  - The ModelScope polling phase intentionally does not retry the whole image
    job after a task id exists, avoiding duplicate upstream generation tasks.
    Instead, missing output URLs, task `FAILED`, task `CANCELED`, poll
    timeout, poll non-retryable 4xx, and exhausted retryable poll errors now
    write typed `/legacy/create_img` sidecar details for diagnostics and
    model-call telemetry.
  - Focused tests cover task-creation 429 Retry-After retry followed by poll
    success, non-retryable create-task 400 fail-closed telemetry without an
    extra poll, poll 400 fail-closed telemetry without waiting for timeout,
    exhausted poll 503 evidence preserving Retry-After/retry metadata, and
    typed 504 timeout evidence for long-running tasks. Full model telemetry
    plus Responses/capability/promotion-gate regressions passed. R18-04-C
    remains PARTIAL pending remaining image edge coverage, legacy direct-chat
    retry-loop migration, and provider-specific edges outside the AgentBridge
    native gateway.
- Hardened OpenAI/ChatGPT legacy direct-chat retry ownership:
  - Added `models/openai/legacy_reply_retry.py` as a shared helper for legacy
    OpenAI-compatible `reply_text` surfaces. It normalizes compat exceptions
    into provider error details, reuses the shared retry taxonomy/backoff
    policy, preserves header and provider-body Retry-After evidence, and
    builds old-shape failure results with typed retry metadata.
  - `models/openai/openai_compat.py` now preserves HTTP headers and
    `retry_after` when mapping `OpenAIHTTPError` into compat exceptions, so
    direct-chat retry decisions no longer lose upstream backoff hints.
  - `ChatGPTBot.reply_text` and `OpenAIBot.reply_text` now use the shared
    retry decision for 408/429/5xx/timeout/network failures and return typed
    fail-closed evidence for non-retryable 4xx responses instead of falling
    through to untyped legacy sentinels. Network errors keep status `0` so
    telemetry classifies them as `network_error`.
  - `models/legacy_reply_gateway.py` now propagates `error_taxonomy`,
    `retry_after*`, `retryable`, `retry_attempt`, `max_retries`, and
    `retry_exhausted` from legacy `reply_text` failure results into
    `/legacy/reply_text` telemetry.
  - Focused tests cover ChatGPT 429 Retry-After retry success, ChatGPT 400
    fail-closed evidence, and OpenAI legacy completion 503 retry exhaustion
    evidence, body-level `retry_after_ms` backoff, plus a local adapter
    exception guard that records non-retryable `unknown` /
    `legacy_adapter_error` telemetry instead of provider retry exhaustion.
    Full model telemetry and combined capability/model-handler/Responses/
    promotion-gate regressions passed. R18-04-C remains PARTIAL pending
    remaining non-OpenAI legacy direct-chat retry-loop migration,
    remaining image edge coverage, and provider-specific edges outside the
    AgentBridge native gateway.
- Migrated REST legacy direct-chat retry ownership:
  - Added `models/legacy_direct_chat_retry.py` as the shared retry/failure
    helper for legacy HTTP REST `reply_text` providers. It normalizes HTTP
    responses and `requests` timeout/network exceptions, reuses the shared
    Retry-After/backoff policy, and returns old-shape failure dicts with typed
    retry metadata for `/legacy/reply_text` telemetry.
  - DeepSeek, Doubao, Moonshot, MiMo, MiniMax, and Qianfan direct-chat
    `reply_text` paths now route 408/429/5xx/timeouts/network errors through
    the shared decision helper, accept injectable `model_retry_sleep` for
    deterministic tests, and fail closed on non-retryable 4xx with status,
    taxonomy, retryable, and retry-exhaustion evidence.
  - Qianfan's shared `_error_result()` now avoids retrying text chat from the
    vision error path when no chat session exists, preserving typed failure
    evidence instead of creating a follow-on adapter exception.
  - Focused tests cover REST direct-chat 429 Retry-After retry success,
    non-retryable 400 fail-closed telemetry, requests timeout retry
    exhaustion, and HTTP 408 response retry exhaustion across DeepSeek,
    Doubao, Moonshot, MiMo, MiniMax, and Qianfan. Full model telemetry and
    combined capability/model-handler/Responses/promotion-gate regressions
    passed. R18-04-C remains PARTIAL pending SDK/app-code legacy direct-chat
    retry-loop migration, remaining image edge coverage, and provider-specific
    edges outside the AgentBridge native gateway.
- Migrated SDK/app-code legacy direct-chat retry ownership:
  - Extended `models/legacy_direct_chat_retry.py` with SDK response-object and
    SDK exception normalization. HTTP-like SDK exceptions still preserve
    `response.json()` details, DashScope-style proxy objects are read safely
    without `hasattr()` traps, Retry-After headers/body fields are retained,
    and unknown local adapter exceptions remain non-retryable
    `legacy_adapter_error` instead of masquerading as provider 500 failures.
  - Claude and LinkAI special HTTP/app-code legacy `reply_text` paths now route
    non-200 responses and transport exceptions through the shared retry
    decision helper, accept injectable `model_retry_sleep`, and return typed
    fail-closed evidence for `/legacy/reply_text` telemetry.
  - Zhipu and DashScope SDK legacy `reply_text` paths now normalize SDK
    response objects and provider exceptions before retrying, respect
    Retry-After/backoff, avoid noisy provider-error stack traces, and keep
    unknown adapter exceptions as non-retryable local failures.
  - LinkAI's production `_chat()` path, used by `reply()` for text/app-code
    conversations and `/legacy/linkai_chat` telemetry, now uses the same shared
    retry decision/failure helper for non-200 responses, timeout/network
    exceptions, and unknown adapter exceptions while preserving per-attempt
    model-call spans.
  - Focused tests cover Claude/LinkAI 429 Retry-After retry success and
    non-retryable 400 fail-closed telemetry, Zhipu/DashScope SDK Retry-After
    retry success, SDK 400 fail-closed telemetry, LinkAI `_chat` Retry-After
    retry success, timeout/network shared backoff, and unknown adapter
    exceptions that do not retry. R18-04-C remains PARTIAL pending older
    direct-chat edges such as Baidu Wenxin, remaining image edge coverage, and
    provider-specific edges outside the AgentBridge native gateway.
- Migrated Baidu Wenxin legacy direct-chat retry ownership:
  - `models/baidu/baidu_wenxin.py` now routes HTTP non-200 responses through
    `models/legacy_direct_chat_retry.py`, preserving Retry-After/backoff,
    timeout/network retry, non-retryable 4xx fail-closed evidence, and
    `/legacy/reply_text` telemetry propagation.
  - Baidu's 200-status provider error bodies now normalize `error_code`,
    `error_msg`, status, and retry-after fields before the shared retry
    decision runs, so body-level 429/5xx evidence can retry without being
    mistaken for a successful `result` response.
  - Unknown local adapter exceptions remain non-retryable
    `legacy_adapter_error` and clear the session, while provider/transport
    failures no longer clear conversation state during retryable outages.
  - Baidu access-token acquisition now uses the same configured
    `request_timeout` and raises response-backed token errors on non-200 token
    responses, so token endpoint stalls and token HTTP `Retry-After` outages
    enter the shared retry path before the chat request starts.
  - Focused tests cover HTTP 429 Retry-After retry success, body-level
    retry-after-ms retry success, non-retryable 400 fail-closed telemetry,
    chat timeout retry exhaustion, access-token timeout retry with configured
    timeout propagation, access-token HTTP `Retry-After` retry, and unknown
    adapter-error non-retry. R18-04-C remains
    PARTIAL pending remaining image edge coverage and provider-specific edges
    outside the AgentBridge native gateway.
- Promoted Run Center from diagnostics panel to first-class runtime control:
  - `desktop/src/App.tsx` now exposes Run Center from the main sidebar with a
    count badge and independent modal while retaining the diagnostics-embedded
    panel for support workflows.
  - Run Center merges `activeRequests` and `recentTerminalRequests`, so failed
    and interrupted recent terminal rows remain visible for open/recover,
    diagnostics, and retry policy decisions instead of disappearing once the
    live request leaves the active list.
  - `/api/active-requests` now attaches a backend Run Center action policy to
    every active/recent row: `actions.open/recover/retry/stop/diagnostics`,
    `retry_mode`, and `retry_disabled_reason`. Failed ordinary chat-session rows
    support explicit `manual_retry_prepare`; subagent and scheduler rows remain
    stop/diagnostics-only until replay contracts are implemented.
  - Desktop Stop now treats ordinary/scheduler `/cancel` no-op results as
    failures instead of showing a false success toast; subagent stop continues
    to use `/api/subagents/{task_id}/cancel` with request-scoped fallback.
  - The v0.1.18 promotion gate now requires Run Center evidence for
    first-class navigation and retry/recover policy, so old diagnostics-only
    evidence cannot satisfy R18-06-A by accident.
  - Focused tests cover the Run Center source contract, backend action-policy
    snapshot, active/recent terminal row truth, promotion-gate markers, and
    Desktop TypeScript typechecking. R18-06-A is promoted to PASS; v0.1.18
    remains NO-GO only while model-call rows R18-04-A/R18-04-C remain PARTIAL.
- Closed the R18-04-A provider capability matrix gap:
  - `models/model_capabilities.py` now drives `get_model_capabilities()` from
    declarative `ModelCapabilityRule` entries instead of a second hardcoded
    branch chain. The same rule set exports a machine-readable provider
    capability matrix with schema `ecorex.model-capabilities.v1`.
  - Matrix rows include provider/model id, API family, host policy,
    system-message policy, tool/stream/stream-usage support, sampling support,
    unsupported params, token-limit mapping, reasoning/verbosity/thinking
    controls, surfaces, rule ids, full rule evidence, and the resolved runtime
    capability object.
  - `/api/models` now exposes the chat `capability_matrix` generated from the
    configured provider catalog without credential fields, giving Desktop,
    diagnostics, and the v0.1.18 evidence gate a stable machine-readable
    surface for provider/model behavior.
  - Official OpenAI fixed-sampling and o1 rules remain explicit, Azure OpenAI
    has its own API-family/host-policy rule that is reachable from explicit
    `chatGPTOnAzure` routes and the legacy `use_azure_chatgpt` flag, and custom
    OpenAI-compatible o1 routes intentionally keep native system messages
    rather than inheriting official OpenAI coercion semantics. Native provider
    rows advertise only native/AgentBridge surfaces, so official-only Responses
    support is not overclaimed for DeepSeek, DashScope, Gemini, or similar
    providers.
  - Focused tests cover JSON-serializable matrix shape, shared resolver rule
    ids, official OpenAI fixed-sampling/o1 behavior, Azure OpenAI explicit and
    legacy-flag provider resolution plus matrix export, custom-compatible o1
    non-coercion, DeepSeek thinking controls with native-only surfaces,
    AgentBridge model-control gating, legacy ChatGPT arg sanitization,
    ModelsHandler export without credential/base leakage, and the promotion
    gate's new
    `provider capability matrix` evidence marker. R18-04-A is promoted to PASS;
    R18-04-C remains PARTIAL pending Azure legacy DALL-E image retry ownership
    and image-generation skill provider retry/fail-closed inventory.
- Closed the R18-04-C retry/fallback gap:
  - `AzureChatGPTBot.create_img()` now routes DALL-E 2 submit/poll and DALL-E 3
    generation through shared image retry/fail-closed helpers. Azure image
    calls preserve Retry-After/backoff, timeout/network classification,
    non-retryable 4xx sidecar evidence, retry exhaustion metadata, config
    fallback for legacy OpenAI base/key/deployment settings, and deterministic
    `model_retry_sleep` injection for tests.
  - `skills/image-generation/scripts/generate.py` now has a standalone typed
    provider error and bounded retry layer for OpenAI, LinkAI, Gemini,
    Seedream, Qwen, and MiniMax HTTP calls. Provider downloads also retry,
    non-retryable provider errors fail closed without trying unrelated
    providers, and fallback is limited to retryable exhausted provider
    failures with JSON `provider_error` evidence.
  - Focused tests cover Azure DALL-E 2/3 Retry-After retry, fail-closed 4xx,
    timeout evidence, config fallback, and existing `/legacy/create_img`
    regressions; skill tests cover all six provider labels, fail-closed main
    behavior, retry-exhausted fallback, and body-level provider errors for
    Qwen, Seedream, and MiniMax. R18-04-C is promoted to PASS.
  - The promotion gate now reports GO with 21 checks, 0 blockers, and 0
    warnings after final model-gateway multi-agent consensus.

## Release Packaging Handoff

- Advanced the release boundary to v0.1.18 across Desktop, runtime, WebUI,
  Admin API, download site defaults, smoke defaults, release scripts, and
  installer-only README templates. Compatibility client-event keys for older
  desktop/WebUI clients remain intentionally present.
- Closed release-slice review blockers found by the parallel agents:
  - Desktop telemetry now retries compatible enterprise client-event keys on
    `invalid client key`, matching the WebUI/Admin compatibility contract.
  - macOS DMG workflow upload paths no longer hardcode `0.1.17`; WebUI macOS
    smoke defaults now target `0.1.18` / `v0.1.18`.
  - Windows signed packaging now regenerates `latest.yml` and `.blockmap` after
    setup signing via `desktop/scripts/regenerate-win-update-feed.mjs`, so the
    update feed can match the final signed installer bytes. The signed path was
    hardened again after discovering that `electron-builder --prepackaged ...
    nsis` rewrites `resources/elevate.exe`; `sign-win.ps1 -NsisHelperOnly` now
    signs the electron-builder NSIS `elevate.exe` cache before NSIS generation.
  - The download page now passes the manifest version into `ready-unsigned`
    install-smoke checks and refuses ready links when size/hash are absent.
  - The macOS release lane now explicitly supports unsigned/unnotarized DMGs:
    strict signing remains enforced only when notarization is requested. The
    workflow dispatch helper uses `int64` run ids, because current GitHub run
    ids exceed PowerShell `Int32`.
  - macOS capability preinstall now prefers binary wheels and requires a binary
    `cryptography` wheel, avoiding transient Rust/OpenSSL source builds on
    GitHub macOS x64 runners.
- Built, signed, and validated current release artifacts:
  - `desktop/release/EcoreX_0.1.18_x64-setup.exe`, size `157,440,216`,
    SHA256 `AE5E6E702BD431EE2D5FBF5EED2B6DF80A8DE651F8376B56E7BE8E15F9B3281E`;
    Authenticode status is `Valid`. `desktop/release/latest.yml` and
    `.blockmap` were regenerated after setup signing with SHA512
    `9uRo8pR/GLU0rJOs6hO/pwXR9R6qaK0QS18alQ6PBm9ikRl5urmhUpe1cSdDtk9aihv71f7105+Bb4CQFMcADw==`.
  - `release-artifacts/EcoreX_0.1.18-webui-windows-x64.zip`, size
    `80,940,550`, SHA256
    `4E0765C9D687338D12A63FD7E9EE4A9464E13BAD97DF644C6629550DE53D79F7`.
  - `release-artifacts/EcoreX_0.1.18-webui-macos-universal.zip`, size
    `158,050,345`, SHA256
    `27F7240291B55DCA0264D321BA212C8977A72C5A901AA244E64EC607C1867F12`.
  - `release-artifacts/EcoreX_0.1.18-web-linux-service.tar.gz`, size
    `3,349,211`, SHA256
    `2184ADF0047F3D4FAF52826FAF360F55215A4E982BE82BFC2DE69996BA630B70`.
  - `release-artifacts/macos-dmg-workflow/ecorex-macos-arm64/EcoreX_0.1.18_arm64.dmg`,
    size `213,691,641`, SHA256
    `2D131EAD984A62F8B5F36135FE1D40B0D5E4EC95736E8A1D3304E58175A7A26E`;
    unsigned install smoke passed in GitHub Actions run `27925947692`.
  - `release-artifacts/macos-dmg-workflow/ecorex-macos-x64/EcoreX_0.1.18_x64.dmg`,
    size `221,016,893`, SHA256
    `6E5E04AC1703D71E65F123DDA507C20CE78896EA46E08B7859D4CFFE3B06F435`;
    unsigned install smoke passed in GitHub Actions run `27926165346`.
  - `release-artifacts/EcoreX_0.1.18-public-release.zip`, size
    `834,704,384`, SHA256
    `9DC1880DF3AAE35015AF1E7289CB6AC63F1AB87AB57D4762F694D84C03A0D950`.
- Windows signing is closed for this release. The provider preflight can still
  report Smart Card / CertProp stopped and no visible SimplySign CSP key
  containers from this non-elevated shell, but actual signing succeeds with the
  configured `C:/脚本签名工具` toolchain when `-SkipProviderPreflight` is used.
  `npm run package:win:signed` now reproduces the signed installer end to end,
  including unpacked executable signatures, NSIS helper source signing, setup
  signing, and post-sign update-feed regeneration. Installed-app smoke wrote
  `docs/v0.1.18/win-installed-smoke.json` with sidecar/auth/auth-negative
  checks passing and all required signatures `Valid`.
- macOS DMG dispatch is closed for the unsigned release lane. The first all-arch
  run exposed two release-script issues: unsigned bundle validation still
  enforced strict `codesign --verify`, and x64 `office-pdf` preinstall fell back
  to a source `cryptography` build. Both were fixed, arm64 and x64 install
  smoke JSON imported into `deploy/ecorex-site/manifest.json`, and the final
  public release bundle validates with both DMGs marked `ready-unsigned`.

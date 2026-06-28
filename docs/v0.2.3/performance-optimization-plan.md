# EcoreX v0.2.3 Performance Optimization Slice

## Slice ID

R23-16P: Long-session and complex-task performance optimization.

This slice is inserted before the final release gate. It must not roll back any
v0.2.2 or v0.2.3 capability.

## Symptom

After EcoreX runs for a while, or after complex tasks with tools, artifacts,
OCR/browser work, scheduler activity, image jobs, subagents, or reconnects, the
runtime and UI can become noticeably slower.

## Non-Negotiable Constraints

- No capability rollback: CDP-first, Fast OCR, External Connections, image jobs,
  scheduler, subagents, memory, permissions, and self-learning skills remain.
- `RunEventLedger` stays the durable source of runtime truth.
- `RuntimeProjection` remains the frontend state source; the frontend must not
  invent connection/run/skill states locally.
- Run Center remains hidden from ordinary users.
- Optimization must be measurable, replayable, and documented in v0.2.3
  evidence files.
- Security posture cannot weaken: no raw secret, cookie, OCR full text, browser
  profile, or token-shaped payload in performance logs or metrics.

## Initial Hotspot Hypotheses

- Runtime projection replay can become expensive when long sessions accumulate
  many events, artifacts, image-job updates, subagent updates, and tool payloads.
- SSE handlers and history refresh can over-apply projection updates or re-render
  large message lists too often.
- `App.tsx` holds a large amount of session, runtime, artifact, approval, and
  settings state; complex tasks may fan out into broad React re-renders.
- Message rendering and artifact previews can repeatedly parse or normalize
  large Markdown/artifact payloads.
- Browser/CDP, OCR, image generation, scheduler, and subagent work can leave
  processes, threads, timers, caches, temporary files, or polling loops alive
  longer than needed.
- Diagnostics and release harnesses currently prove correctness more than
  sustained performance.

## Execution Plan

### R23-16P-00 Baseline And Metrics

- Add a sensitive-data-safe performance evidence schema for CPU/memory samples, projection
  latency, SSE backlog, render/update latency, browser/OCR latency, image-job
  queue timing, and cache sizes.
- Define baseline scenarios:
  - 200-message long session with reload/reconnect.
  - Complex task with browser/CDP + OCR + tool calls + artifacts.
  - Image job with OCR reuse and multiple artifacts.
  - Scheduler/subagent event replay.
  - Settings navigation with External Connections and skills loaded.
- PASS: baseline numbers are recorded before optimization; no metric contains
  raw user text, OCR text, secrets, cookies, or token-shaped values.

### R23-16P-01 Runtime Projection Efficiency

- Profile `RuntimeProjectionService` request/session/history projection paths.
- Eliminate session projection N+1 replay by grouping session events once and
  reducing per request from that grouped set.
- Add a per-request projection cache keyed by `request_id + latest_event_id`.
- Build safe event DTOs only when `include_events=1`; default projection paths
  should avoid repeated event sanitization when UI does not need raw event rows.
- History overlay should replay current page request ids plus active/recent
  request ids, not every request in a long session.
- Add bounded projection windows and summarized older event ranges where safe,
  without losing durable ledger fidelity.
- Cache per-request projection by latest event id where invalidation is explicit.
- Keep raw event replay available for diagnostics/export.
- PASS: long-session projection latency improves without changing replay output
  semantics for current/recent turns.

### R23-16P-02 Event Payload And Ledger Hygiene

- Audit noisy event producers for high-frequency updates and oversized payloads.
- Measure ledger append/query latency, SQLite busy/locked count, payload bytes,
  JSON serialization/redaction time, and event write frequency by event type.
- Review SQLite connection/PRAGMA overhead and query indexes for image job,
  request, session, and event-type lookups.
- Coalesce non-terminal progress updates when equivalent state is already
  represented, while preserving terminal events and auditability.
- Preserve every terminal/error/cancelled/interrupted/timeout, permission,
  artifact, and materialization event.
- Add payload byte-size counters and redacted truncation evidence.
- PASS: event count and payload size are bounded in stress scenarios; refresh
  still reconstructs correct terminal state.

### R23-16P-03 Frontend Render And State Isolation

- Profile session message rendering, SSE update handlers, artifact previews,
  markdown normalization, runtime snapshot refresh, and settings panels.
- Stabilize callbacks and props passed into memoized message components so
  periodic runtime ticks and snapshot refreshes do not re-render historical
  messages.
- Consolidate SSE non-terminal updates into a per-request render buffer; flush
  terminal/error/cancelled/replay-gap events immediately.
- Make collapsed long replies truly render a bounded preview, and render full
  Markdown only after expansion.
- Lazy-mount process/step details while collapsed; render only summary/current
  step until expanded.
- Add shared artifact availability/status TTL cache keyed by canonical path or
  status path, with in-flight promise sharing and terminal refresh invalidation.
- Cache context/token estimates by message id/content hash instead of rescanning
  full message and step trees on unrelated updates.
- Introduce memoized/virtualized rendering or scoped stores where needed, while
  keeping projection as the source of truth.
- Throttle non-terminal visual updates and avoid re-normalizing stable message
  content.
- PASS: no text overlap or UI regression; long task UI remains responsive during
  streaming and after history reload.

### R23-16P-04 Tool Resource Lifecycle

- Audit browser/CDP worker shutdown, Playwright persistent fallback, OCR cache,
  image-job temp/status files, scheduler timers, subagent handles, and optional
  ability polling.
- Add mtime-aware scheduler projection caching; invalidate on task mutation.
- Add subagent terminal TTL/archive summaries so `.ecorex/subagents.json` does
  not grow without bound while active/cancelling/running tasks remain recoverable.
- Bound image-job worker concurrency and coalesce equivalent non-terminal
  progress while preserving terminal/artifact events.
- Add terminal image-job hot-set retention with `max_entries` plus TTL, while
  ledger/projection remain canonical for historical reconstruction.
- Add Browser/CDP idle detach, profile health checks, screenshot/tmp TTL cleanup,
  and diagnostics for live browser processes, profile size, screenshot count,
  and profile lock/stuck states.
- Add scheduler workerization: the scan loop claims due tasks, bounded workers
  execute tasks, and failed tasks get `retry_after_at` / `backoff_count`.
- Add MCP stdout queue bounds, shutdown drain visibility, noisy server handling,
  and rotating logs.
- Add OCR cache locking, input pixel limits, LRU/TTL evidence, and peak input
  memory metrics.
- Add bounded caches, idle cleanup, idempotent close paths, and diagnostics for
  live workers/processes.
- PASS: after a complex-task soak, there are no unbounded threads/processes,
  cache growth, temp files, or polling loops.

### R23-16P-05 Performance Harness And Release Gate

- Add repeatable smoke scripts and pytest contracts for projection latency,
  event-volume bounds, OCR/browser latency, renderer long-task smoke, and
  complex-task soak summaries.
- Add `docs/v0.2.3/performance-harness-matrix.json`; each row must declare
  scenario, commands, metrics, thresholds, artifact, and redaction contract.
- Extend `docs/v0.2.3/harness-matrix.json`, evidence ledger, and review log with
  numeric thresholds.
- Make R23-16P a release-blocking gate, not a best-effort note.
- PASS: Runtime/Backend, Frontend/UX, Tools/Resource, Harness/Test,
  Security/Audit, and Release/Regression reviewers all PASS with evidence.

## Candidate Metrics

- Projection request P95 <= 150 ms for a 200-message session replay.
- Session history projection P95 <= 300 ms for 20 visible messages plus recent
  runtime overlay.
- SSE update application P95 <= 50 ms for non-terminal events.
- Renderer long task count <= 3 per 60-second complex-task smoke.
- Streaming P95 frame time <= 32 ms in the frontend long-task smoke after
  thresholds are calibrated.
- DOM node count grows with visible window height, not linearly with total
  history length, once virtualization/lazy mounting is implemented.
- OCR URL extraction P95 <= 2 seconds with local provider; bounded fallback when
  no local OCR provider exists.
- Browser/CDP first action succeeds through CDP auto-launch or persistent
  fallback without manual Chrome setup.
- Process/thread/cache counts return to within bounded limits after idle cleanup.
- Idle resource counts return to baseline + bounded allowance: threads and child
  processes <= baseline + 2, tmp growth < 100 MB after cleanup, scheduler tick
  lag < 5 seconds, and cancel-to-terminal/stuck visibility <= 10 seconds.
- Terminal job hot memory retains only the most recent 100 jobs or jobs inside
  the configured TTL window.
- Same-machine optimized runs must not regress more than 10% against the
  recorded baseline unless an explicit reviewer-approved threshold replaces the
  planning target.

Thresholds are initial planning targets. They must be calibrated against the
first baseline run before final release gating.

## Required Harness Scenarios

- Long session: 200/500/1000 messages with mixed assistant deltas, tool events,
  artifacts, permission requests, image jobs, subagent events, and reload.
- Complex task: browser/CDP, OCR, shell/read/write, artifact generation, and
  subagent events in one sustained run.
- Refresh replay: in-flight refresh, completed-run refresh, repeated reconnects,
  and history pagination. State must converge only through RuntimeProjection.
- Large artifact/image job: multi-image output, OCR reuse, cancellation, failure,
  retry, and artifact merge.
- Scheduler/subagent: scheduled task batches, concurrent subagents, heartbeat,
  timeout, and cancel.
- Resource lifecycle soak: repeated browser navigate/screenshot runs, many image
  jobs, OCR calls, scheduler failures, subagent timeout/cancel, and noisy MCP
  output.
- Browser/OCR: CDP auto-launch, CDP fallback, URL text extraction, screenshot
  OCR, and no-local-provider fallback.

## Redaction Contract

Performance artifacts may contain only hashes, event ids, event types, counts,
enum statuses, durations, byte sizes, process categories, and bounded numeric
samples. They must not contain raw prompts, raw OCR text, message bodies,
cookies, bearer tokens, API keys, full local paths, full browser profiles, or
secret-shaped values.

## Review Roles

- Runtime/Backend reviewer: ledger, projection, SSE, history, image jobs,
  scheduler, and subagent state.
- Frontend/UX reviewer: render responsiveness, state isolation, message/artifact
  rendering, settings panels, and visual stability.
- Tools/Resource reviewer: browser/CDP, OCR, optional abilities, long-lived
  processes, caches, timers, and temp files.
- Harness/Test reviewer: repeatable performance scenarios and numeric gates.
- Security/Audit reviewer: metric redaction and no sensitive payload leakage.
- Release/Regression reviewer: v0.2.2/v0.2.3 capabilities remain intact.

## Open Questions For Parallel Review

- Which current path contributes most to perceived slowness: projection replay,
  frontend render, long-lived tool resources, or event volume?
- Where can we summarize or cache without weakening replay correctness?
- Which performance thresholds are realistic on the current Windows desktop
  target and web deployment target?
- What minimal observability should ship in v0.2.3 without adding heavy runtime
  overhead?

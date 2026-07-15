# EcoreX v0.2.8 Runtime Observability And Queue Architecture

## Scope

v0.2.8 moves EcoreX toward a production-grade agent runtime in two areas:

- same-session input is queue-first, so a new user message does not implicitly cancel the active run;
- long-running work emits observable task health, timeout/intervention signals, and terminal events.

## Runtime Model

The runtime now treats a user request as a run with a durable event stream:

- `run.queued` records accepted queued work.
- `run.started` records the moment a queued run leaves the queue.
- `task.*` records long-running sub-work such as tools and image jobs.
- `image_job.*` remains the detailed image-job replay contract.

`RuntimeProjectionService` reduces these streams into request projections, including:

- `state`;
- assistant/user messages;
- `image_jobs`;
- `task_observations`;
- action plans and recent events.

## Same-Session Queue

When a request arrives while the same session is busy, WebChannel accepts it as queued instead of interrupting the current request.

Expected behavior:

- current run keeps its stream and cancel token;
- queued run gets a stable `request_id`;
- RunLedger stores it as `status=queued`, `phase=queued`, with no `started_at`;
- queued request payload is written to workspace `.ecorex/queued-requests`;
- RunLedger atomically claims queued starts with `lease_owner` and `lease_expires_at`;
- after the active session lock releases, the next queued request starts automatically and receives `started_at`.
- the queued chat card exposes `引导`, which re-observes the queued payload and reinserts it if needed; this action does not preempt the active run.

Current production gap:

- queue start is protected against duplicate starts across WebChannel runtimes, but there is still no external queue scheduler. A future distributed deployment may need explicit worker heartbeats and lease renewal for very slow admission paths.

## Task Observation

`TaskObserver` is the common event helper for long-running work. It emits:

- `task.started`;
- `task.heartbeat`;
- `task.health_changed`;
- `task.intervention_requested`;
- `task.completed`;
- `task.failed`;
- `task.cancelled`.

Tool execution uses this model for heartbeats, adaptive deadline extension, and timeout intervention.

## Image Job Canary

Image jobs are the v0.2.8 canary for long task observation because multi-image generation can spend many minutes waiting on a provider.

Image-job observation is job-level:

- detailed per-image progress stays in `image_job.tasks`;
- job health is exposed as `task_observations`;
- default per-image soft/stall baseline is 120 seconds;
- batch deadlines scale by waves: `ceil(task_count / effective_max_parallel) * 120s`;
- provider polling/waiting/retry/rate-limit/fallback statuses extend the current observation lease;
- thresholds are configurable through config, environment, or start metadata;
- intervention suggests `continue`, `stop`, and `background`.

Image generation speed policy is deliberately conservative:

- single-image work defaults to one lane;
- multi-image work defaults to two bounded lanes when `max_parallel` is omitted;
- explicit caller `max_parallel`, provider concurrency, configured cap, and production hard cap all clamp the final effective parallelism;
- native `imagegen.tasks` and Web ImageJobs share the same parallelism policy so batch tasks do not silently fall back to serial execution.

The image-job action API now supports:

- `status`;
- `collect`;
- `cancel`;
- `continue` / `extend`;
- `background`.

## User Surface

The chat stream can receive `task_observation` SSE items and convert them into phase text. Runtime projection and Desktop API types expose task observation fields so Run Center or future image panels can show the same state without parsing raw events.

Queued-message guidance stays in the chat surface. The user can click `引导` to ask the runtime to re-observe the queued request; the runtime decides whether reinsertion is necessary based on the durable ledger and payload store.

Run Center now receives task-observation summaries in active request snapshots and can:

- show a compact observation line on each run;
- call image-job `continue` / `background` actions when intervention is requested;
- keep generic stop/retry/diagnostics controls available.

Current product gap:

- Run Center still shows only the primary observation summary. A full timeline with per-event detail and task-level filtering remains future UI work.

## Acceptance Checks

The current slice is covered by:

- queued run `started_at` lifecycle tests;
- `TaskObserver` lifecycle and redaction tests;
- runtime projection `task_observations` tests;
- image-job observation/intervention/action tests;
- image-job 120s baseline, status lease, and default batch parallelism tests;
- native `imagegen.tasks` bounded parallel execution tests;
- durable queued payload recovery tests;
- queued-run claim lease and double-start prevention tests;
- queued guidance reinsertion tests;
- active request snapshot task-observation tests;
- focused queue, active request, image-job, and projection regressions;
- Desktop renderer and Electron builds.
